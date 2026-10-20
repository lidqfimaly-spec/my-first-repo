"""Controlled local test execution.

Only one runner (``python -m unittest``) and one target grammar (dotted test
identifiers) are supported.  The caller never supplies an executable, an
interpreter flag, an environment variable, or a shell string.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable


UNITTEST_RUNNER = "unittest"
RUN_TESTS_TOOL = "run_tests"
SUPPORTED_RUNNERS = frozenset({UNITTEST_RUNNER})
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 60.0
MIN_OUTPUT_BYTES = 256
MAX_OUTPUT_BYTES = 65536
EXCERPT_CHARS = 2000
TERMINATE_GRACE_SECONDS = 5.0

#: ``tests.test_mod`` or ``tests.test_mod.Class.test_case``; no paths,
#: spaces, shell metacharacters, or leading dashes.
TARGET_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
WORKSPACE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EVIDENCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IMAGE_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$")
AUTH_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_REQUIRED_ARTIFACTS = 64
MIN_DISK_LIMIT_BYTES = 1024 * 1024


class TestExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def validate_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize structured test parameters.

    This function validates shape and range only.  Provenance is the caller's
    responsibility: model candidates may contribute ``test_target``, while
    workspace, image, backend, evidence, budget and authorization fields must
    come from trusted task parameters or executor derivation.
    """
    unknown = set(parameters) - {
        "runner",
        "target",
        "workspace",
        "timeout_seconds",
        "max_output_bytes",
        "allowed_side_effects",
        "protected_paths",
        "execution_backend",
        "evidence_id",
        "image",
        "config_digest",
        "command_digest",
        "host_authorization",
        "required_artifacts",
        "disk_limit_bytes",
        "workspace_budget_bytes",
        "auto_provision_isolation",
    }
    if unknown:
        raise TestExecutionError(
            "unsupported_parameter", f"unknown test parameters: {sorted(unknown)}"
        )
    runner = parameters.get("runner")
    if runner not in SUPPORTED_RUNNERS:
        raise TestExecutionError(
            "unsupported_runner", f"runner is not allowed: {runner!r}"
        )
    target = parameters.get("target")
    if not isinstance(target, str) or not TARGET_PATTERN.fullmatch(target):
        raise TestExecutionError(
            "invalid_test_target",
            "test target must be a dotted identifier without paths or flags",
        )
    workspace = parameters.get("workspace")
    if not isinstance(workspace, str) or not WORKSPACE_PATTERN.fullmatch(
        workspace
    ):
        raise TestExecutionError(
            "invalid_workspace",
            "workspace must be a short name registered under workspaces/",
        )
    timeout = parameters.get("timeout_seconds", 10.0)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not MIN_TIMEOUT_SECONDS <= float(timeout) <= MAX_TIMEOUT_SECONDS
    ):
        raise TestExecutionError(
            "invalid_timeout",
            f"timeout must be between {MIN_TIMEOUT_SECONDS} and "
            f"{MAX_TIMEOUT_SECONDS} seconds",
        )
    limit = parameters.get("max_output_bytes", 8192)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not MIN_OUTPUT_BYTES <= limit <= MAX_OUTPUT_BYTES
    ):
        raise TestExecutionError(
            "invalid_output_limit",
            f"max_output_bytes must be between {MIN_OUTPUT_BYTES} and "
            f"{MAX_OUTPUT_BYTES}",
        )
    from .workspace_snapshot import SnapshotError, _normalize_prefix

    policies: dict[str, list[str]] = {}
    for field in ("allowed_side_effects", "protected_paths"):
        raw = parameters.get(field) or []
        if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
            raise TestExecutionError(
                "invalid_policy", f"{field} must be an array of paths"
            )
        try:
            policies[field] = [_normalize_prefix(str(item)) for item in raw]
        except SnapshotError as exc:
            raise TestExecutionError(exc.code, exc.message) from exc
    # ``container`` is the default and requires verified evidence; ``host``
    # is the legacy non-isolated mode and must be requested explicitly.
    backend = parameters.get("execution_backend", "container")
    if backend not in {"container", "host"}:
        raise TestExecutionError(
            "invalid_execution_backend",
            "execution_backend must be 'container' or 'host'",
        )

    evidence_id = parameters.get("evidence_id")
    if evidence_id is not None and (
        not isinstance(evidence_id, str)
        or not EVIDENCE_ID_PATTERN.fullmatch(evidence_id)
    ):
        raise TestExecutionError(
            "invalid_evidence_id",
            "evidence_id must be 1-128 chars of [A-Za-z0-9_-]",
        )

    image = parameters.get("image")
    if image is not None and (
        not isinstance(image, str)
        or not IMAGE_REFERENCE_PATTERN.fullmatch(image)
        or (":" not in image and "@" not in image)
    ):
        raise TestExecutionError(
            "invalid_image",
            "image must be a pinned tag or digest reference",
        )

    digests: dict[str, str | None] = {}
    for field in ("config_digest", "command_digest"):
        value = parameters.get(field)
        if value is not None and (
            not isinstance(value, str)
            or not SHA256_PATTERN.fullmatch(value)
        ):
            raise TestExecutionError(
                f"invalid_{field}",
                f"{field} must be a lowercase sha256 hex digest",
            )
        digests[field] = value

    host_authorization = parameters.get("host_authorization")
    if host_authorization is not None and (
        not isinstance(host_authorization, str)
        or not AUTH_TOKEN_PATTERN.fullmatch(host_authorization)
    ):
        raise TestExecutionError(
            "invalid_host_authorization",
            "host_authorization must be a bounded token",
        )

    required_raw = parameters.get("required_artifacts") or []
    if isinstance(required_raw, str) or not isinstance(
        required_raw, (list, tuple)
    ):
        raise TestExecutionError(
            "invalid_required_artifacts",
            "required_artifacts must be an array of paths",
        )
    if len(required_raw) > MAX_REQUIRED_ARTIFACTS:
        raise TestExecutionError(
            "invalid_required_artifacts",
            f"required_artifacts exceeds {MAX_REQUIRED_ARTIFACTS} entries",
        )
    required_artifacts: list[str] = []
    for item in required_raw:
        if not isinstance(item, str):
            raise TestExecutionError(
                "invalid_required_artifacts",
                "required artifact paths must be strings",
            )
        try:
            normalized_item = _normalize_prefix(item)
        except SnapshotError as exc:
            raise TestExecutionError(
                "invalid_required_artifacts",
                f"{exc.code}: {exc.message}",
            ) from exc
        if not normalized_item:
            raise TestExecutionError(
                "invalid_required_artifacts",
                "required artifact paths must not be empty",
            )
        required_artifacts.append(normalized_item)

    disk_limit = parameters.get("disk_limit_bytes")
    if disk_limit is not None and (
        not isinstance(disk_limit, int)
        or isinstance(disk_limit, bool)
        or disk_limit < MIN_DISK_LIMIT_BYTES
    ):
        raise TestExecutionError(
            "invalid_disk_limit",
            f"disk_limit_bytes must be an integer >= {MIN_DISK_LIMIT_BYTES}",
        )

    workspace_budget = parameters.get("workspace_budget_bytes")
    if workspace_budget is not None and (
        not isinstance(workspace_budget, int)
        or isinstance(workspace_budget, bool)
        or workspace_budget < 0
    ):
        raise TestExecutionError(
            "invalid_workspace_budget",
            "workspace_budget_bytes must be a non-negative integer",
        )

    auto_provision = parameters.get("auto_provision_isolation", False)
    if not isinstance(auto_provision, bool):
        raise TestExecutionError(
            "invalid_auto_provision_isolation",
            "auto_provision_isolation must be a boolean",
        )
    if auto_provision and backend != "container":
        raise TestExecutionError(
            "invalid_auto_provision_isolation",
            "auto_provision_isolation requires the container backend",
        )

    return {
        "runner": runner,
        "target": target,
        "workspace": workspace,
        "timeout_seconds": float(timeout),
        "max_output_bytes": int(limit),
        "execution_backend": backend,
        "allowed_side_effects": policies["allowed_side_effects"],
        "protected_paths": policies["protected_paths"],
        "evidence_id": evidence_id,
        "image": image,
        "config_digest": digests["config_digest"],
        "command_digest": digests["command_digest"],
        "host_authorization": host_authorization,
        "required_artifacts": required_artifacts,
        "disk_limit_bytes": disk_limit,
        "workspace_budget_bytes": workspace_budget,
        "auto_provision_isolation": auto_provision,
    }


