"""Fail-closed isolation executor and macOS Seatbelt probe.

The probe records what the **kernel** allowed or denied for a real child
process.  Only when every required boundary is denied does the backend count
as verified; otherwise protected execution is refused rather than run
unisolated.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

#: Operations explicitly allowed to run without OS isolation.  This is a
#: policy declaration, never an exception-based fallback.
UNISOLATED_ALLOWED_OPERATIONS = frozenset({"run_tests"})

#: Boundaries a profile must actually enforce before it counts as verified.
REQUIRED_BOUNDARIES = (
    "write_outside_denied",
    "network_denied",
    "read_outside_denied",
    "write_inside_allowed",
)


class IsolationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def isolation_available() -> bool:
    return os.path.isfile(SANDBOX_EXEC) and os.access(SANDBOX_EXEC, os.X_OK)


def build_profile(workspace: Path) -> str:
    """Deny writes outside the workspace and deny the network.

    Reads are intentionally global: narrowing them to the workspace plus
    runtime paths aborts the interpreter on this platform, so read
    confinement is reported as *not enforced* instead of being faked.
    """
    resolved = str(workspace.resolve())
    return (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process*)\n"
        "(allow file-read*)\n"
        f'(allow file-write* (subpath "{resolved}"))\n'
        "(deny network*)\n"
    )


PROBE = r"""
import json, os, socket, sys
workspace, outside = sys.argv[1], sys.argv[2]
result = {}

def attempt(name, fn):
    try:
        fn()
        result[name] = "allowed"
    except PermissionError:
        result[name] = "denied"
    except OSError as exc:
        result[name] = "oserror:%s" % exc.errno

attempt("read_inside", lambda: open(os.path.join(workspace, "inside.txt")).read())
attempt("read_outside", lambda: open(outside).read())
def write_inside():
    with open(os.path.join(workspace, "written.txt"), "w") as handle:
        handle.write("ok")
attempt("write_inside", write_inside)
def write_outside():
    with open(outside + ".new", "w") as handle:
        handle.write("no")
attempt("write_outside", write_outside)
def connect():
    sock = socket.socket(); sock.settimeout(1.0)
    try:
        sock.connect(("127.0.0.1", 9))
    finally:
        sock.close()
