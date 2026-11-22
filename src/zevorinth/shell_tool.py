"""Controlled shell execution with a fixed command whitelist.

Security boundaries (per architecture.md:152):
- Parameter array only — no shell string, no ``shell=True``.
- Explicit whitelist of read-only commands.
- Bounded timeout per execution.
- Output size limit with truncation marker.
- Working directory locked to the project root.
- Path arguments validated against workspace escape.
- No stdin, no environment override, no interpreters.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from .models import ExecutionMode, ToolResult


CONTROLLED_SHELL_TOOL = "controlled_shell"

MAX_OUTPUT_BYTES = 64 * 1024
MAX_TIMEOUT_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 15
MIN_TIMEOUT_SECONDS = 1
MAX_ARGUMENTS = 32
MAX_ARGUMENT_LENGTH = 4096
MAX_TOTAL_ARGUMENT_BYTES = 32 * 1024

#: Whitelist of allowed command binaries. All are read-only or
#: bounded-output utilities. No interpreters, no write commands,
#: no network commands.
ALLOWED_COMMANDS = frozenset(
    {
        "ls",
        "cat",
        "wc",
        "head",
        "tail",
        "grep",
        "find",
        "pwd",
        "date",
        "echo",
        "file",
        "stat",
        "diff",
        "sort",
        "uniq",
        "basename",
        "dirname",
        "cut",
        "tr",
        "rev",
        "strings",
    }
)

#: Dangerous flags that change command semantics or cause side effects.
_FORBIDDEN_FLAGS = frozenset(
    {
        "-delete",
        "--delete",
        "-exec",
        "--exec",
        "-execdir",
        "--execdir",
        "-ok",
        "--ok",
        "-okdir",
        "--okdir",
        "-print0",
        "--print0",
        "-fls",
        "--fls",
        "-fprint",
        "--fprint",
        "-fprintf",
        "--fprintf",
        "-ls",
        "--ls",
    }
)

#: Sensitive path patterns that must never appear in arguments.
_SENSITIVE_PATH_PATTERNS = (
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    "/.ssh/",
    "/id_rsa",
    "/id_ed25519",
    "/id_ecdsa",
    "/.aws/credentials",
    "/.gnupg/",
    "/.env",
)

_SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "USER",
        "LOGNAME",
        "TMPDIR",
    }
)


class ShellToolError(Exception):
    """Raised when shell parameters fail validation or execution is unsafe."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_shell_parameters(
    parameters: dict[str, Any],
    *,
    workspace_root: Path,
) -> dict[str, Any]:
    """Validate and normalize controlled_shell parameters.

    Required keys:
        command: str — must be in ALLOWED_COMMANDS.
        arguments: list[str] — parameter array, each a bounded string.

    Optional keys:
        timeout_seconds: int — 1..MAX_TIMEOUT_SECONDS (default 15).
        working_directory: str — subdirectory within workspace_root.
    """
    if not isinstance(parameters, dict):
        raise ShellToolError(
            "invalid_parameters",
            "parameters must be an object",
        )

    command = parameters.get("command")
    if not isinstance(command, str) or not command:
        raise ShellToolError(
            "invalid_command",
            "command must be a non-empty string",
        )
    # Resolve the binary name: reject any path separators.
    if "/" in command or "\\" in command or os.path.sep in command:
        raise ShellToolError(
            "command_must_be_name_only",
            "command must be a bare binary name, not a path",
        )
    if command not in ALLOWED_COMMANDS:
        raise ShellToolError(
            "command_not_whitelisted",
            f"command is not in the whitelist: {command!r}",
        )

    raw_args = parameters.get("arguments", [])
    if not isinstance(raw_args, list):
        raise ShellToolError(
            "invalid_arguments",
            "arguments must be an array of strings",
        )
    if len(raw_args) > MAX_ARGUMENTS:
        raise ShellToolError(
            "too_many_arguments",
            f"arguments must not exceed {MAX_ARGUMENTS} items",
        )

    validated_args: list[str] = []
    total_bytes = 0
    for idx, arg in enumerate(raw_args):
        if not isinstance(arg, str):
            raise ShellToolError(
                "invalid_argument_type",
                f"argument {idx} must be a string",
            )
        if "\x00" in arg:
            raise ShellToolError(
                "invalid_argument_null",
                f"argument {idx} contains a null byte",
            )
        if len(arg) > MAX_ARGUMENT_LENGTH:
            raise ShellToolError(
                "argument_too_long",
                f"argument {idx} exceeds {MAX_ARGUMENT_LENGTH} characters",
            )
        total_bytes += len(arg.encode("utf-8"))
        if total_bytes > MAX_TOTAL_ARGUMENT_BYTES:
            raise ShellToolError(
                "arguments_too_large",
                "total argument size exceeds the limit",
            )
        if arg in _FORBIDDEN_FLAGS:
            raise ShellToolError(
                "forbidden_flag",
                f"argument {arg!r} is a forbidden flag",
            )
        _validate_argument_path_safety(arg, workspace_root)
        validated_args.append(arg)

    timeout = parameters.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise ShellToolError(
            "invalid_timeout",
            "timeout_seconds must be an integer",
        )
    if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ShellToolError(
            "timeout_out_of_range",
            f"timeout_seconds must be between {MIN_TIMEOUT_SECONDS} "
            f"and {MAX_TIMEOUT_SECONDS}",
        )

    working_dir = parameters.get("working_directory", "")
    if not isinstance(working_dir, str):
        raise ShellToolError(
            "invalid_working_directory",
            "working_directory must be a string",
        )
    resolved_cwd = _resolve_working_directory(working_dir, workspace_root)

    return {
        "command": command,
        "arguments": validated_args,
        "timeout_seconds": timeout,
        "working_directory": str(resolved_cwd),
    }


