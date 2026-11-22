"""Deterministic, bounded planning for the first executable task type."""

from __future__ import annotations

import hashlib
from typing import Protocol
from uuid import uuid4

from .artifact_tool import MAX_ARTIFACT_BYTES
from .models import (
    Plan,
    PlannerMode,
    PlanningClassification,
    PlanningOutcome,
    PlanStep,
    TaskRequest,
)
from .multi_step import (
    MULTI_STEP_OPERATION,
    MultiStepPlanError,
    validate_plan_graph,
)
from .research_workflow import RESEARCH_REPORT_TOOL
from .shell_tool import CONTROLLED_SHELL_TOOL
from .workspace_edit import EDIT_TEXT_FILE_TOOL
from .workspace_inspection import INSPECT_WORKSPACE_TOOL


WRITE_TEXT_ARTIFACT = "write_text_artifact"
RUN_TESTS = "run_tests"
READ_TEXT_ARTIFACT = "read_text_artifact"
MODIFY_TEXT_ARTIFACT = "modify_text_artifact"
INSPECT_WORKSPACE = INSPECT_WORKSPACE_TOOL
EDIT_TEXT_FILE = EDIT_TEXT_FILE_TOOL
RESEARCH_REPORT = RESEARCH_REPORT_TOOL
CONTROLLED_SHELL = CONTROLLED_SHELL_TOOL


class PlanningError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Planner(Protocol):
    mode: PlannerMode
    name: str

    def plan_request(self, request: TaskRequest) -> PlanningOutcome: ...


def build_artifact_plan(
    request: TaskRequest,
    *,
    planner_name: str,
    deterministic: bool,
) -> Plan:
    artifact_path = request.parameters.get("artifact_path")
    content = request.parameters.get("content")
    if not isinstance(artifact_path, str) or not isinstance(content, str):
        raise PlanningError(
            "invalid_plan_inputs",
            "planner requires artifact_path and content strings",
        )
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_ARTIFACT_BYTES:
        raise PlanningError("content_too_large", "content exceeds the 64 KiB limit")
    digest = hashlib.sha256(content_bytes).hexdigest()
    return Plan(
        plan_id=str(uuid4()),
        planner_name=planner_name,
        deterministic=deterministic,
        steps=[
            PlanStep(
                step_id="write-1",
                tool_name=WRITE_TEXT_ARTIFACT,
                expected_result=(
                    f"create {artifact_path} in the task artifact directory "
                    f"with {len(content_bytes)} bytes and sha256 {digest}"
                ),
                parameters={
                    "artifact_path": artifact_path,
                    "content_sha256": digest,
                    "content_bytes": len(content_bytes),
                },
            )
        ],
    )


def build_test_plan(
    request: TaskRequest,
    *,
    planner_name: str,
) -> Plan:
    """Plan one bounded local test run.

    The planner only copies already-validated structured fields; it never
    composes a command string.  Policy re-validates everything before the
    executor starts a process.
    """
    validation = dict(request.parameters)
    # Format and policy checks belong to ``ArtifactPolicy``; planning only
    # copies structured fields so a rejection surfaces as ``blocked`` rather
    # than a planning failure.
    return Plan(
        plan_id=str(uuid4()),
        planner_name=planner_name,
        deterministic=True,
        steps=[
            PlanStep(
                step_id="test-1",
                tool_name=RUN_TESTS,
                expected_result=(
                    f"run {validation.get('runner')} target "
                    f"{validation.get('target')} in workspace "
                    f"{validation.get('workspace')}"
                ),
                parameters={
                    "runner": validation.get("runner"),
                    "target": validation.get("target"),
                    "workspace": validation.get("workspace"),
                    "timeout_seconds": validation.get("timeout_seconds", 10.0),
                    "max_output_bytes": validation.get("max_output_bytes", 8192),
                    "allowed_side_effects": list(
                        validation.get("allowed_side_effects") or []
                    ),
                    "protected_paths": list(
                        validation.get("protected_paths") or []
                    ),
                    "execution_backend": validation.get(
                        "execution_backend", "container"
                    ),
                    "evidence_id": validation.get("evidence_id"),
                    "image": validation.get("image"),
                    "config_digest": validation.get("config_digest"),
                    "command_digest": validation.get("command_digest"),
                    "host_authorization": validation.get("host_authorization"),
                    "required_artifacts": list(
                        validation.get("required_artifacts") or []
                    ),
                    "disk_limit_bytes": validation.get("disk_limit_bytes"),
                    "workspace_budget_bytes": validation.get(
                        "workspace_budget_bytes"
                    ),
                    "auto_provision_isolation": bool(
                        validation.get("auto_provision_isolation")
                    ),
                },
            )
        ],
    )


