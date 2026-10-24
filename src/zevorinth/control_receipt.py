"""Shared classification for persisted control-action receipts."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .models import EventType, TaskStatus
from .state_machine import is_terminal


CONTROL_RECEIPT_EVENT_TYPES = frozenset(
    {
        EventType.CONTROL_ACTION_REQUESTED.value,
        EventType.CONTROL_ACTION_COMPLETED.value,
        EventType.CONTROL_ACTION_FAILED.value,
    }
)


def classify_control_receipt(
    events: Iterable[tuple[str, Mapping[str, Any], str | None]],
) -> dict[str, Any] | None:
    """Return one fail-closed state for all evidence for one request ID."""
    requested = False
    completed = False
    failed = False
    actions: set[str] = set()
    request_digests: set[str | None] = set()
    task_status: str | None = None
    task_error_code: str | None = None
    has_task_error_code = False
    plan_version: int | None = None
    error_code: str | None = None
    claim_reason: str | None = None
    occurred_at: str | None = None
    completed_signatures: set[tuple[Any, ...]] = set()
    failed_signatures: set[tuple[Any, ...]] = set()

    for event_type, payload, event_occurred_at in events:
        if event_type not in CONTROL_RECEIPT_EVENT_TYPES:
            continue
        actions.add(str(payload.get("action") or ""))
        request_digests.add(payload.get("request_digest"))
        task_status = payload.get("task_status") or task_status
        if "task_error_code" in payload:
            task_error_code = payload.get("task_error_code")
            has_task_error_code = True
        if isinstance(payload.get("plan_version"), int):
            plan_version = payload["plan_version"]
        error_code = payload.get("error_code") or error_code
        claim_reason = payload.get("claim_reason") or claim_reason
        occurred_at = event_occurred_at or occurred_at
        if event_type == EventType.CONTROL_ACTION_REQUESTED.value:
            requested = True
        elif event_type == EventType.CONTROL_ACTION_COMPLETED.value:
            completed = True
            completed_signatures.add(
                (
                    payload.get("action"),
                    payload.get("request_digest"),
                    payload.get("task_status"),
                    payload.get("task_error_code"),
                    "task_error_code" in payload,
                    payload.get("plan_version"),
                )
            )
        elif event_type == EventType.CONTROL_ACTION_FAILED.value:
            failed = True
            failed_signatures.add(
                (
                    payload.get("action"),
                    payload.get("request_digest"),
                    payload.get("task_status"),
                    payload.get("task_error_code"),
                    "task_error_code" in payload,
                    payload.get("plan_version"),
                    payload.get("error_code"),
                )
            )

    if not (requested or completed or failed):
        return None

    action = next(iter(actions)) if len(actions) == 1 else None
    request_digest = (
        next(iter(request_digests)) if len(request_digests) == 1 else None
    )
    inconsistent = (
        len(actions) != 1
        or len(request_digests) != 1
        or None in request_digests
        or (completed and failed)
        or ((completed or failed) and not requested)
        or len(completed_signatures) > 1
        or len(failed_signatures) > 1
    )
    if inconsistent:
        return {
            "state": "inconsistent",
            "action": action,
            "request_digest": request_digest,
            "task_status": task_status,
            "task_error_code": task_error_code,
            "has_task_error_code": has_task_error_code,
            "plan_version": plan_version,
            "error_code": "action_receipt_inconsistent",
            "claim_reason": claim_reason,
            "occurred_at": occurred_at,
        }
    if completed:
        state = "completed"
    elif failed:
        state = "failed"
    else:
        state = "unknown"
    return {
        "state": state,
        "action": action,
        "request_digest": request_digest,
        "task_status": task_status,
        "task_error_code": task_error_code,
        "has_task_error_code": has_task_error_code,
        "plan_version": plan_version,
        "error_code": error_code,
        "claim_reason": claim_reason,
        "occurred_at": occurred_at,
    }


def resolve_control_receipt(
    events: Iterable[tuple[str, Mapping[str, Any], str | None]],
    *,
    task_status: str,
    task_attempt_id: str | None,
    lease_active: bool = False,
    revision: Mapping[str, Any] | None = None,
    cancelled_event: Mapping[str, Any] | None = None,
    claimed_event: Mapping[str, Any] | None = None,
    note_event: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve terminal evidence once for both query and replay paths."""
    receipt = classify_control_receipt(events)
    has_side_evidence = any(
        item is not None
        for item in (revision, cancelled_event, claimed_event, note_event)
    )
    if receipt is not None and receipt["state"] == "failed":
        expected_claim_failure = (
            receipt.get("claim_reason") == "preclaimed_validation_failed"
            and claimed_event is not None
            and revision is None
            and cancelled_event is None
            and note_event is None
        )
        if has_side_evidence and not expected_claim_failure:
            return {
                "state": "inconsistent",
                "action": receipt.get("action"),
                "error_code": "action_receipt_inconsistent",
                "task_status": task_status,
            }, None
        return receipt, None
    if receipt is not None and receipt["state"] != "unknown":
        action = receipt.get("action")
        evidence_matches_action = (
            (revision is not None and action == "revise_unstarted_task")
            or (
                cancelled_event is not None
                and action == "cancel_queued_task"
            )
            or (note_event is not None and action == "add_task_note")
            or (
                claimed_event is not None
                and action in {"answer_clarification", "start_queued_task"}
                and claimed_event.get("payload", {}).get("control_action")
                == action
                and claimed_event.get("payload", {}).get("attempt_id")
                == task_attempt_id
            )
        )
        if has_side_evidence and not evidence_matches_action:
            return {
                "state": "inconsistent",
                "action": action,
                "error_code": "action_receipt_inconsistent",
                "task_status": task_status,
            }, None
        return receipt, None

    requested_action = receipt.get("action") if receipt is not None else None
    if receipt is None:
        if has_side_evidence:
            return {
                "state": "inconsistent",
                "action": None,
                "error_code": "action_receipt_inconsistent",
                "task_status": task_status,
            }, None
        return None, None

    try:
        task_is_terminal = is_terminal(TaskStatus(task_status))
    except ValueError:
        return {
            "state": "inconsistent",
            "action": requested_action,
            "error_code": "action_receipt_inconsistent",
            "task_status": task_status,
        }, None

    candidates: list[tuple[dict[str, Any], str | None]] = []
    if claimed_event is not None:
        claimed_payload = claimed_event.get("payload", {})
        claimed_attempt = claimed_payload.get("attempt_id")
        occurred_at = claimed_event.get("occurred_at")
        claimed_action = claimed_payload.get("control_action")
        if (
            claimed_action == requested_action
            and claimed_attempt == task_attempt_id
            and lease_active
        ):
            candidates.append(
                (
                    {
                        "state": "in_progress",
                        "action": requested_action,
                        "task_status": task_status,
                        "occurred_at": occurred_at,
                    },
                    "task_claimed_event",
                )
            )
        elif (
            claimed_action == "start_queued_task"
            and task_is_terminal
            and claimed_attempt == task_attempt_id
        ):
            candidates.append(
                (
                    {
                        "state": "completed",
                        "action": "start_queued_task",
                        "task_status": task_status,
                        "occurred_at": occurred_at,
                    },
                    "task_claimed_terminal",
                )
            )
        elif claimed_action == "start_queued_task" and task_is_terminal:
            candidates.append(
                (
                    {
                        "state": "unknown",
                        "action": "start_queued_task",
                        "task_status": task_status,
                        "occurred_at": occurred_at,
                    },
                    None,
                )
            )
        elif claimed_action == "start_queued_task":
            candidates.append(
                (
                    {
                        "state": "unknown",
                        "action": "start_queued_task",
                        "task_status": task_status,
                        "occurred_at": occurred_at,
                    },
                    None,
                )
            )
    if cancelled_event is not None:
        candidates.append(
            (
                {
                    "state": "completed",
                    "action": "cancel_queued_task",
                    "task_status": task_status,
                    "occurred_at": cancelled_event.get("occurred_at"),
                },
                "task_cancelled_event",
            )
        )
    if revision is not None:
        candidates.append(
            (
                {
                    "state": "completed",
                    "action": "revise_unstarted_task",
                    "task_status": task_status,
                    "occurred_at": revision.get("changed_at"),
                },
                "plan_revision",
            )
        )
    if note_event is not None:
        candidates.append(
            (
                {
                    "state": "completed",
                    "action": "add_task_note",
                    "task_status": task_status,
                    "occurred_at": note_event.get("occurred_at"),
                },
                "task_note_event",
            )
        )
    if len(candidates) > 1:
        return {
            "state": "inconsistent",
            "action": None,
            "error_code": "action_receipt_inconsistent",
            "task_status": task_status,
        }, None
    if not candidates:
        return receipt, None
    candidate, reconciled_from = candidates[0]
    if (
        requested_action is not None
        and requested_action != candidate["action"]
    ):
        return {
            "state": "inconsistent",
            "action": None,
            "error_code": "action_receipt_inconsistent",
            "task_status": task_status,
        }, None
    return candidate, reconciled_from
