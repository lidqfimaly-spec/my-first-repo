"""macOS Seatbelt (sandbox-exec) isolation prototype.

This runs a probe inside a kernel-enforced profile: reads are restricted to
the workspace plus system libraries, writes to the workspace only, and the
network is denied.  The probe reports kernel errnos, so an ``EPERM`` here is
a kernel denial rather than an application-level policy decision.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SYSTEM_READ_PATHS = (
    "/usr/lib",
    "/usr/share",
    "/System",
    "/Library/Apple",
    "/private/var/db/dyld",
    "/opt/homebrew",
    "/usr/local",
    "/dev/null",
    "/dev/urandom",
    "/dev/dtracehelper",
)


class IsolationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def isolation_available() -> bool:
    return os.path.isfile(SANDBOX_EXEC) and os.access(SANDBOX_EXEC, os.X_OK)


def build_profile(
    workspace: Path,
    *,
    allow_network: bool = False,
) -> str:
    executable = Path(sys.executable).resolve()
    read_paths = [
        str(workspace.resolve()),
        sys.prefix,
        sys.base_prefix,
        str(executable),
        str(executable.parent),
    ]
    read_paths.extend(SYSTEM_READ_PATHS)
    read_rules = " ".join(
        f'(subpath "{path}")' for path in dict.fromkeys(read_paths)
    )
    write_rules = f'(subpath "{workspace.resolve()}") (literal "/dev/null")'
    network = "" if allow_network else "(deny network*)"
    return (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process*)\n"
        "(allow sysctl-read)\n"
        "(allow mach-lookup)\n"
        "(allow process-exec)\n"
        "(allow file-read-metadata)\n"
        "(allow file-map-executable)\n"
        f"(allow file-read* {read_rules})\n"
        f"(allow file-write* {write_rules})\n"
        f"{network}\n"
    )


PROBE = r"""
import json, os, socket, sys
workspace, outside = sys.argv[1], sys.argv[2]
result = {}

def attempt(name, fn):
    try:
        fn()
        result[name] = "allowed"
    except PermissionError as exc:
        result[name] = f"denied:{type(exc).__name__}"
    except OSError as exc:
        result[name] = f"oserror:{exc.errno}"

attempt("read_inside", lambda: open(os.path.join(workspace, "inside.txt")).read())
attempt("read_outside", lambda: open(outside).read())
def write_inside():
    with open(os.path.join(workspace, "written.txt"), "w") as handle:
        handle.write("ok")
attempt("write_inside", write_inside)
def write_outside():
    with open(os.path.join(outside + ".new"), "w") as handle:
        handle.write("no")
attempt("write_outside", write_outside)
def connect():
    sock = socket.socket()
    sock.settimeout(1.0)
    try:
        sock.connect(("127.0.0.1", 9))
    finally:
        sock.close()
attempt("network", connect)
print(json.dumps(result, sort_keys=True))
"""


def run_probe(
    workspace: Path,
    decoy: Path,
    *,
    timeout: float = 20.0,
    allow_network: bool = False,
) -> dict[str, Any]:
    """Run the probe under Seatbelt and return kernel-observed results."""
    if not isolation_available():
        raise IsolationError(
            "isolation_unavailable", "sandbox-exec is not available"
        )
    workspace = workspace.resolve()
    (workspace / "inside.txt").write_text("inside\n", encoding="utf-8")
    profile = build_profile(workspace, allow_network=allow_network)
    process = subprocess.run(
        [
            SANDBOX_EXEC,
            "-p",
            profile,
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
    payload: dict[str, Any] = {}
    if process.stdout.strip():
        try:
            payload = json.loads(process.stdout.strip().splitlines()[-1])
        except json.JSONDecodeError:
            payload = {}
    denials = sorted(
        name
        for name, value in payload.items()
        if isinstance(value, str) and value.startswith("denied:")
    )
    applied = process.returncode == 0 and bool(payload)
    return {
        "exit_code": process.returncode,
        "results": payload,
        "stderr": process.stderr[-500:],
        "profile_applied": applied,
        # An unstarted or aborted probe proves nothing about the boundary.
        "state": "verified" if applied else "unverified",
        "failure_kind": None if applied else "probe_did_not_complete",
        "kernel_denials": denials,
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
