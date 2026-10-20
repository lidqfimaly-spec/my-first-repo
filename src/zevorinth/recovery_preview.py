"""Read-only recovery preview and approval binding."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import Event, Task, TaskStatus
from .task_lifecycle import audit_task_lifecycle


PREVIEW_VERSION = "recovery-preview-v1"


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
        }


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
    return RecoveryApproval(
        approval_id=approval_id,
        preview_id=preview.preview_id,
        preview_version=preview.preview_version,
        task_id=preview.task_id,
        state_digest=preview.state_digest,
        approver=approver,
        approved_at=approved.isoformat(timespec="milliseconds"),
        scope="plan_acknowledgement_only",
        execution_authorized=False,
    )


def validate_recovery_approval(
    approval: RecoveryApproval,
    preview: RecoveryPreview,
) -> dict[str, Any]:
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