def _validate_argument_path_safety(
    arg: str,
    workspace_root: Path,
) -> None:
    """Reject arguments that reference sensitive paths or escape workspace."""
    for pattern in _SENSITIVE_PATH_PATTERNS:
        if pattern in arg:
            raise ShellToolError(
                "sensitive_path_rejected",
                f"argument references a sensitive path: {arg!r}",
            )
    # Reject absolute paths outside the workspace.
    if arg.startswith("/"):
        try:
            resolved = Path(arg).resolve()
            root_resolved = workspace_root.resolve()
            if not str(resolved).startswith(str(root_resolved)):
                raise ShellToolError(
                    "absolute_path_outside_workspace",
                    f"absolute path is outside the workspace: {arg!r}",
                )
        except (OSError, RuntimeError):
            raise ShellToolError(
                "unresolvable_path",
                f"could not resolve path: {arg!r}",
            )
    # Reject path traversal beyond workspace.
    if ".." in arg.split("/"):
        try:
            test_path = (workspace_root / arg).resolve()
            root_resolved = workspace_root.resolve()
            if not str(test_path).startswith(str(root_resolved)):
                raise ShellToolError(
                    "path_traversal_outside_workspace",
                    f"path traversal escapes the workspace: {arg!r}",
                )
        except (OSError, RuntimeError):
            raise ShellToolError(
                "unresolvable_path",
                f"could not resolve path: {arg!r}",
            )


def _resolve_working_directory(
    working_dir: str,
    workspace_root: Path,
) -> Path:
    """Resolve working_directory to a path within workspace_root."""
    if not working_dir:
        return workspace_root.resolve()
    if working_dir.startswith("/"):
        raise ShellToolError(
            "working_directory_must_be_relative",
            "working_directory must be a relative path within the workspace",
        )
    if ".." in working_dir.split("/"):
        raise ShellToolError(
            "working_directory_no_traversal",
            "working_directory must not contain parent references",
        )
    resolved = (workspace_root / working_dir).resolve()
    root_resolved = workspace_root.resolve()
    if not str(resolved).startswith(str(root_resolved)):
        raise ShellToolError(
            "working_directory_outside_workspace",
            "working_directory resolves outside the workspace",
        )
    return resolved


def _build_safe_env() -> dict[str, str]:
    """Build a minimal environment with only safe variables."""
    env: dict[str, str] = {}
    for key in _SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


