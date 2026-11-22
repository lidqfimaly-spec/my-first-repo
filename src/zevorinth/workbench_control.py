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

from .artifact_tool import ArtifactToolError, validate_artifact_name
from .capabilities import SUPPORTED_TASK_OPERATIONS
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
from .workspace_edit import EDIT_AUTHORIZATION_TOKEN


CONTROL_CONTRACT_VERSION = "task-control-v1"
NATURAL_GOAL_PREVIEW_VERSION = "natural-goal-preview-v1"
ANSWER_CLARIFICATION = "answer_clarification"
REVISE_UNSTARTED_TASK = "revise_unstarted_task"
CANCEL_QUEUED_TASK = "cancel_queued_task"
START_QUEUED_TASK = "start_queued_task"
ADD_TASK_NOTE = "add_task_note"
PAUSE_TASK = "pause_task"
RESUME_TASK = "resume_task"
APPROVE_STEP = "approve_step"
REJECT_STEP = "reject_step"
REPLAN_PENDING_STEPS = "replan_pending_steps"
CONTROL_STARTABLE_OPERATIONS = frozenset(
    set(SUPPORTED_TASK_OPERATIONS) - {"run_tests"}
)
SUPPORTED_ACTIONS = frozenset(
    {
        ANSWER_CLARIFICATION,
        REVISE_UNSTARTED_TASK,
        CANCEL_QUEUED_TASK,
        START_QUEUED_TASK,
        ADD_TASK_NOTE,
        PAUSE_TASK,
        RESUME_TASK,
        APPROVE_STEP,
        REJECT_STEP,
        REPLAN_PENDING_STEPS,
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
_CREATE_INTENT = re.compile(
    r"(?:创建|新建|生成|写入|写下|保存|写一个|写一份|做一个|"
    r"create|write|save|generate)",
    re.IGNORECASE,
)
_CODE_INTENT = re.compile(
    r"(?:代码|函数|变量|类|补丁|仓库|工作区|workspace|"
    r"patch|function|class|variable|源码|\.py\b)",
    re.IGNORECASE,
)
_RESEARCH_INTENT = re.compile(
    r"(?:研究|调研|资料|引用|文献|取证|"
    r"research|citation|literature)",
    re.IGNORECASE,
)
_COMPLEX_INTENT = re.compile(
    r"(?:多步|多步骤|依赖|工作流|先.+再|然后|之后|"
    r"multi[- ]?step|workflow|depends?\s+on|first.+then)",
    re.IGNORECASE | re.DOTALL,
)
_TEST_INTENT = re.compile(
    r"(?:运行|执行|跑).{0,8}(?:测试|test)|"
    r"(?:run|execute).{0,12}(?:test)",
    re.IGNORECASE,
)
_CONTINUE_INTENT = re.compile(
    r"(?:继续|修改|更新|追加|添加|补充|替换|另存|新版本|"
    r"末尾|刚才.*文件|这份文件|这份报告|保留原文件|"
    r"continue|modify|append|replace|update|save as|new version)",
    re.IGNORECASE,
)
_APPEND_INTENT = re.compile(
    r"(?:追加|添加|补充|末尾|再加|append|add to|at the end)",
    re.IGNORECASE,
)
_REPLACE_INTENT = re.compile(
    r"(?:替换|覆盖|改成|改为|替换成|replace|overwrite)",
    re.IGNORECASE,
)
_ARTIFACT_EXTENSIONS = (
    "txt",
    "md",
    "markdown",
    "json",
    "csv",
    "log",
    "yaml",
    "yml",
    "html",
)
_EXTENSION_PATTERN = "|".join(_ARTIFACT_EXTENSIONS)
_SAVE_AS_PATH = re.compile(
    rf"(?:另存为|另存到|新文件名为|save as)\s*"
    rf"[`'\"“”]?(?P<path>[^`'\"“”\s\\/]+\.(?:{_EXTENSION_PATTERN}))"
    rf"[`'\"“”]?",
    re.IGNORECASE,
)
_QUOTED_ARTIFACT_PATH = re.compile(
    rf"[`'\"“”](?P<path>[^`'\"“”\r\n]{{1,255}}\.(?:{_EXTENSION_PATTERN}))"
    rf"[`'\"“”]",
    re.IGNORECASE,
)
_BARE_ARTIFACT_PATH = re.compile(
    rf"(?<![\w/.-])(?P<path>[\w\u4e00-\u9fff][\w\u4e00-\u9fff.-]{{0,200}}\."
    rf"(?:{_EXTENSION_PATTERN}))(?![\w.-])",
    re.IGNORECASE,
)
_FENCED_CONTENT = re.compile(
    r"```(?:text|markdown|md|json)?[ \t]*\r?\n"
    r"(?P<content>.*?)\r?\n```",
    re.IGNORECASE | re.DOTALL,
)
_QUOTED_CONTENT = re.compile(
    r"(?:内容|正文|content)\s*(?:(?:写|为|是|使用)\s*)?"
    r"(?:[:：=]\s*)?"
    r"[`'\"“](?P<content>.*?)[`'\"”]",
    re.IGNORECASE | re.DOTALL,
)
_INLINE_CONTENT = re.compile(
    r"(?:内容|正文|content)\s*(?:(?:写|为|是|使用)\s*)?"
    r"(?:[:：=]\s*)?"
    r"(?P<content>.+)\Z",
    re.IGNORECASE | re.DOTALL,
)
_CONTINUATION_QUOTED_CONTENT = re.compile(
    r"(?:追加|添加|补充|再写|写入|替换为|替换成|改成|改为|"
    r"内容写(?:成|为)?)"
    r"[^:：`'\"“”\r\n]{0,40}?[：:]?\s*"
    r"[`'\"“](?P<content>.*?)[`'\"”]",
    re.IGNORECASE,
)
_CONTINUATION_INLINE_CONTENT = re.compile(
    r"(?:追加|添加|补充|再写|写入|替换为|替换成|改成|改为|"
    r"内容写(?:成|为)?)"
    r"[^:：`'\"“”\r\n]{0,40}?[：:]\s*"
    r"(?P<content>[^\r\n]+?)"
    r"(?=(?:[，,]\s*(?:并\s*)?(?:另存|保存|新文件名为|save as))|\s*$)",
    re.IGNORECASE,
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
        is_multi_step = task["operation"] == "execute_plan"
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
            and task["operation"] in CONTROL_STARTABLE_OPERATIONS
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
        if is_multi_step and status in {
            TaskStatus.QUEUED.value,
            TaskStatus.RUNNING.value,
            TaskStatus.AWAITING_APPROVAL.value,
        } and task.get("pause_requested_at") is None and (
            (
                status == TaskStatus.RUNNING.value
                and lease_state == "active"
            )
            or (
                status
                in {
                    TaskStatus.QUEUED.value,
                    TaskStatus.AWAITING_APPROVAL.value,
                }
                and lease_state == "none"
            )
        ):
            actions.append(
                {
                    "action": PAUSE_TASK,
                    "requires_confirmation": True,
                    "pause_request_only": status == TaskStatus.RUNNING.value,
                }
            )
        if (
            is_multi_step
            and
            status == TaskStatus.PAUSED.value
            and lease_state == "none"
        ):
            actions.append(
                {
                    "action": RESUME_TASK,
                    "requires_confirmation": True,
                }
            )
        if (
            is_multi_step
            and
            status == TaskStatus.AWAITING_APPROVAL.value
            and lease_state == "none"
            and task.get("pending_approval_step_id")
        ):
            # F-11: approving a step is a decision about content, so the
            # preview carries the tool, the full parameters, what the step
            # touches, and whether this exact content was approved before.
            # A checkbox alone would be a consent ritual, not consent.
            form = ReadOnlyWorkbench(self.project_root).approval_form(task_id)
            actions.append(
                {
                    "action": APPROVE_STEP,
                    "requires_confirmation": True,
                    "step_id": task["pending_approval_step_id"],
                    "approval_form": form,
                }
            )
            actions.append(
                {
                    "action": REJECT_STEP,
                    "requires_confirmation": True,
                    "step_id": task["pending_approval_step_id"],
                    "approval_form": form,
                }
            )
        if (
            is_multi_step
            and
            status
            in {
                TaskStatus.PAUSED.value,
                TaskStatus.AWAITING_APPROVAL.value,
            }
            and lease_state == "none"
            and task.get("plan")
        ):
            actions.append(
                {
                    "action": REPLAN_PENDING_STEPS,
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

    def preview_goal(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Interpret a bounded natural-language goal without mutating state."""

        unknown = set(payload) - {"goal", "selected_task_id"}
        if unknown:
            raise ControlActionError(
                "unknown_goal_preview_fields",
                "goal preview payload contains unknown fields",
                status=400,
            )
        goal = _required_text(payload, "goal", maximum=16 * 1024)
        if not goal.strip() or "\x00" in goal:
            raise ControlActionError(
                "invalid_task_goal",
                "task goal must be a non-empty bounded string",
                status=400,
            )
        goal = goal.strip()
        selected_task_id = payload.get("selected_task_id")
        if selected_task_id is not None:
            if (
                not isinstance(selected_task_id, str)
                or not _REQUEST_ID.fullmatch(selected_task_id)
            ):
                raise ControlActionError(
                    "invalid_selected_task_id",
                    "selected_task_id must be a canonical UUIDv4",
                    status=400,
                )
        continuation_context: dict[str, Any] | None = None
        if selected_task_id and _CONTINUE_INTENT.search(goal):
            continuation_context = self._continuation_context(selected_task_id)
        interpretation = _interpret_natural_goal(
            goal,
            continuation_context=continuation_context,
        )
        request = TaskRequest.from_mapping(
            {
                "goal": goal,
                "operation": interpretation.get("operation"),
                "parameters": interpretation.get("parameters", {}),
            }
        )
        plan_steps: list[dict[str, Any]] = []
        plan_classification: str | None = None
        missing_fields = list(interpretation.get("missing_fields", []))
        if request.operation in {"write_text_artifact", "modify_text_artifact"}:
            outcome = DeterministicPlanner().plan_request(request)
            plan_classification = outcome.classification.value
            planner_missing = list(outcome.missing_fields)
            if interpretation.get("requires_task_selection"):
                planner_missing = [
                    "selected_task_id"
                    if item in {"source_task_id", "expected_sha256"}
                    else item
                    for item in planner_missing
                ]
                planner_missing = list(dict.fromkeys(planner_missing))
            missing_fields = list(
                dict.fromkeys([*missing_fields, *planner_missing])
            )
            if outcome.plan is not None:
                plan_steps = [
                    {
                        "step_id": step.step_id,
                        "tool_name": step.tool_name,
                        "expected_result": step.expected_result,
                    }
                    for step in outcome.plan.steps
                ]
        confirmable = bool(
            interpretation.get("supported")
            and request.operation
            in {"write_text_artifact", "modify_text_artifact"}
            and not missing_fields
        )
        if not interpretation.get("supported"):
            state = "unsupported"
        elif missing_fields:
            state = "needs_clarification"
        elif confirmable:
            state = "ready"
        else:
            state = "unsupported"
        return {
            "contract_version": CONTROL_CONTRACT_VERSION,
            "preview_version": NATURAL_GOAL_PREVIEW_VERSION,
            "state": state,
            "goal": goal,
            "supported": bool(interpretation.get("supported")),
            "guided_flow": interpretation.get("guided_flow"),
            "continuation_context": continuation_context is not None,
            "operation": request.operation,
            "operation_label": interpretation.get("operation_label"),
            "parameters": dict(request.parameters),
            "required_fields": list(interpretation.get("required_fields", [])),
            "missing_fields": missing_fields,
            "assumptions": list(interpretation.get("assumptions", [])),
            "plan_classification": plan_classification,
            "plan_steps": plan_steps,
            "limits": list(interpretation.get("limits", [])),
            "reason_code": interpretation.get("reason_code"),
            "reason": interpretation.get("reason"),
            "confirmation_required": confirmable,
            "execution_started": False,
            "mutation_started": False,
        }

    def _continuation_context(self, task_id: str) -> dict[str, Any]:
        """Resolve trusted continuation metadata for a verified artifact."""
        try:
            detail = ReadOnlyWorkbench(self.project_root).task_detail(task_id)
        except WorkbenchError as exc:
            raise ControlActionError(
                exc.code,
                exc.message,
                status=exc.status,
            ) from exc
        task = detail["task"]
        observation = task.get("artifact_observation")
        artifact = task.get("artifact")
        verification = task.get("verification")
        if (
            task.get("status") != TaskStatus.SUCCEEDED.value
            or not isinstance(artifact, Mapping)
            or not isinstance(verification, Mapping)
            or verification.get("passed") is not True
            or not isinstance(observation, Mapping)
            or observation.get("state") != "current"
        ):
            raise ControlActionError(
                "continuation_source_not_verified",
                "the selected task has no current, independently verified artifact",
            )
        return {
            "source_task_id": task_id,
            "artifact_path": str(artifact["relative_path"]),
            "expected_sha256": str(artifact["sha256"]),
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
        if request.operation == "edit_text_file":
            parameters = dict(request.parameters)
            parameters["authorization"] = EDIT_AUTHORIZATION_TOKEN
            request = TaskRequest(
                goal=request.goal,
                operation=request.operation,
                parameters=parameters,
            )
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
        step_id: str | None = None,
        replacement_steps: list[Mapping[str, Any]] | None = None,
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
        permit_running_pause = (
            action == PAUSE_TASK
            and task.status
            in {
                TaskStatus.RUNNING,
                TaskStatus.AWAITING_APPROVAL,
            }
        )
        if (
            not preclaimed
            and task.lease_token is not None
            and not permit_running_pause
        ):
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
        elif action == PAUSE_TASK:
            if task.status not in {
                TaskStatus.QUEUED,
                TaskStatus.RUNNING,
                TaskStatus.AWAITING_APPROVAL,
            }:
                raise ControlActionError(
                    "task_pause_not_allowed",
                    "the task state does not permit a pause request",
                )
        elif action == RESUME_TASK:
            if task.status is not TaskStatus.PAUSED:
                raise ControlActionError(
                    "task_resume_not_allowed",
                    "only a paused task can resume",
                )
        elif action in {APPROVE_STEP, REJECT_STEP}:
            if (
                task.status is not TaskStatus.AWAITING_APPROVAL
                or not step_id
                or task.pending_approval_step_id != step_id
            ):
                raise ControlActionError(
                    "approval_step_changed",
                    "the pending approval step changed or is not eligible",
                )
        elif action == REPLAN_PENDING_STEPS:
            if (
                task.status
                not in {
                    TaskStatus.PAUSED,
                    TaskStatus.AWAITING_APPROVAL,
                }
                or task.plan is None
                or not replacement_steps
            ):
                raise ControlActionError(
                    "task_replan_not_allowed",
                    "only a paused or approval-waiting task can be replanned",
                )
        elif action == START_QUEUED_TASK:
            if (
                task.status is not TaskStatus.QUEUED
                or (not preclaimed and task.claim_generation != 0)
                or task.execution_mode is not ExecutionMode.NOT_EXECUTED
                or task.tool_result is not None
                or task.artifact is not None
                or task.request.operation not in CONTROL_STARTABLE_OPERATIONS
            ):
                raise ControlActionError(
                    "task_start_not_allowed",
                    "only an unstarted supported deterministic task can be started",
                )
        else:
            raise ControlActionError(
                "unsupported_control_action",
                "the requested control action is not enabled",
                status=400,
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
        step_id = None
        replacement_steps = None
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
        elif action in {APPROVE_STEP, REJECT_STEP}:
            allowed_fields = common_fields | {"step_id"}
        elif action == REPLAN_PENDING_STEPS:
            allowed_fields = common_fields | {"steps"}
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
        elif action in {APPROVE_STEP, REJECT_STEP}:
            step_id = _required_text(payload, "step_id", maximum=128)
            request_digest = _versioned_request_digest(
                action=action,
                expected_updated_at=expected_updated_at,
                expected_plan_version=expected_plan_version,
                details={"step_id": step_id},
            )
        elif action == REPLAN_PENDING_STEPS:
            raw_steps = payload.get("steps")
            if not isinstance(raw_steps, list) or not raw_steps:
                raise ControlActionError(
                    "invalid_replan_steps",
                    "steps must be a non-empty array",
                    status=400,
                )
            replacement_steps = []
            for item in raw_steps:
                if not isinstance(item, Mapping):
                    raise ControlActionError(
                        "invalid_replan_steps",
                        "every replacement step must be an object",
                        status=400,
                    )
                replacement_steps.append(dict(item))
            request_digest = _versioned_request_digest(
                action=action,
                expected_updated_at=expected_updated_at,
                expected_plan_version=expected_plan_version,
                details={"steps": replacement_steps},
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
            elif action in {APPROVE_STEP, REJECT_STEP}:
                assert step_id is not None
                requested_payload["step_id"] = step_id
            elif action == REPLAN_PENDING_STEPS:
                assert replacement_steps is not None
                requested_payload["step_ids"] = [
                    str(item.get("step_id") or "")
                    for item in replacement_steps
                ]
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
                        step_id=step_id,
                        replacement_steps=replacement_steps,
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
                        step_id=step_id,
                        replacement_steps=replacement_steps,
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
                elif action == PAUSE_TASK:
                    updated = service.request_pause(
                        task_id,
                        requested_by=f"control-center:{request_id}",
                        reason="control center pause request",
                    )
                elif action == RESUME_TASK:
                    updated = service.resume(
                        task_id,
                        expected_plan_version=expected_plan_version,
                    )
                elif action in {APPROVE_STEP, REJECT_STEP}:
                    assert step_id is not None
                    updated = service.approve_step(
                        task_id,
                        step_id,
                        approved=action == APPROVE_STEP,
                        expected_plan_version=expected_plan_version,
                    )
                elif action == REPLAN_PENDING_STEPS:
                    assert replacement_steps is not None
                    updated = service.replan_pending_steps(
                        task_id,
                        replacement_steps,
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


def _interpret_natural_goal(
    goal: str,
    *,
    continuation_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    limits = [
        "仅写入当前任务的独立 artifacts 目录",
        "单个文本产物最多 64 KiB",
        "预览不会创建任务、取得租约或执行工具",
        "创建成功后仍需显式受控启动",
    ]
    test_requested = bool(_TEST_INTENT.search(goal))
    create_requested = bool(_CREATE_INTENT.search(goal))
    research_requested = bool(_RESEARCH_INTENT.search(goal))
    code_requested = bool(_CODE_INTENT.search(goal))
    continuation_requested = bool(_CONTINUE_INTENT.search(goal)) and not (
        code_requested or research_requested
    )
    complex_requested = bool(_COMPLEX_INTENT.search(goal)) and not (
        code_requested or research_requested or continuation_requested
    )
    if complex_requested:
        return {
            "supported": True,
            "guided_flow": "complex",
            "operation": "execute_plan",
            "operation_label": "构建有界多步计划",
            "parameters": {},
            "required_fields": ["steps"],
            "missing_fields": ["steps"],
            "assumptions": [
                "自然语言只用于识别复杂任务意图；计划步骤必须由用户检查并确认",
            ],
            "limits": [
                "最多 8 个步骤，每步最多 3 次尝试",
                "依赖与数据引用必须先通过计划图校验",
                "预览不会创建任务、取得租约或执行工具",
            ],
            "reason_code": "guided_complex_plan_required",
            "reason": (
                "已识别为复杂任务。请在多步计划入口补充步骤、依赖和每一步的"
                "预期结果，系统不会从一句话自动猜测或执行计划。"
            ),
        }
    if research_requested:
        fields = [
            "workspace",
            "sources",
            "title",
            "output_path",
            "findings",
        ]
        return {
            "supported": True,
            "guided_flow": "research",
            "operation": "research_report",
            "operation_label": "生成本地研究可追溯报告",
            "parameters": {},
            "required_fields": fields,
            "missing_fields": list(fields),
            "assumptions": [
                "研究来源限定为已注册工作区中的本地文本",
            ],
            "limits": [
                "不联网、不浏览网页",
                "每条结论必须有可在来源中精确匹配的引文",
                "预览不会创建任务、取得租约或写入报告",
            ],
            "reason_code": "guided_research_fields_required",
            "reason": (
                "已识别为本地研究任务。请补充工作区、来源、报告信息和带引文的"
                " findings；系统不会猜测来源或伪造引文。"
            ),
        }
    if code_requested or test_requested:
        fields = (
            [
                "workspace",
                "path",
                "expected_sha256",
                "old_text",
                "new_text",
            ]
            if code_requested
            else ["workspace", "target"]
        )
        return {
            "supported": True,
            "guided_flow": "code",
            "operation": "edit_text_file" if code_requested else "run_tests",
            "operation_label": (
                "检查、审批、修改并测试代码"
                if code_requested
                else "运行已注册测试"
            ),
            "parameters": {},
            "required_fields": list(fields),
            "missing_fields": list(fields),
            "assumptions": [
                "代码修改使用精确文本替换并要求当前文件摘要",
            ],
            "limits": [
                "只允许已注册工作区中的安全相对路径",
                "修改步骤需要单独审批，测试后端权限不能由自然语言推断",
                "预览不会创建任务、取得租约或修改文件",
            ],
            "reason_code": "guided_code_fields_required",
            "reason": (
                "已识别为代码任务。请补充工作区、目标文件、当前摘要和精确修改"
                "文本；若只需测试，请切换到测试引导字段。"
            ),
        }
    if continuation_requested:
        parameters: dict[str, Any] = {}
        missing_fields: list[str] = []
        assumptions: list[str] = []
        if continuation_context is None:
            missing_fields.append("selected_task_id")
            assumptions.append(
                "请先从任务列表选择一个已完成且当前仍通过验证的产物"
            )
        else:
            parameters.update(dict(continuation_context))
            parameters["output_path"] = _continuation_output_name(
                goal,
                str(continuation_context["artifact_path"]),
            )
            assumptions.append(
                "来源任务、产物路径和预期摘要由当前已验证任务自动绑定"
            )
            assumptions.append(
                f"新版本默认文件名为 {parameters['output_path']}"
            )
        mode: str | None = None
        if _APPEND_INTENT.search(goal):
            mode = "append"
        elif _REPLACE_INTENT.search(goal):
            mode = "replace"
        if mode is None:
            missing_fields.append("mode")
        else:
            parameters["mode"] = mode
        content = _extract_continuation_content(goal)
        if content is None or content == "":
            missing_fields.append("content")
        else:
            parameters["content"] = content
        return {
            "supported": True,
            "guided_flow": "file",
            "operation": "modify_text_artifact",
            "operation_label": "创建修改后的新版本",
            "parameters": parameters,
            "required_fields": (
                ([] if continuation_context is not None else ["selected_task_id"])
                + ["content", "mode"]
            ),
            "missing_fields": list(dict.fromkeys(missing_fields)),
            "requires_task_selection": continuation_context is None,
            "assumptions": assumptions,
            "limits": [
                "只继续当前已验证且未发生变化的产物",
                "新版本写入当前新任务的独立 artifacts 目录",
                "源产物保持只读，不会被覆盖",
                "预览不会创建任务、取得租约或执行工具",
            ],
            "reason_code": (
                "continuation_task_selection_required"
                if continuation_context is None
                else None
            ),
            "reason": (
                "请从任务列表选择一个已验证产物后再继续修改。"
                if continuation_context is None
                else None
            ),
        }
    if not create_requested:
        return {
            "supported": False,
            "guided_flow": None,
            "operation": None,
            "operation_label": None,
            "parameters": {},
            "required_fields": [],
            "missing_fields": [],
            "assumptions": [],
            "limits": limits,
            "reason_code": "natural_goal_unsupported",
            "reason": (
                "当前无法把该目标识别为受支持的文本文件创建任务。"
                "请明确要创建、写入或保存的文件。"
            ),
        }

    path, path_match = _extract_artifact_path(goal)
    assumptions: list[str] = []
    if path is None:
        path = "report.md" if "报告" in goal else "task-output.txt"
        assumptions.append(f"未指定文件名，预览使用 {path}")
    else:
        try:
            path = validate_artifact_name(path)
        except ArtifactToolError as exc:
            raise ControlActionError(
                exc.code,
                exc.message,
                status=400,
            ) from exc

    content = _extract_natural_content(goal, path_match=path_match)
    parameters: dict[str, Any] = {"artifact_path": path}
    missing_fields: list[str] = []
    if content is None or content == "":
        missing_fields.append("content")
    else:
        parameters["content"] = content
    if missing_fields:
        assumptions.append("未识别到明确正文；请在下方的内容字段补充或修改")
    return {
        "supported": True,
        "guided_flow": "file",
        "operation": "write_text_artifact",
        "operation_label": "创建并验证文本文件",
        "parameters": parameters,
        "required_fields": ["artifact_path", "content"],
        "missing_fields": missing_fields,
        "assumptions": assumptions,
        "limits": limits,
        "reason_code": None,
        "reason": None,
    }


def _versioned_artifact_name(value: str) -> str:
    name = validate_artifact_name(value)
    stem, separator, extension = name.rpartition(".")
    if not separator:
        return validate_artifact_name(f"{name}-v2")
    return validate_artifact_name(f"{stem}-v2.{extension}")


def _continuation_output_name(goal: str, source_path: str) -> str:
    match = _SAVE_AS_PATH.search(goal)
    if match is not None:
        return validate_artifact_name(match.group("path"))
    return _versioned_artifact_name(source_path)


def _extract_continuation_content(goal: str) -> str | None:
    for pattern in (
        _FENCED_CONTENT,
        _CONTINUATION_QUOTED_CONTENT,
        _QUOTED_CONTENT,
        _CONTINUATION_INLINE_CONTENT,
    ):
        match = pattern.search(goal)
        if match is not None:
            return match.group("content")
    return None


def _extract_artifact_path(goal: str) -> tuple[str | None, re.Match[str] | None]:
    for pattern in (_QUOTED_ARTIFACT_PATH, _BARE_ARTIFACT_PATH):
        match = pattern.search(goal)
        if match is not None:
            return match.group("path"), match
    return None, None


def _extract_natural_content(
    goal: str,
    *,
    path_match: re.Match[str] | None,
) -> str | None:
    for pattern in (_FENCED_CONTENT, _QUOTED_CONTENT, _INLINE_CONTENT):
        for match in pattern.finditer(goal):
            if path_match is not None and match.start() < path_match.end():
                continue
            return match.group("content")
    return None


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
    details: Mapping[str, Any] | None = None,
) -> str:
    payload = {
        "action": action,
        "expected_updated_at": expected_updated_at,
        "expected_plan_version": expected_plan_version,
    }
    if details is not None:
        payload["details"] = dict(details)
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
