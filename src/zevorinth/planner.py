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


WRITE_TEXT_ARTIFACT = "write_text_artifact"


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


class DeterministicPlanner:
    mode = PlannerMode.DETERMINISTIC
    name = "deterministic-v1"

    def plan(self, request: TaskRequest) -> Plan:
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