def execute_controlled_shell(
    *,
    task_id: str,
    attempt_id: str,
    command: str,
    arguments: Sequence[str],
    timeout_seconds: int,
    working_directory: str,
    started_at: str,
    finished_at: str,
    execution_mode: ExecutionMode = ExecutionMode.REAL,
    lease_token: str | None = None,
    claim_generation: int | None = None,
) -> ToolResult:
    """Execute a whitelisted command with bounded timeout and output.

    This function never uses ``shell=True``. The command and arguments
    are passed directly to ``subprocess.run`` as a list.
    """
    if execution_mode is ExecutionMode.SIMULATED:
        return ToolResult(
            attempt_id=attempt_id,
            tool_name=CONTROLLED_SHELL_TOOL,
            execution_mode=ExecutionMode.SIMULATED,
            success=True,
            started_at=started_at,
            finished_at=finished_at,
            workspace_effects={
                "command": command,
                "arguments": list(arguments),
                "output": (
                    f"[simulated] {command} "
                    f"{' '.join(arguments) if arguments else ''}"
                ),
                "exit_code": 0,
                "duration_seconds": 0.0,
                "output_bytes": 0,
                "truncated": False,
                "timed_out": False,
            },
        )

    args_list = [command, *arguments]
    env = _build_safe_env()
    cwd = Path(working_directory)

    start = time.monotonic()
    timed_out = False
    exit_code: int
    stdout_bytes: bytes
    stderr_bytes: bytes

    try:
        proc = subprocess.run(
            args_list,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            shell=False,
            check=False,
        )
        exit_code = proc.returncode
        stdout_bytes = proc.stdout or b""
        stderr_bytes = proc.stderr or b""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = -1
        stdout_bytes = exc.stdout or b""
        stderr_bytes = exc.stderr or b""
        if isinstance(stdout_bytes, str):
            stdout_bytes = stdout_bytes.encode("utf-8", errors="replace")
        if isinstance(stderr_bytes, str):
            stderr_bytes = stderr_bytes.encode("utf-8", errors="replace")
    except FileNotFoundError:
        return ToolResult(
            attempt_id=attempt_id,
            tool_name=CONTROLLED_SHELL_TOOL,
            execution_mode=execution_mode,
            success=False,
            started_at=started_at,
            finished_at=finished_at,
            error_code="command_not_found",
            error_message=f"command not found: {command}",
            workspace_effects={
                "command": command,
                "arguments": list(arguments),
                "output": "",
                "exit_code": 127,
                "duration_seconds": time.monotonic() - start,
                "output_bytes": 0,
                "truncated": False,
                "timed_out": False,
            },
        )
    except PermissionError:
        return ToolResult(
            attempt_id=attempt_id,
            tool_name=CONTROLLED_SHELL_TOOL,
            execution_mode=execution_mode,
            success=False,
            started_at=started_at,
            finished_at=finished_at,
            error_code="permission_denied",
            error_message=f"permission denied: {command}",
            workspace_effects={
                "command": command,
                "arguments": list(arguments),
                "output": "",
                "exit_code": 126,
                "duration_seconds": time.monotonic() - start,
                "output_bytes": 0,
                "truncated": False,
                "timed_out": False,
            },
        )

    duration = time.monotonic() - start

    # Combine stdout and stderr, then enforce the output limit.
    combined = stdout_bytes + stderr_bytes
    truncated = len(combined) > MAX_OUTPUT_BYTES
    if truncated:
        combined = combined[:MAX_OUTPUT_BYTES]
        combined += b"\n[output truncated at %d bytes]\n" % MAX_OUTPUT_BYTES

    output_text = combined.decode("utf-8", errors="replace")
    succeeded = exit_code == 0 and not timed_out
    error_parts: list[str] = []
    if timed_out:
        error_parts.append(
            f"command timed out after {timeout_seconds}s"
        )
    if exit_code != 0:
        error_parts.append(f"exit code {exit_code}")

    return ToolResult(
        attempt_id=attempt_id,
        tool_name=CONTROLLED_SHELL_TOOL,
        execution_mode=execution_mode,
        success=succeeded,
        started_at=started_at,
        finished_at=finished_at,
        error_code=(
            "timeout" if timed_out
            else f"exit_{exit_code}" if exit_code != 0
            else None
        ),
        error_message="; ".join(error_parts) if error_parts else None,
        workspace_effects={
            "command": command,
            "arguments": list(arguments),
            "output": output_text,
            "exit_code": exit_code,
            "duration_seconds": duration,
            "output_bytes": len(combined),
            "truncated": truncated,
            "timed_out": timed_out,
        },
    )
