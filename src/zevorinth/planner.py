"""Deterministic, bounded planning for the first executable task type."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from .models import Plan, PlanStep, TaskRequest
from .artifact_tool import MAX_ARTIFACT_BYTES


WRITE_TEXT_ARTIFACT = "write_text_artifact"


class PlanningError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DeterministicPlanner:
    name = "deterministic-v1"

    def plan(self, request: TaskRequest) -> Plan:
        if request.operation != WRITE_TEXT_ARTIFACT:
            raise PlanningError(
                "unsupported_operation",
                f"unsupported operation: {request.operation}",
            )
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
            planner_name=self.name,
            deterministic=True,
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
