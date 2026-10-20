"""Single controlled recovery operation with a real, reversible side effect."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .recovery_preview import (
    ApprovalStoreError,
    RecoveryAction,
    RecoveryApprovalStore,
    RecoveryPreview,
    approval_record_digest,
    validate_recovery_approval,
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


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def controlled_recovery_evidence_digest(
    intent: dict[str, Any],
    execution: dict[str, Any],
    verification: dict[str, Any],
) -> str:
    return _canonical_digest(
        {
            "intent": intent,
            "execution": execution,
            "verification": verification,
        }
    )


def _seal_recovery_result(result: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(result)
    intent = sealed.get("intent")
    execution = sealed.get("execution")
    verification = sealed.get("verification")
    if (
        isinstance(intent, dict)
        and isinstance(execution, dict)
        and isinstance(verification, dict)
    ):
        sealed["evidence_digest"] = controlled_recovery_evidence_digest(
            intent, execution, verification
        )
    return sealed


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
        "operation": CONTROLLED_RESTORE_ACTION,
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
    checkpoint: Checkpoint | None = None,
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
        if checkpoint is not None:
            checkpoint("during_side_effect")
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
    result = _seal_recovery_result(result)
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
    if checkpoint is not None:
        checkpoint("after_claim")
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
        checkpoint=checkpoint,
    )
    if checkpoint is not None:
        checkpoint("after_side_effect")
    first_verification = verify_controlled_restore(root)
    if checkpoint is not None:
        checkpoint("after_verification")
    # Re-read after the interruption hook; only the final read is evidence.
    verification = verify_controlled_restore(root)
    if checkpoint is not None:
        checkpoint("before_completion_write")
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
    result = _seal_recovery_result(result)
    evidence_digest = str(result["evidence_digest"])
    try:
        completed = store.complete(
            approval_id,
            execution_attempt_id=attempt_id,
            result=result,
            verification_passed=True,
            verification_result=(
                "independent no-follow readback verified "
                f"sha256={CONTROLLED_RESTORE_SHA256} "
                f"evidence_sha256={evidence_digest}"
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
    if checkpoint is not None:
        checkpoint("after_completion_write")
    return {
        "status": "succeeded",
        "code": "controlled_marker_restored",
        "manual_review": False,
        "safe_to_retry": False,
        "execution_attempt_id": attempt_id,
        "approval_status": completed["approval_status"],
        "observation": verification,
        "evidence_digest": evidence_digest,
        "execution_supported": True,
        "general_recovery_supported": False,
    }


def _audit_approval_shape(approval: Any) -> list[str]:
    issues: list[str] = []
    forbidden = {
        "approved": (
            "execution_attempt_id",
            "claimed_at",
            "execution_intent",
            "intent_recorded_at",
            "completed_at",
            "result",
            "verification_result",
            "verification_passed",
        ),
        "revoked": (
            "execution_attempt_id",
            "claimed_at",
            "execution_intent",
            "intent_recorded_at",
            "completed_at",
            "result",
            "verification_result",
            "verification_passed",
        ),
        "rejected": (
            "execution_attempt_id",
            "claimed_at",
            "execution_intent",
            "intent_recorded_at",
            "completed_at",
            "result",
            "verification_result",
            "verification_passed",
        ),
        "consuming": (
            "completed_at",
            "result",
            "verification_result",
            "verification_passed",
        ),
    }
    for field in forbidden.get(approval.status, ()):
        if getattr(approval, field) is not None:
            issues.append(f"{approval.status}_has_{field}")
    if approval.status in {"consuming", "consumed", "failed"}:
        if not approval.execution_attempt_id:
            issues.append("claimed_state_missing_attempt")
        if not approval.claimed_at:
            issues.append("claimed_state_missing_claim_time")
    return issues


def _audit_intent(approval: Any, root: Path) -> list[str]:
    intent = approval.execution_intent
    if not isinstance(intent, dict):
        return ["intent_missing"]
    issues: list[str] = []
    expected = {
        "intent_version": "controlled-restore-intent-v1",
        "execution_attempt_id": approval.execution_attempt_id,
        "action": approval.action,
        "target": approval.target,
        "action_digest": approval.action_digest,
        "workspace_root": str(root),
        "relative_path": CONTROLLED_RESTORE_RELATIVE_PATH,
        "expected_sha256": CONTROLLED_RESTORE_SHA256,
        "expected_size": len(CONTROLLED_RESTORE_CONTENT),
        "expected_mode": CONTROLLED_RESTORE_MODE,
    }
    for field, value in expected.items():
        if intent.get(field) != value:
            issues.append(f"intent_{field}_mismatch")
    return issues


def _audit_stored_evidence(approval: Any, root: Path) -> list[str]:
    issues: list[str] = []
    if not isinstance(approval.result, dict):
        return ["result_missing"]
    intent = approval.execution_intent
    execution = approval.result.get("execution")
    verification = approval.result.get("verification")
    issues.extend(_audit_intent(approval, root))
    if not isinstance(intent, dict):
        return issues
    if approval.result.get("operation") != CONTROLLED_RESTORE_ACTION:
        issues.append("result_operation_mismatch")
    if approval.result.get("target") != approval.target:
        issues.append("result_target_mismatch")
    if not isinstance(execution, dict):
        issues.append("execution_evidence_missing")
    if not isinstance(verification, dict):
        issues.append("verification_evidence_missing")
    if issues:
        return issues
    if approval.result.get("intent") != intent:
        issues.append("result_intent_mismatch")
    digest = controlled_recovery_evidence_digest(
        intent, execution, verification
    )
    if approval.result.get("evidence_digest") != digest:
        issues.append("evidence_digest_mismatch")
    if f"evidence_sha256={digest}" not in (
        approval.verification_result or ""
    ):
        issues.append("verification_result_binding_missing")
    if (
        intent.get("execution_attempt_id")
        != approval.execution_attempt_id
        or intent.get("action") != approval.action
        or intent.get("target") != approval.target
        or intent.get("action_digest") != approval.action_digest
    ):
        issues.append("intent_identity_mismatch")
    for evidence, label in (
        (execution, "execution"),
        (verification, "verification"),
    ):
        if evidence.get("operation") != CONTROLLED_RESTORE_ACTION:
            issues.append(f"{label}_operation_mismatch")
        if evidence.get("target") != approval.target:
            issues.append(f"{label}_target_mismatch")
        if (
            evidence.get("relative_path")
            != CONTROLLED_RESTORE_RELATIVE_PATH
        ):
            issues.append(f"{label}_relative_path_mismatch")
    if execution.get("bytes_written") != len(CONTROLLED_RESTORE_CONTENT):
        issues.append("execution_size_mismatch")
    if execution.get("mode") != CONTROLLED_RESTORE_MODE:
        issues.append("execution_mode_mismatch")
    expected = {
        "sha256": CONTROLLED_RESTORE_SHA256,
        "size": len(CONTROLLED_RESTORE_CONTENT),
        "mode": CONTROLLED_RESTORE_MODE,
    }
    for field, value in expected.items():
        if verification.get(field) != value:
            issues.append(f"verification_{field}_mismatch")
        if (
            field == "sha256"
            and execution.get(field) != value
        ):
            issues.append("execution_sha256_mismatch")
    if verification.get("state") != "verified":
        issues.append("verification_not_verified")
    if execution.get("status") != "materialized":
        issues.append("execution_not_materialized")
    if (
        execution.get("device"),
        execution.get("inode"),
    ) != (
        verification.get("device"),
        verification.get("inode"),
    ):
        issues.append("execution_verification_identity_mismatch")
    if not approval.verification_passed:
        issues.append("approval_verification_not_passed")
    return list(dict.fromkeys(issues))


def _audit_failed_evidence(approval: Any, root: Path) -> list[str]:
    result = approval.result
    if not isinstance(result, dict):
        return ["result_missing"]
    issues: list[str] = []
    if result.get("operation") != CONTROLLED_RESTORE_ACTION:
        issues.append("result_operation_mismatch")
    if result.get("target") != approval.target:
        issues.append("result_target_mismatch")
    if isinstance(result.get("precondition"), dict):
        return issues
    execution = result.get("execution")
    verification = result.get("verification")
    if not isinstance(execution, dict):
        issues.append("execution_evidence_missing")
    if not isinstance(verification, dict):
        issues.append("verification_evidence_missing")
    if not isinstance(approval.execution_intent, dict):
        issues.append("intent_missing")
    if issues:
        return list(dict.fromkeys(issues))
    issues.extend(_audit_intent(approval, root))
    if result.get("intent") != approval.execution_intent:
        issues.append("result_intent_mismatch")
    digest = controlled_recovery_evidence_digest(
        approval.execution_intent, execution, verification
    )
    if result.get("evidence_digest") != digest:
        issues.append("evidence_digest_mismatch")
    for evidence, label in (
        (execution, "execution"),
        (verification, "verification"),
    ):
        if evidence.get("operation") != CONTROLLED_RESTORE_ACTION:
            issues.append(f"{label}_operation_mismatch")
        if evidence.get("target") != approval.target:
            issues.append(f"{label}_target_mismatch")
        if (
            evidence.get("relative_path")
            != CONTROLLED_RESTORE_RELATIVE_PATH
        ):
            issues.append(f"{label}_relative_path_mismatch")
    return list(dict.fromkeys(issues))


def audit_controlled_recovery(
    store: RecoveryApprovalStore,
    approval_id: str,
    workspace_root: Path | str,
    *,
    now: str | None = None,
    preview: RecoveryPreview | None = None,
) -> dict[str, Any]:
    """Audit persisted approval evidence against the current real file."""
    root = _workspace_root(workspace_root)
    target = controlled_restore_target(root)
    checked_at = (
        datetime.fromisoformat(now)
        if now is not None
        else datetime.now(timezone.utc)
    )
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    audit = {
        "auditor_version": "controlled-recovery-audit-v1",
        "checked_at": checked_at.isoformat(timespec="milliseconds"),
        "scope": "controlled_recovery_consistency_only",
        "security_certification": False,
        "approval_id": approval_id,
        "target": target,
        "manual_review": False,
        "safe_to_retry": False,
        "execution_supported": False,
        "general_recovery_supported": False,
    }
    try:
        approval = store.load(approval_id)
    except ApprovalStoreError as exc:
        return {
            **audit,
            "status": "unverified",
            "manual_review": True,
            "reason": "approval_store_unreadable",
            "code": exc.code,
        }
    shape_issues = _audit_approval_shape(approval)
    if shape_issues:
        return {
            **audit,
            "status": "unverified",
            "reason": "approval_state_inconsistent",
            "manual_review": True,
            "approval_status": approval.status,
            "approval_issues": shape_issues,
            "observation": verify_controlled_restore(root),
        }
    observation = verify_controlled_restore(root)
    audit.update(
        approval_status=approval.status,
        approval_record_digest=approval_record_digest(approval),
        approval_state_digest=approval.state_digest,
        execution_attempt_id=approval.execution_attempt_id,
        intent_recorded=approval.execution_intent is not None,
        observation=observation,
        observation_digest=_canonical_digest(observation),
        approval_freshness="not_checked",
    )
    if approval.execution_intent is not None:
        audit["intent_digest"] = _canonical_digest(
            approval.execution_intent
        )
    if approval.status in {"revoked", "rejected"}:
        return {**audit, "status": "denied"}
    if (
        approval.action != CONTROLLED_RESTORE_ACTION
        or approval.target != target
    ):
        return {
            **audit,
            "status": "unverified",
            "reason": "controlled_action_binding_mismatch",
            "manual_review": True,
        }
    if approval.status == "approved":
        if preview is not None:
            verdict = validate_recovery_approval(approval, preview)
            audit["approval_freshness"] = verdict["state"]
            if not verdict["valid"]:
                return {
                    **audit,
                    "status": "unverified",
                    "reason": "approval_freshness_invalid",
                    "manual_review": True,
                    "approval_verdict": verdict,
                }
        return {**audit, "status": "not_claimed"}
    if approval.status == "consuming":
        if approval.execution_intent is None:
            reason = "claimed_without_persisted_intent"
        elif intent_issues := _audit_intent(approval, root):
            return {
                **audit,
                "status": "unverified",
                "reason": "execution_intent_inconsistent",
                "manual_review": True,
                "intent_issues": intent_issues,
                "execution_supported": True,
            }
        elif observation["state"] == "target_absent":
            reason = "interrupted_before_or_during_side_effect"
        elif observation["verified"]:
            reason = "side_effect_observed_without_persisted_completion"
        else:
            reason = "side_effect_state_uncertain"
        return {
            **audit,
            "status": "unverified",
            "reason": reason,
            "manual_review": True,
            "execution_supported": True,
        }
    if approval.status == "failed":
        failure_issues = _audit_failed_evidence(approval, root)
        if failure_issues:
            return {
                **audit,
                "status": "unverified",
                "reason": "stored_failure_evidence_inconsistent",
                "manual_review": True,
                "evidence_issues": failure_issues,
                "execution_supported": True,
            }
        return {
            **audit,
            "status": "verified_failure",
            "reason": (
                "failed_attempt_with_currently_verified_target"
                if observation["verified"]
                else "failed_attempt_recorded"
            ),
            "manual_review": True,
            "execution_supported": True,
        }
    if approval.status != "consumed":
        return {
            **audit,
            "status": "unverified",
            "reason": "approval_status_unknown",
            "manual_review": True,
        }
    evidence_issues = _audit_stored_evidence(approval, root)
    if evidence_issues:
        return {
            **audit,
            "status": "unverified",
            "reason": "stored_evidence_inconsistent",
            "manual_review": True,
            "evidence_issues": evidence_issues,
        }
    if not observation["verified"]:
        return {
            **audit,
            "status": "unverified",
            "reason": "historical_success_current_observation_diverged",
            "manual_review": True,
            "execution_supported": True,
        }
    verification = approval.result["verification"]
    if (
        observation.get("device"),
        observation.get("inode"),
    ) != (
        verification.get("device"),
        verification.get("inode"),
    ):
        return {
            **audit,
            "status": "unverified",
            "reason": "current_file_identity_differs_from_completion_evidence",
            "manual_review": True,
            "execution_supported": True,
        }
    return {
        **audit,
        "status": "consistent",
        "reason": "stored_evidence_and_current_file_match",
        "evidence_digest": approval.result.get("evidence_digest"),
        "execution_supported": True,
    }


def reconcile_controlled_restore(
    store: RecoveryApprovalStore,
    approval_id: str,
    workspace_root: Path | str,
) -> dict[str, Any]:
    """Read-only crash reconciliation for the controlled operation."""
    audit = audit_controlled_recovery(store, approval_id, workspace_root)
    if audit["status"] == "consistent":
        return {**audit, "status": "completed"}
    return audit
