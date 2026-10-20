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
QUARANTINE_DIR = ".quarantine"
QUARANTINE_CHECKPOINTS = (
    "opened",
    "before_quarantine",
    "after_quarantine",
    "quarantine_verified",
    "before_unlink",
    "after_unlink",
)


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
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Move the verified target into quarantine, re-verify it, then unlink.

    ``rename`` keeps the file on the same filesystem, so no copy fallback is
    needed.  The object is re-opened **from the quarantine directory** and its
    inode and hash are checked again before deletion.
    """
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
    op_id = operation_id or f"op-{os.getpid()}-{id(decision):x}"
    base = {
        "relative_path": decision.relative_path,
        "operation_id": op_id,
    }
    root_fd = parent_fd = file_fd = qdir_fd = opdir_fd = qfile_fd = None
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
            return {**base, "status": "rejected", "code": "not_a_file",
                    "reason": "target is not a regular file", "removed": False}
        data = _read_fd(file_fd)
        actual = hashlib.sha256(data).hexdigest()
        if actual != decision.expected_sha256:
            return {**base, "status": "rejected", "code": "hash_mismatch",
                    "reason": "content does not match the ownership record",
                    "removed": False}
        if checkpoint is not None:
            checkpoint("verified")
            checkpoint("before_quarantine")
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is None or (current.st_dev, current.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            return {**base, "status": "unverified", "code": "target_replaced",
                    "reason": "target changed between verification and move",
                    "removed": False}
        try:
            os.mkdir(QUARANTINE_DIR, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        qdir_fd = os.open(
            QUARANTINE_DIR,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        os.mkdir(op_id, 0o700, dir_fd=qdir_fd)
        opdir_fd = os.open(
            op_id,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=qdir_fd,
        )
        os.rename(
            name, name, src_dir_fd=parent_fd, dst_dir_fd=opdir_fd
        )
        if checkpoint is not None:
            checkpoint("after_quarantine")
        # Re-open the object from quarantine and verify identity + content.
        qfile_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=opdir_fd,
        )
        qinfo = os.fstat(qfile_fd)
        qdata = _read_fd(qfile_fd)
        qdigest = hashlib.sha256(qdata).hexdigest()
        identity_ok = (qinfo.st_dev, qinfo.st_ino) == (info.st_dev, info.st_ino)
        if not identity_ok or qdigest != decision.expected_sha256:
            return {
                **base,
                "status": "unverified",
                "code": "quarantine_mismatch",
                "reason": "quarantined object does not match the record",
                "removed": False,
                "quarantine_path": f"{QUARANTINE_DIR}/{op_id}/{name}",
                "identity_verified": identity_ok,
                "hash_verified": qdigest == decision.expected_sha256,
            }
        if checkpoint is not None:
            checkpoint("quarantine_verified")
            checkpoint("before_unlink")
        os.unlink(name, dir_fd=opdir_fd)
        if checkpoint is not None:
            checkpoint("after_unlink")
        try:
            os.stat(name, dir_fd=opdir_fd, follow_symlinks=False)
            gone = False
        except FileNotFoundError:
            gone = True
        return {
            **base,
            "status": "completed" if gone else "unverified",
            "code": "removed" if gone else "removal_unconfirmed",
            "reason": "file removed" if gone else "file still present",
            "removed": gone,
            "sha256": decision.expected_sha256,
            "device": info.st_dev,
            "inode": info.st_ino,
            "quarantine_path": f"{QUARANTINE_DIR}/{op_id}/{name}",
            "identity_verified": True,
            "hash_verified": True,
        }
    except CleanupError as exc:
        return {**base, "status": "rejected", "code": exc.code,
                "reason": exc.message, "removed": False}
    except FileNotFoundError:
        return {**base, "status": "rejected", "code": "missing_target",
                "reason": "target does not exist", "removed": False}
    except FileExistsError:
        return {**base, "status": "unverified",
                "code": "quarantine_conflict",
                "reason": "quarantine entry already exists", "removed": False}
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return {**base, "status": "rejected", "code": "unsafe_path",
                    "reason": "path component is a symlink or not a directory",
                    "removed": False}
        return {**base, "status": "failed", "code": "quarantine_failed",
                "reason": type(exc).__name__, "removed": False}
    finally:
        for fd in (qfile_fd, opdir_fd, qdir_fd, file_fd, parent_fd, root_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def reconcile_cleanup(
    *,
    workspace_root: Path,
    relative_path: str,
    operation_id: str,
    expected_sha256: str,
    intent_persisted: bool,
    completed_event: bool,
    current_generation: int,
    record_generation: int,
) -> dict[str, Any]:
    """Read-only reconciliation of one cleanup operation."""
    if current_generation != record_generation:
        return {
            "operation_id": operation_id,
            "state": "reconciled_conflict",
            "reason": "generation no longer matches the ownership record",
        }
    if not intent_persisted:
        return {
            "operation_id": operation_id,
            "state": "reconciled_unverified",
            "reason": "no persisted cleanup intent",
        }
    parts = tuple(p for p in relative_path.split("/") if p)
    original = workspace_root.joinpath(*parts)
    quarantine = (
        workspace_root.joinpath(*parts[:-1], QUARANTINE_DIR, operation_id, parts[-1])
    )
    original_exists = original.exists() or original.is_symlink()
    quarantine_exists = quarantine.exists() or quarantine.is_symlink()
    quarantine_hash = None
    if quarantine_exists and quarantine.is_file() and not quarantine.is_symlink():
        quarantine_hash = hashlib.sha256(quarantine.read_bytes()).hexdigest()
    base = {
        "operation_id": operation_id,
        "relative_path": relative_path,
        "original_exists": original_exists,
        "quarantine_exists": quarantine_exists,
        "quarantine_sha256": quarantine_hash,
    }
    if completed_event:
        if not original_exists and not quarantine_exists:
            return {**base, "state": "reconciled_completed"}
        return {
            **base,
            "state": "reconciled_conflict",
            "reason": "completion recorded but filesystem disagrees",
        }
    if original_exists and quarantine_exists:
        return {
            **base,
            "state": "reconciled_conflict",
            "reason": "both the original and the quarantined copy exist",
        }
    if original_exists and not quarantine_exists:
        return {**base, "state": "reconciled_not_executed"}
    if quarantine_exists:
        if quarantine_hash != expected_sha256:
            return {
                **base,
                "state": "reconciled_conflict",
                "reason": "quarantined content does not match the record",
            }
        return {
            **base,
            "state": "reconciled_pending",
            "reason": "file is quarantined and verified but not deleted",
        }
    return {
        **base,
        "state": "reconciled_unverified",
        "reason": "neither the original nor the quarantined object exists",
    }


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
