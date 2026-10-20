"""Read-only recovery preview and approval binding."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Event, Task, TaskStatus
from .task_lifecycle import audit_task_lifecycle


PREVIEW_VERSION = "recovery-preview-v1"
APPROVAL_DIR = "recovery_approvals"
APPROVAL_ID_PATTERN = re.compile(r"^approval-[A-Za-z0-9_-]{1,120}$")


def _now(value: str | None) -> datetime:
    if value:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    action: str
    target: str
    current_state: str
    operation: str
    preconditions: tuple[str, ...]
    expected_result: str
    risk: str
    verification: str
    requires_approval: bool
    executable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "target": self.target,
            "current_state": self.current_state,
            "operation": self.operation,
            "preconditions": list(self.preconditions),
            "expected_result": self.expected_result,
            "risk": self.risk,
            "verification": self.verification,
            "requires_approval": self.requires_approval,
            "executable": self.executable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecoveryAction":
        return cls(
            action=str(data["action"]),
            target=str(data["target"]),
            current_state=str(data["current_state"]),
            operation=str(data["operation"]),
            preconditions=tuple(str(item) for item in data["preconditions"]),
            expected_result=str(data["expected_result"]),
            risk=str(data["risk"]),
            verification=str(data["verification"]),
            requires_approval=bool(data["requires_approval"]),
            executable=bool(data.get("executable", False)),
        )


@dataclass(frozen=True, slots=True)
class RecoveryPreview:
    preview_id: str
    preview_version: str
    created_at: str
    task_id: str
    state_digest: str
    confirmed: tuple[str, ...]
    unknown: tuple[str, ...]
    actions: tuple[RecoveryAction, ...]
    audit: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "preview_id": self.preview_id,
            "preview_version": self.preview_version,
            "created_at": self.created_at,
            "task_id": self.task_id,
            "state_digest": self.state_digest,
            "confirmed": list(self.confirmed),
            "unknown": list(self.unknown),
            "actions": [action.to_dict() for action in self.actions],
            "audit": self.audit,
            "execution_authorized": False,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecoveryPreview":
        return cls(
            preview_id=str(data["preview_id"]),
            preview_version=str(data["preview_version"]),
            created_at=str(data["created_at"]),
            task_id=str(data["task_id"]),
            state_digest=str(data["state_digest"]),
            confirmed=tuple(str(item) for item in data["confirmed"]),
            unknown=tuple(str(item) for item in data["unknown"]),
            actions=tuple(
                RecoveryAction.from_dict(item) for item in data["actions"]
            ),
            audit=dict(data["audit"]),
        )


@dataclass(frozen=True, slots=True)
class RecoveryApproval:
    approval_id: str
    preview_id: str
    preview_version: str
    task_id: str
    state_digest: str
    approver: str
    approved_at: str
    scope: str
    execution_authorized: bool = False
    status: str = "approved"
    revoked_at: str | None = None
    revoked_reason: str | None = None
    record_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "preview_id": self.preview_id,
            "preview_version": self.preview_version,
            "task_id": self.task_id,
            "state_digest": self.state_digest,
            "approver": self.approver,
            "approved_at": self.approved_at,
            "scope": self.scope,
            "execution_authorized": self.execution_authorized,
            "status": self.status,
            "revoked_at": self.revoked_at,
            "revoked_reason": self.revoked_reason,
            "record_digest": self.record_digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecoveryApproval":
        approval = cls(
            approval_id=str(data["approval_id"]),
            preview_id=str(data["preview_id"]),
            preview_version=str(data["preview_version"]),
            task_id=str(data["task_id"]),
            state_digest=str(data["state_digest"]),
            approver=str(data["approver"]),
            approved_at=str(data["approved_at"]),
            scope=str(data["scope"]),
            execution_authorized=bool(data.get("execution_authorized", False)),
            status=str(data.get("status", "approved")),
            revoked_at=(
                str(data["revoked_at"])
                if data.get("revoked_at") is not None
                else None
            ),
            revoked_reason=(
                str(data["revoked_reason"])
                if data.get("revoked_reason") is not None
                else None
            ),
            record_digest=(
                str(data["record_digest"])
                if data.get("record_digest") is not None
                else None
            ),
        )
        if approval_record_digest(approval) != approval.record_digest:
            raise ApprovalStoreError(
                "approval_corrupt", "approval record digest mismatch"
            )
        return approval


class ApprovalStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def approval_record_digest(approval: RecoveryApproval) -> str:
    payload = approval.to_dict()
    payload.pop("record_digest", None)
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class RecoveryApprovalStore:
    """Atomic, restart-readable approval records; never executes actions."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.directory = self.root / APPROVAL_DIR

    def _ensure_directory(self) -> None:
        if self.directory.is_symlink():
            raise ApprovalStoreError(
                "unsafe_store", "approval directory is a symlink"
            )
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)

    def _path(self, approval_id: str) -> Path:
        if not APPROVAL_ID_PATTERN.fullmatch(approval_id):
            raise ApprovalStoreError(
                "invalid_approval_id", "approval id has an invalid format"
            )
        return self.directory / f"{approval_id}.json"

    def save(self, approval: RecoveryApproval) -> dict[str, Any]:
        self._ensure_directory()
        if approval.execution_authorized:
            raise ApprovalStoreError(
                "execution_authorization_forbidden",
                "approval records cannot authorize execution",
            )
        if approval_record_digest(approval) != approval.record_digest:
            raise ApprovalStoreError(
                "approval_corrupt", "approval digest does not match"
            )
        target = self._path(approval.approval_id)
        if target.is_symlink():
            raise ApprovalStoreError(
                "unsafe_store", "approval record path is a symlink"
            )
        if target.exists():
            existing = self.load(approval.approval_id)
            if existing.to_dict() == approval.to_dict():
                return {
                    "status": "unchanged",
                    "approval_id": approval.approval_id,
                }
            raise ApprovalStoreError(
                "approval_conflict",
                "a different record already uses this approval id",
            )
        self._write_record(approval)
        return {"status": "saved", "approval_id": approval.approval_id}

    def _write_record(self, approval: RecoveryApproval) -> None:
        data = _canonical(approval.to_dict()).encode("utf-8")
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{approval.approval_id}.", dir=str(self.directory)
        )
        target = self._path(approval.approval_id)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, target)
            target.chmod(0o600)
        except OSError as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise ApprovalStoreError(
                "approval_store_write_failed", type(exc).__name__
            ) from exc

    def load(self, approval_id: str) -> RecoveryApproval:
        path = self._path(approval_id)
        if path.is_symlink():
            raise ApprovalStoreError(
                "unsafe_store", "approval record path is a symlink"
            )
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise ApprovalStoreError(
                "approval_missing", "approval record does not exist"
            ) from exc
        except OSError as exc:
            raise ApprovalStoreError(
                "approval_store_unavailable", type(exc).__name__
            ) from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApprovalStoreError(
                "approval_corrupt", "approval record is not valid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise ApprovalStoreError(
                "approval_corrupt", "approval record is not an object"
            )
        try:
            return RecoveryApproval.from_dict(data)
        except ApprovalStoreError:
            raise
        except Exception as exc:
            raise ApprovalStoreError(
                "approval_corrupt", type(exc).__name__
            ) from exc

    def list(self) -> list[RecoveryApproval]:
        if not self.directory.is_dir():
            return []
        approvals = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                approvals.append(self.load(path.stem))
            except ApprovalStoreError:
                continue
        return approvals

    def revoke(
        self, approval_id: str, *, reason: str, now: str | None = None
    ) -> RecoveryApproval:
        current = self.load(approval_id)
        if current.status == "revoked":
            return current
        if current.status != "approved":
            raise ApprovalStoreError(
                "approval_not_revocable", current.status
            )
        revoked = replace(
            current,
            status="revoked",
            revoked_at=_now(now).isoformat(timespec="milliseconds"),
            revoked_reason=reason,
        )
        revoked = replace(
            revoked,
            record_digest=approval_record_digest(revoked),
        )
        self._write_record(revoked)
        return revoked


