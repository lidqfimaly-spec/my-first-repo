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
    PAUSED = "paused"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    BLOCKED = "blocked"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    PLANNING_FAILED = "planning_failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    SIMULATED = "simulated"


class ExecutionMode(str, Enum):
    NOT_EXECUTED = "not_executed"
    REAL = "real"
    SIMULATED = "simulated"


class ConversationStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class TurnStatus(str, Enum):
    RECEIVED = "received"
    CLASSIFIED = "classified"
    DISPATCHED = "dispatched"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    NEEDS_ATTENTION = "needs_attention"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IntentKind(str, Enum):
    NEW_TASK = "new_task"
    SUPPLEMENT = "supplement"
    MODIFY_PENDING = "modify_pending"
    CLARIFICATION_ANSWER = "clarification_answer"
    STATUS_QUERY = "status_query"
    AMBIGUOUS = "ambiguous"
    CANCEL_REQUEST = "cancel_request"


class ClarificationStatus(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class ClarificationAnswerStatus(str, Enum):
    PENDING_APPLY = "pending_apply"
    APPLIED = "applied"
    REJECTED = "rejected"


class PlannerMode(str, Enum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"


class PlanningClassification(str, Enum):
    PLANNED = "planned"
    CLARIFICATION = "clarification"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class EventType(str, Enum):
    TASK_CREATED = "task_created"
    PLANNING_STARTED = "planning_started"
    PLANNING_COMPLETED = "planning_completed"
    PLANNING_CLARIFICATION = "planning_clarification"
    PLANNING_REJECTED = "planning_rejected"
    PLANNING_FAILED = "planning_failed"
    CLARIFICATION_REQUIRED = "clarification_required"
    CLARIFICATION_UPDATED = "clarification_updated"
    TASK_PLANNED = "task_planned"
    TASK_CLAIMED = "task_claimed"
    CLAIM_REJECTED = "claim_rejected"
    LEASE_EXPIRED = "lease_expired"
    LEASE_RENEWED = "lease_renewed"
    LEASE_RELEASED = "lease_released"
    POLICY_REJECTED = "policy_rejected"
    TOOL_STARTED = "tool_started"
    EXECUTION_PLAN_FINALIZED = "execution_plan_finalized"
    TOOL_SUCCEEDED = "tool_succeeded"
    TOOL_FAILED = "tool_failed"
    SIMULATION_COMPLETED = "simulation_completed"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_COMPLETED = "verification_completed"
    TASK_INTERRUPTED = "task_interrupted"
    TASK_RESUMED = "task_resumed"
    PAUSE_REQUESTED = "pause_requested"
    TASK_PAUSED = "task_paused"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    STEP_SKIPPED = "step_skipped"
    STEP_APPROVAL_REQUIRED = "step_approval_required"
    STEP_APPROVED = "step_approved"
    STEPS_REPLANNED = "steps_replanned"
    TASK_PARTIAL = "task_partial"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    CANCEL_REQUESTED = "cancel_requested"
    PLAN_REVISED = "plan_revised"
    STAGING_CLEANED = "staging_cleaned"
    CONTROL_ACTION_REQUESTED = "control_action_requested"
    CONTROL_ACTION_COMPLETED = "control_action_completed"
    CONTROL_ACTION_FAILED = "control_action_failed"
    TASK_NOTE_ADDED = "task_note_added"


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
    depends_on: list[str] = field(default_factory=list)
    requires_approval: bool = False
    max_attempts: int = 1
    retry_on_not_executed: bool = False

    def to_dict(self) -> JsonObject:
        return {
            "step_id": self.step_id,
            "tool_name": self.tool_name,
            "expected_result": self.expected_result,
            "parameters": self.parameters,
            "depends_on": list(self.depends_on),
            "requires_approval": self.requires_approval,
            "max_attempts": self.max_attempts,
            "retry_on_not_executed": self.retry_on_not_executed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlanStep":
        depends_on = data.get("depends_on", [])
        if not isinstance(depends_on, list):
            raise ValueError("plan step depends_on must be a list")
        return cls(
            step_id=str(data["step_id"]),
            tool_name=str(data["tool_name"]),
            expected_result=str(data["expected_result"]),
            parameters=dict(data.get("parameters", {})),
            depends_on=[str(item) for item in depends_on],
            requires_approval=bool(data.get("requires_approval", False)),
            max_attempts=int(data.get("max_attempts", 1)),
            retry_on_not_executed=bool(
                data.get("retry_on_not_executed", False)
            ),
        )


@dataclass(slots=True)
class Plan:
    plan_id: str
    planner_name: str
    deterministic: bool
    steps: list[PlanStep]
    failure_policy: str = "fail_fast"

    def to_dict(self) -> JsonObject:
        return {
            "plan_id": self.plan_id,
            "planner_name": self.planner_name,
            "deterministic": self.deterministic,
            "steps": [step.to_dict() for step in self.steps],
            "failure_policy": self.failure_policy,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Plan":
        return cls(
            plan_id=str(data["plan_id"]),
            planner_name=str(data["planner_name"]),
            deterministic=bool(data["deterministic"]),
            steps=[PlanStep.from_dict(step) for step in data.get("steps", [])],
            failure_policy=str(data.get("failure_policy", "fail_fast")),
        )


@dataclass(slots=True)
class StepResult:
    step_id: str
    tool_name: str
    status: str
    attempt_count: int
    child_task_id: str | None
    started_at: str | None
    finished_at: str | None
    artifact: "ArtifactEvidence | None" = None
    verification: "VerificationResult | None" = None
    error: "TaskError | None" = None
    output: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "step_id": self.step_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "child_task_id": self.child_task_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "artifact": self.artifact.to_dict() if self.artifact else None,
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
            "error": self.error.to_dict() if self.error else None,
            "output": self.output,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StepResult":
        artifact = data.get("artifact")
        verification = data.get("verification")
        error = data.get("error")
        return cls(
            step_id=str(data["step_id"]),
            tool_name=str(data["tool_name"]),
            status=str(data["status"]),
            attempt_count=int(data.get("attempt_count", 1)),
            child_task_id=(
                str(data["child_task_id"])
                if data.get("child_task_id") is not None
                else None
            ),
            started_at=(
                str(data["started_at"])
                if data.get("started_at") is not None
                else None
            ),
            finished_at=(
                str(data["finished_at"])
                if data.get("finished_at") is not None
                else None
            ),
            artifact=(
                ArtifactEvidence.from_dict(artifact)
                if isinstance(artifact, Mapping)
                else None
            ),
            verification=(
                VerificationResult.from_dict(verification)
                if isinstance(verification, Mapping)
                else None
            ),
            error=(
                TaskError.from_dict(error)
                if isinstance(error, Mapping)
                else None
            ),
            output=dict(data.get("output", {})),
        )


@dataclass(slots=True)
class ArtifactEvidence:
    relative_path: str
    byte_count: int
    sha256: str
    attempt_id: str | None = None
    claim_generation: int | None = None

    def to_dict(self) -> JsonObject:
        return {
            "relative_path": self.relative_path,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "attempt_id": self.attempt_id,
            "claim_generation": self.claim_generation,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactEvidence":
        return cls(
            relative_path=str(data["relative_path"]),
            byte_count=int(data["byte_count"]),
            sha256=str(data["sha256"]),
            attempt_id=(
                str(data["attempt_id"])
                if data.get("attempt_id") is not None
                else None
            ),
            claim_generation=(
                int(data["claim_generation"])
                if data.get("claim_generation") is not None
                else None
            ),
        )


@dataclass(slots=True)
class ArtifactReadEvidence:
    """Bounded bytes observed from one already-verified source artifact."""

    source_task_id: str
    relative_path: str
    byte_count: int
    sha256: str
    content: str
    source_attempt_id: str | None = None
    source_claim_generation: int | None = None

    def to_dict(self) -> JsonObject:
        return {
            "source_task_id": self.source_task_id,
            "relative_path": self.relative_path,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "content": self.content,
            "source_attempt_id": self.source_attempt_id,
            "source_claim_generation": self.source_claim_generation,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactReadEvidence":
        return cls(
            source_task_id=str(data["source_task_id"]),
            relative_path=str(data["relative_path"]),
            byte_count=int(data["byte_count"]),
            sha256=str(data["sha256"]),
            content=str(data["content"]),
            source_attempt_id=(
                str(data["source_attempt_id"])
                if data.get("source_attempt_id") is not None
                else None
            ),
            source_claim_generation=(
                int(data["source_claim_generation"])
                if data.get("source_claim_generation") is not None
                else None
            ),
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
class PlanRevision:
    revision_id: str
    from_version: int
    to_version: int
    source_id: str
    changed_at: str
    confirmed: bool
    old_request: JsonObject
    new_request: JsonObject
    old_plan: JsonObject | None
    new_plan: JsonObject | None
    changed_steps: list[str]

    def to_dict(self) -> JsonObject:
        return {
            "revision_id": self.revision_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "source_id": self.source_id,
            "changed_at": self.changed_at,
            "confirmed": self.confirmed,
            "old_request": self.old_request,
            "new_request": self.new_request,
            "old_plan": self.old_plan,
            "new_plan": self.new_plan,
            "changed_steps": list(self.changed_steps),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlanRevision":
        return cls(
            revision_id=str(data["revision_id"]),
            from_version=int(data["from_version"]),
            to_version=int(data["to_version"]),
            source_id=str(data["source_id"]),
            changed_at=str(data["changed_at"]),
            confirmed=bool(data["confirmed"]),
            old_request=dict(data.get("old_request", {})),
            new_request=dict(data.get("new_request", {})),
            old_plan=(
                dict(data["old_plan"])
                if data.get("old_plan") is not None
                else None
            ),
            new_plan=(
                dict(data["new_plan"])
                if data.get("new_plan") is not None
                else None
            ),
            changed_steps=[
                str(item) for item in data.get("changed_steps", [])
            ],
        )


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
    test_evidence: "TestExecutionEvidence | None" = None
    workspace_effects: JsonObject | None = None
    read_evidence: ArtifactReadEvidence | None = None
    lineage: JsonObject | None = None
    workspace_inspection: JsonObject | None = None
    workspace_edit: JsonObject | None = None
    research_evidence: JsonObject | None = None

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
            "test_evidence": (
                self.test_evidence.to_dict()
                if self.test_evidence is not None
                else None
            ),
            "workspace_effects": self.workspace_effects,
            "read_evidence": (
                self.read_evidence.to_dict()
                if self.read_evidence is not None
                else None
            ),
            "lineage": self.lineage,
            "workspace_inspection": self.workspace_inspection,
            "workspace_edit": self.workspace_edit,
            "research_evidence": self.research_evidence,
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
            test_evidence=(
                TestExecutionEvidence.from_dict(data["test_evidence"])
                if data.get("test_evidence")
                else None
            ),
            workspace_effects=(
                dict(data["workspace_effects"])
                if data.get("workspace_effects")
                else None
            ),
            read_evidence=(
                ArtifactReadEvidence.from_dict(data["read_evidence"])
                if data.get("read_evidence")
                else None
            ),
            lineage=(
                dict(data["lineage"])
                if data.get("lineage")
                else None
            ),
            workspace_inspection=(
                dict(data["workspace_inspection"])
                if data.get("workspace_inspection")
                else None
            ),
            workspace_edit=(
                dict(data["workspace_edit"])
                if data.get("workspace_edit")
                else None
            ),
            research_evidence=(
                dict(data["research_evidence"])
                if data.get("research_evidence")
                else None
            ),
        )


@dataclass(slots=True)
class TestExecutionEvidence:
    """Structured, bounded record of one controlled test run."""

    runner: str
    target: str
    workspace: str
    attempt_id: str
    claim_generation: int
    exit_code: int | None
    started_at: str
    finished_at: str
    duration_ms: float
    timed_out: bool
    cancelled: bool
    truncated: bool
    stdout_sha256: str
    stderr_sha256: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_excerpt: str
    stderr_excerpt: str
    terminated_confirmed: bool
    output_limit_bytes: int
    timeout_seconds: float
    before_snapshot: JsonObject | None = None
    after_snapshot: JsonObject | None = None
    workspace_diff: JsonObject | None = None
    evidence_id: str | None = None
    container_name: str | None = None
    config_digest: str | None = None
    command_digest: str | None = None
    #: Identity of the single-use host execution grant this run spent.  The
    #: grant store keeps its own audit trail; this is the in-task half of the
    #: chain from grant to task to evidence.
    host_grant_id: str | None = None
    host_grant_issuer: str | None = None

    def to_dict(self) -> JsonObject:
        return {
            "runner": self.runner,
            "target": self.target,
            "workspace": self.workspace,
            "attempt_id": self.attempt_id,
            "claim_generation": self.claim_generation,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
            "terminated_confirmed": self.terminated_confirmed,
            "output_limit_bytes": self.output_limit_bytes,
            "timeout_seconds": self.timeout_seconds,
            "before_snapshot": self.before_snapshot,
            "after_snapshot": self.after_snapshot,
            "workspace_diff": self.workspace_diff,
            "evidence_id": self.evidence_id,
            "container_name": self.container_name,
            "config_digest": self.config_digest,
            "command_digest": self.command_digest,
            "host_grant_id": self.host_grant_id,
            "host_grant_issuer": self.host_grant_issuer,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TestExecutionEvidence":
        exit_code = data.get("exit_code")
        return cls(
            runner=str(data["runner"]),
            target=str(data["target"]),
            workspace=str(data["workspace"]),
            attempt_id=str(data["attempt_id"]),
            claim_generation=int(data["claim_generation"]),
            exit_code=int(exit_code) if exit_code is not None else None,
            started_at=str(data["started_at"]),
            finished_at=str(data["finished_at"]),
            duration_ms=float(data["duration_ms"]),
            timed_out=bool(data["timed_out"]),
            cancelled=bool(data["cancelled"]),
            truncated=bool(data["truncated"]),
            stdout_sha256=str(data["stdout_sha256"]),
            stderr_sha256=str(data["stderr_sha256"]),
            stdout_bytes=int(data["stdout_bytes"]),
            stderr_bytes=int(data["stderr_bytes"]),
            stdout_excerpt=str(data.get("stdout_excerpt", "")),
            stderr_excerpt=str(data.get("stderr_excerpt", "")),
            terminated_confirmed=bool(data["terminated_confirmed"]),
            output_limit_bytes=int(data["output_limit_bytes"]),
            timeout_seconds=float(data["timeout_seconds"]),
            before_snapshot=(
                dict(data["before_snapshot"])
                if data.get("before_snapshot")
                else None
            ),
            after_snapshot=(
                dict(data["after_snapshot"])
                if data.get("after_snapshot")
                else None
            ),
            workspace_diff=(
                dict(data["workspace_diff"])
                if data.get("workspace_diff")
                else None
            ),
            evidence_id=(
                str(data["evidence_id"])
                if data.get("evidence_id") is not None
                else None
            ),
            container_name=(
                str(data["container_name"])
                if data.get("container_name") is not None
                else None
            ),
            config_digest=(
                str(data["config_digest"])
                if data.get("config_digest") is not None
                else None
            ),
            command_digest=(
                str(data["command_digest"])
                if data.get("command_digest") is not None
                else None
            ),
            host_grant_id=(
                str(data["host_grant_id"])
                if data.get("host_grant_id") is not None
                else None
            ),
            host_grant_issuer=(
                str(data["host_grant_issuer"])
                if data.get("host_grant_issuer") is not None
                else None
            ),
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
    execution_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: str | None = None
    claim_generation: int = 0
    planner_mode: PlannerMode = PlannerMode.DETERMINISTIC
    planning_metadata: JsonObject = field(default_factory=dict)
    clarification_questions: list[str] = field(default_factory=list)
    conversation_id: str | None = None
    execution_started_at: str | None = None
    execution_deadline_at: str | None = None
    plan_version: int = 0
    plan_revisions: list[PlanRevision] = field(default_factory=list)
    cancel_requested_at: str | None = None
    cancel_requested_by: str | None = None
    cancel_reason: str | None = None
    execution_plan_version: int | None = None
    execution_plan_snapshot: JsonObject | None = None
    parent_task_id: str | None = None
    step_results: list[StepResult] = field(default_factory=list)
    current_step_id: str | None = None
    pending_approval_step_id: str | None = None
    approved_step_ids: list[str] = field(default_factory=list)
    # step_id -> digest of the exact step content that was approved. Approval
    # is a statement about content, not about an identifier (R-08).
    approved_step_digests: dict[str, str] = field(default_factory=dict)
    pause_requested_at: str | None = None
    metadata: JsonObject = field(default_factory=dict)

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
            "execution_owner": self.execution_owner,
            "lease_token": self.lease_token,
            "lease_expires_at": self.lease_expires_at,
            "conversation_id": self.conversation_id,
            "claim_generation": self.claim_generation,
            "execution_started_at": self.execution_started_at,
            "execution_deadline_at": self.execution_deadline_at,
            "plan_version": self.plan_version,
            "plan_revisions": [
                revision.to_dict() for revision in self.plan_revisions
            ],
            "cancel_requested_at": self.cancel_requested_at,
            "cancel_requested_by": self.cancel_requested_by,
            "cancel_reason": self.cancel_reason,
            "execution_plan_version": self.execution_plan_version,
            "execution_plan_snapshot": self.execution_plan_snapshot,
            "parent_task_id": self.parent_task_id,
            "step_results": [
                result.to_dict() for result in self.step_results
            ],
            "current_step_id": self.current_step_id,
            "pending_approval_step_id": self.pending_approval_step_id,
            "approved_step_ids": list(self.approved_step_ids),
            "approved_step_digests": dict(self.approved_step_digests),
            "pause_requested_at": self.pause_requested_at,
            "planner_mode": self.planner_mode.value,
            "planning_metadata": self.planning_metadata,
            "clarification_questions": list(self.clarification_questions),
            "metadata": dict(self.metadata),
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
            execution_owner=data.get("execution_owner"),
            lease_token=data.get("lease_token"),
            lease_expires_at=data.get("lease_expires_at"),
            conversation_id=(
                str(data["conversation_id"])
                if data.get("conversation_id") is not None
                else None
            ),
            claim_generation=int(data.get("claim_generation", 0)),
            execution_started_at=(
                str(data["execution_started_at"])
                if data.get("execution_started_at") is not None
                else None
            ),
            execution_deadline_at=(
                str(data["execution_deadline_at"])
                if data.get("execution_deadline_at") is not None
                else None
            ),
            plan_version=int(data.get("plan_version", 0)),
            plan_revisions=[
                PlanRevision.from_dict(item)
                for item in data.get("plan_revisions", [])
            ],
            cancel_requested_at=(
                str(data["cancel_requested_at"])
                if data.get("cancel_requested_at") is not None
                else None
            ),
            cancel_requested_by=(
                str(data["cancel_requested_by"])
                if data.get("cancel_requested_by") is not None
                else None
            ),
            cancel_reason=(
                str(data["cancel_reason"])
                if data.get("cancel_reason") is not None
                else None
            ),
            execution_plan_version=(
                int(data["execution_plan_version"])
                if data.get("execution_plan_version") is not None
                else None
            ),
            execution_plan_snapshot=(
                dict(data["execution_plan_snapshot"])
                if data.get("execution_plan_snapshot") is not None
                else None
            ),
            parent_task_id=(
                str(data["parent_task_id"])
                if data.get("parent_task_id") is not None
                else None
            ),
            step_results=[
                StepResult.from_dict(item)
                for item in data.get("step_results", [])
            ],
            current_step_id=(
                str(data["current_step_id"])
                if data.get("current_step_id") is not None
                else None
            ),
            pending_approval_step_id=(
                str(data["pending_approval_step_id"])
                if data.get("pending_approval_step_id") is not None
                else None
            ),
            approved_step_ids=[
                str(item) for item in data.get("approved_step_ids", [])
            ],
            approved_step_digests={
                str(key): str(value)
                for key, value in (
                    data.get("approved_step_digests", {}) or {}
                ).items()
            },
            pause_requested_at=(
                str(data["pause_requested_at"])
                if data.get("pause_requested_at") is not None
                else None
            ),
            planner_mode=PlannerMode(data.get("planner_mode", "deterministic")),
            planning_metadata=dict(data.get("planning_metadata", {})),
            clarification_questions=[
                str(item) for item in data.get("clarification_questions", [])
            ],
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(slots=True)
class Conversation:
    conversation_id: str
    status: ConversationStatus
    version: int
    created_at: str
    updated_at: str
    metadata: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "conversation_id": self.conversation_id,
            "status": self.status.value,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Conversation":
        return cls(
            conversation_id=str(data["conversation_id"]),
            status=ConversationStatus(data["status"]),
            version=int(data["version"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(slots=True)
class Message:
    message_id: str
    conversation_id: str
    turn_id: str
    role: MessageRole
    kind: str
    content: str
    content_sha256: str
    byte_count: int
    client_message_id: str | None
    created_at: str
    metadata: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "role": self.role.value,
            "kind": self.kind,
            "content": self.content,
            "content_sha256": self.content_sha256,
            "byte_count": self.byte_count,
            "client_message_id": self.client_message_id,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Message":
        return cls(
            message_id=str(data["message_id"]),
            conversation_id=str(data["conversation_id"]),
            turn_id=str(data["turn_id"]),
            role=MessageRole(data["role"]),
            kind=str(data["kind"]),
            content=str(data["content"]),
            content_sha256=str(data["content_sha256"]),
            byte_count=int(data["byte_count"]),
            client_message_id=(
                str(data["client_message_id"])
                if data.get("client_message_id") is not None
                else None
            ),
            created_at=str(data["created_at"]),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(slots=True)
class ConversationTurn:
    turn_id: str
    conversation_id: str
    turn_index: int
    status: TurnStatus
    intent_kind: str | None
    task_id: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonObject:
        return {
            "turn_id": self.turn_id,
            "conversation_id": self.conversation_id,
            "turn_index": self.turn_index,
            "status": self.status.value,
            "intent_kind": self.intent_kind,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConversationTurn":
        return cls(
            turn_id=str(data["turn_id"]),
            conversation_id=str(data["conversation_id"]),
            turn_index=int(data["turn_index"]),
            status=TurnStatus(data["status"]),
            intent_kind=(
                str(data["intent_kind"])
                if data.get("intent_kind") is not None
                else None
            ),
            task_id=(
                str(data["task_id"]) if data.get("task_id") is not None else None
            ),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class IntentClassification:
    kind: IntentKind
    reason: str
    target_task_id: str | None = None
    question_id: str | None = None
    requires_question_binding: bool = False
    requires_confirmation: bool = False

    def to_dict(self) -> JsonObject:
        return {
            "kind": self.kind.value,
            "reason": self.reason,
            "target_task_id": self.target_task_id,
            "question_id": self.question_id,
            "requires_question_binding": self.requires_question_binding,
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass(slots=True)
class ClarificationRequest:
    question_id: str
    conversation_id: str
    turn_id: str
    task_id: str
    field_name: str
    ordinal: int
    question_text: str
    status: ClarificationStatus
    created_at: str
    answered_at: str | None = None

    def to_dict(self) -> JsonObject:
        return {
            "question_id": self.question_id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "task_id": self.task_id,
            "field_name": self.field_name,
            "ordinal": self.ordinal,
            "question_text": self.question_text,
            "status": self.status.value,
            "created_at": self.created_at,
            "answered_at": self.answered_at,
        }


@dataclass(slots=True)
class ClarificationAnswer:
    answer_id: str
    question_id: str
    message_id: str
    conversation_id: str
    normalized_value: Any
    validation_status: ClarificationAnswerStatus
    created_at: str
    applied_at: str | None = None

    def to_dict(self) -> JsonObject:
        return {
            "answer_id": self.answer_id,
            "question_id": self.question_id,
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "normalized_value": self.normalized_value,
            "validation_status": self.validation_status.value,
            "created_at": self.created_at,
            "applied_at": self.applied_at,
        }


@dataclass(slots=True)
class ConversationProcessResult:
    outcome: str
    turn: ConversationTurn
    message: Message
    intent: IntentClassification
    task: Task | None = None
    clarifications: list[ClarificationRequest] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> JsonObject:
        return {
            "outcome": self.outcome,
            "turn": self.turn.to_dict(),
            "message": self.message.to_dict(),
            "intent": self.intent.to_dict(),
            "task": self.task.to_dict() if self.task else None,
            "clarifications": [
                item.to_dict() for item in self.clarifications
            ],
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class ClaimResult:
    acquired: bool
    task: Task
    reason: str


@dataclass(slots=True)
class PlanningOutcome:
    classification: PlanningClassification
    request: TaskRequest | None = None
    plan: Plan | None = None
    missing_fields: list[str] = field(default_factory=list)
    clarification_questions: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    metadata: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "classification": self.classification.value,
            "request": self.request.to_dict() if self.request else None,
            "plan": self.plan.to_dict() if self.plan else None,
            "missing_fields": list(self.missing_fields),
            "clarification_questions": list(self.clarification_questions),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": self.metadata,
        }