def resolve_workspace(project_root: Path, workspace: str) -> Path:
    """Resolve a registered workspace, rejecting escapes and symlinks out."""
    if not WORKSPACE_PATTERN.fullmatch(workspace):
        raise TestExecutionError("invalid_workspace", "workspace name is invalid")
    root = (project_root / "workspaces").resolve()
    candidate = project_root / "workspaces" / workspace
    if candidate.is_symlink():
        raise TestExecutionError(
            "unsafe_workspace", "workspace must not be a symlink"
        )
    if not candidate.is_dir():
        raise TestExecutionError(
            "workspace_missing", f"workspace is not registered: {workspace}"
        )
    resolved = candidate.resolve()
    if resolved != root / workspace:
        raise TestExecutionError(
            "unsafe_workspace", "workspace escapes the registered root"
        )
    return resolved


def build_command(target: str) -> list[str]:
    if not TARGET_PATTERN.fullmatch(target):
        raise TestExecutionError(
            "invalid_test_target", "test target is not a dotted identifier"
        )
    return [sys.executable, "-m", "unittest", "-q", target]


def _child_environment(workspace: Path) -> dict[str, str]:
    """Minimal environment: no inherited secrets, no site hooks."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(workspace),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }


def _drain(stream: Any, limit: int) -> tuple[bytes, int]:
    """Read a pipe in fixed chunks, keeping at most ``limit`` bytes."""
    kept = bytearray()
    total = 0
    while True:
        chunk = stream.read(4096)
        if not chunk:
            break
        total += len(chunk)
        if len(kept) < limit:
            kept.extend(chunk[: limit - len(kept)])
    return bytes(kept), total


def _terminate(process: subprocess.Popen) -> bool:
    """Kill the whole process group and report whether it exited."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return process.returncode is not None