def build_read_plan(
    request: TaskRequest,
    *,
    planner_name: str,
) -> Plan:
    parameters = request.parameters
    return Plan(
        plan_id=str(uuid4()),
        planner_name=planner_name,
        deterministic=True,
        steps=[
            PlanStep(
                step_id="read-1",
                tool_name=READ_TEXT_ARTIFACT,
                expected_result=(
                    f"read {parameters.get('artifact_path')} from verified "
                    f"task {parameters.get('source_task_id')}"
                ),
                parameters={
                    "source_task_id": parameters.get("source_task_id"),
                    "artifact_path": parameters.get("artifact_path"),
                    "expected_sha256": parameters.get("expected_sha256"),
                },
            )
        ],
    )


def build_modify_plan(
    request: TaskRequest,
    *,
    planner_name: str,
) -> Plan:
    parameters = request.parameters
    content = parameters.get("content")
    if not isinstance(content, str):
        raise PlanningError(
            "invalid_plan_inputs",
            "modify_text_artifact requires a text content patch",
        )
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_ARTIFACT_BYTES:
        raise PlanningError(
            "content_too_large",
            "modify content exceeds the 64 KiB limit",
        )
    digest = hashlib.sha256(content_bytes).hexdigest()
    artifact_path = parameters.get("artifact_path")
    return Plan(
        plan_id=str(uuid4()),
        planner_name=planner_name,
        deterministic=True,
        steps=[
            PlanStep(
                step_id="modify-1",
                tool_name=MODIFY_TEXT_ARTIFACT,
                expected_result=(
                    f"create a new verified version of {artifact_path} from "
                    f"source task {parameters.get('source_task_id')}"
                ),
                parameters={
                    "source_task_id": parameters.get("source_task_id"),
                    "artifact_path": artifact_path,
                    "output_path": parameters.get(
                        "output_path", artifact_path
                    ),
                    "expected_sha256": parameters.get("expected_sha256"),
                    "mode": parameters.get("mode", "replace"),
                    "content_sha256": digest,
                    "content_bytes": len(content_bytes),
                },
            )
        ],
    )


def build_workspace_inspection_plan(
    request: TaskRequest,
    *,
    planner_name: str,
) -> Plan:
    parameters = request.parameters
    return Plan(
        plan_id=str(uuid4()),
        planner_name=planner_name,
        deterministic=True,
        steps=[
            PlanStep(
                step_id="inspect-1",
                tool_name=INSPECT_WORKSPACE,
                expected_result=(
                    f"inspect registered workspace "
                    f"{parameters.get('workspace')!r}"
                ),
                parameters={
                    "workspace": parameters.get("workspace"),
                    "path": parameters.get("path", ""),
                    "query": parameters.get("query"),
                    "max_files": parameters.get("max_files", 200),
                    "max_file_bytes": parameters.get(
                        "max_file_bytes",
                        512 * 1024,
                    ),
                },
            )
        ],
    )


def build_workspace_edit_plan(
    request: TaskRequest,
    *,
    planner_name: str,
) -> Plan:
    parameters = request.parameters
    patch_digest = hashlib.sha256(
        (
            str(parameters.get("old_text", ""))
            + "\0"
            + str(parameters.get("new_text", ""))
        ).encode("utf-8")
    ).hexdigest()
    return Plan(
        plan_id=str(uuid4()),
        planner_name=planner_name,
        deterministic=True,
        steps=[
            PlanStep(
                step_id="edit-1",
                tool_name=EDIT_TEXT_FILE,
                expected_result=(
                    f"replace exactly one occurrence in workspace file "
                    f"{parameters.get('path')!r}"
                ),
                parameters={
                    "workspace": parameters.get("workspace"),
                    "path": parameters.get("path"),
                    "expected_sha256": parameters.get("expected_sha256"),
                    "old_text": parameters.get("old_text"),
                    "new_text": parameters.get("new_text"),
                    "authorization": parameters.get("authorization"),
                    "patch_sha256": patch_digest,
                },
            )
        ],
    )


def build_research_plan(
    request: TaskRequest,
    *,
    planner_name: str,
) -> Plan:
    parameters = request.parameters
    return Plan(
        plan_id=str(uuid4()),
        planner_name=planner_name,
        deterministic=True,
        steps=[
            PlanStep(
                step_id="research-1",
                tool_name=RESEARCH_REPORT,
                expected_result=(
                    f"create source-preserving research report "
                    f"{parameters.get('output_path')!r} from "
                    f"{len(parameters.get('sources') or [])} local sources"
                ),
                parameters={
                    "workspace": parameters.get("workspace"),
                    "sources": list(parameters.get("sources") or []),
                    "title": parameters.get("title"),
                    "output_path": parameters.get("output_path"),
                    "findings": list(parameters.get("findings") or []),
                },
            )
        ],
    )


