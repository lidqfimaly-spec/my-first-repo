"""Read-only consistency audit for tasks, verification and cleanup records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .models import Event, Task, TaskStatus


CLEANUP_INTENT_TTL_SECONDS = 24 * 60 * 60
RUN_TESTS = "run_tests"
AUDITOR_VERSION = "lifecycle-audit-v1"


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _cleanup_operation_states(
    events: Iterable[Event],
    *,
    now: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    current = _parse_time(now) if now else datetime.now(timezone.utc)
    if current is None:
        current = datetime.now(timezone.utc)
    intents: dict[str, Event] = {}
    terminals: dict[str, list[Event]] = {}
    issues: list[dict[str, str]] = []
    for event in events:
        operation_id = event.payload.get("operation_id")
        if not isinstance(operation_id, str):
            continue
        if event.event_type == "cleanup_intent":
            if operation_id in intents:
                issues.append(
                    {
                        "code": "duplicate_cleanup_intent",
                        "detail": operation_id,
                    }
                )
            intents[operation_id] = event
        elif event.event_type in {
            "cleanup_completed",
            "cleanup_unverified",
            "cleanup_rejected",
        }:
            terminals.setdefault(operation_id, []).append(event)
    states: list[dict[str, Any]] = []
    operation_ids = sorted(set(intents) | set(terminals))
    for operation_id in operation_ids:
        intent = intents.get(operation_id)
        terminal_list = terminals.get(operation_id, [])
        terminal = terminal_list[-1] if terminal_list else None
        if len(terminal_list) > 1:
            issues.append(
                {
                    "code": "multiple_cleanup_terminals",
                    "detail": operation_id,
                }
            )
        if terminal is not None and intent is None:
            if terminal.event_type in {
                "cleanup_completed",
                "cleanup_unverified",
            }:
                issues.append(
                    {
                        "code": "cleanup_terminal_without_intent",
                        "detail": operation_id,
                    }
                )
        status = "pending"
        code = None
        retryable = True
        manual = False
        expired = False
        if terminal is not None:
            status = {
                "cleanup_completed": "completed",
                "cleanup_unverified": "unverified",
                "cleanup_rejected": "rejected",
            }[terminal.event_type]
            code = terminal.payload.get("code")
            if status == "completed" or code == "already_cleaned":
                status = "completed" if code == "already_cleaned" else status
                retryable = False
            elif status == "rejected":
                retryable = code in {
                    "cleanup_disabled",
                    "cleanup_unauthorized",
                    "active_lease",
                }
                manual = not retryable
            elif code in {"intent_not_persisted", "quarantine_failed"}:
                retryable = True
            else:
                retryable = False
                manual = True
        else:
            anchor = intent if intent is not None else None
            occurred = _parse_time(anchor.occurred_at) if anchor else None
            if anchor is not None and occurred is None:
                issues.append(
                    {
                        "code": "invalid_cleanup_timestamp",
                        "detail": operation_id,
                    }
                )
                status = "unverified"
                retryable = False
                manual = True
            elif occurred is not None and (
                current - occurred
            ).total_seconds() > CLEANUP_INTENT_TTL_SECONDS:
                status = "expired"
                retryable = False
                manual = True
                expired = True
        states.append(
            {
                "operation_id": operation_id,
                "relative_path": (
                    (intent.payload.get("relative_path") if intent else None)
                    or (terminal.payload.get("relative_path") if terminal else None)
                ),
                "source": (
                    (intent.payload.get("source") if intent else None)
                    or (terminal.payload.get("source") if terminal else None)
                ),
                "status": status,
                "code": code,
                "retryable": retryable,
                "manual_intervention": manual,
                "expired": expired,
                "intent_persisted": intent is not None,
            }
        )
    return states, issues


def audit_task_lifecycle(
    task: Task,
    events: Iterable[Event],
    *,
    now: str | None = None,
    evidence_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute lifecycle consistency without mutating task or events."""
    event_list = list(events)
    event_types = [event.event_type for event in event_list]
    checked = _parse_time(now) if now else datetime.now(timezone.utc)
    if checked is None:
        checked = datetime.now(timezone.utc)
    issues: list[dict[str, str]] = []

    def issue(code: str, detail: str) -> None:
        issues.append({"code": code, "detail": detail})

    tool = (
        task.plan.steps[0].tool_name
        if task.plan is not None and task.plan.steps
        else task.request.operation
    )
    is_test = tool == RUN_TESTS
    evidence = (
        task.tool_result.test_evidence
        if task.tool_result is not None
        else None
    )
    snapshot = task.execution_plan_snapshot
    for terminal in ("task_succeeded", "task_failed", "task_cancelled"):
        if event_types.count(terminal) > 1:
            issue("duplicate_terminal_event", terminal)

    if task.status is TaskStatus.SUCCEEDED:
        if task.verification is None or not task.verification.passed:
            issue("succeeded_without_passed_verification", task.task_id)
        if task.tool_result is None or not task.tool_result.success:
            issue("succeeded_without_successful_tool", task.task_id)
        if task.error is not None:
            issue("succeeded_with_error", task.task_id)
        if "task_succeeded" not in event_types:
            issue("succeeded_without_task_event", task.task_id)
        if "task_failed" in event_types or "task_cancelled" in event_types:
            issue("succeeded_with_terminal_failure_event", task.task_id)
    elif task.status is TaskStatus.FAILED:
        if task.error is None:
            issue("failed_without_error", task.task_id)
        if "task_failed" not in event_types:
            issue("failed_without_task_event", task.task_id)
        if "task_succeeded" in event_types:
            issue("failed_with_success_event", task.task_id)
    elif task.status is TaskStatus.CANCELLED:
        if "task_cancelled" not in event_types:
            issue("cancelled_without_task_event", task.task_id)
        if "task_succeeded" in event_types:
            issue("cancelled_with_success_event", task.task_id)

    if "task_succeeded" in event_types and task.status is not TaskStatus.SUCCEEDED:
        issue("success_event_without_success_state", task.task_id)
    if "task_failed" in event_types and task.status is not TaskStatus.FAILED:
        issue("failure_event_without_failure_state", task.task_id)
    if "task_cancelled" in event_types and task.status is not TaskStatus.CANCELLED:
        issue("cancel_event_without_cancel_state", task.task_id)

    if is_test and snapshot is None:
        issue("missing_final_execution_plan", task.task_id)
    if snapshot is not None:
        if task.execution_plan_version != task.plan_version:
            issue("execution_plan_version_mismatch", task.task_id)
        if snapshot.get("kind") == RUN_TESTS and evidence is not None:
            expected = {
                "task_id": task.task_id,
                "attempt_id": evidence.attempt_id,
                "claim_generation": evidence.claim_generation,
                "evidence_id": evidence.evidence_id,
                "config_digest": evidence.config_digest,
                "command_digest": evidence.command_digest,
                "runner": evidence.runner,
                "target": evidence.target,
                "workspace": evidence.workspace,
            }
            for field, value in expected.items():
                if snapshot.get(field) != value:
                    issue(
                        f"execution_plan_{field}_mismatch",
                        task.task_id,
                    )
    if task.status is TaskStatus.SUCCEEDED and is_test:
        if evidence is None:
            issue("succeeded_without_test_evidence", task.task_id)
        else:
            if evidence.cancelled or evidence.timed_out:
                issue("succeeded_with_non_success_evidence", task.task_id)
            if evidence.exit_code != 0:
                issue("succeeded_with_nonzero_test_exit", task.task_id)
            if task.verification is None or task.verification.reason != "tests_passed":
                issue("succeeded_without_tests_passed_reason", task.task_id)
            if snapshot is None or snapshot.get("kind") != RUN_TESTS:
                issue("succeeded_without_final_test_plan", task.task_id)
        if (
            isinstance(evidence_state, Mapping)
            and evidence_state.get("state") != "verified"
        ):
            issue("succeeded_with_unverified_evidence", task.task_id)

    cleanup_states, cleanup_issues = _cleanup_operation_states(
        event_list, now=now
    )
    issues.extend(cleanup_issues)
    manual = bool(issues)
    if task.status in {
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
        TaskStatus.INTERRUPTED,
        TaskStatus.BLOCKED,
        TaskStatus.AWAITING_CLARIFICATION,
    }:
        manual = True
    if task.error is not None and task.error.code in {
        "test_execution_unknown",
        "container_termination_unverified",
        "isolation_unverified",
    }:
        manual = True
    return {
        "task_id": task.task_id,
        "auditor_version": AUDITOR_VERSION,
        "checked_at": checked.isoformat(timespec="milliseconds"),
        "scope": "consistency_only",
        "security_certification": False,
        "status": task.status.value,
        "phase": task.phase,
        "tool": tool,
        "ok": not issues,
        "issues": issues,
        "evidence_state": (
            dict(evidence_state) if evidence_state is not None else None
        ),
        "task_recovery": {
            "safe_to_retry": False,
            "manual_intervention": manual,
            "recommendation": "manual_review" if manual else "none",
        },
        "cleanup_operations": cleanup_states,
        "event_types": event_types,
    }
