"""Single controlled recovery operation with a real, reversible side effect."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .recovery_preview import (
    ApprovalStoreError,
    RecoveryAction,
    RecoveryApprovalStore,
    RecoveryPreview,
)


CONTROLLED_RESTORE_ACTION = "restore_controlled_marker"
CONTROLLED_RESTORE_DIRECTORY = "controlled-recovery"
CONTROLLED_RESTORE_FILENAME = "restored-marker.txt"
CONTROLLED_RESTORE_RELATIVE_PATH = (
    f"{CONTROLLED_RESTORE_DIRECTORY}/{CONTROLLED_RESTORE_FILENAME}"
)
CONTROLLED_RESTORE_CONTENT = b"zevorinth-controlled-recovery-v1\n"
CONTROLLED_RESTORE_SHA256 = hashlib.sha256(
    CONTROLLED_RESTORE_CONTENT
).hexdigest()
CONTROLLED_RESTORE_MODE = 0o600
MAX_VERIFICATION_BYTES = 4096
Checkpoint = Callable[[str], None]


def _workspace_root(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def controlled_restore_target(path: Path | str) -> str:
    root = _workspace_root(path)
    return f"file:{root / CONTROLLED_RESTORE_RELATIVE_PATH}"


def _open_root(path: Path | str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(_workspace_root(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(errno.ENOTDIR, "workspace is not a directory")
        return fd, info
    except Exception:
        os.close(fd)
        raise


def _read_bounded(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(fd, min(4096, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _observation_base(root: Path) -> dict[str, Any]:
    return {
        "target": controlled_restore_target(root),
        "relative_path": CONTROLLED_RESTORE_RELATIVE_PATH,
        "expected_sha256": CONTROLLED_RESTORE_SHA256,
        "expected_size": len(CONTROLLED_RESTORE_CONTENT),
        "expected_mode": CONTROLLED_RESTORE_MODE,
        "verified": False,
    }


def verify_controlled_restore(path: Path | str) -> dict[str, Any]:
    """Independently read the real target state without following symlinks."""
    root = _workspace_root(path)
    observation = _observation_base(root)
    parent_fd = None
    target_fd = None
    try:
        root_fd, root_info = _open_root(root)
    except FileNotFoundError:
        return {
            **observation,
            "state": "workspace_unavailable",
            "target_exists": False,
        }
    except OSError as exc:
        return {
            **observation,
            "state": (
                "root_unsafe"
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}
                else "workspace_unavailable"
            ),
            "target_exists": False,
            "error": type(exc).__name__,
        }
    observation.update(
        workspace_device=root_info.st_dev,
        workspace_inode=root_info.st_ino,
    )
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        try:
            parent_fd = os.open(
                CONTROLLED_RESTORE_DIRECTORY,
                directory_flags,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return {
                **observation,
                "state": "target_absent",
                "target_exists": False,
                "parent_state": "absent",
            }
        except OSError as exc:
            return {
                **observation,
                "state": "parent_unsafe",
                "target_exists": False,
                "parent_state": "unsafe",
                "error": type(exc).__name__,
            }
        parent_info = os.fstat(parent_fd)
        observation["parent_state"] = "present"
        observation["parent_device"] = parent_info.st_dev
        observation["parent_inode"] = parent_info.st_ino
        if stat.S_IMODE(parent_info.st_mode) != 0o700:
            return {
                **observation,
                "state": "parent_mode_mismatch",
                "target_exists": False,
            }
        try:
            entry = os.stat(
                CONTROLLED_RESTORE_FILENAME,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return {
                **observation,
                "state": "target_absent",
                "target_exists": False,
            }
        if stat.S_ISLNK(entry.st_mode):
            return {
                **observation,
                "state": "target_symlink",
                "target_exists": True,
            }
        if not stat.S_ISREG(entry.st_mode):
            return {
                **observation,
                "state": "target_not_regular",
                "target_exists": True,
            }
        file_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        try:
            target_fd = os.open(
                CONTROLLED_RESTORE_FILENAME,
                file_flags,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            return {
                **observation,
                "state": "target_unreadable",
                "target_exists": True,
                "error": type(exc).__name__,
            }
        file_info = os.fstat(target_fd)
        if (file_info.st_dev, file_info.st_ino) != (
            entry.st_dev,
            entry.st_ino,
        ):
            return {
                **observation,
                "state": "target_identity_changed",
                "target_exists": True,
            }
        data = _read_bounded(target_fd, MAX_VERIFICATION_BYTES)
        digest = hashlib.sha256(data).hexdigest()
        mode = stat.S_IMODE(file_info.st_mode)
        observation.update(
            target_exists=True,
            size=len(data),
            sha256=digest,
            mode=mode,
            device=file_info.st_dev,
            inode=file_info.st_ino,
        )
        if len(data) > MAX_VERIFICATION_BYTES:
            return {**observation, "state": "target_oversized"}
        if mode != CONTROLLED_RESTORE_MODE:
            return {**observation, "state": "target_mode_mismatch"}
        if (
            len(data) != len(CONTROLLED_RESTORE_CONTENT)
            or digest != CONTROLLED_RESTORE_SHA256
        ):
            return {**observation, "state": "target_digest_mismatch"}
        return {**observation, "state": "verified", "verified": True}
    finally:
        if target_fd is not None:
            os.close(target_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def controlled_restore_action(path: Path | str) -> RecoveryAction:
    root = _workspace_root(path)
    before = verify_controlled_restore(root)
    if before["state"] != "target_absent":
        raise ValueError(
            "controlled restore requires an absent target in an available "
            f"workspace, got {before['state']}"
        )
    return RecoveryAction(
        action=CONTROLLED_RESTORE_ACTION,
        target=controlled_restore_target(root),
        current_state="target_absent",
        operation=(
            "create one fixed recovery marker in the approved workspace"
        ),
        preconditions=(
            "workspace root identity and controlled directory remain safe",
            "target path is absent and has no symlink parent",
            "execution intent is persisted before file creation",
        ),
        expected_result=(
            "regular file mode=0600 with sha256="
            f"{CONTROLLED_RESTORE_SHA256}"
        ),
        risk="low",
        verification="independent no-follow readback and SHA-256 check",
        requires_approval=True,
        executable=False,
    )


def with_controlled_restore_action(
    preview: RecoveryPreview,
    workspace_root: Path | str,
) -> RecoveryPreview:
    """Append the one allowlisted recovery action to a read-only preview."""
    if any(
        action.action == CONTROLLED_RESTORE_ACTION
        for action in preview.actions
    ):
        raise ValueError("controlled restore action already exists")
    action = controlled_restore_action(workspace_root)
    return replace(
        preview,
        actions=(*preview.actions, action),
        confirmed=(
            *preview.confirmed,
            f"controlled_restore_precondition={action.current_state}",
        ),
    )


def _materialize_controlled_restore(
    workspace_root: Path | str,
    *,
    precondition: dict[str, Any],
) -> dict[str, Any]:
    root = _workspace_root(workspace_root)
    base = {
        "operation": CONTROLLED_RESTORE_ACTION,
        "target": controlled_restore_target(root),
        "relative_path": CONTROLLED_RESTORE_RELATIVE_PATH,
    }
    root_fd = parent_fd = target_fd = None
    try:
        root_fd, root_info = _open_root(root)
        if (
            root_info.st_dev != precondition.get("workspace_device")
            or root_info.st_ino != precondition.get("workspace_inode")
        ):
            return {
                **base,
                "status": "failed",
                "code": "workspace_identity_changed",
                "created": False,
            }
        if precondition.get("parent_state") == "absent":
            os.mkdir(
                CONTROLLED_RESTORE_DIRECTORY, 0o700, dir_fd=root_fd
            )
        elif precondition.get("parent_state") != "present":
            return {
                **base,
                "status": "failed",
                "code": "parent_state_changed",
                "created": False,
            }
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        parent_fd = os.open(
            CONTROLLED_RESTORE_DIRECTORY,
            directory_flags,
            dir_fd=root_fd,
        )
        parent_info = os.fstat(parent_fd)
        if stat.S_IMODE(parent_info.st_mode) != 0o700:
            return {
                **base,
                "status": "failed",
                "code": "parent_mode_mismatch",
                "created": False,
            }
        if precondition.get("parent_inode") is not None and (
            parent_info.st_dev != precondition.get("parent_device")
            or parent_info.st_ino != precondition.get("parent_inode")
        ):
            return {
                **base,
                "status": "failed",
                "code": "parent_identity_changed",
                "created": False,
            }
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        target_fd = os.open(
            CONTROLLED_RESTORE_FILENAME,
            file_flags,
            CONTROLLED_RESTORE_MODE,
            dir_fd=parent_fd,
        )
        os.fchmod(target_fd, CONTROLLED_RESTORE_MODE)
        written = 0
        while written < len(CONTROLLED_RESTORE_CONTENT):
            count = os.write(
                target_fd, CONTROLLED_RESTORE_CONTENT[written:]
            )
            if count <= 0:
                raise OSError(errno.EIO, "marker write made no progress")
            written += count
        os.fsync(target_fd)
        file_info = os.fstat(target_fd)
        os.close(target_fd)
        target_fd = None
        os.fsync(parent_fd)
        return {
            **base,
            "status": "materialized",
            "code": "created",
            "created": True,
            "bytes_written": written,
            "sha256": CONTROLLED_RESTORE_SHA256,
            "mode": stat.S_IMODE(file_info.st_mode),
            "device": file_info.st_dev,
            "inode": file_info.st_ino,
            "parent_device": parent_info.st_dev,
            "parent_inode": parent_info.st_ino,
        }
    except FileExistsError:
        return {
            **base,
            "status": "failed",
            "code": "target_already_exists",
            "created": False,
        }
    except OSError as exc:
        return {
            **base,
            "status": "failed",
            "code": (
                "unsafe_path"
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}
                else "materialization_failed"
            ),
            "created": False,
            "error": type(exc).__name__,
        }
    finally:
        if target_fd is not None:
            os.close(target_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        if root_fd is not None:
            os.close(root_fd)


def _completion_failure(
    store: RecoveryApprovalStore,
    approval_id: str,
    *,
    attempt_id: str,
    result: dict[str, Any],
    reason: str,
    now: str | None,
    observation: dict[str, Any],
) -> dict[str, Any]:
    try:
        store.complete(
            approval_id,
            execution_attempt_id=attempt_id,
            result=result,
            verification_passed=False,
            verification_result=reason,
            now=now,
        )
    except ApprovalStoreError as exc:
        return {
            "status": "unverified",
            "code": "completion_persistence_failed",
            "reason": exc.code,
            "manual_review": True,
            "safe_to_retry": False,
            "execution_attempt_id": attempt_id,
            "observation": observation,
            "execution_supported": True,
            "general_recovery_supported": False,
        }
    return {
        "status": "failed",
        "code": reason,
        "manual_review": True,
        "safe_to_retry": False,
        "execution_attempt_id": attempt_id,
        "observation": observation,
        "execution_supported": True,
        "general_recovery_supported": False,
    }


def execute_controlled_restore(
    store: RecoveryApprovalStore,
    approval_id: str,
    preview: RecoveryPreview,
    workspace_root: Path | str,
    *,
    now: str | None = None,
    checkpoint: Checkpoint | None = None,
) -> dict[str, Any]:
    """Execute only the approved fixed marker operation, after durable intent."""
    root = _workspace_root(workspace_root)
    target = controlled_restore_target(root)
    try:
        approval = store.load(approval_id)
    except ApprovalStoreError as exc:
        return {
            "status": "denied",
            "code": exc.code,
            "manual_review": False,
            "safe_to_retry": False,
            "execution_supported": False,
            "general_recovery_supported": False,
        }
    if (
        approval.action != CONTROLLED_RESTORE_ACTION
        or approval.target != target
    ):
        return {
            "status": "denied",
            "code": "controlled_action_binding_mismatch",
            "manual_review": False,
            "safe_to_retry": False,
            "execution_supported": False,
            "general_recovery_supported": False,
        }
    try:
        claim = store.claim(
            approval_id,
            preview,
            action=CONTROLLED_RESTORE_ACTION,
            target=target,
            now=now,
        )
    except ApprovalStoreError as exc:
        return {
            "status": "unverified",
            "code": "claim_persistence_failed",
            "reason": exc.code,
            "manual_review": True,
            "safe_to_retry": False,
            "execution_supported": False,
            "general_recovery_supported": False,
        }
    if claim["status"] != "claimed":
        return {
            **claim,
            "general_recovery_supported": False,
        }
    attempt_id = str(claim["execution_attempt_id"])

    before = verify_controlled_restore(root)
    if before["state"] != "target_absent":
        return _completion_failure(
            store,
            approval_id,
            attempt_id=attempt_id,
            result={
                "operation": CONTROLLED_RESTORE_ACTION,
                "target": target,
                "precondition": before,
            },
            reason=f"precondition_{before['state']}",
            now=now,
            observation=before,
        )

    intent = {
        "intent_version": "controlled-restore-intent-v1",
        "execution_attempt_id": attempt_id,
        "action": CONTROLLED_RESTORE_ACTION,
        "target": target,
        "action_digest": approval.action_digest,
        "workspace_root": str(root),
        "relative_path": CONTROLLED_RESTORE_RELATIVE_PATH,
        "expected_sha256": CONTROLLED_RESTORE_SHA256,
        "expected_size": len(CONTROLLED_RESTORE_CONTENT),
        "expected_mode": CONTROLLED_RESTORE_MODE,
        "precondition": {
            key: before.get(key)
            for key in (
                "state",
                "workspace_device",
                "workspace_inode",
                "parent_state",
                "parent_device",
                "parent_inode",
            )
        },
    }
    try:
        recorded = store.record_intent(
            approval_id,
            execution_attempt_id=attempt_id,
            intent=intent,
            now=now,
        )
    except ApprovalStoreError as exc:
        return {
            "status": "unverified",
            "code": "intent_persistence_failed",
            "reason": exc.code,
            "manual_review": True,
            "safe_to_retry": False,
            "execution_attempt_id": attempt_id,
            "observation": before,
            "execution_supported": True,
            "general_recovery_supported": False,
        }
    if recorded["status"] not in {"recorded", "unchanged"}:
        return {
            **recorded,
            "general_recovery_supported": False,
        }
    if checkpoint is not None:
        checkpoint("after_intent")

    execution = _materialize_controlled_restore(
        root,
        precondition=intent["precondition"],
    )
    if checkpoint is not None:
        checkpoint("after_side_effect")
    verification = verify_controlled_restore(root)
    result = {
        "operation": CONTROLLED_RESTORE_ACTION,
        "target": target,
        "intent": intent,
        "execution": execution,
        "verification": verification,
    }
    if not verification["verified"]:
        return _completion_failure(
            store,
            approval_id,
            attempt_id=attempt_id,
            result=result,
            reason=f"independent_verification_{verification['state']}",
            now=now,
            observation=verification,
        )
    try:
        completed = store.complete(
            approval_id,
            execution_attempt_id=attempt_id,
            result=result,
            verification_passed=True,
            verification_result=(
                "independent no-follow readback verified "
                f"sha256={CONTROLLED_RESTORE_SHA256}"
            ),
            now=now,
        )
    except ApprovalStoreError as exc:
        return {
            "status": "unverified",
            "code": "completion_persistence_failed",
            "reason": exc.code,
            "manual_review": True,
            "safe_to_retry": False,
            "execution_attempt_id": attempt_id,
            "observation": verification,
            "execution_supported": True,
            "general_recovery_supported": False,
        }
    return {
        "status": "succeeded",
        "code": "controlled_marker_restored",
        "manual_review": False,
        "safe_to_retry": False,
        "execution_attempt_id": attempt_id,
        "approval_status": completed["approval_status"],
        "observation": verification,
        "execution_supported": True,
        "general_recovery_supported": False,
    }


def reconcile_controlled_restore(
    store: RecoveryApprovalStore,
    approval_id: str,
    workspace_root: Path | str,
) -> dict[str, Any]:
    """Read-only crash reconciliation for the controlled operation."""
    approval = store.load(approval_id)
    root = _workspace_root(workspace_root)
    target = controlled_restore_target(root)
    base = store.reconcile(approval_id)
    if (
        approval.action != CONTROLLED_RESTORE_ACTION
        or approval.target != target
    ):
        return {
            **base,
            "status": "unverified",
            "reason": "controlled_action_binding_mismatch",
            "manual_review": True,
            "safe_to_retry": False,
            "execution_supported": False,
            "general_recovery_supported": False,
        }
    observation = verify_controlled_restore(root)
    if approval.status == "consuming":
        return {
            **base,
            "status": "unverified",
            "reason": "claimed_controlled_operation_requires_manual_review",
            "manual_review": True,
            "safe_to_retry": False,
            "observation": observation,
            "execution_supported": True,
            "general_recovery_supported": False,
        }
    if approval.status == "consumed" and not observation["verified"]:
        return {
            **base,
            "status": "unverified",
            "reason": "terminal_state_differs_from_current_observation",
            "manual_review": True,
            "safe_to_retry": False,
            "observation": observation,
            "execution_supported": True,
            "general_recovery_supported": False,
        }
    return {
        **base,
        "observation": observation,
        "execution_supported": approval.status in {"consumed", "failed"},
        "general_recovery_supported": False,
    }