def build_controlled_shell_plan(
    request: TaskRequest,
    *,
    planner_name: str,
) -> Plan:
    parameters = request.parameters
    command = parameters.get("command")
    if not isinstance(command, str) or not command:
        raise PlanningError(
            "invalid_shell_command",
            "command must be a non-empty string",
        )
    arguments = parameters.get("arguments", [])
    if not isinstance(arguments, list):
        raise PlanningError(
            "invalid_shell_arguments",
            "arguments must be an array",
        )
    return Plan(
        plan_id=str(uuid4()),
        planner_name=planner_name,
        deterministic=True,
        steps=[
            PlanStep(
                step_id="shell-1",
                tool_name=CONTROLLED_SHELL,
                expected_result=(
                    f"execute whitelisted command {command} "
                    f"with {len(arguments)} argument(s) "
                    f"and bounded output"
                ),
                parameters={
                    "command": command,
                    "arguments": list(arguments),
                    "timeout_seconds": parameters.get(
                        "timeout_seconds", 15
                    ),
                    "working_directory": parameters.get(
                        "working_directory", ""
                    ),
                },
            )
        ],
    )


def build_multi_step_plan(
    request: TaskRequest,
    *,
    planner_name: str,
    deterministic: bool,
) -> Plan:
    raw_steps = request.parameters.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanningError(
            "invalid_multi_step_plan",
            "execute_plan requires a non-empty steps list",
        )
    steps: list[PlanStep] = []
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            raise PlanningError(
                "invalid_multi_step_plan",
                f"step {index} must be an object",
            )
        try:
            step = PlanStep.from_dict(raw_step)
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanningError(
                "invalid_multi_step_plan",
                f"step {index} is malformed",
            ) from exc
        steps.append(step)
    plan = Plan(
        plan_id=str(uuid4()),
        planner_name=planner_name,
        deterministic=deterministic,
        steps=steps,
        failure_policy=str(
            request.parameters.get("failure_policy", "fail_fast")
        ),
    )
    try:
        validate_plan_graph(plan)
    except MultiStepPlanError as exc:
        raise PlanningError(exc.code, exc.message) from exc
    return plan