def _state_digest(task: Task, events: Iterable[Event], audit: dict[str, Any]) -> str:
    payload = {
        "task": task.to_dict(),
        "events": [event.to_dict() for event in events],
        "audit": {
            "status": audit.get("status"),
            "ok": audit.get("ok"),
            "issues": audit.get("issues"),
            "evidence_state": audit.get("evidence_state"),
            "cleanup_operations": audit.get("cleanup_operations"),
        },
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def build_recovery_preview(
    task: Task,
    events: Iterable[Event],
    *,
    audit: dict[str, Any] | None = None,
    now: str | None = None,
) -> RecoveryPreview:
    """Build a read-only preview; it never executes an action."""
    event_list = list(events)
    report = audit or audit_task_lifecycle(task, event_list, now=now)
    checked = _now(now)
    digest = _state_digest(task, event_list, report)
    confirmed: list[str] = [
        f"task_id={task.task_id}",
        f"status={task.status.value}",
    ]
    unknown: list[str] = []
    if task.plan is not None and task.plan.steps:
        confirmed.append(f"tool={task.plan.steps[0].tool_name}")
    if report.get("evidence_state") is not None:
        state = report["evidence_state"].get("state")
        confirmed.append(f"evidence_state={state}")
        if state != "verified":
            unknown.append("evidence_not_verified")
    if report.get("cleanup_operations"):
        unknown.append("cleanup_lifecycle_not_empty")
    for item in report.get("issues", []):
        code = str(item.get("code"))
        if code in {
            "storage_unreadable",
            "missing_final_execution_plan",
            "succeeded_with_unverified_evidence",
            "invalid_cleanup_timestamp",
        }:
            unknown.append(code)

    actions: list[RecoveryAction] = []
    issue_codes = {str(item.get("code")) for item in report.get("issues", [])}
    if "storage_unreadable" in issue_codes:
        actions.append(
            RecoveryAction(
                action="manual_review",
                target=f"task:{task.task_id}",
                current_state="storage_unreadable",
                operation="inspect a temporary copy; do not repair in place",
                preconditions=("operator reviews storage diagnostics",),
                expected_result="confirmed diagnosis or not_recoverable",
                risk="high",
                verification="independent storage/evidence inspection",
                requires_approval=True,
            )
        )
    elif task.status in {
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
        TaskStatus.INTERRUPTED,
        TaskStatus.BLOCKED,
        TaskStatus.AWAITING_CLARIFICATION,
    }:
        actions.append(
            RecoveryAction(
                action="manual_review",
                target=f"task:{task.task_id}",
                current_state=task.status.value,
                operation="inspect persisted evidence and events; do not retry",
                preconditions=("current attempt and evidence identity checked",),
                expected_result="confirmed resume-read-only or not_recoverable",
                risk="high",
                verification="lifecycle audit and independent evidence review",
                requires_approval=True,
            )
        )
    else:
        actions.append(
            RecoveryAction(
                action="none",
                target=f"task:{task.task_id}",
                current_state=task.status.value,
                operation="no recovery execution",
                preconditions=("terminal state remains immutable",),
                expected_result="no state change",
                risk="low",
                verification="repeat lifecycle audit",
                requires_approval=False,
            )
        )

    for state_item in report.get("cleanup_operations", []):
        if state_item.get("status") in {
            "pending",
            "expired",
            "unverified",
        }:
            actions.append(
                RecoveryAction(
                    action="manual_reconcile_cleanup",
                    target=f"cleanup:{state_item.get('operation_id')}",
                    current_state=str(state_item.get("status")),
                    operation="inspect intent, quarantine and filesystem only",
                    preconditions=(
                        "ownership and hash still match the persisted intent",
                        "no automatic retry or deletion",
                    ),
                    expected_result="reconciled_completed or reconciled_unverified",
                    risk="high",
                    verification="reconcile_cleanup read-only",
                    requires_approval=True,
                )
            )
    preview_id = f"recovery-{digest[:16]}"
    return RecoveryPreview(
        preview_id=preview_id,
        preview_version=PREVIEW_VERSION,
        created_at=checked.isoformat(timespec="milliseconds"),
        task_id=task.task_id,
        state_digest=digest,
        confirmed=tuple(dict.fromkeys(confirmed)),
        unknown=tuple(dict.fromkeys(unknown)),
        actions=tuple(actions),
        audit=report,
    )


def build_unavailable_preview(
    task_id: str,
    audit: dict[str, Any],
    *,
    now: str | None = None,
) -> RecoveryPreview:
    """Build a manual-only preview when task storage cannot be read."""
    checked = _now(now)
    digest = hashlib.sha256(
        _canonical(
            {
                "task_id": task_id,
                "audit": {
                    "ok": audit.get("ok"),
                    "issues": audit.get("issues"),
                },
            }
        ).encode("utf-8")
    ).hexdigest()
    action = RecoveryAction(
        action="manual_review",
        target=f"task:{task_id}",
        current_state="storage_unreadable",
        operation="inspect a temporary database copy; do not repair or retry",
        preconditions=("operator reviews storage diagnostics",),
        expected_result="confirmed diagnosis or not_recoverable",
        risk="high",
        verification="independent storage/evidence inspection",
        requires_approval=True,
    )
    return RecoveryPreview(
        preview_id=f"recovery-{digest[:16]}",
        preview_version=PREVIEW_VERSION,
        created_at=checked.isoformat(timespec="milliseconds"),
        task_id=task_id,
        state_digest=digest,
        confirmed=(f"task_id={task_id}",),
        unknown=("storage_unreadable",),
        actions=(action,),
        audit=audit,
    )


def approve_recovery_preview(
    preview: RecoveryPreview,
    *,
    approver: str,
    now: str | None = None,
) -> RecoveryApproval:
    if not approver.strip():
        raise ValueError("approver must not be empty")
    approved = _now(now)
    approval_id = "approval-" + hashlib.sha256(
        (
            f"{preview.preview_id}|{preview.state_digest}|"
            f"{approver}|{approved.isoformat()}"
        ).encode("utf-8")
    ).hexdigest()[:16]
    approval = RecoveryApproval(
        approval_id=approval_id,
        preview_id=preview.preview_id,
        preview_version=preview.preview_version,
        task_id=preview.task_id,
        state_digest=preview.state_digest,
        approver=approver,
        approved_at=approved.isoformat(timespec="milliseconds"),
        scope="plan_acknowledgement_only",
        execution_authorized=False,
        status="approved",
    )
    return replace(
        approval,
        record_digest=approval_record_digest(approval),
    )


def reject_recovery_preview(
    preview: RecoveryPreview,
    *,
    approver: str,
    reason: str,
    now: str | None = None,
) -> RecoveryApproval:
    if not approver.strip() or not reason.strip():
        raise ValueError("approver and reason must not be empty")
    decided = _now(now)
    approval_id = "approval-" + hashlib.sha256(
        (
            f"{preview.preview_id}|{preview.state_digest}|"
            f"{approver}|{decided.isoformat()}|rejected"
        ).encode("utf-8")
    ).hexdigest()[:16]
    approval = RecoveryApproval(
        approval_id=approval_id,
        preview_id=preview.preview_id,
        preview_version=preview.preview_version,
        task_id=preview.task_id,
        state_digest=preview.state_digest,
        approver=approver,
        approved_at=decided.isoformat(timespec="milliseconds"),
        scope="plan_acknowledgement_only",
        execution_authorized=False,
        status="rejected",
        revoked_reason=reason,
    )
    return replace(
        approval,
        record_digest=approval_record_digest(approval),
    )


def validate_recovery_approval(
    approval: RecoveryApproval,
    preview: RecoveryPreview,
) -> dict[str, Any]:
    if approval.execution_authorized:
        return {
            "valid": False,
            "state": "invalid",
            "reason": "approval_cannot_authorize_execution",
            "execution_authorized": False,
        }
    if approval.status != "approved":
        return {
            "valid": False,
            "state": approval.status,
            "reason": f"approval_status_{approval.status}",
            "execution_authorized": False,
        }
    if (
        approval.preview_version != preview.preview_version
        or approval.task_id != preview.task_id
    ):
        return {
            "valid": False,
            "state": "invalid",
            "reason": "preview_identity_mismatch",
            "execution_authorized": False,
        }
    if approval.state_digest != preview.state_digest:
        return {
            "valid": False,
            "state": "stale",
            "reason": "task_state_changed_after_approval",
            "execution_authorized": False,
        }
    if approval.preview_id != preview.preview_id:
        return {
            "valid": False,
            "state": "invalid",
            "reason": "preview_identity_mismatch",
            "execution_authorized": False,
        }
    return {
        "valid": True,
        "state": "valid",
        "reason": "approval matches current preview",
        "execution_authorized": False,
    }


def authorize_recovery_action(
    approval: RecoveryApproval,
    preview: RecoveryPreview,
    *,
    action: str,
    target: str,
) -> dict[str, Any]:
    """Check capability separately from approval; never execute."""
    verdict = validate_recovery_approval(approval, preview)
    if not verdict["valid"]:
        return {
            "authorized": False,
            "execution_supported": False,
            "reason": verdict["reason"],
        }
    if approval.scope != "plan_acknowledgement_only":
        return {
            "authorized": False,
            "execution_supported": False,
            "reason": "approval_scope_mismatch",
        }
    matching = [
        item
        for item in preview.actions
        if item.action == action and item.target == target
    ]
    if not matching:
        return {
            "authorized": False,
            "execution_supported": False,
            "reason": "action_not_in_preview",
        }
    return {
        "authorized": True,
        "execution_supported": False,
        "reason": "approval_valid_for_preview_action_only",
    }