def run_unittest(
    *,
    project_root: Path,
    parameters: dict[str, Any],
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run one approved unittest target under resource limits."""
    normalized = validate_parameters(parameters)
    workspace = resolve_workspace(project_root, normalized["workspace"])
    command = build_command(normalized["target"])
    limit = normalized["max_output_bytes"]
    started_at = time.time()
    process = subprocess.Popen(
        command,
        cwd=workspace,
        env=_child_environment(workspace),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    collected: dict[str, tuple[bytes, int]] = {}

    def _reader(name: str, stream: Any) -> None:
        collected[name] = _drain(stream, limit)

    threads = [
        threading.Thread(target=_reader, args=("stdout", process.stdout)),
        threading.Thread(target=_reader, args=("stderr", process.stderr)),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    cancelled = False
    terminated = False
    deadline = started_at + normalized["timeout_seconds"]
    while True:
        if process.poll() is not None:
            break
        if cancel_check is not None and cancel_check():
            cancelled = True
            terminated = _terminate(process)
            break
        if time.time() >= deadline:
            timed_out = True
            terminated = _terminate(process)
            break
        time.sleep(0.02)
    exit_code = process.wait()
    for thread in threads:
        thread.join(timeout=TERMINATE_GRACE_SECONDS)
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except Exception:
            pass
    finished_at = time.time()
    stdout, stdout_total = collected.get("stdout", (b"", 0))
    stderr, stderr_total = collected.get("stderr", (b"", 0))
    return {
        "runner": normalized["runner"],
        "target": normalized["target"],
        "workspace": normalized["workspace"],
        "exit_code": exit_code if not (timed_out or cancelled) else None,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "terminated_confirmed": (
            terminated if (timed_out or cancelled) else True
        ),
        "truncated": stdout_total > limit or stderr_total > limit,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_bytes": stdout_total,
        "stderr_bytes": stderr_total,
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "duration_ms": round((finished_at - started_at) * 1000.0, 3),
        "output_limit_bytes": limit,
        "timeout_seconds": normalized["timeout_seconds"],
        "command": command,
        "workspace_path": str(workspace),
    }


def excerpt(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")[:EXCERPT_CHARS]
