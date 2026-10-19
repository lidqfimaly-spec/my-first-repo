"""Core data contracts for the bounded Zevorinth task runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4


JsonObject = dict[str, Any]


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    BLOCKED = "blocked"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    SIMULATED = "simulated"


class ExecutionMode(str, Enum):
    NOT_EXECUTED = "not_executed"
    REAL = "real"
    SIMULATED = "simulated"


class EventType(str, Enum):
    TASK_CREATED = "task_created"
    CLARIFICATION_REQUIRED = "clarification_required"
    CLARIFICATION_UPDATED = "clarification_updated"
    TASK_PLANNED = "task_planned"
    POLICY_REJECTED = "policy_rejected"
    TOOL_STARTED = "tool_started"
    TOOL_SUCCEEDED = "tool_succeeded"
    TOOL_FAILED = "tool_failed"
    SIMULATION_COMPLETED = "simulation_completed"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_COMPLETED = "verification_completed"
    TASK_INTERRUPTED = "task_interrupted"
    TASK_RESUMED = "task_resumed"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_FAILED = "task_failed"


@dataclass(slots=True)
class TaskRequest:
    goal: str | None
    operation: str | None
    parameters: JsonObject = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TaskRequest":
        parameters = data.get("parameters", {})
        if not isinstance(parameters, Mapping):
            raise ValueError("parameters must be an object")
        goal = data.get("goal")
        operation = data.get("operation")
        if goal is not None and not isinstance(goal, str):
            raise ValueError("goal must be a string or null")
        if operation is not None and not isinstance(operation, str):
            raise ValueError("operation must be a string or null")
        return cls(goal=goal, operation=operation, parameters=dict(parameters))

    def to_dict(self) -> JsonObject:
        return {
            "goal": self.goal,
            "operation": self.operation,
            "parameters": self.parameters,
        }

    def merged(self, patch: Mapping[str, Any]) -> "TaskRequest":
        updated = self.to_dict()
        for key in ("goal", "operation"):
            if key in patch:
                updated[key] = patch[key]
        if "parameters" in patch:
            parameter_patch = patch["parameters"]
            if not isinstance(parameter_patch, Mapping):
                raise ValueError("parameters must be an object")
            parameters = dict(self.parameters)
            parameters.update(parameter_patch)
            updated["parameters"] = parameters
        return TaskRequest.from_mapping(updated)


@dataclass(slots=True)
class PlanStep:
    step_id: str
    tool_name: str
    expected_result: str
    parameters: JsonObject

    def to_dict(self) -> JsonObject:
        return {
            "step_id": self.step_id,
            "tool_name": self.tool_name,
            "expected_result": self.expected_result,
            "parameters": self.parameters,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlanStep":
        return cls(
            step_id=str(data["step_id"]),
            tool_name=str(data["tool_name"]),
            expected_result=str(data["expected_result"]),
            parameters=dict(data.get("parameters", {})),
        )


@dataclass(slots=True)
class Plan:
    plan_id: str
    planner_name: str
    deterministic: bool
    steps: list[PlanStep]

    def to_dict(self) -> JsonObject:
        return {
            "plan_id": self.plan_id,
            "planner_name": self.planner_name,
            "deterministic": self.deterministic,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Plan":
        return cls(
            plan_id=str(data["plan_id"]),
            planner_name=str(data["planner_name"]),
            deterministic=bool(data["deterministic"]),
            steps=[PlanStep.from_dict(step) for step in data.get("steps", [])],
        )


@dataclass(slots=True)
class ArtifactEvidence:
    relative_path: str
    byte_count: int
    sha256: str

    def to_dict(self) -> JsonObject:
        return {
            "relative_path": self.relative_path,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactEvidence":
        return cls(
            relative_path=str(data["relative_path"]),
            byte_count=int(data["byte_count"]),
            sha256=str(data["sha256"]),
        )


@dataclass(slots=True)
class TaskError:
    code: str
    message: str

    def to_dict(self) -> JsonObject:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskError":
        return cls(code=str(data["code"]), message=str(data["message"]))


@dataclass(slots=True)
class ToolResult:
    attempt_id: str
    tool_name: str
    execution_mode: ExecutionMode
    success: bool
    started_at: str
    finished_at: str
    artifact: ArtifactEvidence | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> JsonObject:
        return {
            "attempt_id": self.attempt_id,
            "tool_name": self.tool_name,
            "execution_mode": self.execution_mode.value,
            "success": self.success,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "artifact": self.artifact.to_dict() if self.artifact else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ToolResult":
        artifact = data.get("artifact")
        return cls(
            attempt_id=str(data["attempt_id"]),
            tool_name=str(data["tool_name"]),
            execution_mode=ExecutionMode(data["execution_mode"]),
            success=bool(data["success"]),
            started_at=str(data["started_at"]),
            finished_at=str(data["finished_at"]),
            artifact=ArtifactEvidence.from_dict(artifact) if artifact else None,
            error_code=data.get("error_code"),
            error_message=data.get("error_message"),
        )


@dataclass(slots=True)
class VerificationResult:
    verifier_name: str
    passed: bool
    exists: bool
    path_safe: bool
    content_matches: bool
    evidence_matches: bool
    reason: str
    artifact: ArtifactEvidence | None
    checked_at: str

    def to_dict(self) -> JsonObject:
        return {
            "verifier_name": self.verifier_name,
            "passed": self.passed,
            "exists": self.exists,
            "path_safe": self.path_safe,
            "content_matches": self.content_matches,
            "evidence_matches": self.evidence_matches,
            "reason": self.reason,
            "artifact": self.artifact.to_dict() if self.artifact else None,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VerificationResult":
        artifact = data.get("artifact")
        return cls(
            verifier_name=str(data["verifier_name"]),
            passed=bool(data["passed"]),
            exists=bool(data["exists"]),
            path_safe=bool(data["path_safe"]),
            content_matches=bool(data["content_matches"]),
            evidence_matches=bool(data["evidence_matches"]),
            reason=str(data["reason"]),
            artifact=ArtifactEvidence.from_dict(artifact) if artifact else None,
            checked_at=str(data["checked_at"]),
        )


@dataclass(slots=True)
class Event:
    event_id: str
    task_id: str
    event_type: str
    occurred_at: str
    payload: JsonObject
    sequence: int | None = None

    @classmethod
    def new(
        cls,
        task_id: str,
        event_type: EventType | str,
        occurred_at: str,
        payload: Mapping[str, Any] | None = None,
    ) -> "Event":
        event_type_value = event_type.value if isinstance(event_type, EventType) else event_type
        return cls(
            event_id=str(uuid4()),
            task_id=task_id,
            event_type=event_type_value,
            occurred_at=occurred_at,
            payload=dict(payload or {}),
        )

    def to_dict(self) -> JsonObject:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "payload": self.payload,
            "sequence": self.sequence,
        }


@dataclass(slots=True)
class Task:
    task_id: str
    status: TaskStatus
    request: TaskRequest
    execution_mode: ExecutionMode
    attempt_id: str
    phase: str
    created_at: str
    updated_at: str
    plan: Plan | None = None
    artifact: ArtifactEvidence | None = None
    tool_result: ToolResult | None = None
    verification: VerificationResult | None = None
    missing_fields: list[str] = field(default_factory=list)
    error: TaskError | None = None

    def to_dict(self) -> JsonObject:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "request": self.request.to_dict(),
            "execution_mode": self.execution_mode.value,
            "attempt_id": self.attempt_id,
            "phase": self.phase,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "plan": self.plan.to_dict() if self.plan else None,
            "artifact": self.artifact.to_dict() if self.artifact else None,
            "tool_result": self.tool_result.to_dict() if self.tool_result else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "missing_fields": list(self.missing_fields),
            "error": self.error.to_dict() if self.error else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Task":
        plan = data.get("plan")
        artifact = data.get("artifact")
        tool_result = data.get("tool_result")
        verification = data.get("verification")
        error = data.get("error")
        return cls(
            task_id=str(data["task_id"]),
            status=TaskStatus(data["status"]),
            request=TaskRequest.from_mapping(data["request"]),
            execution_mode=ExecutionMode(data["execution_mode"]),
            attempt_id=str(data["attempt_id"]),
            phase=str(data["phase"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            plan=Plan.from_dict(plan) if plan else None,
            artifact=ArtifactEvidence.from_dict(artifact) if artifact else None,
            tool_result=ToolResult.from_dict(tool_result) if tool_result else None,
            verification=VerificationResult.from_dict(verification) if verification else None,
            missing_fields=[str(item) for item in data.get("missing_fields", [])],
            error=TaskError.from_dict(error) if error else None,
        )


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    code: str
    reason: str
