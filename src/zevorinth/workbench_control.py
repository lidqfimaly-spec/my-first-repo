"""Server-side action contracts for the local workbench control center."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .control_receipt import resolve_control_receipt
from .event_store import PlanRevisionConflict, TaskNotFound
from .models import (
    EventType,
    ExecutionMode,
    PlannerMode,
    PlanningClassification,
    TaskRequest,
    TaskStatus,
)
from .planner import DeterministicPlanner
from .service import TaskClaimNotAcquired, TaskService
from .state_machine import InvalidTransition
from .workbench import (
    ReadOnlyWorkbench,
    WorkbenchError,
    _lease_is_active,
    _redact,
    task_request_digest,
)


CONTROL_CONTRACT_VERSION = "task-control-v1"
ANSWER_CLARIFICATION = "answer_clarification"
REVISE_UNSTARTED_TASK = "revise_unstarted_task"
CANCEL_QUEUED_TASK = "cancel_queued_task"
START_QUEUED_TASK = "start_queued_task"
ADD_TASK_NOTE = "add_task_note"
SUPPORTED_ACTIONS = frozenset(
    {
        ANSWER_CLARIFICATION,
        REVISE_UNSTARTED_TASK,
        CANCEL_QUEUED_TASK,
        START_QUEUED_TASK,
        ADD_TASK_NOTE,
    }
)
LOGGER = logging.getLogger(__name__)
_REVISABLE_STATUSES = frozenset(
    {
        TaskStatus.QUEUED,
        TaskStatus.AWAITING_CLARIFICATION,
        TaskStatus.BLOCKED,
    }
)
_REQUEST_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class ControlActionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 409,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class TaskControlService:
    """Serialize control actions within one local server process."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self._action_lock = threading.Lock()

    def action_preview(self, task_id: str) -> dict[str, Any]:
        detail = ReadOnlyWorkbench(self.project_root).task_detail(task_id)
        task = detail["task"]
        actions: list[dict[str, Any]] = []
        unavailable_reason: str | None = None
        deterministic = (
            task["planner_mode"] == PlannerMode.DETERMINISTIC.value
        )
        lease_state = task["lease_state"]
        status = task["status"]
        if status == TaskStatus.AWAITING_CLARIFICATION.value:
            if lease_state == "active":
                unavailable_reason = "task_claim_active"
            elif lease_state == "expired":
                unavailable_reason = "task_claim_recovery_required"
            elif not deterministic:
                unavailable_reason = "planner_mode_not_supported_by_control_center"
            elif task["operation"] != "write_text_artifact":
                unavailable_reason = "task_clarification_start_not_supported"
            elif task.get("missing_fields"):
                actions.append(
                    {
                        "action": ANSWER_CLARIFICATION,
                        "requires_confirmation": True,
                        "fields": list(task["missing_fields"]),
                    }
                )
        if (
            deterministic
            and lease_state == "none"
            and status in {item.value for item in _REVISABLE_STATUSES}
            and task["execution_mode"] == ExecutionMode.NOT_EXECUTED.value
        ):
            actions.append(
                {
                    "action": REVISE_UNSTARTED_TASK,
                    "requires_confirmation": True,
                    "current_goal": task["goal"],
                    "operation": task["operation"],
                    "request_digest": task["request_digest"],
                    "parameters_mode": "patch",
                }
            )
        if (
            deterministic
            and lease_state == "none"
            and status == TaskStatus.QUEUED.value
            and task["claim_generation"] == 0
            and task["execution_mode"] == ExecutionMode.NOT_EXECUTED.value
            and task["operation"] == "write_text_artifact"
        ):
            actions.append(
                {
                    "action": START_QUEUED_TASK,
                    "requires_confirmation": True,
                }
            )
        if (
            lease_state == "none"
            and status == TaskStatus.QUEUED.value
            and task["claim_generation"] == 0
            and task["execution_mode"] == ExecutionMode.NOT_EXECUTED.value
        ):
            actions.append(
                {
                    "action": CANCEL_QUEUED_TASK,
                    "requires_confirmation": True,
                }
            )
        if lease_state == "none":
            actions.append(
                {
                    "action": ADD_TASK_NOTE,
                    "requires_confirmation": False,
                }
            )
        if actions:
            unavailable_reason = None
        elif lease_state == "active":
            unavailable_reason = "task_claim_active"
        elif lease_state == "expired":
            unavailable_reason = "task_claim_recovery_required"
        elif status == TaskStatus.INTERRUPTED.value and deterministic:
            unavailable_reason = "task_interrupted_revision_not_supported"
        elif unavailable_reason is None:
            if not deterministic:
                unavailable_reason = "planner_mode_not_supported_by_control_center"
            elif task["execution_mode"] != ExecutionMode.NOT_EXECUTED.value:
                unavailable_reason = "task_has_execution_evidence"
            else:
                unavailable_reason = "task_status_not_actionable"
        return {
            "contract_version": CONTROL_CONTRACT_VERSION,
            "task_id": task["task_id"],
            "task_status": task["status"],
            "task_updated_at": task["updated_at"],
            "plan_version": task["plan_version"],
            "lease_state": lease_state,
            "claim_held": lease_state == "active",
            "actions": actions,
            "unavailable_reason": unavailable_reason,
            "read_only": False,
        }

    def create_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed_fields = {
            "request_id",
            "confirmed",
            "goal",
            "operation",
            "parameters",
        }
        unknown = set(payload) - allowed_fields
        if unknown:
            raise ControlActionError(
                "unknown_task_fields",
                "task creation payload contains unknown fields",
                status=400,
            )
        request_id = _required_text(payload, "request_id", maximum=36)
        if not _REQUEST_ID.fullmatch(request_id):
            raise ControlActionError(
                "invalid_action_request_id",
                "request_id must be a canonical UUIDv4",
                status=400,
            )
        if payload.get("confirmed") is not True:
            raise ControlActionError(
                "confirmation_required",
                "task creation requires explicit confirmation",
                status=403,
            )
        try:
            request = TaskRequest.from_mapping(
                {
                    "goal": payload.get("goal"),
                    "operation": payload.get("operation"),
                    "parameters": payload.get("parameters", {}),
                }
            )
        except (TypeError, ValueError) as exc:
            raise ControlActionError(
                "invalid_task_definition",
                "the task definition is invalid",
                status=400,
            ) from exc
        _validate_finite_json(request.to_dict())
        if not request.goal or not request.goal.strip() or "\x00" in request.goal:
            raise ControlActionError(
                "invalid_task_goal",
                "task goal must be a non-empty bounded string",
                status=400,
            )
        if not request.operation:
            raise ControlActionError(
                "invalid_task_operation",
                "task operation must be provided",
                status=400,
            )
        planner = DeterministicPlanner()
        outcome = planner.plan_request(request)
        if outcome.classification not in {
            PlanningClassification.PLANNED,
            PlanningClassification.CLARIFICATION,
        }:
            raise ControlActionError(
                outcome.error_code or "invalid_task_definition",
                outcome.error_message or "the task definition is not supported",
                status=400,
            )
        request_digest = task_request_digest(request)
        creation_preview = {
            "classification": outcome.classification.value,
            "missing_fields": list(outcome.missing_fields),
            "questions": list(outcome.clarification_questions),
            "plan_steps": [
                {
                    "step_id": step.step_id,
                    "tool_name": step.tool_name,
                    "expected_result": step.expected_result,
                }
                for step in (outcome.plan.steps if outcome.plan else [])
            ],
        }
        with self._action_lock:
            service = TaskService(
                self.project_root,
                planner=planner,
                recover_leases=False,
            )
            existing = service.store.get_task(request_id)
            if existing is not None:
                created = next(
                    (
                        event
                        for event in service.store.list_events(request_id)
                        if event.event_type == EventType.TASK_CREATED.value
                    ),
                    None,
                )
                existing_digest = (
                    created.payload.get("request_digest")
                    if created is not None
                    else None
                )
                if existing_digest != request_digest:
                    raise ControlActionError(
                        "task_creation_conflict",
                        "request_id belongs to a different task definition",
                    )
                return {
                    "contract_version": CONTROL_CONTRACT_VERSION,
                    "status": "already_exists",
                    "request_id": request_id,
                    "task_id": existing.task_id,
                    "task_status": existing.status.value,
                    "plan_version": existing.plan_version,
                }
            created_now = False
            try:
                task = service.enqueue(
                    request,
                    task_id=request_id,
                    creation_preview=creation_preview,
                )
                created_now = True
            except Exception as exc:
                LOGGER.error(
                    "task creation failed task_id=%s exception_type=%s",
                    request_id,
                    type(exc).__name__,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                existing = service.store.get_task(request_id)
                if existing is None:
                    raise ControlActionError(
                        "task_creation_failed",
                        "the task could not be created",
                        status=500,
                    ) from exc
                created = next(
                    (
                        event
                        for event in service.store.list_events(request_id)
                        if event.event_type == EventType.TASK_CREATED.value
                    ),
                    None,
                )
                existing_digest = (
                    created.payload.get("request_digest")
                    if created is not None
                    else None
                )
                if existing_digest != request_digest:
                    raise ControlActionError(
                        "task_creation_conflict",
                        "request_id belongs to a different task definition",
                    ) from exc
                task = existing
            return {
                "contract_version": CONTROL_CONTRACT_VERSION,
                "status": "created" if created_now else "already_exists",
                "request_id": request_id,
                "task_id": task.task_id,
                "task_status": task.status.value,
                "plan_version": task.plan_version,
                "task_request_digest": request_digest,
                "preview": creation_preview,
            }

    def request_status(
        self,
        task_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        if not _REQUEST_ID.fullmatch(request_id):
            raise ControlActionError(
                "invalid_action_request_id",
                "request_id must be a canonical UUIDv4",
                status=400,
            )
        evidence = ReadOnlyWorkbench(
            self.project_root
        ).control_request_evidence(task_id, request_id)
        task = evidence["task"]
        receipt, reconciled_from = resolve_control_receipt(
            [
                (
                    event["event_type"],
                    event["payload"],
                    event["occurred_at"],
                )
                for event in evidence["control_events"]
            ],
            task_status=task.status.value,
            task_attempt_id=task.attempt_id,
            lease_active=_task_lease_is_active(task),
            revision=evidence["revision"],
            cancelled_event=evidence["cancelled_event"],
            claimed_event=evidence["claimed_event"],
            note_event=evidence["note_event"],
        )
        if (
            receipt is None
            and evidence["revision"] is None
            and evidence["cancelled_event"] is None
            and evidence["claimed_event"] is None
            and evidence["note_event"] is None
        ):
            raise ControlActionError(
                "control_request_not_found",
                "the control action request was not found for this task",
                status=404,
            )
        assert receipt is not None
        state = receipt["state"]
        current_task_error = task.error.code if task.error is not None else None
        task_error_code = (
            receipt.get("task_error_code")
            if receipt.get("has_task_error_code")
            else current_task_error
        )
        return {
            "contract_version": CONTROL_CONTRACT_VERSION,
            "task_id": task_id,
            "request_id": request_id,
            "action": receipt["action"],
            "state": state,
            "manual_review_required": state in {"unknown", "inconsistent"},
            "task_status": (
                receipt.get("task_status")
                or task.status.value
            ),
            "task_error_code": task_error_code,
            "control_error_code": receipt.get("error_code"),
            "claim_reason": receipt.get("claim_reason"),
            "last_event_at": receipt.get("occurred_at"),
            "plan_version": (
                evidence["revision"]["to_version"]
                if evidence["revision"] is not None
                and reconciled_from == "plan_revision"
                else receipt.get("plan_version")
                if isinstance(receipt.get("plan_version"), int)
                else task.plan_version
            ),
            "reconciled_from": reconciled_from,
            "read_only": True,
        }

    def _validate_action_eligibility(
        self,
        task: Any,
        *,
        action: str,
        expected_plan_version: int,
        expected_fields: list[str] | None,
        values: Mapping[str, str] | None,
        expected_request_digest: str | None,
        changes: Mapping[str, Any] | None,
        preclaimed: bool = False,
    ) -> None:
        if (
            action
            in {
                ANSWER_CLARIFICATION,
                REVISE_UNSTARTED_TASK,
                START_QUEUED_TASK,
            }
            and task.planner_mode is not PlannerMode.DETERMINISTIC
        ):
            raise ControlActionError(
                "planner_mode_not_supported",
                "control-center actions require the deterministic planner",
            )
        if not preclaimed and task.lease_token is not None:
            if _task_lease_is_active(task):
                raise ControlActionError(
                    "task_claim_not_acquired",
                    "the task is currently owned by another execution claim",
                )
            raise ControlActionError(
                "task_claim_recovery_required",
                "an expired execution claim must be recovered before "
                "control actions are available",
            )
        if task.plan_version != expected_plan_version:
            raise ControlActionError(
                "task_version_changed",
                "the task plan changed after the action preview",
            )
        if action == ANSWER_CLARIFICATION:
            assert values is not None
            if task.request.operation != "write_text_artifact":
                raise ControlActionError(
                    "task_clarification_start_not_supported",
                    "clarification execution is enabled only for artifact tasks",
                )
            if task.status is not TaskStatus.AWAITING_CLARIFICATION:
                raise ControlActionError(
                    "task_status_changed",
                    "the task is no longer awaiting clarification",
                )
            current_fields = list(task.missing_fields)
            if sorted(current_fields) != sorted(expected_fields or []):
                raise ControlActionError(
                    "task_clarification_changed",
                    "the missing fields changed after the action preview",
                )
            if set(values) != set(current_fields):
                raise ControlActionError(
                    "clarification_fields_mismatch",
                    "parameters must provide exactly the current missing fields",
                    status=400,
                )
            for field, value in values.items():
                if not isinstance(value, str) or value == "":
                    raise ControlActionError(
                        "invalid_clarification_value",
                        f"clarification value is invalid: {field}",
                        status=400,
                    )
        elif action == REVISE_UNSTARTED_TASK:
            assert changes is not None
            assert expected_request_digest is not None
            if task.status not in _REVISABLE_STATUSES:
                if task.status is TaskStatus.INTERRUPTED:
                    raise ControlActionError(
                        "task_revision_not_allowed",
                        "interrupted tasks cannot be revised safely",
                    )
                raise ControlActionError(
                    "task_status_changed",
                    "the task is no longer eligible for revision",
                )
            if (
                task.execution_mode is not ExecutionMode.NOT_EXECUTED
                or task.tool_result is not None
                or task.artifact is not None
            ):
                raise ControlActionError(
                    "task_revision_not_allowed",
                    "the task already has execution evidence",
                )
            if task_request_digest(task.request) != expected_request_digest:
                raise ControlActionError(
                    "task_definition_changed",
                    "the task definition changed after the action preview",
                )
            revised_request = task.request.merged(changes)
            if revised_request.to_dict() == task.request.to_dict():
                raise ControlActionError(
                    "no_task_changes",
                    "the revision does not change the task",
                    status=400,
                )
        elif action == ADD_TASK_NOTE:
            return
        elif action == CANCEL_QUEUED_TASK:
            if (
                task.status is not TaskStatus.QUEUED
                or (not preclaimed and task.claim_generation != 0)
                or task.execution_mode is not ExecutionMode.NOT_EXECUTED
                or task.tool_result is not None
                or task.artifact is not None
            ):
                raise ControlActionError(
                    "task_cancel_not_allowed",
                    "only an unstarted queued task can be cancelled",
                )
        else:
            if (
                task.status is not TaskStatus.QUEUED
                or (not preclaimed and task.claim_generation != 0)
                or task.execution_mode is not ExecutionMode.NOT_EXECUTED
                or task.tool_result is not None
                or task.artifact is not None
                or task.request.operation != "write_text_artifact"
            ):
                raise ControlActionError(
                    "task_start_not_allowed",
                    "only an unstarted artifact task can be started",
                )

    def execute(
        self,
        task_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        action = _required_text(payload, "action", maximum=64)
        if action not in SUPPORTED_ACTIONS:
            raise ControlActionError(
                "unsupported_control_action",
                "the requested control action is not enabled",
                status=400,
            )
        common_fields = {
            "action",
            "request_id",
            "confirmed",
            "expected_updated_at",
            "expected_plan_version",
        }
        expected_fields = None
        values = None
        expected_request_digest = None
        changes = None
        note_message = None
        if action == ANSWER_CLARIFICATION:
            allowed_fields = common_fields | {
                "expected_missing_fields",
                "parameters",
            }
        elif action == REVISE_UNSTARTED_TASK:
            allowed_fields = common_fields | {
                "expected_request_digest",
                "changes",
            }
        elif action == ADD_TASK_NOTE:
            allowed_fields = common_fields | {"message"}
        else:
            allowed_fields = common_fields
        unknown_payload_fields = set(payload) - allowed_fields
        if unknown_payload_fields:
            raise ControlActionError(
                "unknown_action_fields",
                "control action payload contains unknown fields",
                status=400,
            )
        request_id = _required_text(payload, "request_id", maximum=36)
        if not _REQUEST_ID.fullmatch(request_id):
            raise ControlActionError(
                "invalid_action_request_id",
                "request_id must be a canonical UUIDv4",
                status=400,
            )
        if (
            action != ADD_TASK_NOTE
            and payload.get("confirmed") is not True
        ):
            raise ControlActionError(
                "confirmation_required",
                "the action requires explicit confirmation",
                status=403,
            )
        expected_updated_at = _required_text(
            payload,
            "expected_updated_at",
            maximum=64,
        )
        expected_plan_version = payload.get("expected_plan_version")
        if (
            not isinstance(expected_plan_version, int)
            or expected_plan_version < 0
        ):
            raise ControlActionError(
                "invalid_expected_plan_version",
                "expected_plan_version must be a non-negative integer",
                status=400,
            )
        if action == ANSWER_CLARIFICATION:
            expected_fields = payload.get("expected_missing_fields")
            if not isinstance(expected_fields, list) or not all(
                isinstance(item, str) for item in expected_fields
            ):
                raise ControlActionError(
                    "invalid_expected_missing_fields",
                    "expected_missing_fields must be an array of field names",
                    status=400,
                )
            values = payload.get("parameters")
            if not isinstance(values, Mapping):
                raise ControlActionError(
                    "invalid_action_parameters",
                    "parameters must be an object",
                    status=400,
                )
            request_digest = _request_digest(
                action=action,
                expected_updated_at=expected_updated_at,
                expected_plan_version=expected_plan_version,
                expected_fields=expected_fields,
                values=values,
            )
        elif action == REVISE_UNSTARTED_TASK:
            expected_request_digest = _required_text(
                payload,
                "expected_request_digest",
                maximum=64,
            )
            if not re.fullmatch(r"[0-9a-f]{64}", expected_request_digest):
                raise ControlActionError(
                    "invalid_expected_request_digest",
                    "expected_request_digest must be a SHA-256 hex digest",
                    status=400,
                )
            changes = _validate_revision_changes(payload.get("changes"))
            request_digest = _revision_request_digest(
                action=action,
                expected_updated_at=expected_updated_at,
                expected_plan_version=expected_plan_version,
                expected_request_digest=expected_request_digest,
                changes=changes,
            )
        elif action == ADD_TASK_NOTE:
            note_message = _redact(
                _required_text(
                    payload,
                    "message",
                    maximum=4000,
                )
            )
            if "\x00" in note_message:
                raise ControlActionError(
                    "invalid_task_note",
                    "task note contains an invalid character",
                    status=400,
                )
            request_digest = _note_request_digest(
                action=action,
                expected_updated_at=expected_updated_at,
                expected_plan_version=expected_plan_version,
                message=note_message,
            )
        else:
            request_digest = _versioned_request_digest(
                action=action,
                expected_updated_at=expected_updated_at,
                expected_plan_version=expected_plan_version,
            )

        with self._action_lock:
            service = TaskService(
                self.project_root,
                planner=DeterministicPlanner(),
                recover_leases=False,
            )
            try:
                task = service.require_task(task_id)
            except TaskNotFound as exc:
                raise ControlActionError(
                    "task_not_found",
                    "the task does not exist",
                    status=404,
                ) from exc
            preclaim_task = task
            requested_payload: dict[str, Any] = {
                "request_id": request_id,
                "action": action,
                "expected_updated_at": expected_updated_at,
                "expected_plan_version": expected_plan_version,
                "request_digest": request_digest,
            }
            if action == ANSWER_CLARIFICATION:
                assert expected_fields is not None
                requested_payload["missing_fields"] = list(expected_fields)
            elif action == REVISE_UNSTARTED_TASK:
                assert changes is not None
                requested_payload["change_keys"] = sorted(changes)
            elif action == ADD_TASK_NOTE:
                assert note_message is not None
                requested_payload["message"] = note_message
            reservation = service.inspect_control_action(
                task_id,
                request_id=request_id,
                action=action,
                request_digest=request_digest,
            )
            if reservation is None:
                try:
                    self._validate_action_eligibility(
                        task,
                        action=action,
                        expected_plan_version=expected_plan_version,
                        expected_fields=expected_fields,
                        values=values,
                        expected_request_digest=expected_request_digest,
                        changes=changes,
                    )
                except ControlActionError as exc:
                    service.store.record_control_action_preclaim_failure(
                        task_id,
                        request_id=request_id,
                        action=action,
                        request_digest=request_digest,
                        occurred_at=_utc_now(),
                        requested_payload=requested_payload,
                        error_code=exc.code,
                        error_type=type(exc).__name__,
                        task_status=task.status.value,
                    )
                    raise
                try:
                    claim_required = action in {
                        ANSWER_CLARIFICATION,
                        START_QUEUED_TASK,
                    }
                    allowed_statuses = (
                        frozenset({TaskStatus.AWAITING_CLARIFICATION})
                        if action == ANSWER_CLARIFICATION
                        else frozenset({TaskStatus.QUEUED})
                        if action == START_QUEUED_TASK
                        else frozenset()
                    )
                    reservation = service.reserve_control_action(
                        task_id,
                        request_id=request_id,
                        action=action,
                        request_digest=request_digest,
                        expected_updated_at=expected_updated_at,
                        expected_plan_version=expected_plan_version,
                        payload=requested_payload,
                        require_claim=claim_required,
                        allowed_statuses=allowed_statuses,
                    )
                except PlanRevisionConflict as exc:
                    service.store.record_control_action_preclaim_failure(
                        task_id,
                        request_id=request_id,
                        action=action,
                        request_digest=request_digest,
                        occurred_at=_utc_now(),
                        requested_payload=requested_payload,
                        error_code="task_version_changed",
                        error_type=type(exc).__name__,
                        task_status=service.require_task(task_id).status.value,
                    )
                    raise ControlActionError(
                        "task_version_changed",
                        "the task changed after the action preview",
                    ) from exc
            if not reservation["reserved"]:
                if reservation["state"] == "conflict":
                    raise ControlActionError(
                        "action_request_conflict",
                        "request_id was already used for different content",
                    )
                if reservation["state"] == "inconsistent":
                    raise ControlActionError(
                        "action_receipt_inconsistent",
                        "persisted control evidence conflicts and requires "
                        "manual review",
                    )
                if reservation["state"] == "in_progress":
                    raise ControlActionError(
                        "action_in_progress",
                        "a prior request with this ID is still in progress",
                    )
                if reservation["state"] == "claim_not_acquired":
                    receipt = reservation.get("receipt", {})
                    reason = receipt.get("claim_reason")
                    error_code = (
                        "task_claim_recovery_required"
                        if receipt.get("error_code")
                        == "task_claim_recovery_required"
                        else "task_claim_not_acquired"
                    )
                    raise ControlActionError(
                        error_code,
                        "the task execution claim could not be acquired"
                        + (f": {reason}" if isinstance(reason, str) else ""),
                    )
                if reservation["state"] == "completed":
                    task = service.require_task(task_id)
                    return {
                        "contract_version": CONTROL_CONTRACT_VERSION,
                        "status": "already_processed",
                        "action": action,
                        "request_id": request_id,
                        "task_status": task.status.value,
                        "plan_version": (
                            reservation.get("receipt", {}).get("plan_version")
                            if isinstance(
                                reservation.get("receipt", {}).get(
                                    "plan_version"
                                ),
                                int,
                            )
                            else task.plan_version
                        ),
                        "task_error_code": (
                            task.error.code if task.error is not None else None
                        ),
                    }
                if reservation["state"] == "failed":
                    prior_error = reservation.get("receipt", {}).get(
                        "error_code"
                    )
                    raise ControlActionError(
                        "action_already_failed",
                        "a prior request with this ID was recorded as failed"
                        + (
                            f": {prior_error}"
                            if isinstance(prior_error, str)
                            else ""
                        ),
                    )
                raise ControlActionError(
                    "action_state_unknown",
                    "a prior request with this ID has no verified completion",
                )
            task = reservation["task"]
            claim_rolled_back = False
            try:
                try:
                    self._validate_action_eligibility(
                        task,
                        action=action,
                        expected_plan_version=expected_plan_version,
                        expected_fields=expected_fields,
                        values=values,
                        expected_request_digest=expected_request_digest,
                        changes=changes,
                        preclaimed=bool(reservation.get("lease_token")),
                    )
                except ControlActionError:
                    lease_token = reservation.get("lease_token")
                    if isinstance(lease_token, str):
                        service.store.rollback_control_claim(
                            task_id,
                            lease_token=lease_token,
                            previous=preclaim_task,
                            now=_utc_now(),
                            payload={
                                "reason": "preclaimed_validation_failed",
                                "request_id": request_id,
                                "action": action,
                            },
                        )
                        claim_rolled_back = True
                    raise
                if action == ANSWER_CLARIFICATION:
                    assert values is not None
                    request_patch = _clarification_patch(values)
                    updated = service.resume(
                        task_id,
                        request_patch,
                        require_claim=True,
                        expected_plan_version=expected_plan_version,
                        preclaimed_lease_token=reservation.get("lease_token"),
                    )
                elif action == REVISE_UNSTARTED_TASK:
                    assert changes is not None
                    updated = service.revise_plan(
                        task_id,
                        changes,
                        source_id=request_id,
                        expected_plan_version=expected_plan_version,
                        confirmed=True,
                        allowed_statuses=_REVISABLE_STATUSES,
                    )
                elif action == CANCEL_QUEUED_TASK:
                    updated = service.cancel(
                        task_id,
                        expected_plan_version=expected_plan_version,
                        expected_claim_generation=0,
                        metadata={
                            "request_id": request_id,
                            "source": "control_center",
                        },
                    )
                elif action == ADD_TASK_NOTE:
                    assert note_message is not None
                    updated, _ = service.store.record_event_and_update(
                        task_id,
                        EventType.TASK_NOTE_ADDED,
                        _utc_now(),
                        payload={
                            "request_id": request_id,
                            "message": note_message,
                        },
                        require_no_lease=True,
                        expected_updated_at=task.updated_at,
                        expected_plan_version=expected_plan_version,
                    )
                else:
                    updated = service.execute_task(
                        task_id,
                        require_claim=True,
                        expected_plan_version=expected_plan_version,
                        claim_metadata={
                            "control_request_id": request_id,
                            "source": "control_center",
                        },
                        preclaimed_lease_token=reservation.get("lease_token"),
                    )
            except ControlActionError as exc:
                _record_control_failure(
                    service,
                    task_id,
                    request_id=request_id,
                    action=action,
                    request_digest=request_digest,
                    error_code=exc.code,
                    error_type=type(exc).__name__,
                    claim_reason=(
                        "preclaimed_validation_failed"
                        if claim_rolled_back
                        else None
                    ),
                    task_status=service.require_task(task_id).status.value,
                )
                raise
            except TaskClaimNotAcquired as exc:
                _record_control_failure(
                    service,
                    task_id,
                    request_id=request_id,
                    action=action,
                    request_digest=request_digest,
                    error_code=exc.code,
                    error_type=type(exc).__name__,
                    claim_reason=exc.reason,
                    task_status=exc.current_status,
                )
                raise ControlActionError(
                    exc.code,
                    "another executor or a terminal state prevented this "
                    "control action",
                ) from exc
            except PlanRevisionConflict as exc:
                error_code = (
                    _cancel_conflict_code(exc)
                    if action == CANCEL_QUEUED_TASK
                    else _revision_conflict_code(exc)
                )
                _record_control_failure(
                    service,
                    task_id,
                    request_id=request_id,
                    action=action,
                    request_digest=request_digest,
                    error_code=error_code,
                    error_type=type(exc).__name__,
                    task_status=service.require_task(task_id).status.value,
                )
                raise ControlActionError(
                    error_code,
                    "the task action could not be applied safely",
                ) from exc
            except InvalidTransition as exc:
                _record_control_failure(
                    service,
                    task_id,
                    request_id=request_id,
                    action=action,
                    request_digest=request_digest,
                    error_code="task_status_changed",
                    error_type=type(exc).__name__,
                    task_status=service.require_task(task_id).status.value,
                )
                raise ControlActionError(
                    "task_status_changed",
                    "the task state does not permit this action",
                ) from exc
            except Exception as exc:
                LOGGER.error(
                    "unexpected control action failure task_id=%s "
                    "request_id=%s action=%s exception_type=%s",
                    task_id,
                    request_id,
                    action,
                    type(exc).__name__,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                _record_control_failure(
                    service,
                    task_id,
                    request_id=request_id,
                    action=action,
                    request_digest=request_digest,
                    error_code=_exception_code(exc),
                    error_type=type(exc).__name__,
                    task_status=service.require_task(task_id).status.value,
                )
                outcome_unknown = reservation.get("lease_token") is not None
                raise ControlActionError(
                    (
                        "control_action_outcome_unknown"
                        if outcome_unknown
                        else "control_action_failed"
                    ),
                    (
                        "the task action raised after claim; manual "
                        "reconciliation is required"
                        if outcome_unknown
                        else "the backend rejected the task action"
                    ),
                    status=500 if outcome_unknown else 409,
                ) from exc
            completed_at = _utc_now()
            service.store.record_event_and_update(
                task_id,
                EventType.CONTROL_ACTION_COMPLETED,
                completed_at,
                payload={
                    "request_id": request_id,
                    "action": action,
                    "task_status": updated.status.value,
                    "task_error_code": (
                        updated.error.code
                        if updated.error is not None
                        else None
                    ),
                    "plan_version": updated.plan_version,
                    "request_digest": request_digest,
                },
            )
            return {
                "contract_version": CONTROL_CONTRACT_VERSION,
                "status": "completed",
                "action": action,
                "request_id": request_id,
                "task_status": updated.status.value,
                "task_error_code": (
                    updated.error.code if updated.error is not None else None
                ),
                "plan_version": updated.plan_version,
            }


def _validate_revision_changes(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ControlActionError(
            "invalid_revision_changes",
            "changes must be an object",
            status=400,
        )
    _validate_finite_json(value)
    unknown = set(value) - {"goal", "parameters"}
    if unknown:
        raise ControlActionError(
            "unknown_revision_fields",
            "revision changes contain unsupported fields",
            status=400,
        )
    changes: dict[str, Any] = {}
    if "goal" in value:
        goal = value["goal"]
        if (
            not isinstance(goal, str)
            or not goal
            or len(goal) > 16 * 1024
            or "\x00" in goal
        ):
            raise ControlActionError(
                "invalid_revision_goal",
                "goal must be a non-empty bounded string",
                status=400,
            )
        changes["goal"] = goal
    if "parameters" in value:
        parameters = value["parameters"]
        if not isinstance(parameters, Mapping):
            raise ControlActionError(
                "invalid_revision_parameters",
                "parameters must be an object",
                status=400,
            )
        if not all(
            isinstance(key, str) and 0 < len(key) <= 64
            for key in parameters
        ):
            raise ControlActionError(
                "invalid_revision_parameters",
                "parameter names must be bounded strings",
                status=400,
            )
        changes["parameters"] = dict(parameters)
    if not changes:
        raise ControlActionError(
            "no_task_changes",
            "the revision does not change the task",
            status=400,
        )
    return changes


def _validate_finite_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ControlActionError(
            "invalid_json_number",
            "control action numbers must be finite",
            status=400,
        )
    if isinstance(value, Mapping):
        for item in value.values():
            _validate_finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_finite_json(item)


def _revision_request_digest(
    *,
    action: str,
    expected_updated_at: str,
    expected_plan_version: int,
    expected_request_digest: str,
    changes: Mapping[str, Any],
) -> str:
    payload = {
        "action": action,
        "expected_updated_at": expected_updated_at,
        "expected_plan_version": expected_plan_version,
        "expected_request_digest": expected_request_digest,
        "changes": dict(changes),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _versioned_request_digest(
    *,
    action: str,
    expected_updated_at: str,
    expected_plan_version: int,
) -> str:
    payload = {
        "action": action,
        "expected_updated_at": expected_updated_at,
        "expected_plan_version": expected_plan_version,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _note_request_digest(
    *,
    action: str,
    expected_updated_at: str,
    expected_plan_version: int,
    message: str,
) -> str:
    payload = {
        "action": action,
        "expected_updated_at": expected_updated_at,
        "expected_plan_version": expected_plan_version,
        "message": message,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_control_failure(
    service: TaskService,
    task_id: str,
    *,
    request_id: str,
    action: str,
    request_digest: str,
    error_code: str,
    error_type: str,
    claim_reason: str | None = None,
    task_status: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "request_id": request_id,
        "action": action,
        "error_code": error_code,
        "error_type": error_type,
        "request_digest": request_digest,
    }
    if claim_reason is not None:
        payload["claim_reason"] = claim_reason
    if task_status is not None:
        payload["task_status"] = task_status
    service.store.record_event_and_update(
        task_id,
        EventType.CONTROL_ACTION_FAILED,
        _utc_now(),
        payload=payload,
    )


def _revision_conflict_code(exc: PlanRevisionConflict) -> str:
    message = str(exc).lower()
    if "version" in message:
        return "task_version_changed"
    if "expired execution lease" in message:
        return "task_claim_recovery_required"
    if "active execution lease" in message:
        return "task_claim_not_acquired"
    if "interrupted" in message:
        return "task_revision_not_allowed"
    if "execution evidence" in message:
        return "task_revision_not_allowed"
    if "safe plan" in message:
        return "revision_rejected"
    return "task_revision_conflict"


def _cancel_conflict_code(exc: PlanRevisionConflict) -> str:
    message = str(exc).lower()
    if "version" in message:
        return "task_version_changed"
    if "already terminal" in message:
        return "task_status_changed"
    if "expired execution lease" in message:
        return "task_claim_recovery_required"
    if "active execution lease" in message:
        return "task_cancel_not_allowed"
    if "execution claim" in message or "execution evidence" in message:
        return "task_cancel_not_allowed"
    return "task_cancel_conflict"


def _required_text(
    payload: Mapping[str, Any],
    field: str,
    *,
    maximum: int,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ControlActionError(
            f"invalid_{field}",
            f"{field} must be a non-empty string",
            status=400,
        )
    return value


def _clarification_patch(values: Mapping[str, str]) -> dict[str, object]:
    patch: dict[str, object] = {}
    parameters: dict[str, str] = {}
    for field, value in values.items():
        if field in {"goal", "operation"}:
            patch[field] = value
        else:
            parameters[field] = value
    if parameters:
        patch["parameters"] = parameters
    return patch


def _request_digest(
    *,
    action: str,
    expected_updated_at: str,
    expected_plan_version: int,
    expected_fields: list[str],
    values: Mapping[str, str],
) -> str:
    payload = {
        "action": action,
        "expected_updated_at": expected_updated_at,
        "expected_plan_version": expected_plan_version,
        "expected_fields": expected_fields,
        "parameters": dict(values),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _exception_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return str(code) if code else type(exc).__name__


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _task_lease_is_active(task: Any) -> bool:
    return (
        task.lease_token is not None
        and _lease_is_active(task.lease_expires_at)
    )