class DeterministicPlanner:
    mode = PlannerMode.DETERMINISTIC
    name = "deterministic-v1"

    def plan(self, request: TaskRequest) -> Plan:
        if request.operation == MULTI_STEP_OPERATION:
            return build_multi_step_plan(
                request,
                planner_name=self.name,
                deterministic=True,
            )
        if request.operation == INSPECT_WORKSPACE:
            return build_workspace_inspection_plan(
                request,
                planner_name=self.name,
            )
        if request.operation == EDIT_TEXT_FILE:
            return build_workspace_edit_plan(
                request,
                planner_name=self.name,
            )
        if request.operation == RESEARCH_REPORT:
            return build_research_plan(
                request,
                planner_name=self.name,
            )
        if request.operation == RUN_TESTS:
            return build_test_plan(request, planner_name=self.name)
        if request.operation == READ_TEXT_ARTIFACT:
            return build_read_plan(request, planner_name=self.name)
        if request.operation == MODIFY_TEXT_ARTIFACT:
            return build_modify_plan(request, planner_name=self.name)
        if request.operation == CONTROLLED_SHELL:
            return build_controlled_shell_plan(
                request,
                planner_name=self.name,
            )
        if request.operation != WRITE_TEXT_ARTIFACT:
            raise PlanningError(
                "unsupported_operation",
                f"unsupported operation: {request.operation}",
            )
        return build_artifact_plan(
            request,
            planner_name=self.name,
            deterministic=True,
        )

    def plan_request(self, request: TaskRequest) -> PlanningOutcome:
        missing: list[str] = []
        if request.goal is None or not request.goal.strip():
            missing.append("goal")
        if request.operation is None or not request.operation.strip():
            missing.append("operation")
        if request.operation == MULTI_STEP_OPERATION:
            if not isinstance(request.parameters.get("steps"), list):
                missing.append("steps")
            if missing:
                return PlanningOutcome(
                    classification=PlanningClassification.CLARIFICATION,
                    request=request,
                    missing_fields=missing,
                    clarification_questions=[
                        f"请提供 {field}" for field in missing
                    ],
                    metadata={
                        "planner_name": self.name,
                        "planner_mode": self.mode.value,
                    },
                )
            try:
                plan = self.plan(request)
            except PlanningError as exc:
                return PlanningOutcome(
                    classification=PlanningClassification.FAILED,
                    request=request,
                    error_code=exc.code,
                    error_message=exc.message,
                    metadata={
                        "planner_name": self.name,
                        "planner_mode": self.mode.value,
                    },
                )
            return PlanningOutcome(
                classification=PlanningClassification.PLANNED,
                request=request,
                plan=plan,
                metadata={
                    "planner_name": self.name,
                    "planner_mode": self.mode.value,
                    "multi_step": True,
                },
            )
        if request.operation == RUN_TESTS:
            for field in ("runner", "target", "workspace"):
                if request.parameters.get(field) is None:
                    missing.append(field)
            if not missing:
                try:
                    plan = build_test_plan(request, planner_name=self.name)
                except PlanningError as exc:
                    return PlanningOutcome(
                        classification=PlanningClassification.FAILED,
                        request=request,
                        error_code=exc.code,
                        error_message=exc.message,
                        metadata={
                            "planner_name": self.name,
                            "planner_mode": self.mode.value,
                        },
                    )
                return PlanningOutcome(
                    classification=PlanningClassification.PLANNED,
                    request=request,
                    plan=plan,
                    metadata={
                        "planner_name": self.name,
                        "planner_mode": self.mode.value,
                    },
                )
            return PlanningOutcome(
                classification=PlanningClassification.CLARIFICATION,
                request=request,
                missing_fields=missing,
                clarification_questions=[
                    f"请提供 {field}" for field in missing
                ],
                metadata={
                    "planner_name": self.name,
                    "planner_mode": self.mode.value,
                },
            )
        required_operation_fields: tuple[str, ...] = ()
        if request.operation == READ_TEXT_ARTIFACT:
            required_operation_fields = ("source_task_id", "artifact_path")
        elif request.operation == MODIFY_TEXT_ARTIFACT:
            required_operation_fields = (
                "source_task_id",
                "artifact_path",
                "content",
                "expected_sha256",
            )
        elif request.operation == INSPECT_WORKSPACE:
            required_operation_fields = ("workspace",)
        elif request.operation == EDIT_TEXT_FILE:
            required_operation_fields = (
                "workspace",
                "path",
                "expected_sha256",
                "old_text",
                "new_text",
                "authorization",
            )
        elif request.operation == RESEARCH_REPORT:
            required_operation_fields = (
                "workspace",
                "sources",
                "title",
                "output_path",
                "findings",
            )
        elif request.operation == CONTROLLED_SHELL:
            required_operation_fields = ("command",)
        if required_operation_fields:
            for field in required_operation_fields:
                if request.parameters.get(field) is None:
                    missing.append(field)
            if missing:
                questions = [f"请提供 {field}" for field in missing]
                return PlanningOutcome(
                    classification=PlanningClassification.CLARIFICATION,
                    request=request,
                    missing_fields=missing,
                    clarification_questions=questions,
                    metadata={
                        "planner_name": self.name,
                        "planner_mode": self.mode.value,
                    },
                )
            try:
                plan = self.plan(request)
            except PlanningError as exc:
                return PlanningOutcome(
                    classification=PlanningClassification.FAILED,
                    request=request,
                    error_code=exc.code,
                    error_message=exc.message,
                    metadata={
                        "planner_name": self.name,
                        "planner_mode": self.mode.value,
                    },
                )
            return PlanningOutcome(
                classification=PlanningClassification.PLANNED,
                request=request,
                plan=plan,
                metadata={
                    "planner_name": self.name,
                    "planner_mode": self.mode.value,
                },
            )
        if request.parameters.get("artifact_path") is None:
            missing.append("artifact_path")
        if request.parameters.get("content") is None:
            missing.append("content")
        if missing:
            questions = [f"请提供 {field}" for field in missing]
            return PlanningOutcome(
                classification=PlanningClassification.CLARIFICATION,
                request=request,
                missing_fields=missing,
                clarification_questions=questions,
                metadata={
                    "planner_name": self.name,
                    "planner_mode": self.mode.value,
                },
            )
        try:
            plan = self.plan(request)
        except PlanningError as exc:
            classification = (
                PlanningClassification.UNSUPPORTED
                if exc.code == "unsupported_operation"
                else PlanningClassification.FAILED
            )
            return PlanningOutcome(
                classification=classification,
                request=request,
                error_code=exc.code,
                error_message=exc.message,
                metadata={
                    "planner_name": self.name,
                    "planner_mode": self.mode.value,
                },
            )
        return PlanningOutcome(
            classification=PlanningClassification.PLANNED,
            request=request,
            plan=plan,
            metadata={
                "planner_name": self.name,
                "planner_mode": self.mode.value,
            },
        )
