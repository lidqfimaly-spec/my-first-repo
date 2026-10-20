"""Guarded cleanup of files a task provably created.

Path resolution walks the trusted workspace root one component at a time
with ``O_NOFOLLOW``/``O_DIRECTORY`` directory handles, opens the target with
``O_NOFOLLOW``, verifies ``fstat`` and the content hash **through that file
descriptor**, then unlinks by name relative to the parent directory handle.

That narrows the race window but does not make "verify then unlink" atomic:
POSIX has no compare-and-unlink primitive.  The residual window is recorded
in the audit document and exercised by the checkpoint tests.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .workspace_effects import validate_workspace_effects
from .workspace_snapshot import EffectPolicy, SnapshotError


MAX_CLEANUP_BYTES = 1024 * 1024
CHECKPOINTS = ("opened", "verified", "before_unlink", "after_unlink")


class CleanupError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class OwnershipRecord:
    task_id: str
    attempt_id: str
    claim_generation: int
    workspace: str
    relative_path: str
    source: str
    created_sha256: str


@dataclass(slots=True)
class CleanupDecision:
    allowed: bool
    code: str
    reason: str
    relative_path: str | None = None
    expected_sha256: str | None = None
    ownership: OwnershipRecord | None = None
    _parts: tuple[str, ...] = field(default=(), repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "relative_path": self.relative_path,
            "expected_sha256": self.expected_sha256,
        }


def _normalize_relative(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise CleanupError("invalid_path", "path must be a non-empty string")
    if relative_path.startswith("/") or "\\" in relative_path:
        raise CleanupError("invalid_path", "path must be relative posix")
    parts = tuple(
        part
        for part in PurePosixPath(relative_path).parts
        if part not in ("", ".")
    )
    if not parts or any(part == ".." for part in parts):
        raise CleanupError("invalid_path", "path must not traverse upward")
    return parts


def ownership_records(effects: Mapping[str, Any]) -> list[OwnershipRecord]:
    """Files this task created under an allowed policy."""
    validate_workspace_effects(effects)
    records: list[OwnershipRecord] = []
    for change in effects["diff"]["changes"]:
        if change["change"] != "added":
            continue
        if change["classification"] != "expected":
            continue
        if not change.get("after_sha256"):
            continue
        records.append(
            OwnershipRecord(
                task_id=str(effects["task_id"]),
                attempt_id=str(effects["attempt_id"]),
                claim_generation=int(effects["claim_generation"]),
                workspace=str(effects["workspace"]),
                relative_path=str(change["path"]),
                source=str(effects["source"]),
                created_sha256=str(change["after_sha256"]),
            )
        )
    return records


def plan_cleanup(
    *,
    effects: Mapping[str, Any],
    workspace_root: Path,
    relative_path: str,
    current_generation: int,
    enabled: bool,
    authorized: bool,
    cleaned_paths: set[str] | None = None,
) -> CleanupDecision:
    """Decide whether ``relative_path`` may be deleted (no filesystem writes)."""
    if not enabled:
        return CleanupDecision(False, "cleanup_disabled", "cleanup is off")
    if not authorized:
        return CleanupDecision(
            False, "cleanup_unauthorized", "cleanup needs explicit authorization"
        )
    try:
        parts = _normalize_relative(relative_path)
    except CleanupError as exc:
        return CleanupDecision(False, exc.code, exc.message)
    normalized = "/".join(parts)
    if cleaned_paths and normalized in cleaned_paths:
        return CleanupDecision(
            False,
            "already_cleaned",
            "this task already removed that path",
            normalized,
        )
    try:
        validate_workspace_effects(effects)
    except SnapshotError as exc:
        return CleanupDecision(False, exc.code, exc.message, normalized)
    if int(effects["claim_generation"]) != int(current_generation):
        return CleanupDecision(
            False,
            "stale_generation",
            "cleanup requires the generation that created the file",
            normalized,
        )
    policy = EffectPolicy.build(
        {
            "allowed_side_effects": effects["policy"].get(
                "allowed_side_effects", []
            ),
            "protected_paths": effects["policy"].get("protected_paths", []),
        }
    )
    if policy._matches(normalized, policy.protected_prefixes):
        return CleanupDecision(
            False, "protected_path", "path is protected from cleanup", normalized
        )
    record = next(
        (
            item
            for item in ownership_records(effects)
            if item.relative_path == normalized
        ),
        None,
    )
    if record is None:
        return CleanupDecision(
            False,
            "not_owned",
            "the task has no ownership record for this path",
            normalized,
        )
    if not (workspace_root / normalized).exists():
        return CleanupDecision(
            False, "missing_target", "target does not exist", normalized
        )
    return CleanupDecision(
        True,
        "allowed",
        "owned and eligible",
        normalized,
        record.created_sha256,
        record,
        parts,
    )


def _open_parent(root_fd: int, parts: tuple[str, ...]) -> tuple[int, str]:
    """Walk ``parts[:-1]`` with directory handles, never following symlinks."""
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
            raise CleanupError(
                "unsafe_path", "path component is missing or a symlink"
            ) from exc
        raise
    return fd, parts[-1]


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_CLEANUP_BYTES + 1
    while remaining > 0:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def execute_cleanup(
    decision: CleanupDecision,
    *,
    workspace_root: Path | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Delete the planned target through directory handles."""
    if not decision.allowed or not decision._parts:
        return {
            "status": "rejected",
            "code": decision.code,
            "reason": decision.reason,
            "removed": False,
            "relative_path": decision.relative_path,
        }
    if workspace_root is None:
        return {
            "status": "rejected",
            "code": "invalid_root",
            "reason": "workspace root is required",
            "removed": False,
        }
    root_fd = None
    parent_fd = None
    file_fd = None
    try:
        root_fd = os.open(
            workspace_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_fd, name = _open_parent(root_fd, decision._parts)
        file_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        if checkpoint is not None:
            checkpoint("opened")
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            return {
                "status": "rejected",
                "code": "not_a_file",
                "reason": "target is not a regular file",
                "removed": False,
                "relative_path": decision.relative_path,
            }
        data = _read_fd(file_fd)
        actual = hashlib.sha256(data).hexdigest()
        if actual != decision.expected_sha256:
            return {
                "status": "rejected",
                "code": "hash_mismatch",
                "reason": "content does not match the ownership record",
                "removed": False,
                "relative_path": decision.relative_path,
            }
        if checkpoint is not None:
            checkpoint("verified")
            checkpoint("before_unlink")
        # Narrow the window: the name must still refer to the same inode.
        try:
            current = os.stat(
                name, dir_fd=parent_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            current = None
        if current is None or (current.st_dev, current.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            return {
                "status": "unverified",
                "code": "target_replaced",
                "reason": "target changed between verification and unlink",
                "removed": False,
                "relative_path": decision.relative_path,
            }
        os.unlink(name, dir_fd=parent_fd)
        if checkpoint is not None:
            checkpoint("after_unlink")
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            gone = False
        except FileNotFoundError:
            gone = True
        return {
            "status": "completed" if gone else "unverified",
            "code": "removed" if gone else "removal_unconfirmed",
            "reason": "file removed" if gone else "file still present",
            "removed": gone,
            "relative_path": decision.relative_path,
            "sha256": decision.expected_sha256,
            "device": info.st_dev,
            "inode": info.st_ino,
        }
    except CleanupError as exc:
        return {
            "status": "rejected",
            "code": exc.code,
            "reason": exc.message,
            "removed": False,
            "relative_path": decision.relative_path,
        }
    except FileNotFoundError:
        return {
            "status": "rejected",
            "code": "missing_target",
            "reason": "target does not exist",
            "removed": False,
            "relative_path": decision.relative_path,
        }
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return {
                "status": "rejected",
                "code": "unsafe_path",
                "reason": "path component is a symlink or not a directory",
                "removed": False,
                "relative_path": decision.relative_path,
            }
        return {
            "status": "failed",
            "code": "unlink_failed",
            "reason": type(exc).__name__,
            "removed": False,
            "relative_path": decision.relative_path,
        }
    finally:
        for fd in (file_fd, parent_fd, root_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def summarize_batch(results: list[dict[str, Any]]) -> str:
    if not results:
        return "rejected"
    statuses = [item["status"] for item in results]
    if all(status == "completed" for status in statuses):
        return "completed"
    if all(status == "rejected" for status in statuses):
        return "rejected"
    if any(status == "unverified" for status in statuses):
        return "unverified"
    if any(status == "completed" for status in statuses):
        return "partial"
    return "failed"