attempt("network", connect)
print(json.dumps(result, sort_keys=True))
"""


def classify(results: dict[str, str]) -> tuple[str, list[str]]:
    """Map probe observations to VERIFIED / PARTIAL / FAILED / BLOCKED."""
    if not results:
        return "blocked", []
    observed = {
        "write_outside_denied": results.get("write_outside") == "denied",
        "network_denied": results.get("network") == "denied",
        "read_outside_denied": results.get("read_outside") == "denied",
        "write_inside_allowed": results.get("write_inside") == "allowed",
    }
    missing = [name for name in REQUIRED_BOUNDARIES if not observed[name]]
    if not missing:
        return "verified", []
    if observed["write_inside_allowed"] is False:
        return "failed", missing
    return "partial", missing


def run_probe(
    workspace: Path,
    decoy: Path,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Run the probe under Seatbelt and classify what the kernel enforced."""
    if not isolation_available():
        return {
            "state": "blocked",
            "reason": "sandbox-exec is not available",
            "results": {},
            "kernel_denials": [],
            "process_started": False,
            "profile_applied": False,
            "unverified_boundaries": list(REQUIRED_BOUNDARIES),
        }
    workspace = workspace.resolve()
    decoy = decoy.resolve()
    (workspace / "inside.txt").write_text("inside\n", encoding="utf-8")
    try:
        process = subprocess.run(
            [
                SANDBOX_EXEC,
                "-p",
                build_profile(workspace),
                sys.executable,
                "-c",
                PROBE,
                str(workspace),
                str(decoy),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(workspace),
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except subprocess.TimeoutExpired:
        return {
            "state": "blocked",
            "reason": "probe timed out",
            "results": {},
            "kernel_denials": [],
            "process_started": True,
            "profile_applied": False,
            "unverified_boundaries": list(REQUIRED_BOUNDARIES),
        }
    payload: dict[str, str] = {}
    if process.stdout.strip():
        try:
            payload = json.loads(process.stdout.strip().splitlines()[-1])
        except json.JSONDecodeError:
            payload = {}
    state, missing = classify(payload)
    if process.returncode != 0 and not payload:
        state, missing = "blocked", list(REQUIRED_BOUNDARIES)
    return {
        "state": state,
        "reason": (
            "all required boundaries denied"
            if state == "verified"
            else f"unenforced boundaries: {missing}"
        ),
        "results": payload,
        "kernel_denials": sorted(
            name for name, value in payload.items() if value == "denied"
        ),
        "process_started": bool(payload) or process.returncode == 0,
        "profile_applied": bool(payload),
        "unverified_boundaries": missing,
        "exit_code": process.returncode,
        "stderr": process.stderr[-400:],
    }


def require_verified(probe: dict[str, Any]) -> None:
    """Fail closed unless every required boundary is kernel-enforced."""
    if probe.get("state") != "verified":
        raise IsolationError(
            "isolation_not_verified",
            str(probe.get("reason", "backend is not verified")),
        )


def run_protected(
    argv: list[str],
    *,
    workspace: Path,
    probe: dict[str, Any],
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Start a protected process only under a verified backend.

    The call never falls back to an unisolated run: when the backend is not
    verified the record is ``blocked`` and no process is spawned.
    """
    if probe.get("state") != "verified":
        return {
            "isolation_backend": "seatbelt",
            "isolation_state": probe.get("state", "blocked"),
            "profile_applied": False,
            "process_started": False,
            "exit_code": None,
            "blocked_reason": probe.get(
                "reason", "isolation backend is not verified"
            ),
            "argv": list(argv),
        }
    process = subprocess.run(
        [SANDBOX_EXEC, "-p", build_profile(workspace), *argv],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return {
        "isolation_backend": "seatbelt",
        "isolation_state": "verified",
        "profile_applied": True,
        "process_started": True,
        "exit_code": process.returncode,
        "stdout": process.stdout[-2000:],
        "stderr": process.stderr[-2000:],
        "argv": list(argv),
    }


def inventory_quarantine(workspace_root: Path) -> list[dict[str, Any]]:
    """Read-only listing of quarantined objects (never deletes anything)."""
    quarantine = workspace_root / ".quarantine"
    if not quarantine.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for operation_dir in sorted(quarantine.iterdir()):
        if not operation_dir.is_dir():
            continue
        for item in sorted(operation_dir.iterdir()):
            if not item.is_file() or item.is_symlink():
                continue
            import hashlib

            entries.append(
                {
                    "operation_id": operation_dir.name,
                    "relative_path": item.name,
                    "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                    "verified": False,
                    "retention_candidate": False,
                }
            )
    return entries


#: Backends we look for locally.  Presence of a binary is *not* evidence of
#: isolation; each entry keeps its own verification state.
BACKEND_CANDIDATES = ("seatbelt", "docker", "podman", "low_priv_user")

BOUNDARY_CHECKS = (
    "read_inside_allowed",
    "read_outside_denied",
    "write_inside_allowed",
    "write_outside_denied",
    "network_denied",
    "credential_denied",
    "subprocess_denied",
)


def detect_backends() -> dict[str, dict[str, Any]]:
    """Report local backend availability without installing or starting anything."""
    import shutil

    seatbelt = isolation_available()
    docker = shutil.which("docker")
    podman = shutil.which("podman")
    backends: dict[str, dict[str, Any]] = {
        "seatbelt": {
            "binary": SANDBOX_EXEC if seatbelt else None,
            "available": seatbelt,
            "state": "probe_required" if seatbelt else "not_executed",
            "limitation": (
                "restricted reads abort even /bin/cat on this platform"
                if seatbelt
                else "sandbox-exec missing"
            ),
        },
        "docker": {
            "binary": docker,
            "available": bool(docker),
            "state": "not_executed",
            "limitation": (
                "daemon not running; starting it is out of scope"
                if docker
                else "docker CLI missing"
            ),
        },
        "podman": {
            "binary": podman,
            "available": bool(podman),
            "state": "not_executed" if podman else "not_executed",
            "limitation": "not installed" if not podman else "not started",
        },
        "low_priv_user": {
            "binary": None,
            "available": False,
            "state": "not_executed",
            "limitation": "creating a system user requires root and is out of scope",
        },
    }
    return backends


def verify_boundaries(
    backend: str,
    workspace: Path,
    decoy: Path,
    credential: Path,
) -> dict[str, Any]:
    """Run the unified boundary matrix for one backend.

    Only a full pass of :data:`BOUNDARY_CHECKS` yields ``verified``; any
    backend that cannot run is reported as ``blocked``/``not_executed``
    rather than being treated as safe.
    """
    if backend != "seatbelt":
        return {
            "backend": backend,
            "state": "not_executed",
            "reason": f"backend {backend} is not available for execution",
            "checks": {name: "not_executed" for name in BOUNDARY_CHECKS},
        }
    probe = run_probe(workspace, decoy)
    results = probe.get("results", {})
    checks: dict[str, str] = {name: "not_executed" for name in BOUNDARY_CHECKS}
    if results:
        checks["read_inside_allowed"] = results.get("read_inside", "unknown")
        checks["write_inside_allowed"] = results.get("write_inside", "unknown")
        checks["read_outside_denied"] = (
            "denied" if results.get("read_outside") == "denied" else "allowed"
        )
        checks["write_outside_denied"] = (
            "denied" if results.get("write_outside") == "denied" else "allowed"
        )
        checks["network_denied"] = (
            "denied" if results.get("network") == "denied" else "allowed"
        )
    expected = {
        "read_inside_allowed": "allowed",
        "read_outside_denied": "denied",
        "write_inside_allowed": "allowed",
        "write_outside_denied": "denied",
        "network_denied": "denied",
        "credential_denied": "denied",
        "subprocess_denied": "denied",
    }
    failed = [
        name
        for name, want in expected.items()
        if checks.get(name) != want
    ]
    if probe.get("state") in {"blocked"}:
        state = "blocked"
    elif not results:
        state = "unverified"
    elif failed:
        state = "partial" if any(
            checks.get(name) == want
            for name, want in expected.items()
        ) else "failed"
    else:
        state = "verified"
    return {
        "backend": backend,
        "state": state,
        "reason": "not all boundaries enforced: " + ", ".join(failed),
        "checks": checks,
        "unverified_boundaries": failed,
        "probe_state": probe.get("state"),
        "kernel_denials": probe.get("kernel_denials", []),
    }
