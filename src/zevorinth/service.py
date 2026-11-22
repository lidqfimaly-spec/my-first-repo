"""Bounded artifact task orchestration with SQLite-backed execution leases."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
import subprocess
import time
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .artifact_tool import (
    MAX_ARTIFACT_BYTES,
    ArtifactToolError,
    ArtifactWriter,
    RealArtifactWriter,
    read_artifact,
    read_artifact_text,
    task_publication_lock,
    validate_artifact_name,
)
from .assurance import with_status_assurance
from .event_store import (
    ClaimLost,
    EventStore,
    EventStoreError,
    PlanRevisionConflict,
    StoreBusyError,
    TaskNotFound,
    lease_state,
)
from .lease_renewal import LeaseRenewer
from .models import (
    ArtifactEvidence,
    ArtifactReadEvidence,
    ClaimResult,
    Event,
    EventType,
    ExecutionMode,
    Task,
    TaskError,
    TaskRequest,
    TaskStatus,
    ToolResult,
    TestExecutionEvidence,
    VerificationResult,
    PlanningClassification,
    PlanningOutcome,
    Plan,
    PlanStep,
    StepResult,
    PlanRevision,
)
from .multi_step import (
    MULTI_STEP_OPERATION,
    MultiStepPlanError,
    resolve_step_parameters,
    step_approval_digest,
    step_is_approved,
    validate_plan_graph,
)
from .local_test_runner import (
    RUN_TESTS_TOOL,
    TestExecutionError,
    excerpt,
    resolve_workspace,
    run_unittest,
)
from .planner import (
    CONTROLLED_SHELL,
    EDIT_TEXT_FILE,
    INSPECT_WORKSPACE,
    MODIFY_TEXT_ARTIFACT,
    READ_TEXT_ARTIFACT,
    RESEARCH_REPORT,
    WRITE_TEXT_ARTIFACT,
    DeterministicPlanner,
    Planner,
)
from .policy import ArtifactPolicy, missing_required_fields
from .proposal_candidates import (
    ValidatedProposal,
    code_task_steps,
    research_task_parameters,
)
from .shell_tool import (
    CONTROLLED_SHELL_TOOL,
    ShellToolError,
    execute_controlled_shell,
    validate_shell_parameters,
)
from .retry_policy import (
    MAX_RETRY_ATTEMPTS,
    TaskRetryError,
    get_retry_decision,
    retry_request_digest,
)
from .state_machine import is_terminal
from .verifier import ArtifactVerifier, TestResultVerifier
from .task_lifecycle import audit_storage_error, audit_task_lifecycle
from .recovery_preview import build_recovery_preview, build_unavailable_preview
from .workspace_snapshot import (
    EffectPolicy,
    Snapshot,
    SnapshotError,
    diff_snapshots,
    scan_workspace,
)
from .workspace_effects import build_workspace_effects, validate_workspace_effects
from .workspace_cleanup import (
    CleanupDecision,
    CleanupError,
    _normalize_relative,
    execute_cleanup,
    plan_cleanup,
    reconcile_cleanup,
    summarize_batch,
)
from .workspace_edit import (
    WorkspaceEditError,
    apply_text_edit,
    validate_edit_precondition,
    verify_text_edit,
    workspace_file_digest,
    workspace_file_observation,
)
from .workspace_inspection import (
    WorkspaceInspectionError,
    inspect_workspace,
    verify_workspace_inspection,
)
from .research_workflow import (
    ResearchWorkflowError,
    build_research_report,
    verify_research_report,
)


LOGGER = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _task_request_digest(request: TaskRequest) -> str:
    encoded = json.dumps(
        request.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


FaultHook = Callable[[str, Task], None]
_ACTIVE_RENEWER: ContextVar[LeaseRenewer | None] = ContextVar(
    "zevorinth_active_lease_renewer",
    default=None,
)


class CancellationRequested(Exception):
    pass


class TaskClaimNotAcquired(RuntimeError):
    """Raised when a strict resume request could not acquire the task."""

    code = "task_claim_not_acquired"

    def __init__(
        self,
        task_id: str,
        reason: str,
        current_status: str,
    ) -> None:
        super().__init__(
            f"task {task_id} could not be resumed: {reason} "
            f"(status={current_status})"
        )
        self.reason = reason
        self.current_status = current_status


class PlanResumeRefused(RuntimeError):
    """A failed plan could not be continued safely, and says why.

    Every code here means "nothing was created and nothing ran". The refusal is
    the feature: re-running a step whose side effects are unknown, or silently
    re-running one that already succeeded, would be worse than stopping.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        step_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.step_id = step_id
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "step_id": self.step_id,
            "details": self.details,
        }


def _collection_status(
    required: list[Any], artifacts: list[dict[str, Any]]
) -> str:
    """``failed`` when any required artifact is missing from the collection."""
    present = {item["relative_path"] for item in artifacts}
    return "failed" if any(str(item) not in present for item in required) else "complete"


def _test_execution_plan_mismatch(
    task: Task,
    evidence: TestExecutionEvidence,
) -> str | None:
    """Compare the persisted final plan with the test result evidence."""
    snapshot = task.execution_plan_snapshot
    if task.execution_plan_version != task.plan_version:
        return "execution_plan_snapshot_mismatch"
    if not isinstance(snapshot, Mapping) or snapshot.get("kind") != "run_tests":
        return "execution_plan_snapshot_missing"
    expected = {
        "task_id": task.task_id,
        "attempt_id": evidence.attempt_id,
        "claim_generation": evidence.claim_generation,
        "plan_version": task.plan_version,
        "tool_name": RUN_TESTS_TOOL,
        "runner": evidence.runner,
        "target": evidence.target,
        "workspace": evidence.workspace,
        "evidence_id": evidence.evidence_id,
        "config_digest": evidence.config_digest,
        "command_digest": evidence.command_digest,
    }
    for field, value in expected.items():
        if snapshot.get(field) != value:
            return "execution_plan_snapshot_mismatch"
    return None


#: Non-secret failure context a runner may attach to a typed error. Only these
#: keys reach the event payload: a runner must not be able to smuggle arbitrary
#: data (or a secret) into the audit trail.
_RUNNER_FAILURE_CONTEXT_KEYS = (
    "host_grant_id",
    "host_grant_issuer",
    "spawn_error",
)


def _grant_failure_context(exc: TestExecutionError) -> dict[str, object]:
    """Whitelist the failure context a typed runner error may publish."""
    details = getattr(exc, "details", None) or {}
    if not isinstance(details, dict):
        return {}
    context: dict[str, object] = {}
    for key in _RUNNER_FAILURE_CONTEXT_KEYS:
        value = details.get(key)
        if isinstance(value, str) and value:
            context[key] = value
    return context


def _test_evidence_revalidation_reason(
    project_root: Path,
    task: Task,
    evidence: TestExecutionEvidence,
) -> str | None:
    """Re-read persisted isolation evidence before accepting test success."""
    if not evidence.evidence_id:
        return None
    from .evidence_store import EvidenceStore, EvidenceStoreError
    from .isolation import (
        ContainerBackendUnavailable,
        _live_image_digest,
        _live_runtime_identity,
    )

    snapshot = task.execution_plan_snapshot or {}
    image = str(snapshot.get("image") or "python:3.13-alpine")
    try:
        verdict = EvidenceStore(project_root).verify_stored_evidence(
            evidence.evidence_id,
            expected_runtime=_live_runtime_identity(),
            expected_image_digest=_live_image_digest(image),
            expected_config_digest=evidence.config_digest or "",
            expected_request_id=task.task_id,
            expected_command_digest=evidence.command_digest or "",
        )
    except EvidenceStoreError:
        return "evidence_revalidation_failed"
    except ContainerBackendUnavailable:
        # Cannot re-check the evidence because the runtime is gone; that is an
        # environment condition and must be reported as one.
        return "container_backend_unavailable"
    state = verdict.get("state")
    reasons = set(verdict.get("reasons") or [])
    if state == "verified":
        return None
    if "evidence_invalidated" in reasons or state == "invalidated":
        return "evidence_invalidated"
    if "evidence_expired" in reasons or state == "expired":
        return "evidence_expired"
    return "evidence_revalidation_failed"


class TaskService:
    """Single-host coordinator using SQLite transactions for task ownership."""

    RUNNABLE_STATUSES = frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.PAUSED,
            TaskStatus.INTERRUPTED,
            TaskStatus.RUNNING,
            TaskStatus.VERIFYING,
        }
    )
    RESUMABLE_STATUSES = frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.PAUSED,
            TaskStatus.AWAITING_CLARIFICATION,
            TaskStatus.BLOCKED,
            TaskStatus.INTERRUPTED,
            TaskStatus.RUNNING,
            TaskStatus.VERIFYING,
        }
    )

    def __init__(
        self,
        project_root: Path,
        *,
        writer: ArtifactWriter | None = None,
        planner: Planner | None = None,
        fault_hook: FaultHook | None = None,
        clock: Callable[[], str] = utc_now,
        lease_seconds: float = 60.0,
        max_execution_seconds: float = 600.0,
        lease_renewal_interval_seconds: float | None = None,
        publish_lock_timeout_seconds: float = 10.0,
        busy_timeout_ms: int = 10_000,
        owner_id: str | None = None,
        recover_leases: bool = True,
        host_grant_store: Any = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_execution_seconds <= 0:
            raise ValueError("max_execution_seconds must be positive")
        if (
            lease_renewal_interval_seconds is not None
            and lease_renewal_interval_seconds <= 0
        ):
            raise ValueError(
                "lease_renewal_interval_seconds must be positive"
            )
        if publish_lock_timeout_seconds <= 0:
            raise ValueError("publish_lock_timeout_seconds must be positive")
        self.project_root = project_root.resolve()
        self.artifacts_root = self.project_root / "artifacts"
        self.database_path = self.project_root / "data" / "zevorinth.sqlite3"
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.max_execution_seconds = max_execution_seconds
        self.lease_renewal_interval_seconds = (
            lease_renewal_interval_seconds
        )
        self.publish_lock_timeout_seconds = publish_lock_timeout_seconds
        self.owner_id = owner_id or str(uuid4())
        self.writer = writer or RealArtifactWriter(
            self.artifacts_root,
            publish_lock_timeout_seconds=publish_lock_timeout_seconds,
        )
        self.fault_hook = fault_hook
        self.planner = planner or DeterministicPlanner()
        self.policy = ArtifactPolicy(host_grant_store=host_grant_store)
        self.verifier = ArtifactVerifier(self.artifacts_root)
        self.test_verifier = TestResultVerifier()
        self.test_runner = run_unittest
        self.host_grant_store = host_grant_store
        self.container_popen = subprocess.Popen
        self.container_terminator = None
        self.store = EventStore(
            self.database_path,
            busy_timeout_ms=busy_timeout_ms,
        )
        self.store.initialize()
        if recover_leases:
            self.recover_expired_leases()

    def enqueue(
        self,
        request: TaskRequest,
        *,
        task_id: str | None = None,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        creation_preview: Mapping[str, Any] | None = None,
        parent_task_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Task:
        now = self.clock()
        resolved_metadata = dict(metadata or {})
        task = Task(
            task_id=task_id or str(uuid4()),
            status=TaskStatus.QUEUED,
            request=request,
            execution_mode=ExecutionMode.NOT_EXECUTED,
            attempt_id=str(uuid4()),
            phase="queued",
            planner_mode=self.planner.mode,
            created_at=now,
            updated_at=now,
            conversation_id=conversation_id,
            parent_task_id=parent_task_id,
            metadata=resolved_metadata,
        )
        created_payload: dict[str, Any] = {
            "operation": request.operation,
            "goal_present": bool(request.goal),
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "parent_task_id": parent_task_id,
            "request_digest": _task_request_digest(request),
            "creation_preview": (
                dict(creation_preview)
                if creation_preview is not None
                else None
            ),
        }
        if resolved_metadata:
            created_payload["metadata"] = dict(resolved_metadata)
        task, _ = self.store.create_task(
            task,
            Event.new(
                task.task_id,
                EventType.TASK_CREATED,
                now,
                created_payload,
            ),
            turn_id=turn_id,
        )
        return task

    def submit(
        self,
        request: TaskRequest,
        *,
        conversation_id: str | None = None,
        turn_id: str | None = None,
    ) -> Task:
        task = self.enqueue(
            request,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
        return self.execute_task(task.task_id)

    def retry_task(
        self,
        task_id: str,
        *,
        max_attempts: int = MAX_RETRY_ATTEMPTS,
        request_id: str | None = None,
    ) -> Task:
        """Retry a failed task, returning the resulting task.

        Returns the original task when the retry policy rejects it, and the
        (possibly pre-existing) retry task otherwise.
        """
        return self.retry_task_detailed(
            task_id,
            max_attempts=max_attempts,
            request_id=request_id,
        )["task"]

    def retry_task_detailed(
        self,
        task_id: str,
        *,
        max_attempts: int = MAX_RETRY_ATTEMPTS,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Retry a failed task with an optional idempotency key.

        Creates a NEW task with the same goal/operation/parameters. The
        original task remains in its terminal state. The new task records
        ``retry_of`` / ``retry_root`` / ``retry_attempt`` in its persisted
        metadata (serialized through ``task_json``, the canonical task
        persistence path).

        When ``request_id`` is supplied it becomes the retry task id, which
        makes task creation idempotent at the database level: a repeated
        request returns the already-created task instead of creating a
        second one, and a reused key with different content is rejected.
        """
        original = self.store.require_task(task_id)
        if request_id is not None and request_id == task_id:
            raise TaskRetryError(
                "retry_request_id_conflict",
                "request_id must not reuse the original task id",
            )
        request = TaskRequest(
            goal=original.request.goal,
            operation=original.request.operation,
            parameters=dict(original.request.parameters),
        )
        digest = retry_request_digest(task_id, request)
        if request_id is not None:
            existing = self.store.get_task(request_id)
            if existing is not None:
                return self._existing_retry_result(
                    existing, task_id, digest
                )
        tool_name = (
            original.plan.steps[0].tool_name
            if original.plan is not None and original.plan.steps
            else None
        )
        error_code = (
            original.error.code
            if original.error is not None
            else None
        )
        attempt_count = self._count_retry_attempts(task_id)
        decision = get_retry_decision(
            tool_name=tool_name,
            error_code=error_code,
            task_status=original.status.value,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
        )
        if not decision["retry"]:
            return {
                "retried": False,
                "already_exists": False,
                "reason": decision["reason"],
                "original_task_id": task_id,
                "task": original,
                "decision": decision,
            }
        metadata = {
            "retry_of": task_id,
            "retry_root": self._retry_root_for(task_id),
            "retry_attempt": attempt_count + 1,
            "retry_request_digest": digest,
            "retry_reason": decision["reason"],
        }
        try:
            queued = self.enqueue(
                request,
                task_id=request_id,
                metadata=metadata,
            )
        except (sqlite3.IntegrityError, EventStoreError) as exc:
            if isinstance(exc, EventStoreError) and not isinstance(
                exc.__cause__, sqlite3.IntegrityError
            ):
                raise
            concurrent = (
                self.store.get_task(request_id)
                if request_id is not None
                else None
            )
            if concurrent is None:
                raise
            return self._existing_retry_result(
                concurrent, task_id, digest
            )
        executed = self.execute_task(queued.task_id)
        return {
            "retried": True,
            "already_exists": False,
            "reason": decision["reason"],
            "original_task_id": task_id,
            "task_id": executed.task_id,
            "retry_attempt": metadata["retry_attempt"],
            "task": executed,
            "decision": decision,
        }

    def _existing_retry_result(
        self,
        existing: Task,
        original_task_id: str,
        digest: str,
    ) -> dict[str, Any]:
        """Resolve a repeated retry request against the stored task."""
        metadata = dict(existing.metadata or {})
        if (
            str(metadata.get("retry_of")) != original_task_id
            or metadata.get("retry_request_digest") != digest
        ):
            raise TaskRetryError(
                "retry_request_conflict",
                "request_id was already used for a different retry request",
            )
        return {
            "retried": False,
            "already_exists": True,
            "reason": "retry request_id already applied",
            "original_task_id": original_task_id,
            "task_id": existing.task_id,
            "retry_attempt": metadata.get("retry_attempt"),
            "task": existing,
        }

    def _retry_root_for(self, task_id: str) -> str:
        """Return the root of the retry chain that contains task_id."""
        try:
            task = self.store.get_task(task_id)
        except TaskNotFound:
            return task_id
        if task is None:
            return task_id
        metadata = dict(task.metadata or {})
        root = metadata.get("retry_root")
        return str(root) if root else task_id

    def _count_retry_attempts(self, task_id: str) -> int:
        """Count retry tasks already created for this retry chain.

        Retry metadata is persisted inside ``task_json`` (the canonical task
        serialization); the ``tasks`` table has no separate metadata column.
        """
        root = self._retry_root_for(task_id)
        db_path = self.store.database_path
        if not db_path.exists():
            return 0
        connection = sqlite3.connect(
            str(db_path), timeout=5.0, isolation_level=None,
        )
        try:
            rows = connection.execute(
                "SELECT task_json FROM tasks WHERE task_json LIKE ?",
                (f'%"retry_root": "{root}"%',),
            ).fetchall()
        finally:
            connection.close()
        count = 0
        for (task_json,) in rows:
            try:
                stored = json.loads(task_json)
            except (TypeError, ValueError):
                continue
            metadata = stored.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if metadata.get("retry_of") is None:
                continue
            if str(metadata.get("retry_root") or metadata.get("retry_of")) == root:
                count += 1
        return count

    def submit_plan(
        self,
        goal: str,
        steps: list[Mapping[str, Any]],
        *,
        failure_policy: str = "fail_fast",
        conversation_id: str | None = None,
        turn_id: str | None = None,
        creation_preview: Mapping[str, Any] | None = None,
    ) -> Task:
        """Submit one bounded structured multi-step plan."""
        request = TaskRequest(
            goal=goal,
            operation=MULTI_STEP_OPERATION,
            parameters={
                "steps": [dict(step) for step in steps],
                "failure_policy": failure_policy,
            },
        )
        task = self.enqueue(
            request,
            conversation_id=conversation_id,
            turn_id=turn_id,
            creation_preview=creation_preview,
        )
        return self.execute_task(task.task_id)

    def submit_code_proposal(
        self,
        proposal: ValidatedProposal,
        *,
        test_backend: str = "container",
        host_authorization: str | None = None,
        conversation_id: str | None = None,
        turn_id: str | None = None,
    ) -> Task:
        """Materialize a validated code candidate into the approval-gated flow."""
        return self.submit_plan(
            proposal.goal,
            code_task_steps(
                proposal,
                test_backend=test_backend,
                host_authorization=host_authorization,
            ),
            conversation_id=conversation_id,
            turn_id=turn_id,
            creation_preview={
                "proposal_provenance": dict(proposal.provenance),
            },
        )

    def submit_research_proposal(
        self,
        proposal: ValidatedProposal,
        *,
        conversation_id: str | None = None,
        turn_id: str | None = None,
    ) -> Task:
        """Materialize a validated research candidate after deterministic checks."""
        request = TaskRequest(
            goal=proposal.goal,
            operation=RESEARCH_REPORT,
            parameters=research_task_parameters(proposal),
        )
        task = self.enqueue(
            request,
            conversation_id=conversation_id,
            turn_id=turn_id,
            creation_preview={
                "proposal_provenance": dict(proposal.provenance),
            },
        )
        return self.execute_task(task.task_id)

    def submit_code_task(
        self,
        goal: str,
        *,
        workspace: str,
        path: str,
        old_text: str,
        new_text: str,
        test_target: str,
        test_backend: str = "container",
        host_authorization: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> Task:
        """Run one inspect -> authorized edit -> test code workflow."""
        from .workspace_edit import EDIT_AUTHORIZATION_TOKEN

        test_parameters: dict[str, Any] = {
            "runner": "unittest",
            "target": test_target,
            "workspace": workspace,
            "execution_backend": test_backend,
            "timeout_seconds": timeout_seconds,
        }
        if host_authorization is not None:
            test_parameters["host_authorization"] = host_authorization
        return self.submit_plan(
            goal,
            [
                {
                    "step_id": "inspect",
                    "tool_name": INSPECT_WORKSPACE,
                    "expected_result": f"inspect {path} in workspace {workspace}",
                    "parameters": {
                        "workspace": workspace,
                        "path": path,
                    },
                },
                {
                    "step_id": "edit",
                    "tool_name": EDIT_TEXT_FILE,
                    "expected_result": f"edit exactly one occurrence in {path}",
                    "depends_on": ["inspect"],
                    "requires_approval": True,
                    "parameters": {
                        "workspace": workspace,
                        "path": path,
                        "expected_sha256": "${inspect.selected_sha256}",
                        "old_text": old_text,
                        "new_text": new_text,
                        "authorization": EDIT_AUTHORIZATION_TOKEN,
                    },
                },
                {
                    "step_id": "test",
                    "tool_name": RUN_TESTS_TOOL,
                    "expected_result": (
                        f"run approved test {test_target} in workspace {workspace}"
                    ),
                    "depends_on": ["edit"],
                    "parameters": test_parameters,
                },
            ],
            failure_policy="fail_fast",
        )

    def request_pause(
        self,
        task_id: str,
        *,
        requested_by: str,
        reason: str | None = None,
    ) -> Task:
        """Request a cooperative pause at the next multi-step boundary."""
        task = self.store.require_task(task_id)
        if task.status is TaskStatus.PAUSED:
            return task
        if task.status not in {
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.AWAITING_APPROVAL,
        }:
            raise PlanRevisionConflict(
                "only queued, running, or approval-waiting tasks can pause"
            )
        if task.pause_requested_at is not None:
            return task
        now = self.clock()
        if task.status is TaskStatus.QUEUED:
            paused, _ = self.store.transition(
                task_id,
                TaskStatus.PAUSED,
                EventType.TASK_PAUSED,
                now,
                payload={
                    "requested_by": requested_by,
                    "reason": reason,
                    "boundary": "before_execution",
                },
                updates={
                    "pause_requested_at": now,
                    "phase": "paused_before_execution",
                },
                require_no_lease=True,
                expected_updated_at=task.updated_at,
                expected_plan_version=task.plan_version,
                expected_status=TaskStatus.QUEUED,
            )
            return paused
        if task.status is TaskStatus.AWAITING_APPROVAL:
            latest = self.store.expire_claim(task_id, now=now)
            if latest.status is not TaskStatus.AWAITING_APPROVAL:
                raise PlanRevisionConflict(
                    "task status changed before pause committed"
                )
            paused, _ = self.store.transition(
                task_id,
                TaskStatus.PAUSED,
                EventType.TASK_PAUSED,
                now,
                payload={
                    "requested_by": requested_by,
                    "reason": reason,
                    "boundary": "before_approval",
                },
                updates={
                    "pause_requested_at": now,
                    "phase": "paused_before_approval",
                },
                require_no_lease=True,
                expected_updated_at=latest.updated_at,
                expected_plan_version=latest.plan_version,
                expected_status=TaskStatus.AWAITING_APPROVAL,
            )
            return paused
        updated, _ = self.store.record_event_and_update(
            task_id,
            EventType.PAUSE_REQUESTED,
            now,
            payload={
                "requested_by": requested_by,
                "reason": reason,
            },
            updates={
                "pause_requested_at": now,
                "phase": "pause_requested",
            },
            expected_updated_at=task.updated_at,
            expected_plan_version=task.plan_version,
            expected_status=TaskStatus.RUNNING,
        )
        return updated

    @staticmethod
    def _plan_step(task: Task, step_id: str) -> PlanStep:
        if task.plan is None:
            raise PlanRevisionConflict("task has no plan")
        for step in task.plan.steps:
            if step.step_id == step_id:
                return step
        raise PlanRevisionConflict("approval step is not in the plan")

    def approve_step(
        self,
        task_id: str,
        step_id: str,
        *,
        approved: bool,
        expected_plan_version: int | None = None,
    ) -> Task:
        """Approve or reject the exact pending multi-step approval."""
        current = self.store.require_task(task_id)
        if current.status is not TaskStatus.AWAITING_APPROVAL:
            raise PlanRevisionConflict("task is not awaiting approval")
        if current.pending_approval_step_id != step_id:
            raise PlanRevisionConflict("approval step does not match")
        claim = self._claim(
            task_id,
            allowed=frozenset({TaskStatus.AWAITING_APPROVAL}),
            expected_plan_version=expected_plan_version,
        )
        if not claim.acquired:
            return claim.task
        lease_token = claim.task.lease_token
        assert lease_token is not None
        if claim.task.pending_approval_step_id != step_id:
            self.store.release_claim(
                task_id,
                lease_token=lease_token,
                now=self.clock(),
                payload={
                    "reason": "approval_identity_changed",
                    "expected_step_id": step_id,
                    "observed_step_id": claim.task.pending_approval_step_id,
                },
            )
            raise PlanRevisionConflict(
                "approval step changed before the claim committed"
            )
        cancelled = self._cancel_if_requested(
            claim.task,
            lease_token,
            checkpoint="before_approval",
            side_effect_observed=False,
        )
        if cancelled is not None:
            return cancelled
        if not approved:
            rejected, _ = self.store.transition(
                task_id,
                TaskStatus.CANCELLED,
                EventType.TASK_CANCELLED,
                self.clock(),
                payload={
                    "code": "approval_rejected",
                    "step_id": step_id,
                },
                updates={
                    **self._clear_claim_updates(),
                    "pending_approval_step_id": None,
                    "current_step_id": None,
                    "phase": "approval_rejected",
                    "error": TaskError(
                        "approval_rejected",
                        "multi-step execution approval was rejected",
                    ),
                },
                lease_token=lease_token,
            )
            return rejected
        approved_ids = list(claim.task.approved_step_ids)
        if step_id not in approved_ids:
            approved_ids.append(step_id)
        # Record what was approved, not only which id. Without this the next
        # replan could keep the id and change the parameters (R-08).
        approved_digests = dict(claim.task.approved_step_digests or {})
        approved_digests[step_id] = step_approval_digest(
            self._plan_step(claim.task, step_id),
            claim.task.plan.plan_id,
        )
        running, _ = self.store.transition(
            task_id,
            TaskStatus.RUNNING,
            EventType.STEP_APPROVED,
            self.clock(),
            payload={
                "step_id": step_id,
                "step_digest": approved_digests[step_id],
            },
            updates={
                "approved_step_ids": approved_ids,
                "approved_step_digests": approved_digests,
                "pending_approval_step_id": None,
                "phase": "step_approved",
                "error": None,
            },
            lease_token=lease_token,
        )
        return self._run_with_renewal(
            running,
            lease_token,
            lambda: self._execute_multi_step_plan(running, lease_token),
        )

    def replan_pending_steps(
        self,
        task_id: str,
        steps: list[Mapping[str, Any]],
        *,
        expected_plan_version: int,
    ) -> Task:
        """Replace only unfinished steps while preserving completed evidence."""
        self.store.expire_claim(task_id, now=self._normalize_time(self.clock()))
        task = self.store.require_task(task_id)
        if task.plan is None:
            raise PlanRevisionConflict("task has no plan to revise")
        if task.plan_version != expected_plan_version:
            raise PlanRevisionConflict("task plan version changed")
        if task.status not in {
            TaskStatus.PAUSED,
            TaskStatus.AWAITING_APPROVAL,
        }:
            raise PlanRevisionConflict(
                "only paused or approval-waiting plans can be replanned"
            )
        if task.lease_token is not None:
            raise PlanRevisionConflict("task still owns an execution lease")
        completed = [
            result.step_id
            for result in task.step_results
            if result.status == "succeeded"
        ]
        try:
            new_steps = [PlanStep.from_dict(dict(item)) for item in steps]
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanRevisionConflict("replacement steps are malformed") from exc
        if set(completed).intersection(step.step_id for step in new_steps):
            raise PlanRevisionConflict(
                "completed steps cannot be replaced"
            )
        completed_steps = [
            step
            for step in task.plan.steps
            if step.step_id in completed
        ]
        revised = Plan(
            plan_id=task.plan.plan_id,
            planner_name=task.plan.planner_name,
            deterministic=task.plan.deterministic,
            steps=[*completed_steps, *new_steps],
            failure_policy=task.plan.failure_policy,
        )
        try:
            validate_plan_graph(revised, completed_step_ids=completed)
        except MultiStepPlanError as exc:
            raise PlanRevisionConflict(exc.message) from exc
        # Approvals survive a replan only where the approved content is still
        # the content in the plan. Anything redefined under the same step id
        # loses its approval and will be asked again at execution time (R-08).
        by_id = {step.step_id: step for step in revised.steps}
        kept_approvals: list[str] = []
        kept_digests: dict[str, str] = {}
        for approved_id in task.approved_step_ids:
            candidate = by_id.get(approved_id)
            if candidate is None:
                continue
            digest = step_approval_digest(candidate, revised.plan_id)
            if (task.approved_step_digests or {}).get(approved_id) == digest:
                kept_approvals.append(approved_id)
                kept_digests[approved_id] = digest
        new_version = task.plan_version + 1
        target_status = (
            TaskStatus.PAUSED
            if task.status is TaskStatus.AWAITING_APPROVAL
            else task.status
        )
        if target_status is task.status:
            updated, _ = self.store.record_event_and_update(
                task_id,
                EventType.STEPS_REPLANNED,
                self.clock(),
                payload={
                    "from_version": task.plan_version,
                    "to_version": new_version,
                    "completed_step_ids": completed,
                },
                updates={
                    "plan": revised,
                    "plan_version": new_version,
                    "pending_approval_step_id": None,
                    "current_step_id": None,
                    "approved_step_ids": kept_approvals,
                    "approved_step_digests": kept_digests,
                    "phase": "steps_replanned",
                },
                require_no_lease=True,
                expected_updated_at=task.updated_at,
                expected_plan_version=expected_plan_version,
                expected_status=TaskStatus.PAUSED,
            )
            return updated
        updated, _ = self.store.transition(
            task_id,
            target_status,
            EventType.STEPS_REPLANNED,
            self.clock(),
            payload={
                "from_version": task.plan_version,
                "to_version": new_version,
                "completed_step_ids": completed,
            },
            updates={
                "plan": revised,
                "plan_version": new_version,
                "pending_approval_step_id": None,
                "current_step_id": None,
                "approved_step_ids": kept_approvals,
                "approved_step_digests": kept_digests,
                "phase": "steps_replanned",
            },
            require_no_lease=True,
            expected_updated_at=task.updated_at,
            expected_plan_version=expected_plan_version,
            expected_status=TaskStatus.AWAITING_APPROVAL,
        )
        return updated

    def execute_task(
        self,
        task_id: str,
        *,
        require_claim: bool = False,
        claim_metadata: Mapping[str, Any] | None = None,
        expected_plan_version: int | None = None,
        preclaimed_lease_token: str | None = None,
    ) -> Task:
        task = self.store.require_task(task_id)
        if is_terminal(task.status):
            if require_claim:
                raise TaskClaimNotAcquired(
                    task_id,
                    "task_already_terminal",
                    task.status.value,
                )
            return task
        if preclaimed_lease_token is not None:
            if (
                expected_plan_version is not None
                and task.plan_version != expected_plan_version
            ):
                raise PlanRevisionConflict(
                    "task plan version changed before preclaimed execution"
                )
            if task.lease_token != preclaimed_lease_token:
                raise TaskClaimNotAcquired(
                    task_id,
                    "preclaimed_lease_not_owned",
                    task.status.value,
                )
            return self._run_claimed(task, preclaimed_lease_token)
        claim_options: dict[str, Any] = {}
        if claim_metadata is not None:
            claim_options["metadata"] = claim_metadata
        if expected_plan_version is not None:
            claim_options["expected_plan_version"] = expected_plan_version
        claim = self._claim(
            task_id,
            allowed=self.RUNNABLE_STATUSES,
            **claim_options,
        )
        if not claim.acquired:
            if require_claim:
                raise TaskClaimNotAcquired(
                    task_id,
                    claim.reason,
                    claim.task.status.value,
                )
            return claim.task
        return self._run_claimed(claim.task, claim.task.lease_token)

    def resume(
        self,
        task_id: str,
        request_patch: Mapping[str, object] | None = None,
        *,
        require_claim: bool = False,
        expected_plan_version: int | None = None,
        preclaimed_lease_token: str | None = None,
    ) -> Task:
        task = self.store.require_task(task_id)
        if is_terminal(task.status):
            if require_claim:
                raise TaskClaimNotAcquired(
                    task_id,
                    "task_already_terminal",
                    task.status.value,
                )
            return task
        if task.status is TaskStatus.BLOCKED and request_patch is None:
            if require_claim:
                raise TaskClaimNotAcquired(
                    task_id,
                    "clarification_patch_required",
                    task.status.value,
                )
            return task

        updated_request = task.request
        if task.status in {
            TaskStatus.AWAITING_CLARIFICATION,
            TaskStatus.BLOCKED,
        } and request_patch is not None:
            unknown = set(request_patch) - {"goal", "operation", "parameters"}
            if unknown:
                raise ValueError(f"unsupported request fields: {sorted(unknown)}")
            updated_request = task.request.merged(request_patch)

        if preclaimed_lease_token is not None:
            claimed = self.store.require_task(task_id)
            if (
                expected_plan_version is not None
                and claimed.plan_version != expected_plan_version
            ):
                raise PlanRevisionConflict(
                    "task plan version changed before preclaimed resume"
                )
            if claimed.lease_token != preclaimed_lease_token:
                raise TaskClaimNotAcquired(
                    task_id,
                    "preclaimed_lease_not_owned",
                    claimed.status.value,
                )
            lease_token = preclaimed_lease_token
        else:
            claim_options: dict[str, Any] = {}
            if expected_plan_version is not None:
                claim_options["expected_plan_version"] = expected_plan_version
            claim = self._claim(
                task_id,
                allowed=self.RESUMABLE_STATUSES,
                **claim_options,
            )
            if not claim.acquired:
                if require_claim:
                    raise TaskClaimNotAcquired(
                        task_id,
                        claim.reason,
                        claim.task.status.value,
                    )
                return claim.task
            claimed = claim.task
            lease_token = claimed.lease_token
        assert lease_token is not None

        if claimed.status in {
            TaskStatus.AWAITING_CLARIFICATION,
            TaskStatus.BLOCKED,
        }:
            missing = missing_required_fields(updated_request)
            if missing:
                remains_blocked = claimed.status is TaskStatus.BLOCKED
                updated, _ = self.store.record_event_and_update(
                    claimed.task_id,
                    EventType.CLARIFICATION_UPDATED,
                    self.clock(),
                    payload={
                        "missing_fields": missing,
                        "provided_parameters": (
                            dict(request_patch.get("parameters", {}))
                            if isinstance(request_patch, Mapping)
                            and isinstance(request_patch.get("parameters"), Mapping)
                            else {}
                        ),
                    },
                    updates={
                        "request": updated_request,
                        "missing_fields": missing,
                        "phase": (
                            "blocked_clarification_incomplete"
                            if remains_blocked
                            else "awaiting_clarification"
                        ),
                        "error": (
                            TaskError(
                                "clarification_incomplete",
                                "request is still missing required fields",
                            )
                            if remains_blocked
                            else claimed.error
                        ),
                    },
                    lease_token=lease_token,
                )
                updated, _ = self.store.release_claim(
                    updated.task_id,
                    lease_token=lease_token,
                    now=self.clock(),
                    payload={"reason": "clarification_incomplete"},
                )
                return updated
            claimed, _ = self.store.transition(
                claimed.task_id,
                TaskStatus.QUEUED,
                EventType.CLARIFICATION_UPDATED,
                self.clock(),
                payload={
                    "missing_fields": [],
                    "provided_parameters": (
                        dict(request_patch.get("parameters", {}))
                        if isinstance(request_patch, Mapping)
                        and isinstance(request_patch.get("parameters"), Mapping)
                        else {}
                    ),
                },
                updates={
                    "request": updated_request,
                    "missing_fields": [],
                    "error": None,
                    "phase": "clarification_completed",
                },
                lease_token=lease_token,
            )

        return self._run_claimed(claimed, lease_token)

    RESUMABLE_FAILURE_STATUSES = frozenset(
        {TaskStatus.PARTIAL, TaskStatus.FAILED}
    )

    def resume_failed_plan(
        self,
        task_id: str,
        *,
        expected_plan_version: int | None = None,
        conversation_id: str | None = None,
        turn_id: str | None = None,
    ) -> Task:
        """Continue a failed multi-step plan without redoing finished work.

        Why a *new* task instead of reopening the failed one: ``PARTIAL`` and
        ``FAILED`` are terminal and the lifecycle audit rejects a
        ``task_partial`` event on a task that is not ``PARTIAL`` (and a second
        terminal event of the same kind). Reopening in place would therefore
        corrupt the audit trail of every resumed plan. The parent keeps its
        verdict and its evidence; the continuation is a fresh plan holding only
        the steps that still need to run.

        Three properties this keeps:

        * **No blind retry.** A step that may have produced a side effect is
          refused, not retried. The code is returned so the caller can ask a
          human.
        * **No stale inputs.** Parameters referencing an earlier step are
          materialised from that step's recorded output, so a resumed step's
          approval covers concrete content rather than ``${s1.content}``.
        * **No inherited approval.** The new task starts with no approvals, so
          every step that wants one is asked again.
        """
        parent = self.store.require_task(task_id)
        if parent.status not in self.RESUMABLE_FAILURE_STATUSES:
            raise PlanResumeRefused(
                "task_not_resumable",
                "only a partial or failed plan can be continued",
                details={"status": parent.status.value},
            )
        if parent.cancel_requested_at is not None:
            raise PlanResumeRefused(
                "task_cancel_requested",
                "a plan with a cancellation request cannot be continued",
                details={"cancel_requested_at": parent.cancel_requested_at},
            )
        now = self.clock()
        if lease_state(parent.lease_token, parent.lease_expires_at, now) != "none":
            raise PlanResumeRefused(
                "task_claim_active",
                "the plan still owns an execution lease",
            )
        if (
            expected_plan_version is not None
            and parent.plan_version != expected_plan_version
        ):
            raise PlanResumeRefused(
                "task_plan_version_changed",
                "the plan version changed before the continuation started",
                details={
                    "expected": expected_plan_version,
                    "observed": parent.plan_version,
                },
            )
        plan = parent.plan
        if plan is None:
            raise PlanResumeRefused(
                "plan_missing", "the plan record is missing"
            )
        results = {item.step_id: item for item in parent.step_results}
        carried = [
            step.step_id
            for step in plan.steps
            if results.get(step.step_id) is not None
            and results[step.step_id].status == "succeeded"
        ]
        remaining = [
            step for step in plan.steps if step.step_id not in carried
        ]
        if not remaining:
            raise PlanResumeRefused(
                "no_failed_steps",
                "every step already succeeded; there is nothing to continue",
            )
        for step in remaining:
            recorded = results.get(step.step_id)
            if recorded is None:
                continue
            if not self._step_attempt_left_no_side_effect(
                recorded,
                plan_step=step,
                parent_task_id=parent.task_id,
            ):
                raise PlanResumeRefused(
                    "step_side_effect_uncertain",
                    (
                        "a step may already have produced a side effect; "
                        "continuing would risk doing it twice"
                    ),
                    step_id=step.step_id,
                    details={
                        "step_status": recorded.status,
                        "child_task_id": recorded.child_task_id,
                    },
                )
        outputs = {
            step_id: dict(results[step_id].output or {})
            for step_id in carried
        }
        resume_steps: list[PlanStep] = []
        for step in remaining:
            try:
                parameters = resolve_step_parameters(step.parameters, outputs)
            except MultiStepPlanError as exc:
                raise PlanResumeRefused(
                    exc.code,
                    exc.message,
                    step_id=step.step_id,
                ) from exc
            resume_steps.append(
                PlanStep(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    expected_result=step.expected_result,
                    parameters=parameters,
                    # Dependencies already satisfied by carried steps are
                    # dropped: their outputs are baked into the parameters.
                    depends_on=[
                        dependency
                        for dependency in step.depends_on
                        if dependency not in carried
                    ],
                    requires_approval=step.requires_approval,
                    max_attempts=step.max_attempts,
                    retry_on_not_executed=step.retry_on_not_executed,
                )
            )
        resume_plan = Plan(
            # Validation only: the persisted plan is rebuilt from the request
            # parameters at planning time, so this identity is never reused.
            plan_id=str(uuid4()),
            planner_name=plan.planner_name,
            deterministic=plan.deterministic,
            steps=resume_steps,
            failure_policy=plan.failure_policy,
        )
        try:
            validate_plan_graph(resume_plan)
        except MultiStepPlanError as exc:
            raise PlanResumeRefused(
                exc.code, exc.message
            ) from exc
        continuation = self.enqueue(
            TaskRequest(
                goal=f"{parent.request.goal} (从失败处继续)",
                operation=MULTI_STEP_OPERATION,
                parameters={
                    "steps": [step.to_dict() for step in resume_steps],
                    "failure_policy": plan.failure_policy,
                },
            ),
            conversation_id=conversation_id or parent.conversation_id,
            turn_id=turn_id,
            creation_preview={
                "resume_of": parent.task_id,
                "resume_parent_status": parent.status.value,
                "resume_plan_version": parent.plan_version,
                "resumed_step_ids": [step.step_id for step in remaining],
                "carried_step_ids": carried,
            },
            metadata={"resume_of": parent.task_id},
        )
        # The parent's verdict does not change; its history records that the
        # work continued elsewhere. A note is an existing, audited event and
        # does not disturb the terminal state the audit expects.
        try:
            self.store.record_event_and_update(
                parent.task_id,
                EventType.TASK_NOTE_ADDED,
                now,
                payload={
                    "kind": "plan_continuation",
                    "message": (
                        "continued as task "
                        f"{continuation.task_id} from step(s): "
                        + ", ".join(step.step_id for step in remaining)
                    ),
                    "continuation_task_id": continuation.task_id,
                    "resumed_step_ids": [
                        step.step_id for step in remaining
                    ],
                    "carried_step_ids": carried,
                },
                updates={},
                require_no_lease=True,
                expected_updated_at=parent.updated_at,
                expected_plan_version=parent.plan_version,
                expected_status=parent.status,
            )
        except (PlanRevisionConflict, ValueError):
            # The continuation exists and is the authoritative record; failing
            # to annotate the parent must not undo it. The link is still
            # visible from the child via `parent_task_id` / creation_preview.
            LOGGER.warning(
                "plan continuation created without parent note task_id=%s",
                parent.task_id,
            )
        # Start it here, for the same reason ``submit_plan`` does: the control
        # plane deliberately cannot start ``execute_plan``, so an enqueued
        # continuation would have no one to start it and would sit in QUEUED
        # forever.
        return self.execute_task(continuation.task_id)

    def _step_attempt_left_no_side_effect(
        self,
        result: StepResult,
        *,
        plan_step: PlanStep,
        parent_task_id: str,
    ) -> bool:
        """True only when the recorded attempt provably did not touch anything.

        Three levels of evidence, weakest last:

        1. ``skipped`` -- the step never had an attempt at all.
        2. no child task exists. The child id is generated and announced
           (``step_started``) before the tool is called, but the child *record*
           is only written once the step gets far enough to execute. A step
           that failed while resolving its parameters therefore has an id in
           the event stream and no task in the store, which is proof that no
           tool ran. This bucket stays resumable: refusing it would report a
           side-effect risk for work that provably never happened.
        3. the child exists -> look at what it actually did. Anything other
           than "not executed, no artifact, never reached ``tool_started``" is
           treated as unknown, and unknown is refused, not retried.

        An explicit ``child_task_id`` that no longer resolves is refused even
        though level 2 would call it clean: a recorded child that vanished
        means something did exist, and "it is gone now" is not proof it never
        wrote anything.
        """
        if result.status == "skipped":
            return True
        if result.child_task_id is not None:
            child = self.store.get_task(result.child_task_id)
            if child is None:
                return False
        else:
            announced = self._announced_child_id(
                parent_task_id, plan_step.step_id
            )
            if announced is None:
                return True
            child = self.store.get_task(announced)
            if child is None:
                # Announced but never persisted: the step died before a child
                # existed, so nothing could have run.
                return True
        if child.execution_mode is not ExecutionMode.NOT_EXECUTED:
            return False
        if child.artifact is not None:
            return False
        return not any(
            event.event_type == EventType.TOOL_STARTED.value
            for event in self.store.list_events(child.task_id)
        )

    def _announced_child_id(
        self, parent_task_id: str, step_id: str
    ) -> str | None:
        """Child id from the last ``step_started`` recorded for this step."""
        found: str | None = None
        for event in self.store.list_events(parent_task_id):
            if event.event_type != EventType.STEP_STARTED.value:
                continue
            payload = event.payload.get("step_result") or {}
            if payload.get("step_id") != step_id:
                continue
            child_id = payload.get("child_task_id")
            found = str(child_id) if child_id else found
        return found

    def reserve_control_action(
        self,
        task_id: str,
        *,
        request_id: str,
        action: str,
        request_digest: str,
        expected_updated_at: str,
        expected_plan_version: int,
        payload: Mapping[str, Any],
        require_claim: bool,
        allowed_statuses: frozenset[TaskStatus],
    ) -> dict[str, Any]:
        occurred_at = self._normalize_time(self.clock())
        if not require_claim:
            return self.store.reserve_control_action(
                task_id,
                request_id=request_id,
                action=action,
                request_digest=request_digest,
                expected_updated_at=expected_updated_at,
                expected_plan_version=expected_plan_version,
                occurred_at=occurred_at,
                payload=payload,
            )
        return self.store.reserve_control_action_with_claim(
            task_id,
            request_id=request_id,
            action=action,
            request_digest=request_digest,
            expected_updated_at=expected_updated_at,
            expected_plan_version=expected_plan_version,
            occurred_at=occurred_at,
            payload=payload,
            owner_id=self.owner_id,
            lease_token=str(uuid4()),
            attempt_id=str(uuid4()),
            expires_at=self._lease_deadline(occurred_at),
            allowed_statuses=allowed_statuses,
            max_execution_seconds=self.max_execution_seconds,
        )

    def inspect_control_action(
        self,
        task_id: str,
        *,
        request_id: str,
        action: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        return self.store.inspect_control_action(
            task_id,
            request_id=request_id,
            action=action,
            request_digest=request_digest,
        )

    def recover_prepublish_publish_lock_failure(
        self,
        task_id: str,
        *,
        authorized: bool = False,
    ) -> dict[str, Any]:
        """Retry one pre-publication lock timeout as a deterministic new task."""
        source = self.store.require_task(task_id)
        if not authorized:
            return self._prepublish_recovery_report(
                source,
                status="rejected",
                code="recovery_unauthorized",
                reason="explicit recovery authorization is required",
            )
        rejected = self._prepublish_recovery_precheck(source)
        if rejected is not None:
            code, reason = rejected
            return self._prepublish_recovery_report(
                source,
                status="rejected",
                code=code,
                reason=reason,
            )

        recovery_kind = "prepublish_publish_lock_timeout"
        replacement_id = str(
            uuid5(
                NAMESPACE_URL,
                f"zevorinth:{source.task_id}:{recovery_kind}",
            )
        )
        existing = self.store.get_task(replacement_id)
        reused = existing is not None
        if existing is None:
            now = self.clock()
            replacement = Task(
                task_id=replacement_id,
                status=TaskStatus.QUEUED,
                request=source.request,
                execution_mode=ExecutionMode.NOT_EXECUTED,
                attempt_id=str(uuid4()),
                phase="queued_prepublish_recovery",
                planner_mode=source.planner_mode,
                created_at=now,
                updated_at=now,
            )
            event = Event.new(
                replacement.task_id,
                EventType.TASK_CREATED,
                now,
                {
                    "operation": source.request.operation,
                    "goal_present": bool(source.request.goal),
                    "recovery": {
                        "kind": recovery_kind,
                        "source_task_id": source.task_id,
                        "source_error_code": source.error.code,
                        "source_attempt_id": source.attempt_id,
                    },
                },
            )
            try:
                replacement, _ = self.store.create_task(replacement, event)
            except (sqlite3.IntegrityError, EventStoreError) as exc:
                if isinstance(exc, EventStoreError) and not isinstance(
                    exc.__cause__, sqlite3.IntegrityError
                ):
                    raise
                replacement = self.store.get_task(replacement_id)
                if replacement is None:
                    raise
                reused = True
        else:
            replacement = existing

        if not self._is_expected_recovery_task(source, replacement, recovery_kind):
            return self._prepublish_recovery_report(
                source,
                status="rejected",
                code="recovery_identity_conflict",
                reason="deterministic recovery task has unexpected provenance",
            )
        if not is_terminal(replacement.status):
            replacement = self.execute_task(replacement.task_id)
        if replacement.status is TaskStatus.SUCCEEDED:
            status = (
                "already_recovered"
                if reused
                else "recovered"
            )
        elif replacement.status is TaskStatus.FAILED:
            status = "recovery_failed"
        else:
            status = "recovery_in_progress"
        return self._prepublish_recovery_report(
            source,
            status=status,
            code=(
                "recovery_task_succeeded"
                if replacement.status is TaskStatus.SUCCEEDED
                else f"recovery_task_{replacement.status.value}"
            ),
            reason=(
                "replacement task independently verified"
                if replacement.status is TaskStatus.SUCCEEDED
                else "replacement task did not succeed"
            ),
            replacement=replacement,
        )

    def _prepublish_recovery_precheck(
        self,
        task: Task,
    ) -> tuple[str, str] | None:
        if task.status is not TaskStatus.FAILED:
            return "recovery_status_not_eligible", "source task is not failed"
        if task.request.operation != WRITE_TEXT_ARTIFACT:
            return "recovery_operation_not_eligible", "source operation is not write_text_artifact"
        if task.error is None or task.error.code != "publish_lock_timeout":
            return "recovery_error_not_eligible", "source error is not publish_lock_timeout"
        result = task.tool_result
        if (
            result is None
            or result.success
            or result.error_code != "publish_lock_timeout"
            or result.execution_mode is not ExecutionMode.REAL
            or result.attempt_id != task.attempt_id
        ):
            return "recovery_evidence_not_eligible", "tool result does not prove a pre-publication lock failure"
        if task.artifact is not None or task.verification is not None:
            return "recovery_side_effect_present", "source task has artifact or verification evidence"
        events = self.store.list_events(task.task_id)
        event_types = [event.event_type for event in events]
        if "tool_succeeded" in event_types or "verification_started" in event_types:
            return "recovery_side_effect_present", "source events do not prove pre-publication failure"
        matching_failure = any(
            event.event_type == "tool_failed"
            and isinstance(event.payload.get("tool_result"), Mapping)
            and event.payload["tool_result"].get("attempt_id") == task.attempt_id
            for event in events
        )
        if not matching_failure:
            return "recovery_evidence_not_eligible", "no matching tool_failed event"
        artifact_path = task.request.parameters.get("artifact_path")
        try:
            name = validate_artifact_name(artifact_path)
        except ArtifactToolError as exc:
            return "recovery_target_not_eligible", exc.message
        root = self.artifacts_root
        if root.is_symlink():
            return "recovery_target_not_eligible", "artifacts root is a symlink"
        if root.exists():
            root = root.resolve(strict=True)
        task_dir = root / task.task_id
        if task_dir.is_symlink():
            return "recovery_target_not_eligible", "task artifact directory is a symlink"
        if task_dir.exists() and not task_dir.is_dir():
            return "recovery_target_not_eligible", "task artifact path is not a directory"
        target = task_dir / name
        if target.exists() or target.is_symlink():
            return "recovery_target_exists", "artifact target already exists"
        staging = task_dir / ".staging"
        if staging.is_symlink():
            return "recovery_staging_unsafe", "artifact staging directory is a symlink"
        if staging.is_dir() and any(staging.iterdir()):
            return "recovery_staging_not_clean", "artifact staging directory is not empty"
        return None

    def _is_expected_recovery_task(
        self,
        source: Task,
        replacement: Task,
        recovery_kind: str,
    ) -> bool:
        created = next(
            (
                event
                for event in self.store.list_events(replacement.task_id)
                if event.event_type == "task_created"
            ),
            None,
        )
        if created is None:
            return False
        recovery = created.payload.get("recovery")
        return (
            isinstance(recovery, Mapping)
            and recovery.get("kind") == recovery_kind
            and recovery.get("source_task_id") == source.task_id
            and replacement.request.to_dict() == source.request.to_dict()
        )

    def _prepublish_recovery_report(
        self,
        source: Task,
        *,
        status: str,
        code: str,
        reason: str,
        replacement: Task | None = None,
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "status": status,
            "code": code,
            "reason": reason,
            "source_task_id": source.task_id,
            "recovery_kind": "prepublish_publish_lock_timeout",
            "replacement_task_id": (
                replacement.task_id if replacement is not None else None
            ),
            "replacement_task": (
                replacement.to_dict() if replacement is not None else None
            ),
            "manual_review": status
            in {"rejected", "recovery_failed", "recovery_in_progress"},
            "safe_to_retry": False,
        }
        return with_status_assurance(report)

    def get_task(self, task_id: str) -> Task | None:
        return self.store.get_task(task_id)

    def audit_lifecycle(
        self, task_id: str, *, now: str | None = None
    ) -> dict[str, Any]:
        """Read-only audit of task, evidence and cleanup consistency."""
        try:
            task = self.store.require_task(task_id)
        except Exception as exc:
            return audit_storage_error(task_id, exc, now=now)
        try:
            events = self.store.list_events(task_id)
        except Exception as exc:
            return audit_storage_error(task_id, exc, now=now)
        evidence_state = None
        evidence = (
            task.tool_result.test_evidence
            if task.tool_result is not None
            else None
        )
        snapshot = task.execution_plan_snapshot
        if (
            evidence is not None
            and evidence.evidence_id
            and isinstance(snapshot, Mapping)
            and snapshot.get("kind") == RUN_TESTS_TOOL
        ):
            from .evidence_store import EvidenceStore, EvidenceStoreError
            from .isolation import (
                ContainerBackendUnavailable,
                _live_image_digest,
                _live_runtime_identity,
            )

            image = str(snapshot.get("image") or "python:3.13-alpine")
            store = EvidenceStore(self.project_root)
            runtime_unknown = False
            try:
                expected_runtime = _live_runtime_identity()
                expected_image = _live_image_digest(image)
            except ContainerBackendUnavailable:
                # Probing the runtime is not required to decide whether the
                # stored record is intact. Degrade the runtime expectation and
                # let the store judge the record first: a corrupt record must
                # still be reported as blocked instead of being hidden behind
                # an environment condition.
                runtime_unknown = True
                expected_runtime = ""
                expected_image = ""
            try:
                evidence_state = store.verify_stored_evidence(
                    evidence.evidence_id,
                    expected_runtime=expected_runtime,
                    expected_image_digest=expected_image,
                    expected_config_digest=evidence.config_digest or "",
                    expected_request_id=task.task_id,
                    expected_command_digest=evidence.command_digest or "",
                )
            except EvidenceStoreError as exc:
                evidence_state = {
                    "state": "unverified",
                    "reasons": [exc.code],
                }
            except Exception as exc:
                evidence_state = {
                    "state": "unverified",
                    "reasons": [type(exc).__name__],
                }
            if runtime_unknown:
                evidence_state = dict(evidence_state)
                if evidence_state.get("state") != "blocked":
                    evidence_state["reasons"] = sorted(
                        {
                            *evidence_state.get("reasons", []),
                            "container_backend_unavailable",
                        }
                    )
        return audit_task_lifecycle(
            task,
            events,
            now=now,
            evidence_state=evidence_state,
        )

    def recovery_preview(
        self, task_id: str, *, now: str | None = None
    ) -> dict[str, Any]:
        """Build a read-only recovery preview; never execute an action."""
        report = self.audit_lifecycle(task_id, now=now)
        try:
            task = self.store.require_task(task_id)
            events = self.store.list_events(task_id)
        except Exception:
            return build_unavailable_preview(
                task_id, report, now=now
            ).to_dict()
        return build_recovery_preview(
            task, events, audit=report, now=now
        ).to_dict()

    def cleanup_provisioning_artifact(
        self,
        task_id: str,
        relative_path: str,
        *,
        enabled: bool = False,
        authorized: bool = False,
        checkpoint: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Clean only a provisioning-owned file recorded by the final plan."""
        task = self.store.require_task(task_id)
        base: dict[str, Any] = {
            "task_id": task_id,
            "relative_path": relative_path,
            "source": "provisioning",
            "removed": False,
        }

        def reject(code: str, reason: str) -> dict[str, Any]:
            record = {
                **base,
                "status": "rejected",
                "code": code,
                "reason": reason,
            }
            self._record_cleanup_event(task, record)
            return record

        if task.lease_token is not None:
            return reject("active_lease", "task still has an active lease")
        snapshot = task.execution_plan_snapshot
        if not isinstance(snapshot, Mapping) or snapshot.get("kind") != "run_tests":
            return reject(
                "no_final_execution_plan",
                "historical task has no final execution plan",
            )
        if (
            snapshot.get("task_id") != task.task_id
            or snapshot.get("attempt_id") != task.attempt_id
            or int(snapshot.get("claim_generation", -1))
            != task.claim_generation
        ):
            return reject(
                "plan_identity_mismatch",
                "final execution plan does not match the task",
            )
        try:
            parts = _normalize_relative(relative_path)
        except CleanupError as exc:
            return reject(exc.code, exc.message)
        normalized = "/".join(parts)
        artifacts = list(snapshot.get("provisioning_artifacts") or [])
        record = next(
            (
                item
                for item in artifacts
                if item.get("path") == normalized
                and item.get("ownership") == "provisioning"
            ),
            None,
        )
        if record is None:
            return reject(
                "not_owned",
                "no provisioning ownership record for this path",
            )
        if record.get("cleanup") != "deferred_to_controlled_cleanup":
            return reject(
                "cleanup_not_deferred",
                "artifact is not marked for controlled cleanup",
            )
        if not enabled:
            return reject("cleanup_disabled", "cleanup is off")
        if not authorized:
            return reject(
                "cleanup_unauthorized",
                "cleanup needs explicit authorization",
            )

        events = self.store.list_events(task_id)
        if any(
            event.event_type == "cleanup_completed"
            and event.payload.get("relative_path") == normalized
            for event in events
        ):
            return reject(
                "already_cleaned", "this provisioning file is already cleaned"
            )

        workspace = resolve_workspace(
            self.project_root, str(snapshot.get("workspace"))
        )
        target = workspace.joinpath(*parts)
        if target.is_symlink() or not target.is_file():
            if not target.is_symlink():
                deadline = time.monotonic() + 1.0
                latest_events: list[Event] = []
                while True:
                    try:
                        latest_events = self.store.list_events(task_id)
                    except Exception:
                        latest_events = []
                    if any(
                        event.event_type == "cleanup_completed"
                        and event.payload.get("relative_path") == normalized
                        for event in latest_events
                    ):
                        return reject(
                            "already_cleaned",
                            "another request completed this cleanup",
                        )
                    if any(
                        event.event_type == "cleanup_intent"
                        and event.payload.get("relative_path") == normalized
                        for event in latest_events
                    ):
                        return reject(
                            "cleanup_in_progress",
                            "another cleanup request is in progress",
                        )
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
            return reject("missing_target", "target does not exist")
        expected_sha256 = str(record.get("sha256") or "")
        try:
            actual_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            return reject("target_unreadable", "target could not be hashed")
        if not expected_sha256 or actual_sha256 != expected_sha256:
            return reject(
                "target_modified",
                "target content no longer matches the provisioning record",
            )

        operation_id = str(uuid4())
        intent = {
            "operation_id": operation_id,
            "task_id": task_id,
            "attempt_id": task.attempt_id,
            "claim_generation": task.claim_generation,
            "relative_path": normalized,
            "expected_sha256": expected_sha256,
            "source": "provisioning",
        }
        result = {
            **base,
            "relative_path": normalized,
            "operation_id": operation_id,
            "decision": {
                "allowed": True,
                "code": "allowed",
                "reason": "provisioning-owned artifact",
                "relative_path": normalized,
                "expected_sha256": expected_sha256,
            },
            "intent_persisted": self._record_cleanup_intent(task, intent),
        }
        if not result["intent_persisted"]:
            persisted = self._intent_exists(task_id, operation_id)
            result["intent_reconciled"] = persisted
            if persisted is not True:
                result.update(
                    status="unverified",
                    code="intent_not_persisted",
                    reason="cleanup intent could not be persisted",
                    removed=False,
                )
                self._record_cleanup_event(task, result)
                return result
        decision = CleanupDecision(
            True,
            "allowed",
            "provisioning-owned artifact",
            normalized,
            expected_sha256,
            None,
            parts,
        )
        result.update(
            execute_cleanup(
                decision,
                workspace_root=workspace,
                checkpoint=checkpoint,
                operation_id=operation_id,
            )
        )
        if result.get("status") != "completed":
            deadline = time.monotonic() + 1.0
            latest_events: list[Event] = []
            completed_elsewhere = False
            while True:
                try:
                    latest_events = self.store.list_events(task_id)
                except Exception:
                    latest_events = []
                completed_elsewhere = any(
                    event.event_type == "cleanup_completed"
                    and event.payload.get("relative_path") == normalized
                    for event in latest_events
                )
                if completed_elsewhere or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            if completed_elsewhere:
                result.update(
                    status="rejected",
                    code="already_cleaned",
                    reason="another request completed this cleanup",
                    removed=False,
                )
            elif any(
                event.event_type == "cleanup_intent"
                and event.payload.get("relative_path") == normalized
                and event.payload.get("operation_id") != operation_id
                for event in latest_events
            ):
                result.update(
                    status="unverified",
                    code="cleanup_in_progress",
                    reason="another cleanup request is in progress",
                    removed=False,
                )
        self._record_cleanup_event(task, result)
        if (
            result.get("status") == "completed"
            and result.get("evidence_persisted") is False
        ):
            result.update(
                status="unverified",
                code="completion_event_not_persisted",
                reason=(
                    "file was removed but completion event was not persisted"
                ),
                removed=True,
            )
        return result

    def reconcile_provisioning_cleanup(
        self, task_id: str, operation_id: str
    ) -> dict[str, Any]:
        """Read-only reconciliation for one provisioning cleanup operation."""
        task = self.store.require_task(task_id)
        snapshot = task.execution_plan_snapshot
        if not isinstance(snapshot, Mapping) or snapshot.get("kind") != "run_tests":
            return {
                "operation_id": operation_id,
                "state": "reconciled_unverified",
                "reason": "no final execution plan",
            }
        events = self.store.list_events(task_id)
        intent = next(
            (
                event
                for event in events
                if event.event_type == "cleanup_intent"
                and event.payload.get("operation_id") == operation_id
                and event.payload.get("source") == "provisioning"
            ),
            None,
        )
        if intent is None:
            return {
                "operation_id": operation_id,
                "state": "reconciled_unverified",
                "reason": "no persisted cleanup intent",
            }
        completed = any(
            event.event_type == "cleanup_completed"
            and event.payload.get("operation_id") == operation_id
            for event in events
        )
        workspace = resolve_workspace(
            self.project_root, str(snapshot.get("workspace"))
        )
        return reconcile_cleanup(
            workspace_root=workspace,
            relative_path=str(intent.payload.get("relative_path")),
            operation_id=operation_id,
            expected_sha256=str(intent.payload.get("expected_sha256")),
            intent_persisted=True,
            completed_event=completed,
            current_generation=task.claim_generation,
            record_generation=int(intent.payload.get("claim_generation", -1)),
        )

    def cleanup_workspace_file(
        self,
        task_id: str,
        relative_path: str,
        *,
        enabled: bool = False,
        authorized: bool = False,
        checkpoint: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Delete one file this task provably created, if strictly allowed."""
        return self.cleanup_workspace_files(
            task_id,
            [relative_path],
            enabled=enabled,
            authorized=authorized,
            checkpoint=checkpoint,
        )["files"][0]

    def cleanup_workspace_files(
        self,
        task_id: str,
        relative_paths: list[str],
        *,
        enabled: bool = False,
        authorized: bool = False,
        checkpoint: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Delete several owned files, validating each one independently."""
        task = self.store.require_task(task_id)
        effects = (
            task.tool_result.workspace_effects
            if task.tool_result is not None
            else None
        )
        try:
            events = self.store.list_events(task_id)
        except Exception:
            return {
                "task_id": task_id,
                "batch_status": "unverified",
                "files": [
                    {
                        "relative_path": path,
                        "status": "unverified",
                        "code": "store_unavailable",
                        "removed": False,
                    }
                    for path in relative_paths
                ],
            }
        cleaned = {
            str(event.payload.get("relative_path"))
            for event in events
            if event.event_type == "cleanup_completed"
            and event.payload.get("relative_path")
        }
        results: list[dict[str, Any]] = []
        if effects is None:
            results = [
                {
                    "relative_path": path,
                    "status": "rejected",
                    "code": "no_effects",
                    "removed": False,
                }
                for path in relative_paths
            ]
            return {
                "task_id": task_id,
                "batch_status": summarize_batch(results),
                "files": results,
            }
        if task.lease_token is not None:
            results = [
                {
                    "relative_path": path,
                    "status": "rejected",
                    "code": "active_lease",
                    "removed": False,
                }
                for path in relative_paths
            ]
            return {
                "task_id": task_id,
                "batch_status": summarize_batch(results),
                "files": results,
            }
        workspace = str(effects.get("workspace"))
        root = (
            resolve_workspace(self.project_root, workspace)
            if effects.get("operation") == RUN_TESTS_TOOL
            else self.artifacts_root / workspace
        )
        for path in relative_paths:
            decision = plan_cleanup(
                effects=effects,
                workspace_root=root,
                relative_path=path,
                current_generation=task.claim_generation,
                enabled=enabled,
                authorized=authorized,
                cleaned_paths=cleaned,
            )
            record: dict[str, Any] = {
                "task_id": task_id,
                "relative_path": path,
                "decision": decision.to_dict(),
            }
            operation_id = str(uuid4())
            record["operation_id"] = operation_id
            record["intent_persisted"] = self._record_cleanup_intent(
                task,
                {
                    "operation_id": operation_id,
                    "task_id": task_id,
                    "attempt_id": task.attempt_id,
                    "claim_generation": task.claim_generation,
                    "relative_path": path,
                    "expected_sha256": decision.expected_sha256,
                },
            )
            if not record["intent_persisted"]:
                # A failed intent write is not permission to proceed: reconcile
                # by operation id and refuse unless it is provably persisted.
                persisted = self._intent_exists(task_id, operation_id)
                record["intent_reconciled"] = persisted
                if persisted is not True:
                    record.update(
                        status="unverified",
                        code="intent_not_persisted",
                        removed=False,
                    )
                    self._record_cleanup_event(task, record)
                    results.append(record)
                    continue
            if not decision.allowed:
                record.update(
                    status="rejected", code=decision.code, removed=False
                )
            else:
                record.update(
                    execute_cleanup(
                        decision,
                        workspace_root=root,
                        checkpoint=checkpoint,
                        operation_id=operation_id,
                    )
                )
            if record.get("status") == "completed":
                cleaned.add(decision.relative_path or path)
            self._record_cleanup_event(task, record)
            results.append(record)
        return {
            "task_id": task_id,
            "batch_status": summarize_batch(results),
            "files": results,
        }

    def _record_cleanup_intent(
        self, task: Task, payload: dict[str, Any]
    ) -> bool:
        try:
            self.store.record_event_and_update(
                task.task_id, "cleanup_intent", self.clock(), payload=payload
            )
        except Exception:
            return False
        return True

    def _intent_exists(self, task_id: str, operation_id: str):
        """Return True/False, or None when the store cannot be read."""
        try:
            events = self.store.list_events(task_id)
        except Exception:
            return None
        return any(
            event.event_type == "cleanup_intent"
            and event.payload.get("operation_id") == operation_id
            for event in events
        )

    def reconcile_cleanup_operation(
        self, task_id: str, operation_id: str
    ) -> dict[str, Any]:
        """Read-only reconciliation of one cleanup operation."""
        task = self.store.require_task(task_id)
        effects = (
            task.tool_result.workspace_effects
            if task.tool_result is not None
            else None
        )
        if effects is None:
            return {"state": "reconciled_unverified", "reason": "no effects"}
        events = self.store.list_events(task_id)
        intent = next(
            (
                e
                for e in events
                if e.event_type == "cleanup_intent"
                and e.payload.get("operation_id") == operation_id
            ),
            None,
        )
        if intent is None:
            return {
                "operation_id": operation_id,
                "state": "reconciled_unverified",
                "reason": "no persisted cleanup intent",
            }
        completed = any(
            e.event_type == "cleanup_completed"
            and e.payload.get("operation_id") == operation_id
            for e in events
        )
        workspace = str(effects.get("workspace"))
        root = (
            resolve_workspace(self.project_root, workspace)
            if effects.get("operation") == RUN_TESTS_TOOL
            else self.artifacts_root / workspace
        )
        return reconcile_cleanup(
            workspace_root=root,
            relative_path=str(intent.payload.get("relative_path")),
            operation_id=operation_id,
            expected_sha256=str(intent.payload.get("expected_sha256")),
            intent_persisted=True,
            completed_event=completed,
            current_generation=task.claim_generation,
            record_generation=int(intent.payload.get("claim_generation", -1)),
        )

    def _record_cleanup_event(
        self, task: Task, record: dict[str, Any]
    ) -> None:
        status = record.get("status")
        event_type = {
            "completed": "cleanup_completed",
            "unverified": "cleanup_unverified",
        }.get(status, "cleanup_rejected")
        try:
            self.store.record_event_and_update(
                task.task_id,
                event_type,
                self.clock(),
                payload=record,
            )
        except Exception:
            # Cleanup evidence is advisory for the original task; a failure
            # to append it must not be reported as a successful cleanup.
            record["evidence_persisted"] = False
            return
        record["evidence_persisted"] = True

    def require_task(self, task_id: str) -> Task:
        return self.store.require_task(task_id)

    def events(self, task_id: str) -> list[Event]:
        return self.store.list_events(task_id)

    def cancel(
        self,
        task_id: str,
        *,
        expected_plan_version: int | None = None,
        expected_claim_generation: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Task:
        return self.store.cancel_task(
            task_id,
            now=self.clock(),
            expected_plan_version=expected_plan_version,
            expected_claim_generation=expected_claim_generation,
            metadata=metadata,
        )

    def request_cancel(
        self,
        task_id: str,
        *,
        requested_by: str,
        reason: str | None = None,
    ) -> Task:
        return self.store.request_cancel(
            task_id,
            requested_by=requested_by,
            reason=reason,
            now=self.clock(),
        )

    def _cancel_if_requested(
        self,
        task: Task,
        lease_token: str,
        *,
        checkpoint: str,
        side_effect_observed: bool,
    ) -> Task | None:
        current = self.store.require_task(task.task_id)
        if current.cancel_requested_at is None or is_terminal(current.status):
            return None
        cancelled, _ = self.store.transition(
            current.task_id,
            TaskStatus.CANCELLED,
            EventType.TASK_CANCELLED,
            self.clock(),
            payload={
                "checkpoint": checkpoint,
                "side_effect_observed": side_effect_observed,
                "artifact": (
                    current.artifact.to_dict()
                    if current.artifact is not None
                    else None
                ),
            },
            updates={
                **self._clear_claim_updates(),
                "phase": f"cancelled_{checkpoint}",
                "error": TaskError(
                    "cancelled",
                    "task was cancelled at a cooperative checkpoint",
                ),
            },
            lease_token=lease_token,
        )
        return cancelled

    def revise_plan(
        self,
        task_id: str,
        patch: Mapping[str, object],
        *,
        source_id: str,
        expected_plan_version: int,
        confirmed: bool,
        allowed_statuses: frozenset[TaskStatus] | None = None,
    ) -> Task:
        """Revise an unexecuted task with an optimistic version check."""
        if not source_id.strip():
            raise ValueError("source_id must not be empty")
        if not confirmed:
            raise PlanRevisionConflict(
                "plan revision requires explicit confirmation"
            )
        current = self.store.require_task(task_id)
        for revision in current.plan_revisions:
            if revision.source_id == source_id:
                return current
        if current.plan_version != expected_plan_version:
            raise PlanRevisionConflict(
                "expected plan version does not match current version"
            )
        allowed = (
            {
                TaskStatus.QUEUED,
                TaskStatus.AWAITING_CLARIFICATION,
                TaskStatus.BLOCKED,
                TaskStatus.INTERRUPTED,
            }
            if allowed_statuses is None
            else allowed_statuses
        )
        if (
            current.status not in allowed
            or current.tool_result is not None
            or current.artifact is not None
            or current.execution_mode is not ExecutionMode.NOT_EXECUTED
        ):
            raise PlanRevisionConflict(
                "task cannot be revised after execution evidence"
            )
        now = self._normalize_time(self.clock())
        current_lease_state = lease_state(
            current.lease_token,
            current.lease_expires_at,
            now,
        )
        if current_lease_state != "none":
            if current_lease_state == "active":
                raise PlanRevisionConflict(
                    "task has an active execution lease"
                )
            raise PlanRevisionConflict(
                "task has an expired execution lease requiring recovery"
            )
        unknown = set(patch) - {"goal", "operation", "parameters"}
        if unknown:
            raise ValueError(f"unsupported request fields: {sorted(unknown)}")
        new_request = current.request.merged(patch)
        outcome = self.planner.plan_request(new_request)
        if outcome.classification not in {
            PlanningClassification.PLANNED,
            PlanningClassification.CLARIFICATION,
        }:
            raise PlanRevisionConflict(
                "revised request could not produce a safe plan"
            )
        new_version = current.plan_version + 1
        new_plan = outcome.plan
        new_status = (
            TaskStatus.AWAITING_CLARIFICATION
            if outcome.classification is PlanningClassification.CLARIFICATION
            else TaskStatus.QUEUED
        )
        new_missing = (
            outcome.missing_fields
            or missing_required_fields(outcome.request or new_request)
        )
        new_questions = outcome.clarification_questions
        changed_steps = self._changed_plan_steps(
            current.plan,
            new_plan,
        )
        revision = PlanRevision(
            revision_id=str(uuid4()),
            from_version=current.plan_version,
            to_version=new_version,
            source_id=source_id,
            changed_at=self.clock(),
            confirmed=True,
            old_request=current.request.to_dict(),
            new_request=new_request.to_dict(),
            old_plan=current.plan.to_dict() if current.plan else None,
            new_plan=new_plan.to_dict() if new_plan else None,
            changed_steps=changed_steps,
        )
        metadata = {
            "classification": outcome.classification.value,
            **dict(outcome.metadata),
        }
        current.status = new_status
        current.request = outcome.request or new_request
        current.plan = new_plan
        current.missing_fields = list(new_missing)
        current.clarification_questions = list(new_questions)
        current.planning_metadata = metadata
        current.planner_mode = self.planner.mode
        current.plan_version = new_version
        current.plan_revisions.append(revision)
        current.phase = "plan_revised"
        current.error = None
        current.execution_mode = ExecutionMode.NOT_EXECUTED
        current.updated_at = revision.changed_at
        current.execution_owner = None
        current.lease_token = None
        current.lease_expires_at = None
        return self.store.apply_plan_revision(
            task_id,
            expected_plan_version=expected_plan_version,
            updated_task=current,
            event=Event.new(
                task_id,
                EventType.PLAN_REVISED,
                revision.changed_at,
                {
                    "revision": revision.to_dict(),
                    "classification": outcome.classification.value,
                },
            ),
            allowed_statuses=allowed_statuses,
        )

    @staticmethod
    def _changed_plan_steps(old_plan, new_plan) -> list[str]:
        if old_plan is None or new_plan is None:
            return ["plan"]
        old_steps = {step.step_id: step for step in old_plan.steps}
        new_steps = {step.step_id: step for step in new_plan.steps}
        changed = []
        for step_id in sorted(set(old_steps) | set(new_steps)):
            old_step = old_steps.get(step_id)
            new_step = new_steps.get(step_id)
            if old_step is None or new_step is None:
                changed.append(step_id)
            elif old_step.to_dict() != new_step.to_dict():
                changed.append(step_id)
        return changed

    def cleanup_staging(self, *, now: str | None = None) -> dict[str, object]:
        """Remove only staging files with provable, non-active ownership."""
        timestamp = self._normalize_time(now or self.clock())
        entries: list[dict[str, object]] = []
        if not self.artifacts_root.exists():
            return {
                "scanned": 0,
                "deleted": 0,
                "retained": 0,
                "errors": [],
                "entries": entries,
            }

        for task_dir in sorted(self.artifacts_root.iterdir()):
            try:
                task_id = str(UUID(task_dir.name))
            except (ValueError, AttributeError):
                continue
            if (
                task_id != task_dir.name
                or task_dir.is_symlink()
                or not task_dir.is_dir()
            ):
                continue
            staging_dir = task_dir / ".staging"
            if not staging_dir.exists():
                continue
            if staging_dir.is_symlink() or not staging_dir.is_dir():
                entries.append(
                    {
                        "task_id": task_id,
                        "action": "retained",
                        "reason": "unsafe_staging_directory",
                    }
                )
                continue
            try:
                with task_publication_lock(
                    self.artifacts_root,
                    task_id,
                    timeout_seconds=self.publish_lock_timeout_seconds,
                ):
                    task = self.store.get_task(task_id)
                    if task is None:
                        entries.append(
                            {
                                "task_id": task_id,
                                "action": "retained",
                                "reason": "task_missing",
                            }
                        )
                        continue
                    known_attempts: set[str] = set()
                    for event in self.store.list_events(task_id):
                        if event.event_type == EventType.TOOL_STARTED.value:
                            attempt_id = event.payload.get("attempt_id")
                            if isinstance(attempt_id, str):
                                known_attempts.add(attempt_id)
                    lease_active = (
                        task.lease_token is not None
                        and task.lease_expires_at is not None
                        and datetime.fromisoformat(task.lease_expires_at)
                        > datetime.fromisoformat(timestamp)
                    )
                    for staging_path in sorted(staging_dir.glob("*.part")):
                        entries.append(
                            self._cleanup_staging_file(
                                task,
                                known_attempts,
                                lease_active,
                                staging_path,
                                timestamp,
                            )
                        )
            except ArtifactToolError as exc:
                entries.append(
                    {
                        "task_id": task_id,
                        "action": "retained",
                        "reason": exc.code,
                    }
                )

        deleted = sum(
            1 for entry in entries if entry["action"] == "deleted"
        )
        errors = [
            entry for entry in entries if entry["action"] == "error"
        ]
        return {
            "scanned": len(entries),
            "deleted": deleted,
            "retained": len(entries) - deleted - len(errors),
            "errors": errors,
            "entries": entries,
        }

    def _cleanup_staging_file(
        self,
        task: Task,
        known_attempts: set[str],
        lease_active: bool,
        staging_path: Path,
        timestamp: str,
    ) -> dict[str, object]:
        attempt_id = staging_path.stem
        try:
            canonical_attempt = str(UUID(attempt_id))
        except (ValueError, AttributeError):
            return {
                "task_id": task.task_id,
                "file": staging_path.name,
                "action": "retained",
                "reason": "unknown_attempt_name",
            }
        if canonical_attempt != attempt_id:
            return {
                "task_id": task.task_id,
                "file": staging_path.name,
                "action": "retained",
                "reason": "unknown_attempt_name",
            }
        if (
            attempt_id == task.attempt_id
            and lease_active
        ):
            return {
                "task_id": task.task_id,
                "file": staging_path.name,
                "attempt_id": attempt_id,
                "action": "retained",
                "reason": "active_attempt",
            }
        if attempt_id not in known_attempts:
            return {
                "task_id": task.task_id,
                "file": staging_path.name,
                "attempt_id": attempt_id,
                "action": "retained",
                "reason": "unknown_attempt",
            }
        if staging_path.is_symlink() or not staging_path.is_file():
            return {
                "task_id": task.task_id,
                "file": staging_path.name,
                "attempt_id": attempt_id,
                "action": "retained",
                "reason": "unsafe_staging_file",
            }
        try:
            byte_count, digest = self._hash_staging_file(staging_path)
            staging_path.unlink()
            self.store.append_event(
                task.task_id,
                Event.new(
                    task.task_id,
                    EventType.STAGING_CLEANED,
                    timestamp,
                    {
                        "attempt_id": attempt_id,
                        "file": staging_path.name,
                        "byte_count": byte_count,
                        "sha256": digest,
                    },
                ),
            )
        except Exception as exc:
            return {
                "task_id": task.task_id,
                "file": staging_path.name,
                "action": "error",
                "reason": type(exc).__name__,
            }
        return {
            "task_id": task.task_id,
            "file": staging_path.name,
            "attempt_id": attempt_id,
            "action": "deleted",
            "reason": "known_inactive_attempt",
            "byte_count": byte_count,
            "sha256": digest,
        }

    @staticmethod
    def _hash_staging_file(path: Path) -> tuple[int, str]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ArtifactToolError(
                    "unsafe_staging_file",
                    "staging file is not a regular file",
                )
            if metadata.st_size > MAX_ARTIFACT_BYTES:
                raise ArtifactToolError(
                    "oversized_staging_file",
                    "staging file exceeds the artifact size limit",
                )
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 16384)
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            os.close(descriptor)
        return metadata.st_size, digest.hexdigest()

    def recover_expired_leases(self) -> list[Task]:
        """Expire stale leases, preserving tasks actively owned by another instance."""
        now = self._normalize_time(self.clock())
        recovered: list[Task] = []
        for task in self.store.find_tasks_by_statuses(
            {
                TaskStatus.QUEUED,
                TaskStatus.RUNNING,
                TaskStatus.VERIFYING,
                TaskStatus.PAUSED,
                TaskStatus.AWAITING_APPROVAL,
            }
        ):
            if task.lease_token:
                updated = self.store.expire_claim(task.task_id, now=now)
                if updated != task:
                    recovered.append(updated)
            elif task.status in {TaskStatus.RUNNING, TaskStatus.VERIFYING}:
                updated, _ = self.store.transition(
                    task.task_id,
                    TaskStatus.INTERRUPTED,
                    EventType.TASK_INTERRUPTED,
                    now,
                    payload={
                        "reason": "legacy_in_progress_without_lease",
                        "previous_status": task.status.value,
                        "previous_phase": task.phase,
                    },
                    updates={
                        "phase": "recovered_legacy_state",
                        "error": TaskError(
                            "interrupted",
                            "legacy in-progress task had no execution lease",
                        ),
                    },
                )
                recovered.append(updated)
        return recovered

    def recover_in_progress_tasks(self) -> list[Task]:
        """Compatibility alias with lease-safe semantics."""
        return self.recover_expired_leases()

    def _claim(
        self,
        task_id: str,
        *,
        allowed: frozenset[TaskStatus],
        metadata: Mapping[str, Any] | None = None,
        expected_plan_version: int | None = None,
    ) -> ClaimResult:
        now = self._normalize_time(self.clock())
        expires_at = self._lease_deadline(now)
        current = self.store.require_task(task_id)
        needs_publication_lock = (
            getattr(self.writer, "execution_mode", ExecutionMode.REAL)
            is ExecutionMode.REAL
            and current.status
            in {
                TaskStatus.RUNNING,
                TaskStatus.VERIFYING,
                TaskStatus.INTERRUPTED,
            }
        )
        coordination = (
            task_publication_lock(
                self.artifacts_root,
                task_id,
                timeout_seconds=self.publish_lock_timeout_seconds,
            )
            if needs_publication_lock
            else nullcontext()
        )
        try:
            with coordination:
                return self.store.claim_task(
                    task_id,
                    owner_id=self.owner_id,
                    lease_token=str(uuid4()),
                    attempt_id=str(uuid4()),
                    now=now,
                    expires_at=expires_at,
                    allowed_statuses=allowed,
                    max_execution_seconds=self.max_execution_seconds,
                    metadata=metadata,
                    expected_plan_version=expected_plan_version,
                )
        except ArtifactToolError as exc:
            raise StoreBusyError(
                f"task publication coordination failed: {exc.code}"
            ) from exc

    def _lease_deadline(self, now: str) -> str:
        current = datetime.fromisoformat(now)
        required = self._effective_lease_seconds()
        return (current + timedelta(seconds=required)).isoformat(
            timespec="milliseconds"
        )

    def _effective_lease_seconds(self) -> float:
        return max(
            self.lease_seconds,
            float(getattr(self.planner, "required_lease_seconds", 0.0)),
        )

    @staticmethod
    def _normalize_time(value: str) -> str:
        current = datetime.fromisoformat(value)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.isoformat(timespec="milliseconds")

    def _run_claimed(self, task: Task, lease_token: str | None) -> Task:
        if lease_token is None:
            raise ValueError("claimed task is missing lease token")
        cancelled = self._cancel_if_requested(
            task,
            lease_token,
            checkpoint="claim",
            side_effect_observed=task.artifact is not None,
        )
        if cancelled is not None:
            return cancelled
        if task.status is TaskStatus.QUEUED:
            return self._run_with_renewal(
                task,
                lease_token,
                lambda: self._plan_and_execute(task, lease_token),
            )
        if task.status is TaskStatus.PAUSED:
            if task.plan is None:
                resumed_planning, _ = self.store.record_event_and_update(
                    task.task_id,
                    EventType.TASK_RESUMED,
                    self.clock(),
                    payload={
                        "resume": True,
                        "source": "user_pause_before_planning",
                    },
                    updates={
                        "pause_requested_at": None,
                        "phase": "resumed_before_planning",
                        "error": None,
                    },
                    lease_token=lease_token,
                )
                return self._run_with_renewal(
                    resumed_planning,
                    lease_token,
                    lambda: self._plan_and_execute(
                        resumed_planning,
                        lease_token,
                    ),
                )
            resumed, _ = self.store.transition(
                task.task_id,
                TaskStatus.RUNNING,
                EventType.TASK_RESUMED,
                self.clock(),
                payload={"resume": True, "source": "user_pause"},
                updates={
                    "pause_requested_at": None,
                    "phase": "resumed_after_pause",
                    "error": None,
                },
                lease_token=lease_token,
            )
            return self._run_with_renewal(
                resumed,
                lease_token,
                lambda: self._execute_multi_step_plan(
                    resumed,
                    lease_token,
                ),
            )
        if task.status is TaskStatus.INTERRUPTED:
            return self._run_with_renewal(
                task,
                lease_token,
                lambda: self._resume_interrupted(task, lease_token),
            )
        return self._release_claim(
            task,
            lease_token,
            reason=f"claim_not_executable:{task.status.value}",
        )

    def _run_with_renewal(
        self,
        task: Task,
        lease_token: str,
        action: Callable[[], Task],
    ) -> Task:
        effective_lease_seconds = self._effective_lease_seconds()
        if task.execution_deadline_at is None:
            raise LeaseRenewalError(
                "execution_budget_missing",
                "claimed task is missing a persisted execution deadline",
            )
        remaining_budget = (
            datetime.fromisoformat(task.execution_deadline_at)
            - datetime.fromisoformat(self._normalize_time(self.clock()))
        ).total_seconds()
        if remaining_budget <= 0:
            failed, _ = self.store.transition(
                task.task_id,
                TaskStatus.FAILED,
                EventType.TASK_FAILED,
                self.clock(),
                payload={
                    "code": "execution_budget_exceeded",
                    "execution_deadline_at": task.execution_deadline_at,
                },
                updates={
                    **self._clear_claim_updates(),
                    "phase": "execution_budget_expired",
                    "error": TaskError(
                        "execution_budget_exceeded",
                        "task execution budget expired before execution started",
                    ),
                },
                lease_token=lease_token,
            )
            return failed
        renewer = LeaseRenewer(
            self.store,
            task_id=task.task_id,
            lease_token=lease_token,
            claim_generation=task.claim_generation,
            clock=self.clock,
            lease_seconds=effective_lease_seconds,
            interval_seconds=(
                self.lease_renewal_interval_seconds
                if self.lease_renewal_interval_seconds is not None
                else max(0.01, effective_lease_seconds / 3.0)
            ),
            max_duration_seconds=remaining_budget,
        )
        reset_token = _ACTIVE_RENEWER.set(renewer)
        renewer.start()
        try:
            renewer.ensure_healthy()
            return action()
        finally:
            renewer.stop()
            _ACTIVE_RENEWER.reset(reset_token)

    def _ensure_execution_health(self) -> None:
        renewer = _ACTIVE_RENEWER.get()
        if renewer is not None:
            renewer.ensure_healthy()

    def _assert_execution_authority(
        self,
        task: Task,
        lease_token: str,
    ) -> None:
        self._ensure_execution_health()
        self.store.assert_task_authority(
            task.task_id,
            lease_token=lease_token,
            attempt_id=task.attempt_id,
            claim_generation=task.claim_generation,
            now=self.clock(),
        )

    def _authorize_publication(
        self,
        task_id: str,
        attempt_id: str,
        lease_token: str,
        claim_generation: int,
    ) -> None:
        self._ensure_execution_health()
        self.store.assert_task_authority(
            task_id,
            lease_token=lease_token,
            attempt_id=attempt_id,
            claim_generation=claim_generation,
            now=self.clock(),
        )
        current = self.store.require_task(task_id)
        if current.cancel_requested_at is not None:
            raise CancellationRequested()

    def _plan_and_execute(self, task: Task, lease_token: str) -> Task:
        return self._plan_with_current_status(task, lease_token)

    def _plan_with_current_status(self, task: Task, lease_token: str) -> Task:
        self._ensure_execution_health()
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.PLANNING_STARTED,
            self.clock(),
            payload={
                "planner_mode": self.planner.mode.value,
                "planner_name": self.planner.name,
            },
            updates={"phase": "planning"},
            lease_token=lease_token,
        )
        try:
            outcome = self.planner.plan_request(task.request)
        except Exception as exc:
            outcome = PlanningOutcome(
                classification=PlanningClassification.FAILED,
                error_code="planner_internal_error",
                error_message=f"planner raised {type(exc).__name__}",
                metadata={
                    "planner_mode": self.planner.mode.value,
                    "planner_name": self.planner.name,
                },
            )
        self._ensure_execution_health()
        cancelled = self._cancel_if_requested(
            task,
            lease_token,
            checkpoint="after_planning",
            side_effect_observed=False,
        )
        if cancelled is not None:
            return cancelled

        planning_metadata = {
            "classification": outcome.classification.value,
            **dict(outcome.metadata),
        }
        if outcome.classification is PlanningClassification.CLARIFICATION:
            clarified_request = outcome.request or task.request
            missing = outcome.missing_fields or missing_required_fields(clarified_request)
            clarified, _ = self.store.transition(
                task.task_id,
                TaskStatus.AWAITING_CLARIFICATION,
                EventType.PLANNING_CLARIFICATION,
                self.clock(),
                payload={
                    "missing_fields": missing,
                    "clarification_questions": outcome.clarification_questions,
                    "planning_metadata": planning_metadata,
                },
                updates={
                    **self._clear_claim_updates(),
                    "request": clarified_request,
                    "planner_mode": self.planner.mode,
                    "missing_fields": missing,
                    "clarification_questions": outcome.clarification_questions,
                    "planning_metadata": planning_metadata,
                    "phase": "awaiting_clarification",
                    "execution_mode": ExecutionMode.NOT_EXECUTED,
                },
                lease_token=lease_token,
            )
            return clarified
        if outcome.classification is PlanningClassification.UNSUPPORTED:
            blocked, _ = self.store.transition(
                task.task_id,
                TaskStatus.BLOCKED,
                EventType.PLANNING_REJECTED,
                self.clock(),
                payload={
                    "code": outcome.error_code,
                    "reason": outcome.error_message,
                    "planning_metadata": planning_metadata,
                    "planner_mode": self.planner.mode.value,
                },
                updates={
                    **self._clear_claim_updates(),
                    "planning_metadata": planning_metadata,
                    "planner_mode": self.planner.mode,
                    "phase": "planning_unsupported",
                    "execution_mode": ExecutionMode.NOT_EXECUTED,
                    "error": TaskError(
                        outcome.error_code or "unsupported_request",
                        outcome.error_message or "request is not supported",
                    ),
                },
                lease_token=lease_token,
            )
            return blocked
        if outcome.classification is PlanningClassification.FAILED:
            failed, _ = self.store.transition(
                task.task_id,
                TaskStatus.PLANNING_FAILED,
                EventType.PLANNING_FAILED,
                self.clock(),
                payload={
                    "code": outcome.error_code,
                    "reason": outcome.error_message,
                    "planning_metadata": planning_metadata,
                },
                updates={
                    **self._clear_claim_updates(),
                    "planning_metadata": planning_metadata,
                    "planner_mode": self.planner.mode,
                    "phase": "planning_failed",
                    "execution_mode": ExecutionMode.NOT_EXECUTED,
                    "error": TaskError(
                        outcome.error_code or "planning_failed",
                        outcome.error_message or "planning failed",
                    ),
                },
                lease_token=lease_token,
            )
            return failed

        if outcome.request is None or outcome.plan is None:
            failed, _ = self.store.transition(
                task.task_id,
                TaskStatus.PLANNING_FAILED,
                EventType.PLANNING_FAILED,
                self.clock(),
                payload={
                    "code": "incomplete_planning_outcome",
                    "planning_metadata": planning_metadata,
                },
                updates={
                    **self._clear_claim_updates(),
                    "planning_metadata": planning_metadata,
                    "planner_mode": self.planner.mode,
                    "phase": "planning_failed",
                    "execution_mode": ExecutionMode.NOT_EXECUTED,
                    "error": TaskError(
                        "incomplete_planning_outcome",
                        "planned outcome is missing request or plan",
                    ),
                },
                lease_token=lease_token,
            )
            return failed

        planned, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.PLANNING_COMPLETED,
            self.clock(),
            payload={"planning_metadata": planning_metadata},
            updates={
                "request": outcome.request,
                "plan": outcome.plan,
                "planner_mode": self.planner.mode,
                "planning_metadata": planning_metadata,
                "clarification_questions": [],
                "missing_fields": [],
                "error": None,
                "phase": "plan_ready",
            },
            lease_token=lease_token,
        )
        running, _ = self.store.transition(
            planned.task_id,
            TaskStatus.RUNNING,
            EventType.TASK_PLANNED,
            self.clock(),
            payload={"plan": outcome.plan.to_dict()},
            updates={"phase": "planned"},
            lease_token=lease_token,
        )
        if self._is_multi_step_plan(running):
            return self._execute_multi_step_plan(running, lease_token)
        decision = self.policy.evaluate(
            request=running.request,
            plan=outcome.plan,
            task_id=running.task_id,
            artifacts_root=self.artifacts_root,
        )
        if not decision.allowed:
            return self._block(
                task,
                lease_token,
                code=decision.code,
                reason=decision.reason,
                phase="blocked",
                mark_not_executed=True,
            )
        return self._execute_tool(running, lease_token)

    def _resume_interrupted(self, task: Task, lease_token: str) -> Task:
        if (
            task.plan is not None
            and task.plan.steps
            and task.plan.steps[0].tool_name == RESEARCH_REPORT
        ):
            return self._resume_research_report(task, lease_token)
        if (
            task.plan is not None
            and task.plan.steps
            and task.plan.steps[0].tool_name == INSPECT_WORKSPACE
        ):
            running, _ = self.store.transition(
                task.task_id,
                TaskStatus.RUNNING,
                EventType.TASK_RESUMED,
                self.clock(),
                payload={"resume": True, "source": "read_only_workspace_inspection"},
                updates={"phase": "resumed_workspace_inspection", "error": None},
                lease_token=lease_token,
            )
            return self._execute_workspace_inspection(
                running,
                lease_token,
                running.plan.steps[0],
            )
        if (
            task.plan is not None
            and task.plan.steps
            and task.plan.steps[0].tool_name == EDIT_TEXT_FILE
        ):
            return self._resume_workspace_edit(task, lease_token)
        if self._is_multi_step_plan(task):
            running, _ = self.store.transition(
                task.task_id,
                TaskStatus.RUNNING,
                EventType.TASK_RESUMED,
                self.clock(),
                payload={
                    "resume": True,
                    "source": "multi_step_plan",
                },
                updates={
                    "phase": "resumed_multi_step",
                    "error": None,
                },
                lease_token=lease_token,
            )
            return self._execute_multi_step_plan(running, lease_token)
        if self._is_test_plan(task):
            return self._resume_test_task(task, lease_token)
        if self._is_read_plan(task):
            evidence = (
                task.tool_result.read_evidence
                if task.tool_result is not None
                else None
            )
            if evidence is not None:
                verifying, _ = self.store.transition(
                    task.task_id,
                    TaskStatus.VERIFYING,
                    EventType.VERIFICATION_STARTED,
                    self.clock(),
                    payload={
                        "resume": True,
                        "source": "persisted_read_evidence",
                    },
                    updates={
                        "phase": "resumed_read_verification",
                        "error": None,
                    },
                    lease_token=lease_token,
                )
                return self._verify_read_and_finish(
                    verifying,
                    lease_token,
                )
        can_verify_existing = (
            task.tool_result is not None
            and task.tool_result.execution_mode is ExecutionMode.REAL
            and task.artifact is not None
        )
        if can_verify_existing:
            verifying, _ = self.store.transition(
                task.task_id,
                TaskStatus.VERIFYING,
                EventType.VERIFICATION_STARTED,
                self.clock(),
                payload={
                    "resume": True,
                    "source": "persisted_tool_evidence",
                    "attempt_id": task.attempt_id,
                },
                updates={"phase": "resumed_verification", "error": None},
                lease_token=lease_token,
            )
            return self._verify_and_finish(verifying, lease_token)

        plan = task.plan
        if plan is None:
            return self._plan_with_current_status(task, lease_token)
        decision = self.policy.evaluate(
            request=task.request,
            plan=plan,
            task_id=task.task_id,
            artifacts_root=self.artifacts_root,
        )
        if not decision.allowed:
            return self._block(
                task,
                lease_token,
                code=decision.code,
                reason=decision.reason,
                phase="blocked",
                mark_not_executed=(
                    task.execution_mode is ExecutionMode.NOT_EXECUTED
                ),
            )
        running, _ = self.store.transition(
            task.task_id,
            TaskStatus.RUNNING,
            EventType.TASK_RESUMED,
            self.clock(),
            payload={"resume": True, "source": "no_persisted_artifact"},
            updates={"phase": "resumed_before_tool", "error": None},
            lease_token=lease_token,
        )
        return self._execute_tool(running, lease_token)

    @staticmethod
    def _is_multi_step_plan(task: Task) -> bool:
        return bool(
            task.plan is not None
            and (
                task.request.operation == MULTI_STEP_OPERATION
                or len(task.plan.steps) > 1
            )
        )

    def _execute_multi_step_plan(
        self,
        task: Task,
        lease_token: str,
    ) -> Task:
        """Execute a validated plan using existing verified child tasks."""
        plan = task.plan
        if plan is None:
            return self._block(
                task,
                lease_token,
                code="multi_step_plan_missing",
                reason="multi-step execution requires a persisted plan",
                phase="multi_step_plan_missing",
                mark_not_executed=True,
            )
        existing = {
            result.step_id: result for result in task.step_results
        }
        completed = [
            step_id
            for step_id, result in existing.items()
            if result.status == "succeeded"
        ]
        try:
            ordered = validate_plan_graph(
                plan,
                completed_step_ids=completed,
            )
        except MultiStepPlanError as exc:
            return self._block(
                task,
                lease_token,
                code=exc.code,
                reason=exc.message,
                phase="multi_step_plan_invalid",
                mark_not_executed=True,
            )

        for step in ordered:
            latest = self.store.require_task(task.task_id)
            self._assert_execution_authority(latest, lease_token)
            cancelled = self._cancel_if_requested(
                latest,
                lease_token,
                checkpoint=f"before_step:{step.step_id}",
                side_effect_observed=any(
                    result.artifact is not None
                    for result in latest.step_results
                ),
            )
            if cancelled is not None:
                return cancelled
            if latest.pause_requested_at is not None:
                paused, _ = self.store.transition(
                    latest.task_id,
                    TaskStatus.PAUSED,
                    EventType.TASK_PAUSED,
                    self.clock(),
                    payload={
                        "step_id": step.step_id,
                        "boundary": "before_step",
                    },
                    updates={
                        **self._clear_claim_updates(),
                        "phase": "paused_before_step",
                    },
                    lease_token=lease_token,
                )
                return paused
            completed_result = next(
                (
                    result
                    for result in latest.step_results
                    if result.step_id == step.step_id
                    and result.status == "succeeded"
                ),
                None,
            )
            if completed_result is not None:
                continue
            unresolved = [
                dependency
                for dependency in step.depends_on
                if not any(
                    result.step_id == dependency
                    and result.status == "succeeded"
                    for result in latest.step_results
                )
            ]
            if unresolved:
                skipped = StepResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    status="skipped",
                    attempt_count=0,
                    child_task_id=None,
                    started_at=None,
                    finished_at=self.clock(),
                    error=TaskError(
                        "dependency_not_satisfied",
                        "required step(s) did not succeed: "
                        + ", ".join(unresolved),
                    ),
                )
                task = self._persist_multi_step_result(
                    latest,
                    lease_token,
                    skipped,
                )
                if plan.failure_policy == "fail_fast":
                    break
                continue
            # Gate on the freshly re-read task, never on the loop's local copy:
            # the approval record must be read at the moment of the decision,
            # not remembered from when the loop started (R-08).
            if step.requires_approval and not step_is_approved(step, latest):
                # A matching step_id is not enough: the approved content must
                # be the content about to run. If a replan changed the
                # parameters under the same id, this asks again (R-08).
                waiting, _ = self.store.transition(
                    task.task_id,
                    TaskStatus.AWAITING_APPROVAL,
                    EventType.STEP_APPROVAL_REQUIRED,
                    self.clock(),
                    payload={
                        "step_id": step.step_id,
                        "tool_name": step.tool_name,
                        "expected_result": step.expected_result,
                        "step_digest": step_approval_digest(
                            step, plan.plan_id
                        ),
                        "reason": (
                            "approval_content_changed"
                            if step.step_id in latest.approved_step_ids
                            else "approval_required"
                        ),
                    },
                    updates={
                        **self._clear_claim_updates(),
                        "current_step_id": step.step_id,
                        "pending_approval_step_id": step.step_id,
                        "phase": "awaiting_step_approval",
                    },
                    lease_token=lease_token,
                )
                return waiting

            outputs = {
                result.step_id: result.output
                for result in task.step_results
            }
            running_result = next(
                (
                    result
                    for result in task.step_results
                    if result.step_id == step.step_id
                    and result.status == "running"
                    and result.child_task_id is not None
                ),
                None,
            )
            if running_result is None:
                running_result = StepResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    status="running",
                    attempt_count=1,
                    child_task_id=str(uuid4()),
                    started_at=self.clock(),
                    finished_at=None,
                )
                task = self._persist_multi_step_result(
                    task,
                    lease_token,
                    running_result,
                )
            while True:
                result = self._execute_multi_step_child(
                    task,
                    lease_token,
                    step,
                    outputs,
                    child_task_id=str(running_result.child_task_id),
                    attempt_count=running_result.attempt_count,
                    started_at=running_result.started_at or self.clock(),
                )
                child = (
                    self.store.get_task(str(result.child_task_id))
                    if result.child_task_id is not None
                    else None
                )
                safe_to_retry = bool(
                    result.status != "succeeded"
                    and child is not None
                    and step.retry_on_not_executed
                    and running_result.attempt_count < step.max_attempts
                    and child.execution_mode is ExecutionMode.NOT_EXECUTED
                    and not any(
                        event.event_type == EventType.TOOL_STARTED.value
                        for event in self.store.list_events(child.task_id)
                    )
                )
                if not safe_to_retry:
                    break
                running_result = StepResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    status="running",
                    attempt_count=running_result.attempt_count + 1,
                    child_task_id=str(uuid4()),
                    started_at=self.clock(),
                    finished_at=None,
                )
                task = self._persist_multi_step_result(
                    task,
                    lease_token,
                    running_result,
                )
            task = self._persist_multi_step_result(
                task,
                lease_token,
                result,
            )
            latest = self.store.require_task(task.task_id)
            if latest.pause_requested_at is not None:
                paused, _ = self.store.transition(
                    latest.task_id,
                    TaskStatus.PAUSED,
                    EventType.TASK_PAUSED,
                    self.clock(),
                    payload={
                        "step_id": step.step_id,
                        "boundary": "after_step",
                    },
                    updates={
                        **self._clear_claim_updates(),
                        "phase": "paused_after_step",
                    },
                    lease_token=lease_token,
                )
                return paused
            if result.status != "succeeded" and plan.failure_policy == "fail_fast":
                break

        latest = self.store.require_task(task.task_id)
        return self._finalize_multi_step_plan(latest, lease_token)

    def _execute_multi_step_child(
        self,
        parent: Task,
        lease_token: str,
        step: PlanStep,
        outputs: Mapping[str, Mapping[str, Any]],
        *,
        child_task_id: str,
        attempt_count: int,
        started_at: str,
    ) -> StepResult:
        self._assert_execution_authority(parent, lease_token)
        try:
            parameters = resolve_step_parameters(step.parameters, outputs)
        except MultiStepPlanError as exc:
            return StepResult(
                step_id=step.step_id,
                tool_name=step.tool_name,
                status="failed",
                attempt_count=attempt_count,
                child_task_id=None,
                started_at=started_at,
                finished_at=self.clock(),
                error=TaskError(exc.code, exc.message),
            )
        request = TaskRequest(
            goal=(
                f"{parent.request.goal or 'multi-step task'} "
                f"[{step.step_id}] {step.expected_result}"
            ),
            operation=step.tool_name,
            parameters=parameters,
        )
        child = self.store.get_task(child_task_id)
        if child is None:
            child = self.enqueue(
                request,
                parent_task_id=parent.task_id,
                task_id=child_task_id,
            )
        else:
            if (
                child.parent_task_id != parent.task_id
                or child.request.to_dict() != request.to_dict()
            ):
                return StepResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    status="failed",
                    attempt_count=attempt_count,
                    child_task_id=child.task_id,
                    started_at=started_at,
                    finished_at=self.clock(),
                    error=TaskError(
                        "step_child_identity_conflict",
                        "persisted child task does not match the step intent",
                    ),
                )
        if not is_terminal(child.status):
            child = self.execute_task(child.task_id)
        succeeded = child.status is TaskStatus.SUCCEEDED
        status = (
            "succeeded"
            if succeeded
            else "blocked"
            if child.status is TaskStatus.BLOCKED
            else "failed"
        )
        output = self._multi_step_child_output(child)
        return StepResult(
            step_id=step.step_id,
            tool_name=step.tool_name,
            status=status,
            attempt_count=attempt_count,
            child_task_id=child.task_id,
            started_at=started_at,
            finished_at=self.clock(),
            artifact=child.artifact,
            verification=child.verification,
            error=child.error,
            output=output,
        )

    def _multi_step_child_output(self, child: Task) -> dict[str, Any]:
        output: dict[str, Any] = {
            "status": child.status.value,
            "child_task_id": child.task_id,
            "execution_mode": child.execution_mode.value,
            "verification_passed": bool(
                child.verification is not None
                and child.verification.passed
            ),
        }
        if child.artifact is not None:
            output.update(
                {
                    "artifact_path": child.artifact.relative_path,
                    "artifact_sha256": child.artifact.sha256,
                    "byte_count": child.artifact.byte_count,
                }
            )
        evidence = (
            child.tool_result.read_evidence
            if child.tool_result is not None
            else None
        )
        if evidence is not None:
            output.update(
                {
                    "artifact_path": evidence.relative_path,
                    "artifact_sha256": evidence.sha256,
                    "byte_count": evidence.byte_count,
                    "content": evidence.content,
                }
            )
        elif child.artifact is not None:
            try:
                content, _, _ = read_artifact_text(
                    self.artifacts_root,
                    child.task_id,
                    child.artifact.relative_path,
                )
            except ArtifactToolError:
                content = None
            if content is not None:
                output["content"] = content
        inspection = (
            child.tool_result.workspace_inspection
            if child.tool_result is not None
            else None
        )
        if inspection is not None:
            output.update(
                {
                    "inspection_workspace": inspection.get("workspace"),
                    "inspection_root_path": inspection.get("root_path"),
                    "inspection_file_count": inspection.get("file_count"),
                    "inspection_truncated": inspection.get("truncated"),
                    "selected_path": inspection.get("selected_path"),
                    "selected_sha256": inspection.get("selected_sha256"),
                    "selected_content": inspection.get("selected_content"),
                }
            )
        workspace_edit = (
            child.tool_result.workspace_edit
            if child.tool_result is not None
            else None
        )
        if workspace_edit is not None:
            output.update(
                {
                    "workspace": workspace_edit.get("workspace"),
                    "path": workspace_edit.get("path"),
                    "source_sha256": workspace_edit.get("source_sha256"),
                    "result_sha256": workspace_edit.get("result_sha256"),
                    "result_byte_count": workspace_edit.get(
                        "result_byte_count"
                    ),
                    "backup_path": workspace_edit.get("backup_path"),
                }
            )
        test_evidence = (
            child.tool_result.test_evidence
            if child.tool_result is not None
            else None
        )
        if test_evidence is not None:
            output["test_exit_code"] = test_evidence.exit_code
            output["test_evidence_id"] = test_evidence.evidence_id
        return output

    def _persist_multi_step_result(
        self,
        task: Task,
        lease_token: str,
        result: StepResult,
    ) -> Task:
        results = [
            item
            for item in task.step_results
            if item.step_id != result.step_id
        ]
        results.append(result)
        event_type = {
            "running": EventType.STEP_STARTED,
            "succeeded": EventType.STEP_COMPLETED,
            "failed": EventType.STEP_FAILED,
            "blocked": EventType.STEP_FAILED,
            "skipped": EventType.STEP_SKIPPED,
        }.get(result.status, EventType.STEP_FAILED)
        updated, _ = self.store.record_event_and_update(
            task.task_id,
            event_type,
            self.clock(),
            payload={"step_result": result.to_dict()},
            updates={
                "step_results": results,
                "current_step_id": None,
                "phase": f"step_{result.status}",
                "execution_mode": (
                    ExecutionMode.REAL
                    if result.artifact is not None
                    or result.output.get("execution_mode") == "real"
                    else task.execution_mode
                ),
            },
            lease_token=lease_token,
        )
        return updated

    def _publish_multi_step_delivery(
        self,
        task: Task,
        lease_token: str,
        results: list[StepResult],
    ) -> tuple[ArtifactEvidence | None, VerificationResult | None]:
        """Publish one parent-owned copy of the final verified step artifact."""
        source_result = next(
            (
                result
                for result in reversed(results)
                if result.status == "succeeded"
                and result.artifact is not None
                and result.child_task_id is not None
            ),
            None,
        )
        if source_result is None:
            return None, None
        assert source_result.artifact is not None
        assert source_result.child_task_id is not None

        content, source_raw, source_sha256 = read_artifact_text(
            self.artifacts_root,
            source_result.child_task_id,
            source_result.artifact.relative_path,
        )
        if (
            source_sha256 != source_result.artifact.sha256
            or len(source_raw) != source_result.artifact.byte_count
        ):
            raise ArtifactToolError(
                "multi_step_child_artifact_changed",
                "the final child artifact no longer matches step evidence",
            )

        artifact_path = source_result.artifact.relative_path
        parent_path = self.artifacts_root / task.task_id / artifact_path
        if parent_path.exists() or parent_path.is_symlink():
            parent_content, parent_raw, parent_sha256 = read_artifact_text(
                self.artifacts_root,
                task.task_id,
                artifact_path,
            )
            if parent_content != content or parent_raw != source_raw:
                raise ArtifactToolError(
                    "multi_step_delivery_conflict",
                    "the parent delivery artifact does not match the final step",
                )
            delivery_result = ToolResult(
                attempt_id=task.attempt_id,
                tool_name="multi_step_delivery",
                execution_mode=ExecutionMode.REAL,
                success=True,
                started_at=self.clock(),
                finished_at=self.clock(),
                artifact=ArtifactEvidence(
                    relative_path=artifact_path,
                    byte_count=len(parent_raw),
                    sha256=parent_sha256,
                    attempt_id=task.attempt_id,
                    claim_generation=task.claim_generation,
                ),
            )
        else:
            if (
                getattr(self.writer, "execution_mode", None)
                is not ExecutionMode.REAL
            ):
                raise ArtifactToolError(
                    "multi_step_delivery_requires_real_writer",
                    "multi-step delivery requires a real artifact writer",
                )
            started_at = self.clock()
            delivery_result = self.writer.write(
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                artifact_path=artifact_path,
                content=content,
                started_at=started_at,
                finished_at=self.clock(),
                lease_token=lease_token,
                claim_generation=task.claim_generation,
                authorize=lambda token, generation: self._authorize_publication(
                    task.task_id,
                    task.attempt_id,
                    token,
                    generation,
                ),
            )
            if delivery_result.artifact is not None:
                delivery_result.artifact.attempt_id = task.attempt_id
                delivery_result.artifact.claim_generation = (
                    task.claim_generation
                )

        verification = self.verifier.verify(
            task_id=task.task_id,
            artifact_path=artifact_path,
            expected_content=content,
            tool_result=delivery_result,
            checked_at=self.clock(),
            expected_attempt_id=task.attempt_id,
            require_provenance=True,
            maximum_claim_generation=task.claim_generation,
        )
        if not verification.passed or verification.artifact is None:
            raise ArtifactToolError(
                "multi_step_delivery_unverified",
                "the parent delivery artifact could not be verified: "
                f"{verification.reason}",
            )
        return verification.artifact, verification

    def _finalize_multi_step_plan(
        self,
        task: Task,
        lease_token: str,
    ) -> Task:
        self._assert_execution_authority(task, lease_token)
        results = list(task.step_results)
        success_count = sum(
            result.status == "succeeded" for result in results
        )
        all_success = success_count == len(task.plan.steps if task.plan else [])
        verifying, _ = self.store.transition(
            task.task_id,
            TaskStatus.VERIFYING,
            EventType.VERIFICATION_STARTED,
            self.clock(),
            payload={
                "multi_step": True,
                "step_count": len(results),
                "succeeded_steps": success_count,
                "attempt_id": task.attempt_id,
            },
            updates={"phase": "multi_step_verification"},
            lease_token=lease_token,
        )
        delivery_artifact: ArtifactEvidence | None = None
        delivery_verification: VerificationResult | None = None
        if all_success:
            try:
                (
                    delivery_artifact,
                    delivery_verification,
                ) = self._publish_multi_step_delivery(
                    verifying,
                    lease_token,
                    results,
                )
            except ArtifactToolError as exc:
                return self._finish_tool_failure(
                    verifying,
                    lease_token,
                    exc.code,
                    exc.message,
                    self.clock(),
                    tool_name="multi_step_delivery",
                )
        delivery_passed = bool(
            delivery_verification is None
            or delivery_verification.passed
        )
        verification = VerificationResult(
            verifier_name="multi-step-aggregate-v1",
            passed=all_success and delivery_passed,
            exists=bool(results),
            path_safe=True,
            content_matches=all_success and delivery_passed,
            evidence_matches=all_success and delivery_passed,
            reason=(
                "multi_step_all_steps_verified"
                if all_success and delivery_passed
                else "multi_step_delivery_unverified"
                if all_success
                else "multi_step_partial_or_failed"
            ),
            artifact=delivery_artifact,
            checked_at=self.clock(),
        )
        verifying, _ = self.store.record_event_and_update(
            verifying.task_id,
            EventType.VERIFICATION_COMPLETED,
            self.clock(),
            payload={
                "verification": verification.to_dict(),
                "step_results": [item.to_dict() for item in results],
                "delivery_verification": (
                    delivery_verification.to_dict()
                    if delivery_verification is not None
                    else None
                ),
                "attempt_id": task.attempt_id,
            },
            updates={
                "verification": verification,
                "artifact": delivery_artifact,
                "phase": (
                    "multi_step_verified"
                    if all_success
                    else "multi_step_incomplete"
                ),
            },
            lease_token=lease_token,
        )
        if all_success:
            succeeded, _ = self.store.transition(
                verifying.task_id,
                TaskStatus.SUCCEEDED,
                EventType.TASK_SUCCEEDED,
                self.clock(),
                payload={
                    "multi_step": True,
                    "step_count": len(results),
                    "attempt_id": task.attempt_id,
                },
                updates=self._clear_claim_updates(),
                lease_token=lease_token,
            )
            return succeeded
        if success_count:
            partial, _ = self.store.transition(
                verifying.task_id,
                TaskStatus.PARTIAL,
                EventType.TASK_PARTIAL,
                self.clock(),
                payload={
                    "succeeded_steps": success_count,
                    "failed_steps": len(results) - success_count,
                    "attempt_id": task.attempt_id,
                },
                updates={
                    **self._clear_claim_updates(),
                    "phase": "multi_step_partial",
                    "error": TaskError(
                        "multi_step_partial",
                        "some steps succeeded while others failed or were skipped",
                    ),
                },
                lease_token=lease_token,
            )
            return partial
        failed, _ = self.store.transition(
            verifying.task_id,
            TaskStatus.FAILED,
            EventType.TASK_FAILED,
            self.clock(),
            payload={
                "code": "multi_step_failed",
                "step_results": [item.to_dict() for item in results],
                "attempt_id": task.attempt_id,
            },
            updates={
                **self._clear_claim_updates(),
                "phase": "multi_step_failed",
                "error": TaskError(
                    "multi_step_failed",
                    "no multi-step task step succeeded",
                ),
            },
            lease_token=lease_token,
        )
        return failed

    @staticmethod
    def _is_test_plan(task: Task) -> bool:
        return bool(
            task.plan is not None
            and task.plan.steps
            and task.plan.steps[0].tool_name == RUN_TESTS_TOOL
        )

    @staticmethod
    def _is_read_plan(task: Task) -> bool:
        return bool(
            task.plan is not None
            and task.plan.steps
            and task.plan.steps[0].tool_name == READ_TEXT_ARTIFACT
        )

    @staticmethod
    def _is_modify_plan(task: Task) -> bool:
        return bool(
            task.plan is not None
            and task.plan.steps
            and task.plan.steps[0].tool_name == MODIFY_TEXT_ARTIFACT
        )

    def _resolve_source_artifact(
        self,
        parameters: Mapping[str, Any],
    ) -> tuple[Task, str, str, bytes, str]:
        """Resolve one immutable, independently verified source artifact."""
        source_task_id = str(parameters["source_task_id"])
        artifact_path = str(parameters["artifact_path"])
        source = self.store.require_task(source_task_id)
        if (
            source.status is not TaskStatus.SUCCEEDED
            or source.verification is None
            or not source.verification.passed
            or source.artifact is None
            or source.artifact.relative_path != artifact_path
        ):
            raise ArtifactToolError(
                "source_artifact_not_verified",
                "source task does not contain the requested verified artifact",
            )
        expected_sha256 = parameters.get("expected_sha256")
        if (
            expected_sha256 is not None
            and source.artifact.sha256 != str(expected_sha256).lower()
        ):
            raise ArtifactToolError(
                "source_artifact_changed",
                "source artifact digest does not match the expected version",
            )
        try:
            text, raw, digest = read_artifact_text(
                self.artifacts_root,
                source_task_id,
                artifact_path,
            )
        except ArtifactToolError:
            raise
        except FileNotFoundError as exc:
            raise ArtifactToolError(
                "source_artifact_missing",
                "source artifact no longer exists",
            ) from exc
        if (
            len(raw) != source.artifact.byte_count
            or digest != source.artifact.sha256
        ):
            raise ArtifactToolError(
                "source_artifact_changed",
                "source artifact bytes no longer match verified evidence",
            )
        return source, artifact_path, text, raw, digest

    def _derive_modify_content(
        self,
        parameters: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        source, artifact_path, source_text, raw, digest = (
            self._resolve_source_artifact(parameters)
        )
        patch = parameters.get("content")
        if not isinstance(patch, str) or not patch:
            raise ArtifactToolError(
                "invalid_content",
                "modification content must be a non-empty string",
            )
        mode = parameters.get("mode", "replace")
        if mode == "replace":
            result_text = patch
        elif mode == "append":
            result_text = source_text + patch
        else:
            raise ArtifactToolError(
                "invalid_modify_mode",
                "mode must be replace or append",
            )
        result_bytes = result_text.encode("utf-8")
        if len(result_bytes) > MAX_ARTIFACT_BYTES:
            raise ArtifactToolError(
                "content_too_large",
                f"result exceeds the {MAX_ARTIFACT_BYTES} byte limit",
            )
        lineage = {
            "operation": MODIFY_TEXT_ARTIFACT,
            "source_task_id": source.task_id,
            "source_artifact_path": artifact_path,
            "source_sha256": digest,
            "source_byte_count": len(raw),
            "source_attempt_id": source.artifact.attempt_id,
            "source_claim_generation": source.artifact.claim_generation,
            "source_content_sha256": hashlib.sha256(
                source_text.encode("utf-8")
            ).hexdigest(),
            "mode": mode,
            "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
            "result_byte_count": len(result_bytes),
        }
        return result_text, lineage

    def _resume_test_task(self, task: Task, lease_token: str) -> Task:
        """Never re-run a test whose result is unknown.

        A persisted test result is verified read-only; anything else is
        blocked as an unknown execution state instead of being retried.
        """
        evidence = (
            task.tool_result.test_evidence
            if task.tool_result is not None
            else None
        )
        if evidence is None:
            return self._block(
                task,
                lease_token,
                code="test_execution_unknown",
                reason=(
                    "test execution state is unknown; refusing to re-run"
                ),
                phase="blocked_test_unknown",
                mark_not_executed=False,
            )
        verifying, _ = self.store.transition(
            task.task_id,
            TaskStatus.VERIFYING,
            EventType.VERIFICATION_STARTED,
            self.clock(),
            payload={
                "resume": True,
                "source": "persisted_test_evidence",
            },
            updates={"phase": "resumed_test_verification", "error": None},
            lease_token=lease_token,
        )
        return self._verify_test_and_finish(
            verifying,
            lease_token,
            {
                "runner": evidence.runner,
                "target": evidence.target,
                "workspace": evidence.workspace,
                "evidence_id": evidence.evidence_id,
                "config_digest": evidence.config_digest,
                "command_digest": evidence.command_digest,
                "allowed_side_effects": list(
                    task.request.parameters.get("allowed_side_effects") or []
                ),
                "protected_paths": list(
                    task.request.parameters.get("protected_paths") or []
                ),
            },
        )

    def _execute_workspace_inspection(
        self,
        task: Task,
        lease_token: str,
        plan_step: PlanStep,
    ) -> Task:
        started_at = self.clock()
        cancelled = self._cancel_if_requested(
            task,
            lease_token,
            checkpoint="before_workspace_inspection",
            side_effect_observed=False,
        )
        if cancelled is not None:
            return cancelled
        self._assert_execution_authority(task, lease_token)
        try:
            inspection = inspect_workspace(
                self.project_root,
                dict(task.request.parameters),
            )
        except WorkspaceInspectionError as exc:
            return self._block(
                task,
                lease_token,
                code=exc.code,
                reason=exc.message,
                phase="workspace_inspection_blocked",
                mark_not_executed=True,
            )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_STARTED,
            started_at,
            payload={
                "tool_name": INSPECT_WORKSPACE,
                "operation": INSPECT_WORKSPACE,
                "workspace": task.request.parameters.get("workspace"),
                "attempt_id": task.attempt_id,
                "intended_execution_mode": ExecutionMode.REAL.value,
                "claim_generation": task.claim_generation,
                "plan_version": task.plan_version,
            },
            updates={
                "execution_mode": ExecutionMode.REAL,
                "phase": "workspace_inspection_started",
                "execution_plan_version": task.plan_version,
                "execution_plan_snapshot": plan_step.parameters,
            },
            lease_token=lease_token,
        )
        result = ToolResult(
            attempt_id=task.attempt_id,
            tool_name=INSPECT_WORKSPACE,
            execution_mode=ExecutionMode.REAL,
            success=True,
            started_at=started_at,
            finished_at=self.clock(),
            workspace_inspection=inspection,
            lineage={
                "operation": INSPECT_WORKSPACE,
                "workspace": inspection["workspace"],
                "file_count": inspection["file_count"],
                "truncated": inspection["truncated"],
            },
        )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_SUCCEEDED,
            self.clock(),
            payload={"tool_result": result.to_dict()},
            updates={
                "tool_result": result,
                "execution_mode": ExecutionMode.REAL,
                "phase": "workspace_inspection_completed",
            },
            lease_token=lease_token,
        )
        verifying, _ = self.store.transition(
            task.task_id,
            TaskStatus.VERIFYING,
            EventType.VERIFICATION_STARTED,
            self.clock(),
            payload={
                "workspace_inspection": True,
                "tool_result_trusted": False,
                "attempt_id": task.attempt_id,
            },
            updates={"phase": "workspace_inspection_verification"},
            lease_token=lease_token,
        )
        if self.fault_hook is not None:
            self.fault_hook(
                "after_workspace_inspection_before_verification",
                verifying,
            )
        return self._verify_workspace_inspection_and_finish(
            verifying,
            lease_token,
        )

    def _verify_workspace_inspection_and_finish(
        self,
        task: Task,
        lease_token: str,
    ) -> Task:
        self._assert_execution_authority(task, lease_token)
        evidence = (
            task.tool_result.workspace_inspection
            if task.tool_result is not None
            else None
        )
        passed, reason = verify_workspace_inspection(
            self.project_root,
            dict(task.request.parameters),
            evidence,
        )
        verification = VerificationResult(
            verifier_name="workspace-inspection-v1",
            passed=passed,
            exists=evidence is not None,
            path_safe=passed,
            content_matches=passed,
            evidence_matches=passed,
            reason=reason,
            artifact=None,
            checked_at=self.clock(),
        )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.VERIFICATION_COMPLETED,
            self.clock(),
            payload={
                "verification": verification.to_dict(),
                "attempt_id": task.attempt_id,
            },
            updates={
                "verification": verification,
                "phase": (
                    "workspace_inspection_verified"
                    if passed
                    else "workspace_inspection_verification_failed"
                ),
            },
            lease_token=lease_token,
        )
        if passed:
            succeeded, _ = self.store.transition(
                task.task_id,
                TaskStatus.SUCCEEDED,
                EventType.TASK_SUCCEEDED,
                self.clock(),
                payload={
                    "workspace_inspection": True,
                    "attempt_id": task.attempt_id,
                },
                updates=self._clear_claim_updates(),
                lease_token=lease_token,
            )
            return succeeded
        failed, _ = self.store.transition(
            task.task_id,
            TaskStatus.FAILED,
            EventType.TASK_FAILED,
            self.clock(),
            payload={"code": reason},
            updates={
                **self._clear_claim_updates(),
                "error": TaskError(reason, reason),
                "phase": "workspace_inspection_failed",
            },
            lease_token=lease_token,
        )
        return failed

    def _execute_workspace_edit(
        self,
        task: Task,
        lease_token: str,
        plan_step: PlanStep,
    ) -> Task:
        started_at = self.clock()
        cancelled = self._cancel_if_requested(
            task,
            lease_token,
            checkpoint="before_workspace_edit",
            side_effect_observed=False,
        )
        if cancelled is not None:
            return cancelled
        self._assert_execution_authority(task, lease_token)
        parameters = dict(task.request.parameters)
        try:
            precondition = validate_edit_precondition(
                self.project_root,
                parameters,
            )
        except WorkspaceEditError as exc:
            return self._block(
                task,
                lease_token,
                code=exc.code,
                reason=exc.message,
                phase="workspace_edit_blocked",
                mark_not_executed=True,
            )
        expected_result_sha256 = str(precondition["result_sha256"])
        expected_source_sha256 = str(precondition["source_sha256"])
        snapshot = plan_step.to_dict()
        snapshot["workspace_edit_expectation"] = {
            "workspace": parameters.get("workspace"),
            "path": parameters.get("path"),
            "source_sha256": expected_source_sha256,
            "result_sha256": expected_result_sha256,
        }
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_STARTED,
            started_at,
            payload={
                "tool_name": EDIT_TEXT_FILE,
                "operation": EDIT_TEXT_FILE,
                "workspace": parameters.get("workspace"),
                "path": parameters.get("path"),
                "source_sha256": expected_source_sha256,
                "result_sha256": expected_result_sha256,
                "attempt_id": task.attempt_id,
                "intended_execution_mode": ExecutionMode.REAL.value,
                "claim_generation": task.claim_generation,
                "plan_version": task.plan_version,
            },
            updates={
                "execution_mode": ExecutionMode.REAL,
                "phase": "workspace_edit_started",
                "execution_plan_version": task.plan_version,
                "execution_plan_snapshot": snapshot,
            },
            lease_token=lease_token,
        )
        try:
            edit = apply_text_edit(
                self.project_root,
                self.artifacts_root,
                task.task_id,
                parameters,
            )
        except WorkspaceEditError as exc:
            try:
                observed = workspace_file_observation(
                    self.project_root,
                    parameters,
                )
            except WorkspaceEditError:
                observed = None
            if observed is not None and observed["sha256"] == expected_result_sha256:
                edit = {
                    "schema_version": "workspace-edit-v1",
                    "workspace": parameters.get("workspace"),
                    "path": parameters.get("path"),
                    "source_sha256": expected_source_sha256,
                    "result_sha256": expected_result_sha256,
                    "observed_sha256": observed["sha256"],
                    "source_byte_count": precondition["original_bytes"].__len__(),
                    "result_byte_count": observed["byte_count"],
                    "backup_path": None,
                    "authorization": parameters.get("authorization"),
                    "recovered_after_error": exc.code,
                }
            else:
                return self._finish_tool_failure(
                    task,
                    lease_token,
                    exc.code,
                    exc.message,
                    started_at,
                    tool_name=EDIT_TEXT_FILE,
                )
        result = ToolResult(
            attempt_id=task.attempt_id,
            tool_name=EDIT_TEXT_FILE,
            execution_mode=ExecutionMode.REAL,
            success=True,
            started_at=started_at,
            finished_at=self.clock(),
            workspace_edit=edit,
            lineage={
                "operation": EDIT_TEXT_FILE,
                "workspace": edit["workspace"],
                "path": edit["path"],
                "source_sha256": edit["source_sha256"],
                "result_sha256": edit["result_sha256"],
            },
        )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_SUCCEEDED,
            self.clock(),
            payload={"tool_result": result.to_dict()},
            updates={
                "tool_result": result,
                "execution_mode": ExecutionMode.REAL,
                "phase": "workspace_edit_completed",
            },
            lease_token=lease_token,
        )
        verifying, _ = self.store.transition(
            task.task_id,
            TaskStatus.VERIFYING,
            EventType.VERIFICATION_STARTED,
            self.clock(),
            payload={
                "workspace_edit": True,
                "tool_result_trusted": False,
                "attempt_id": task.attempt_id,
            },
            updates={"phase": "workspace_edit_verification"},
            lease_token=lease_token,
        )
        if self.fault_hook is not None:
            self.fault_hook(
                "after_workspace_edit_before_verification",
                verifying,
            )
        return self._verify_workspace_edit_and_finish(
            verifying,
            lease_token,
        )

    def _resume_workspace_edit(
        self,
        task: Task,
        lease_token: str,
    ) -> Task:
        assert task.plan is not None and task.plan.steps
        parameters = dict(task.request.parameters)
        evidence = (
            task.tool_result.workspace_edit
            if task.tool_result is not None
            else None
        )
        if evidence is None:
            try:
                observation = workspace_file_observation(
                    self.project_root,
                    parameters,
                )
            except WorkspaceEditError as exc:
                return self._block(
                    task,
                    lease_token,
                    code="workspace_edit_state_unknown",
                    reason=exc.message,
                    phase="workspace_edit_unknown",
                    mark_not_executed=False,
                )
            snapshot = task.execution_plan_snapshot or {}
            expectation = snapshot.get("workspace_edit_expectation")
            expected_result = (
                expectation.get("result_sha256")
                if isinstance(expectation, Mapping)
                else None
            )
            if observation["sha256"] != expected_result:
                return self._block(
                    task,
                    lease_token,
                    code="workspace_edit_state_unknown",
                    reason=(
                        "workspace edit result cannot be proven after "
                        "interruption"
                    ),
                    phase="workspace_edit_unknown",
                    mark_not_executed=False,
                )
            evidence = {
                "schema_version": "workspace-edit-v1",
                "workspace": parameters.get("workspace"),
                "path": parameters.get("path"),
                "source_sha256": parameters.get("expected_sha256"),
                "result_sha256": expected_result,
                "observed_sha256": observation["sha256"],
                "source_byte_count": None,
                "result_byte_count": observation["byte_count"],
                "backup_path": None,
                "authorization": parameters.get("authorization"),
                "recovered_from_intent": True,
            }
            result = ToolResult(
                attempt_id=task.attempt_id,
                tool_name=EDIT_TEXT_FILE,
                execution_mode=ExecutionMode.REAL,
                success=True,
                started_at=task.execution_started_at or self.clock(),
                finished_at=self.clock(),
                workspace_edit=evidence,
                lineage={
                    "operation": EDIT_TEXT_FILE,
                    "workspace": evidence["workspace"],
                    "path": evidence["path"],
                    "source_sha256": evidence["source_sha256"],
                    "result_sha256": evidence["result_sha256"],
                    "recovered_from_intent": True,
                },
            )
            task, _ = self.store.record_event_and_update(
                task.task_id,
                EventType.TOOL_SUCCEEDED,
                self.clock(),
                payload={"tool_result": result.to_dict()},
                updates={
                    "tool_result": result,
                    "execution_mode": ExecutionMode.REAL,
                    "phase": "workspace_edit_recovered",
                },
                lease_token=lease_token,
            )
        verifying, _ = self.store.transition(
            task.task_id,
            TaskStatus.VERIFYING,
            EventType.VERIFICATION_STARTED,
            self.clock(),
            payload={
                "resume": True,
                "workspace_edit": True,
                "source": "persisted_or_derived_edit_evidence",
            },
            updates={"phase": "resumed_workspace_edit_verification", "error": None},
            lease_token=lease_token,
        )
        return self._verify_workspace_edit_and_finish(
            verifying,
            lease_token,
        )

    def _verify_workspace_edit_and_finish(
        self,
        task: Task,
        lease_token: str,
    ) -> Task:
        self._assert_execution_authority(task, lease_token)
        parameters = task.request.parameters
        evidence = (
            task.tool_result.workspace_edit
            if task.tool_result is not None
            else None
        )
        snapshot = task.execution_plan_snapshot
        expectation = (
            snapshot.get("workspace_edit_expectation")
            if isinstance(snapshot, Mapping)
            else None
        )
        expected_result_sha256 = (
            expectation.get("result_sha256")
            if isinstance(expectation, Mapping)
            else None
        )
        expected_snapshot_binding = (
            isinstance(expectation, Mapping)
            and expectation.get("workspace") == parameters.get("workspace")
            and expectation.get("path") == parameters.get("path")
            and expectation.get("source_sha256")
            == parameters.get("expected_sha256")
        )
        if not expected_snapshot_binding:
            passed = False
            reason = "workspace_edit_expectation_missing"
        else:
            passed, reason = verify_text_edit(
                self.project_root,
                evidence,
                expected_workspace=str(parameters.get("workspace")),
                expected_path=str(parameters.get("path")),
                expected_source_sha256=str(
                    parameters.get("expected_sha256")
                ),
                expected_result_sha256=str(expected_result_sha256),
            )
        verification = VerificationResult(
            verifier_name="workspace-edit-v1",
            passed=passed,
            exists=evidence is not None,
            path_safe=passed,
            content_matches=passed,
            evidence_matches=passed,
            reason=reason,
            artifact=None,
            checked_at=self.clock(),
        )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.VERIFICATION_COMPLETED,
            self.clock(),
            payload={
                "verification": verification.to_dict(),
                "attempt_id": task.attempt_id,
            },
            updates={
                "verification": verification,
                "phase": (
                    "workspace_edit_verified"
                    if passed
                    else "workspace_edit_verification_failed"
                ),
            },
            lease_token=lease_token,
        )
        if passed:
            succeeded, _ = self.store.transition(
                task.task_id,
                TaskStatus.SUCCEEDED,
                EventType.TASK_SUCCEEDED,
                self.clock(),
                payload={
                    "workspace_edit": True,
                    "attempt_id": task.attempt_id,
                },
                updates=self._clear_claim_updates(),
                lease_token=lease_token,
            )
            return succeeded
        failed, _ = self.store.transition(
            task.task_id,
            TaskStatus.FAILED,
            EventType.TASK_FAILED,
            self.clock(),
            payload={"code": reason},
            updates={
                **self._clear_claim_updates(),
                "error": TaskError(reason, reason),
                "phase": "workspace_edit_failed",
            },
            lease_token=lease_token,
        )
        return failed

    def _execute_research_report(
        self,
        task: Task,
        lease_token: str,
        plan_step: PlanStep,
    ) -> Task:
        started_at = self.clock()
        cancelled = self._cancel_if_requested(
            task,
            lease_token,
            checkpoint="before_research_report",
            side_effect_observed=False,
        )
        if cancelled is not None:
            return cancelled
        self._assert_execution_authority(task, lease_token)
        parameters = dict(task.request.parameters)
        try:
            report, research_evidence = build_research_report(
                self.project_root,
                parameters,
                generated_at=started_at,
            )
        except ResearchWorkflowError as exc:
            return self._block(
                task,
                lease_token,
                code=exc.code,
                reason=exc.message,
                phase="research_report_blocked",
                mark_not_executed=True,
            )
        snapshot = plan_step.to_dict()
        snapshot["research_report_expectation"] = {
            "workspace": parameters.get("workspace"),
            "output_path": parameters.get("output_path"),
            "generated_at": started_at,
            "report_sha256": research_evidence["report_sha256"],
            "source_count": len(research_evidence["sources"]),
        }
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_STARTED,
            started_at,
            payload={
                "tool_name": RESEARCH_REPORT,
                "operation": RESEARCH_REPORT,
                "workspace": parameters.get("workspace"),
                "source_count": len(research_evidence["sources"]),
                "output_path": parameters.get("output_path"),
                "report_sha256": research_evidence["report_sha256"],
                "attempt_id": task.attempt_id,
                "intended_execution_mode": ExecutionMode.REAL.value,
                "claim_generation": task.claim_generation,
                "plan_version": task.plan_version,
            },
            updates={
                "execution_mode": ExecutionMode.REAL,
                "phase": "research_report_started",
                "execution_plan_version": task.plan_version,
                "execution_plan_snapshot": snapshot,
            },
            lease_token=lease_token,
        )
        try:
            result = self.writer.write(
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                artifact_path=str(parameters["output_path"]),
                content=report,
                started_at=started_at,
                finished_at=self.clock(),
                lease_token=lease_token,
                claim_generation=task.claim_generation,
                authorize=lambda token, generation: self._authorize_publication(
                    task.task_id,
                    task.attempt_id,
                    token,
                    generation,
                ),
            )
            result.tool_name = RESEARCH_REPORT
            result.research_evidence = research_evidence
            result.lineage = {
                "operation": RESEARCH_REPORT,
                "workspace": research_evidence["workspace"],
                "source_sha256": {
                    source["source_id"]: source["sha256"]
                    for source in research_evidence["sources"]
                },
                "report_sha256": research_evidence["report_sha256"],
            }
        except ClaimLost:
            raise
        except ArtifactToolError as exc:
            return self._finish_tool_failure(
                task,
                lease_token,
                exc.code,
                exc.message,
                started_at,
                tool_name=RESEARCH_REPORT,
            )
        except Exception as exc:
            return self._finish_tool_failure(
                task,
                lease_token,
                "research_report_exception",
                f"research report generation raised {type(exc).__name__}",
                started_at,
                tool_name=RESEARCH_REPORT,
            )
        latest = self.store.require_task(task.task_id)
        if latest.cancel_requested_at is not None:
            task = self._persist_tool_result_before_cancel(
                task,
                lease_token,
                result,
            )
            cancelled = self._cancel_if_requested(
                task,
                lease_token,
                checkpoint="after_research_report",
                side_effect_observed=result.success,
            )
            if cancelled is not None:
                return cancelled
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_SUCCEEDED,
            self.clock(),
            payload={"tool_result": result.to_dict()},
            updates={
                "tool_result": result,
                "artifact": result.artifact,
                "execution_mode": ExecutionMode.REAL,
                "phase": "research_report_written",
            },
            lease_token=lease_token,
        )
        verifying, _ = self.store.transition(
            task.task_id,
            TaskStatus.VERIFYING,
            EventType.VERIFICATION_STARTED,
            self.clock(),
            payload={
                "research_report": True,
                "tool_result_trusted": False,
                "attempt_id": task.attempt_id,
            },
            updates={"phase": "research_report_verification"},
            lease_token=lease_token,
        )
        if self.fault_hook is not None:
            self.fault_hook(
                "after_research_report_before_verification",
                verifying,
            )
        return self._verify_research_report_and_finish(
            verifying,
            lease_token,
        )

    def _resume_research_report(
        self,
        task: Task,
        lease_token: str,
    ) -> Task:
        parameters = dict(task.request.parameters)
        if (
            task.tool_result is not None
            and task.tool_result.research_evidence is not None
        ):
            verifying, _ = self.store.transition(
                task.task_id,
                TaskStatus.VERIFYING,
                EventType.VERIFICATION_STARTED,
                self.clock(),
                payload={
                    "resume": True,
                    "research_report": True,
                    "source": "persisted_research_evidence",
                },
                updates={
                    "phase": "resumed_research_report_verification",
                    "error": None,
                },
                lease_token=lease_token,
            )
            return self._verify_research_report_and_finish(
                verifying,
                lease_token,
            )
        output_path = str(parameters.get("output_path"))
        try:
            existing = read_artifact(
                self.artifacts_root,
                task.task_id,
                output_path,
            )
        except FileNotFoundError:
            running, _ = self.store.transition(
                task.task_id,
                TaskStatus.RUNNING,
                EventType.TASK_RESUMED,
                self.clock(),
                payload={
                    "resume": True,
                    "research_report": True,
                    "source": "no_published_report",
                },
                updates={
                    "phase": "resumed_research_report_generation",
                    "error": None,
                },
                lease_token=lease_token,
            )
            assert running.plan is not None and running.plan.steps
            return self._execute_research_report(
                running,
                lease_token,
                running.plan.steps[0],
            )
        except ArtifactToolError as exc:
            return self._block(
                task,
                lease_token,
                code="research_report_state_unknown",
                reason=exc.message,
                phase="research_report_unknown",
                mark_not_executed=False,
            )
        snapshot = task.execution_plan_snapshot
        expectation = (
            snapshot.get("research_report_expectation")
            if isinstance(snapshot, Mapping)
            else None
        )
        generated_at = (
            expectation.get("generated_at")
            if isinstance(expectation, Mapping)
            else None
        )
        if not isinstance(generated_at, str) or not generated_at:
            return self._block(
                task,
                lease_token,
                code="research_report_state_unknown",
                reason="research generation timestamp was not persisted",
                phase="research_report_unknown",
                mark_not_executed=False,
            )
        try:
            report, research_evidence = build_research_report(
                self.project_root,
                parameters,
                generated_at=generated_at,
            )
        except ResearchWorkflowError as exc:
            return self._block(
                task,
                lease_token,
                code="research_report_state_unknown",
                reason=exc.message,
                phase="research_report_unknown",
                mark_not_executed=False,
            )
        if existing != report.encode("utf-8"):
            return self._block(
                task,
                lease_token,
                code="research_report_state_unknown",
                reason=(
                    "published report does not match the deterministic "
                    "reconstruction"
                ),
                phase="research_report_unknown",
                mark_not_executed=False,
            )
        artifact = ArtifactEvidence(
            relative_path=output_path,
            byte_count=len(existing),
            sha256=hashlib.sha256(existing).hexdigest(),
            attempt_id=task.attempt_id,
            claim_generation=task.claim_generation,
        )
        result = ToolResult(
            attempt_id=task.attempt_id,
            tool_name=RESEARCH_REPORT,
            execution_mode=ExecutionMode.REAL,
            success=True,
            started_at=task.execution_started_at or self.clock(),
            finished_at=self.clock(),
            artifact=artifact,
            research_evidence=research_evidence,
            lineage={
                "operation": RESEARCH_REPORT,
                "workspace": research_evidence["workspace"],
                "source_sha256": {
                    source["source_id"]: source["sha256"]
                    for source in research_evidence["sources"]
                },
                "report_sha256": research_evidence["report_sha256"],
                "recovered_from_published_report": True,
            },
        )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_SUCCEEDED,
            self.clock(),
            payload={"tool_result": result.to_dict()},
            updates={
                "tool_result": result,
                "artifact": artifact,
                "execution_mode": ExecutionMode.REAL,
                "phase": "research_report_recovered",
            },
            lease_token=lease_token,
        )
        verifying, _ = self.store.transition(
            task.task_id,
            TaskStatus.VERIFYING,
            EventType.VERIFICATION_STARTED,
            self.clock(),
            payload={
                "resume": True,
                "research_report": True,
                "source": "published_report_reconstruction",
            },
            updates={"phase": "resumed_research_report_verification"},
            lease_token=lease_token,
        )
        return self._verify_research_report_and_finish(
            verifying,
            lease_token,
        )

    def _verify_research_report_and_finish(
        self,
        task: Task,
        lease_token: str,
    ) -> Task:
        self._assert_execution_authority(task, lease_token)
        cancelled = self._cancel_if_requested(
            task,
            lease_token,
            checkpoint="before_research_verification",
            side_effect_observed=task.artifact is not None,
        )
        if cancelled is not None:
            return cancelled
        parameters = dict(task.request.parameters)
        result = task.tool_result
        snapshot = task.execution_plan_snapshot
        expectation = (
            snapshot.get("research_report_expectation")
            if isinstance(snapshot, Mapping)
            else None
        )
        expected_binding = (
            isinstance(expectation, Mapping)
            and expectation.get("workspace") == parameters.get("workspace")
            and expectation.get("output_path") == parameters.get("output_path")
            and isinstance(expectation.get("generated_at"), str)
            and bool(expectation.get("generated_at"))
        )
        expected_generated_at = (
            str(expectation.get("generated_at"))
            if isinstance(expectation, Mapping)
            else ""
        )
        if result is None or not expected_binding:
            verification = VerificationResult(
                verifier_name="research-report-v1",
                passed=False,
                exists=False,
                path_safe=False,
                content_matches=False,
                evidence_matches=False,
                reason="research_expectation_missing",
                artifact=None,
                checked_at=self.clock(),
            )
        else:
            try:
                report, rebuilt_evidence = build_research_report(
                    self.project_root,
                    parameters,
                    generated_at=expected_generated_at,
                )
            except ResearchWorkflowError:
                report = ""
                rebuilt_evidence = None
            artifact_verification = self.verifier.verify(
                task_id=task.task_id,
                artifact_path=str(parameters["output_path"]),
                expected_content=report,
                tool_result=result,
                checked_at=self.clock(),
                expected_attempt_id=result.attempt_id,
                require_provenance=True,
                maximum_claim_generation=task.claim_generation,
            )
            source_passed, source_reason = verify_research_report(
                self.project_root,
                parameters,
                result.research_evidence,
                expected_generated_at=expected_generated_at,
            )
            evidence_matches = bool(
                rebuilt_evidence is not None
                and result.research_evidence == rebuilt_evidence
                and expectation.get("report_sha256")
                == rebuilt_evidence.get("report_sha256")
            )
            passed = (
                artifact_verification.passed
                and source_passed
                and evidence_matches
            )
            verification = VerificationResult(
                verifier_name="research-report-v1",
                passed=passed,
                exists=artifact_verification.exists,
                path_safe=artifact_verification.path_safe,
                content_matches=artifact_verification.content_matches,
                evidence_matches=evidence_matches,
                reason=(
                    "research_report_verified"
                    if passed
                    else source_reason
                    if not source_passed
                    else "research_evidence_mismatch"
                    if not evidence_matches
                    else artifact_verification.reason
                ),
                artifact=artifact_verification.artifact,
                checked_at=self.clock(),
            )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.VERIFICATION_COMPLETED,
            self.clock(),
            payload={
                "verification": verification.to_dict(),
                "attempt_id": task.attempt_id,
            },
            updates={
                "verification": verification,
                "artifact": verification.artifact or task.artifact,
                "phase": (
                    "research_report_verified"
                    if verification.passed
                    else "research_report_failed"
                ),
            },
            lease_token=lease_token,
        )
        if verification.passed:
            succeeded, _ = self.store.transition(
                task.task_id,
                TaskStatus.SUCCEEDED,
                EventType.TASK_SUCCEEDED,
                self.clock(),
                payload={
                    "research_report": True,
                    "attempt_id": task.attempt_id,
                },
                updates=self._clear_claim_updates(),
                lease_token=lease_token,
            )
            return succeeded
        failed, _ = self.store.transition(
            task.task_id,
            TaskStatus.FAILED,
            EventType.TASK_FAILED,
            self.clock(),
            payload={"code": verification.reason},
            updates={
                **self._clear_claim_updates(),
                "phase": "research_report_failed",
                "error": TaskError(
                    verification.reason,
                    verification.reason,
                ),
            },
            lease_token=lease_token,
        )
        return failed

    def _execute_tool(self, task: Task, lease_token: str) -> Task:
        plan_step = (
            task.plan.steps[0]
            if task.plan is not None and task.plan.steps
            else None
        )
        if (
            plan_step is not None
            and plan_step.tool_name == INSPECT_WORKSPACE
        ):
            return self._execute_workspace_inspection(
                task,
                lease_token,
                plan_step,
            )
        if plan_step is not None and plan_step.tool_name == EDIT_TEXT_FILE:
            return self._execute_workspace_edit(
                task,
                lease_token,
                plan_step,
            )
        if plan_step is not None and plan_step.tool_name == RESEARCH_REPORT:
            return self._execute_research_report(
                task,
                lease_token,
                plan_step,
            )
        if plan_step is not None and plan_step.tool_name == RUN_TESTS_TOOL:
            return self._execute_test_tool(task, lease_token, plan_step)
        if plan_step is not None and plan_step.tool_name == READ_TEXT_ARTIFACT:
            return self._execute_read_tool(task, lease_token, plan_step)
        if plan_step is not None and plan_step.tool_name == CONTROLLED_SHELL:
            return self._execute_controlled_shell(
                task, lease_token, plan_step
            )
        parameters = task.request.parameters
        lineage: dict[str, Any] | None = None
        operation_name = WRITE_TEXT_ARTIFACT
        if plan_step is not None and plan_step.tool_name == MODIFY_TEXT_ARTIFACT:
            try:
                content, lineage = self._derive_modify_content(parameters)
                artifact_path = str(
                    parameters.get(
                        "output_path", parameters.get("artifact_path")
                    )
                )
            except ArtifactToolError as exc:
                return self._block(
                    task,
                    lease_token,
                    code=exc.code,
                    reason=exc.message,
                    phase="modify_precondition_failed",
                    mark_not_executed=True,
                )
            operation_name = MODIFY_TEXT_ARTIFACT
        else:
            artifact_path = parameters["artifact_path"]
            content = parameters["content"]
        assert isinstance(artifact_path, str)
        assert isinstance(content, str)

        intended_mode = getattr(self.writer, "execution_mode", ExecutionMode.REAL)
        started_at = self.clock()
        cancelled = self._cancel_if_requested(
            task,
            lease_token,
            checkpoint="before_tool",
            side_effect_observed=False,
        )
        if cancelled is not None:
            return cancelled
        self._assert_execution_authority(task, lease_token)
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_STARTED,
            started_at,
            payload={
                "tool_name": operation_name,
                "operation": operation_name,
                "artifact_path": artifact_path,
                "source_task_id": (
                    lineage.get("source_task_id")
                    if lineage is not None
                    else None
                ),
                "attempt_id": task.attempt_id,
                "intended_execution_mode": intended_mode.value,
                "claim_generation": task.claim_generation,
                "plan_version": task.plan_version,
            },
            updates={
                "execution_mode": intended_mode,
                "phase": "tool_started",
                "execution_plan_version": task.plan_version,
                "execution_plan_snapshot": (
                    task.plan.to_dict() if task.plan is not None else None
                ),
            },
            lease_token=lease_token,
        )
        try:
            task_dir = self.artifacts_root / task.task_id
            before_write = (
                scan_workspace(
                    task_dir,
                    workspace_id=task.task_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    claim_generation=task.claim_generation,
                    captured_at=started_at,
                )
                if task_dir.is_dir()
                else Snapshot(
                    workspace=task.task_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    claim_generation=task.claim_generation,
                    captured_at=started_at,
                    complete=True,
                )
            )
            result = self.writer.write(
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                artifact_path=artifact_path,
                content=content,
                started_at=started_at,
                finished_at=self.clock(),
                lease_token=lease_token,
                claim_generation=task.claim_generation,
                authorize=lambda token, generation: self._authorize_publication(
                    task.task_id,
                    task.attempt_id,
                    token,
                    generation,
                ),
            )
            result.tool_name = (
                MODIFY_TEXT_ARTIFACT
                if operation_name == MODIFY_TEXT_ARTIFACT
                else self.writer.tool_name
            )
            result.lineage = lineage
        except ClaimLost:
            raise
        except CancellationRequested:
            cancelled = self._cancel_if_requested(
                task,
                lease_token,
                checkpoint="before_publish",
                side_effect_observed=False,
            )
            if cancelled is not None:
                return cancelled
            raise
        except ArtifactToolError as exc:
            return self._finish_tool_failure(
                task,
                lease_token,
                exc.code,
                exc.message,
                started_at,
                tool_name=operation_name,
            )
        except Exception as exc:
            return self._finish_tool_failure(
                task,
                lease_token,
                "tool_exception",
                f"tool execution raised {type(exc).__name__}: {exc}",
                started_at,
                tool_name=operation_name,
            )

        after_write = scan_workspace(
            task_dir,
            workspace_id=task.task_id,
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            claim_generation=task.claim_generation,
            captured_at=self.clock(),
        )
        result.workspace_effects = build_workspace_effects(
            operation=operation_name,
            source=self.writer.tool_name,
            workspace=task.task_id,
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            claim_generation=task.claim_generation,
            before=before_write,
            after=after_write,
            # ``.staging`` is the writer's own recovery directory inside the
            # task folder; it is declared explicitly rather than hidden.
            policy_parameters={
                "allowed_side_effects": [artifact_path, ".staging"]
            },
        )
        self._assert_execution_authority(task, lease_token)
        latest = self.store.require_task(task.task_id)
        if latest.cancel_requested_at is not None:
            task = self._persist_tool_result_before_cancel(
                task,
                lease_token,
                result,
            )
            cancelled = self._cancel_if_requested(
                task,
                lease_token,
                checkpoint="after_tool",
                side_effect_observed=result.success,
            )
            if cancelled is not None:
                return cancelled
        if not result.success:
            return self._finish_tool_failure(
                task,
                lease_token,
                result.error_code or "tool_failed",
                result.error_message or "tool reported failure",
                started_at,
                result=result,
                tool_name=result.tool_name,
            )
        if result.execution_mode is ExecutionMode.SIMULATED:
            simulated, _ = self.store.record_event_and_update(
                task.task_id,
                EventType.TOOL_SUCCEEDED,
                self.clock(),
                payload={"tool_result": result.to_dict()},
                updates={
                    "tool_result": result,
                    "execution_mode": ExecutionMode.SIMULATED,
                    "phase": "simulation_completed",
                },
                lease_token=lease_token,
            )
            self._assert_execution_authority(simulated, lease_token)
            simulated, _ = self.store.transition(
                simulated.task_id,
                TaskStatus.SIMULATED,
                EventType.SIMULATION_COMPLETED,
                self.clock(),
                payload={
                    "reason": "simulated tool produced no real artifact",
                    "real_artifact_created": False,
                },
                updates=self._clear_claim_updates(),
                lease_token=lease_token,
            )
            return simulated

        if result.execution_mode is not ExecutionMode.REAL or result.artifact is None:
            failed, _ = self.store.record_event_and_update(
                task.task_id,
                EventType.TOOL_FAILED,
                self.clock(),
                payload={"reason": "real_tool_evidence_missing"},
                updates={
                    "tool_result": result,
                    "execution_mode": result.execution_mode,
                    "phase": "invalid_tool_result",
                    "error": TaskError(
                        "invalid_tool_result",
                        "real execution must return artifact evidence",
                    ),
                },
                lease_token=lease_token,
            )
            failed, _ = self.store.transition(
                failed.task_id,
                TaskStatus.FAILED,
                EventType.TASK_FAILED,
                self.clock(),
                payload={"code": "invalid_tool_result"},
                updates=self._clear_claim_updates(),
                lease_token=lease_token,
            )
            return failed

        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_SUCCEEDED,
            self.clock(),
            payload={"tool_result": result.to_dict()},
            updates={
                "tool_result": result,
                "artifact": result.artifact,
                "execution_mode": ExecutionMode.REAL,
                "phase": "tool_completed",
            },
            lease_token=lease_token,
        )
        verifying, _ = self.store.transition(
            task.task_id,
            TaskStatus.VERIFYING,
            EventType.VERIFICATION_STARTED,
            self.clock(),
            payload={
                "artifact": result.artifact.to_dict(),
                "tool_result_trusted": False,
                "attempt_id": task.attempt_id,
            },
            updates={"phase": "verification_started"},
            lease_token=lease_token,
        )
        if self.fault_hook is not None:
            self.fault_hook("after_write_before_verification", verifying)
        return self._verify_and_finish(verifying, lease_token)

    def _execute_read_tool(
        self,
        task: Task,
        lease_token: str,
        plan_step: Any,
    ) -> Task:
        """Read one verified source artifact without creating a side effect."""
        started_at = self.clock()
        cancelled = self._cancel_if_requested(
            task,
            lease_token,
            checkpoint="before_read",
            side_effect_observed=False,
        )
        if cancelled is not None:
            return cancelled
        self._assert_execution_authority(task, lease_token)
        parameters = task.request.parameters
        try:
            source, artifact_path, text, raw, digest = (
                self._resolve_source_artifact(parameters)
            )
        except ArtifactToolError as exc:
            return self._block(
                task,
                lease_token,
                code=exc.code,
                reason=exc.message,
                phase="read_precondition_failed",
                mark_not_executed=True,
            )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_STARTED,
            started_at,
            payload={
                "tool_name": READ_TEXT_ARTIFACT,
                "operation": READ_TEXT_ARTIFACT,
                "source_task_id": parameters.get("source_task_id"),
                "artifact_path": parameters.get("artifact_path"),
                "attempt_id": task.attempt_id,
                "intended_execution_mode": ExecutionMode.REAL.value,
                "claim_generation": task.claim_generation,
                "plan_version": task.plan_version,
            },
            updates={
                "execution_mode": ExecutionMode.REAL,
                "phase": "read_started",
                "execution_plan_version": task.plan_version,
                "execution_plan_snapshot": plan_step.parameters,
            },
            lease_token=lease_token,
        )
        read_evidence = ArtifactReadEvidence(
            source_task_id=source.task_id,
            relative_path=artifact_path,
            byte_count=len(raw),
            sha256=digest,
            content=text,
            source_attempt_id=source.artifact.attempt_id,
            source_claim_generation=source.artifact.claim_generation,
        )
        result = ToolResult(
            attempt_id=task.attempt_id,
            tool_name=READ_TEXT_ARTIFACT,
            execution_mode=ExecutionMode.REAL,
            success=True,
            started_at=started_at,
            finished_at=self.clock(),
            read_evidence=read_evidence,
            lineage={
                "operation": READ_TEXT_ARTIFACT,
                "source_task_id": source.task_id,
                "source_artifact_path": artifact_path,
                "source_sha256": digest,
                "source_byte_count": len(raw),
                "source_attempt_id": source.artifact.attempt_id,
                "source_claim_generation": source.artifact.claim_generation,
            },
        )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_SUCCEEDED,
            self.clock(),
            payload={"tool_result": result.to_dict()},
            updates={
                "tool_result": result,
                "execution_mode": ExecutionMode.REAL,
                "phase": "read_completed",
            },
            lease_token=lease_token,
        )
        verifying, _ = self.store.transition(
            task.task_id,
            TaskStatus.VERIFYING,
            EventType.VERIFICATION_STARTED,
            self.clock(),
            payload={
                "read_evidence": read_evidence.to_dict(),
                "tool_result_trusted": False,
                "attempt_id": task.attempt_id,
            },
            updates={"phase": "read_verification_started"},
            lease_token=lease_token,
        )
        if self.fault_hook is not None:
            self.fault_hook("after_read_before_verification", verifying)
        return self._verify_read_and_finish(verifying, lease_token)

    def _verify_read_and_finish(
        self,
        task: Task,
        lease_token: str,
    ) -> Task:
        self._assert_execution_authority(task, lease_token)
        parameters = task.request.parameters
        evidence = (
            task.tool_result.read_evidence
            if task.tool_result is not None
            else None
        )
        snapshot_matches = (
            task.execution_plan_version == task.plan_version
            and task.execution_plan_snapshot
            == (
                task.plan.steps[0].parameters
                if task.plan is not None and task.plan.steps
                else None
            )
        )
        if not snapshot_matches:
            verification = VerificationResult(
                verifier_name=self.verifier.name,
                passed=False,
                exists=False,
                path_safe=False,
                content_matches=False,
                evidence_matches=False,
                reason="execution_plan_snapshot_mismatch",
                artifact=None,
                checked_at=self.clock(),
            )
        else:
            verification = self.verifier.verify_read(
                source_task_id=str(parameters["source_task_id"]),
                artifact_path=str(parameters["artifact_path"]),
                expected_sha256=(
                    str(parameters["expected_sha256"])
                    if parameters.get("expected_sha256") is not None
                    else None
                ),
                read_evidence=evidence,
                checked_at=self.clock(),
            )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.VERIFICATION_COMPLETED,
            self.clock(),
            payload={
                "verification": verification.to_dict(),
                "attempt_id": task.attempt_id,
            },
            updates={
                "verification": verification,
                "phase": (
                    "read_verified"
                    if verification.passed
                    else "read_verification_failed"
                ),
            },
            lease_token=lease_token,
        )
        if verification.passed:
            succeeded, _ = self.store.transition(
                task.task_id,
                TaskStatus.SUCCEEDED,
                EventType.TASK_SUCCEEDED,
                self.clock(),
                payload={
                    "read_evidence": (
                        evidence.to_dict() if evidence is not None else None
                    ),
                    "verifier": verification.verifier_name,
                },
                updates=self._clear_claim_updates(),
                lease_token=lease_token,
            )
            return succeeded
        return self._fail(
            task,
            lease_token,
            code="verification_failed",
            message=f"artifact read verification failed: {verification.reason}",
            phase="read_verification_failed",
            payload_extra={"reason": verification.reason},
        )

    def _derive_test_execution_identity(
        self,
        parameters: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Derive the exact container command and digests for one test run."""
        import json as _json

        from .isolation import build_container_command

        image = str(parameters.get("image") or "python:3.13-alpine")
        target = str(parameters["target"])
        workspace = resolve_workspace(
            self.project_root, str(parameters["workspace"])
        )
        argv = ["python", "-B", "-m", "unittest", "-q", target]
        disk_limit = parameters.get("disk_limit_bytes")
        container_command = build_container_command(
            image=image,
            workspace=workspace,
            argv=argv,
            disk_limit_bytes=int(disk_limit) if disk_limit else None,
        )
        return {
            "image": image,
            "target": target,
            "workspace": workspace,
            "argv": argv,
            "container_command": container_command,
            "config_digest": hashlib.sha256(
                " ".join(container_command).encode("utf-8")
            ).hexdigest(),
            "command_digest": hashlib.sha256(
                _json.dumps(argv, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }

    def _build_final_execution_plan(
        self,
        task: Task,
        parameters: Mapping[str, Any],
        identity: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        artifacts: list[dict[str, Any]] = []
        workspace = resolve_workspace(
            self.project_root, str(parameters["workspace"])
        )
        if parameters.get("evidence_id"):
            artifact_specs = (
                (
                    "inside.txt",
                    "read_probe_sentinel",
                    "workspace_input_or_provisioning",
                ),
                (
                    "probe-written.txt",
                    "write_probe_output",
                    "provisioning",
                ),
            )
            for name, role, ownership in artifact_specs:
                path = workspace / name
                if not path.is_file() or path.is_symlink():
                    continue
                artifacts.append(
                    {
                        "path": name,
                        "role": role,
                        "ownership": ownership,
                        "retention": "workspace_baseline",
                        "cleanup": "deferred_to_controlled_cleanup",
                        "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
        return {
            "kind": "run_tests",
            "task_id": task.task_id,
            "attempt_id": task.attempt_id,
            "claim_generation": task.claim_generation,
            "plan_version": task.plan_version,
            "tool_name": RUN_TESTS_TOOL,
            "runner": parameters.get("runner"),
            "target": parameters.get("target"),
            "workspace": parameters.get("workspace"),
            "execution_backend": parameters.get("execution_backend"),
            "evidence_id": parameters.get("evidence_id"),
            "image": (
                identity.get("image") if identity is not None else None
            ),
            "config_digest": (
                identity.get("config_digest") if identity is not None else None
            ),
            "command_digest": (
                identity.get("command_digest") if identity is not None else None
            ),
            "provisioning_artifacts": artifacts,
            "prepared_at": self.clock(),
        }

    def _run_container_test(
        self,
        task: Task,
        parameters: dict[str, Any],
        cancel_check: Any = None,
    ) -> dict[str, Any]:
        """Run ``python -m unittest`` inside the verified container backend.

        Digests are derived here from the command the executor is about to
        run; caller-supplied digests are never trusted.
        """
        import hashlib as _hashlib
        import subprocess as _subprocess

        from .evidence_store import EvidenceStore, EvidenceStoreError
        from .isolation import _live_image_digest, _live_runtime_identity
        from .local_test_runner import TestExecutionError

        identity = self._derive_test_execution_identity(parameters)
        image = identity["image"]
        target = identity["target"]
        workspace = identity["workspace"]
        argv = identity["argv"]
        container_command = identity["container_command"]
        config_digest = identity["config_digest"]
        command_digest = identity["command_digest"]
        evidence_id = parameters.get("evidence_id")
        if not evidence_id:
            raise TestExecutionError(
                "isolation_unverified", "evidence_id_missing"
            )
        snapshot = task.execution_plan_snapshot
        if not isinstance(snapshot, Mapping) or snapshot.get("kind") != "run_tests":
            raise TestExecutionError(
                "execution_plan_snapshot_missing",
                "final execution plan is missing",
            )
        expected = {
            "task_id": task.task_id,
            "attempt_id": task.attempt_id,
            "claim_generation": task.claim_generation,
            "tool_name": RUN_TESTS_TOOL,
            "runner": parameters.get("runner"),
            "target": target,
            "workspace": parameters.get("workspace"),
            "execution_backend": "container",
            "evidence_id": evidence_id,
            "image": image,
            "config_digest": config_digest,
            "command_digest": command_digest,
        }
        if task.execution_plan_version != task.plan_version or any(
            snapshot.get(field) != value
            for field, value in expected.items()
        ):
            raise TestExecutionError(
                "execution_plan_snapshot_mismatch",
                "final execution plan does not match the execution request",
            )
        store = EvidenceStore(self.project_root)
        try:
            verdict = store.verify_stored_evidence(
                str(evidence_id),
                expected_runtime=_live_runtime_identity(),
                expected_image_digest=_live_image_digest(image),
                expected_config_digest=config_digest,
                expected_request_id=task.task_id,
                expected_command_digest=command_digest,
            )
        except EvidenceStoreError as exc:
            raise TestExecutionError("isolation_unverified", exc.code) from exc
        if verdict["state"] != "verified":
            raise TestExecutionError(
                "isolation_unverified",
                "; ".join(verdict["reasons"]) or verdict["state"],
            )
        limit = int(parameters.get("max_output_bytes", 8192))
        timeout = float(parameters.get("timeout_seconds", 10.0))
        import time as _time

        started = _time.time()
        container_name = (
            "zv-" + task.task_id.replace("-", "")[:12] + "-"
            + uuid4().hex[:8]
        )
        container_command = [
            *container_command[:2],
            "--name",
            container_name,
            *container_command[2:],
        ]
        try:
            process = self.container_popen(
                container_command,
                stdout=_subprocess.PIPE,
                stderr=_subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise TestExecutionError(
                "container_start_failed", type(exc).__name__
            ) from exc
        import threading as _threading

        timed_out = False
        cancelled = False
        captured: dict[str, str] = {}

        def _reader() -> None:
            try:
                captured["stdout"], captured["stderr"] = process.communicate()
            except Exception:  # pragma: no cover - defensive
                captured.setdefault("stdout", "")
                captured.setdefault("stderr", "")

        reader = _threading.Thread(target=_reader, daemon=True)
        reader.start()
        deadline = _time.time() + timeout
        while process.poll() is None:
            if cancel_check is not None and cancel_check():
                cancelled = True
                break
            if _time.time() >= deadline:
                timed_out = True
                break
            _time.sleep(0.05)
        if timed_out or cancelled:
            terminated = self._terminate_container(container_name)
            if terminated is not True:
                process.kill()
                raise TestExecutionError(
                    "container_termination_unverified",
                    "container may still be running",
                )
            process.wait(timeout=10)
        reader.join(timeout=15)
        stdout_raw = captured.get("stdout", "")
        stderr_raw = captured.get("stderr", "")
        if timed_out or cancelled:
            artifacts = self._collect_test_artifacts(workspace, limit)
            return {
                "runner": "unittest",
                "target": target,
                "workspace": str(parameters["workspace"]),
                "exit_code": None,
                "timed_out": timed_out,
                "cancelled": cancelled,
                "terminated_confirmed": True,
                "container_name": container_name,
                "artifacts": artifacts,
                "truncated": False,
                "stdout": stdout_raw.encode("utf-8", "replace")[:limit],
                "stderr": stderr_raw.encode("utf-8", "replace")[:limit],
                "stdout_bytes": len(stdout_raw.encode("utf-8", "replace")),
                "stderr_bytes": len(stderr_raw.encode("utf-8", "replace")),
                "stdout_sha256": _hashlib.sha256(
                    stdout_raw.encode("utf-8", "replace")
                ).hexdigest(),
                "stderr_sha256": _hashlib.sha256(
                    stderr_raw.encode("utf-8", "replace")
                ).hexdigest(),
                "duration_ms": round((_time.time() - started) * 1000, 3),
                "output_limit_bytes": limit,
                "timeout_seconds": timeout,
            }
        completed = _subprocess.CompletedProcess(
            container_command, process.returncode, stdout_raw, stderr_raw
        )
        artifacts = self._collect_test_artifacts(workspace, limit)
        workspace_bytes = sum(
            item["bytes"] for item in artifacts
        )
        budget = int(parameters.get("workspace_budget_bytes") or 0)
        if budget and workspace_bytes > budget:
            return self._fail(
                task,
                lease_token,
                code="workspace_over_budget",
                message=(
                    f"collected artifacts exceed the workspace budget "
                    f"({workspace_bytes} > {budget})"
                ),
                phase="workspace_over_budget",
                payload_extra={"artifacts": artifacts},
            )
        collection_status = _collection_status(
            parameters.get("required_artifacts") or [], artifacts
        )
        stdout = completed.stdout.encode("utf-8", "replace")
        stderr = completed.stderr.encode("utf-8", "replace")
        truncated = len(stdout) > limit or len(stderr) > limit
        return {
            "runner": "unittest",
            "target": target,
            "workspace": str(parameters["workspace"]),
            "exit_code": completed.returncode,
            "timed_out": False,
            "cancelled": False,
            "terminated_confirmed": True,
            "truncated": truncated,
            "stdout": stdout[:limit],
            "stderr": stderr[:limit],
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
            "stdout_sha256": _hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": _hashlib.sha256(stderr).hexdigest(),
            "duration_ms": round((_time.time() - started) * 1000, 3),
            "output_limit_bytes": limit,
            "timeout_seconds": timeout,
            "image": image,
            "config_digest": config_digest,
            "command_digest": command_digest,
            "evidence_id": str(evidence_id),
            "container_name": container_name,
            "artifacts": artifacts,
            "collection_status": collection_status,
        }

    def _terminate_container(self, name: str) -> bool:
        """Kill the named container and confirm it is gone."""
        if self.container_terminator is not None:
            return bool(self.container_terminator(name))
        import shutil as _shutil
        import subprocess as _subprocess

        docker = _shutil.which("docker")
        if docker is None:
            return False
        import time as _time

        _subprocess.run(
            [docker, "kill", name], capture_output=True, text=True
        )
        # "Terminated" means *not running*; a stopped container that has not
        # been removed yet still satisfies that.  Poll briefly for the kill
        # to take effect instead of trusting the signal.
        deadline = _time.monotonic() + 5.0
        while True:
            listed = _subprocess.run(
                [
                    docker,
                    "ps",
                    "--filter",
                    f"name={name}",
                    "--format",
                    "{{.ID}}",
                ],
                capture_output=True,
                text=True,
            )
            if listed.returncode != 0:
                return False
            if not listed.stdout.strip():
                return True
            if _time.monotonic() >= deadline:
                return False
            _time.sleep(0.05)

    def _collect_test_artifacts(
        self, workspace: Path, limit: int
    ) -> list[dict[str, Any]]:
        """Collect bounded, hashed files from the task's artifact directory."""
        import hashlib as _hashlib

        root = workspace / "artifacts"
        if not root.is_dir():
            return []
        collected: list[dict[str, Any]] = []
        total = 0
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                continue
            if root.resolve() not in resolved.parents:
                continue
            size = resolved.stat().st_size
            if size > limit or total + size > limit:
                break
            data = resolved.read_bytes()
            total += size
            collected.append(
                {
                    "relative_path": str(resolved.relative_to(root.resolve())),
                    "bytes": size,
                    "sha256": _hashlib.sha256(data).hexdigest(),
                }
            )
        return collected

    def _execute_controlled_shell(
        self,
        task: Task,
        lease_token: str,
        plan_step: Any,
    ) -> Task:
        """Execute a whitelisted shell command with bounded output and timeout.

        The command and arguments are passed as a parameter array to
        ``subprocess.run`` — never ``shell=True``. Policy has already
        validated the whitelist, timeout, and path safety.
        """
        started_at = self.clock()
        cancelled = self._cancel_if_requested(
            task,
            lease_token,
            checkpoint="before_controlled_shell",
            side_effect_observed=False,
        )
        if cancelled is not None:
            return cancelled
        self._assert_execution_authority(task, lease_token)

        # Defense-in-depth: re-validate parameters at execution time.
        workspace_root = self.project_root
        try:
            validated = validate_shell_parameters(
                dict(task.request.parameters),
                workspace_root=workspace_root,
            )
        except ShellToolError as exc:
            return self._block(
                task,
                lease_token,
                code=exc.code,
                reason=exc.message,
                phase="controlled_shell_blocked",
                mark_not_executed=True,
            )

        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_STARTED,
            started_at,
            payload={
                "tool_name": CONTROLLED_SHELL,
                "operation": CONTROLLED_SHELL,
                "command": validated["command"],
                "arguments": validated["arguments"],
                "timeout_seconds": validated["timeout_seconds"],
                "working_directory": validated["working_directory"],
                "attempt_id": task.attempt_id,
                "intended_execution_mode": ExecutionMode.REAL.value,
                "claim_generation": task.claim_generation,
                "plan_version": task.plan_version,
            },
            updates={
                "execution_mode": ExecutionMode.REAL,
                "phase": "controlled_shell_started",
                "execution_plan_version": task.plan_version,
                "execution_plan_snapshot": plan_step.parameters,
            },
            lease_token=lease_token,
        )

        result = execute_controlled_shell(
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            command=validated["command"],
            arguments=validated["arguments"],
            timeout_seconds=validated["timeout_seconds"],
            working_directory=validated["working_directory"],
            started_at=started_at,
            finished_at=self.clock(),
            execution_mode=ExecutionMode.REAL,
        )

        if not result.success:
            return self._finish_tool_failure(
                task,
                lease_token,
                code=result.error_code or "shell_failed",
                message=result.error_message or "command failed",
                started_at=started_at,
                result=result,
                tool_name=CONTROLLED_SHELL,
            )

        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_SUCCEEDED,
            self.clock(),
            payload={"tool_result": result.to_dict()},
            updates={
                "tool_result": result,
                "execution_mode": ExecutionMode.REAL,
                "phase": "controlled_shell_completed",
            },
            lease_token=lease_token,
        )
        verifying, _ = self.store.transition(
            task.task_id,
            TaskStatus.VERIFYING,
            EventType.VERIFICATION_STARTED,
            self.clock(),
            payload={
                "controlled_shell": True,
                "tool_result_trusted": False,
                "attempt_id": task.attempt_id,
            },
            updates={"phase": "controlled_shell_verification"},
            lease_token=lease_token,
        )
        return self._verify_controlled_shell_and_finish(
            verifying, lease_token
        )

    def _verify_controlled_shell_and_finish(
        self,
        task: Task,
        lease_token: str,
    ) -> Task:
        self._assert_execution_authority(task, lease_token)
        result = task.tool_result
        effects = result.workspace_effects if result is not None else None
        passed = (
            effects is not None
            and isinstance(effects.get("command"), str)
            and isinstance(effects.get("output"), str)
            and isinstance(effects.get("exit_code"), int)
            and effects.get("command") == task.request.parameters.get("command")
        )
        verification = VerificationResult(
            verifier_name="controlled-shell-v1",
            passed=passed,
            exists=effects is not None,
            path_safe=passed,
            content_matches=passed,
            evidence_matches=passed,
            reason=(
                "workspace_effects verified"
                if passed
                else "workspace_effects missing or mismatched"
            ),
            artifact=None,
            checked_at=self.clock(),
        )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.VERIFICATION_COMPLETED,
            self.clock(),
            payload={
                "verification": verification.to_dict(),
                "attempt_id": task.attempt_id,
            },
            updates={
                "verification": verification,
                "phase": (
                    "controlled_shell_verified"
                    if passed
                    else "controlled_shell_verification_failed"
                ),
            },
            lease_token=lease_token,
        )
        if not passed:
            failed, _ = self.store.transition(
                task.task_id,
                TaskStatus.FAILED,
                EventType.TASK_FAILED,
                self.clock(),
                payload={
                    "code": "shell_verification_failed",
                    "verification_performed": True,
                },
                updates=self._clear_claim_updates(),
                lease_token=lease_token,
            )
            return failed
        succeeded, _ = self.store.transition(
            task.task_id,
            TaskStatus.SUCCEEDED,
            EventType.TASK_SUCCEEDED,
            self.clock(),
            payload={
                "verification_performed": True,
                "verifier": verification.verifier_name,
            },
            updates=self._clear_claim_updates(),
            lease_token=lease_token,
        )
        return succeeded

    def _execute_test_tool(
        self,
        task: Task,
        lease_token: str,
        plan_step: Any,
    ) -> Task:
        """Run one bounded local test target.

        The executor never builds a command string: ``run_unittest`` takes
        structured fields and the policy has already validated them.
        """
        from .isolation import ContainerBackendUnavailable

        parameters = {
            "runner": plan_step.parameters.get("runner"),
            "target": plan_step.parameters.get("target"),
            "workspace": plan_step.parameters.get("workspace"),
            "timeout_seconds": plan_step.parameters.get("timeout_seconds", 10.0),
            "max_output_bytes": plan_step.parameters.get("max_output_bytes", 8192),
            "execution_backend": plan_step.parameters.get(
                "execution_backend", "container"
            ),
            "evidence_id": plan_step.parameters.get("evidence_id"),
            "image": plan_step.parameters.get("image"),
            "host_authorization": plan_step.parameters.get("host_authorization"),
            "config_digest": plan_step.parameters.get("config_digest"),
            "command_digest": plan_step.parameters.get("command_digest"),
            "required_artifacts": list(
                plan_step.parameters.get("required_artifacts") or []
            ),
            "disk_limit_bytes": plan_step.parameters.get("disk_limit_bytes"),
            "workspace_budget_bytes": plan_step.parameters.get(
                "workspace_budget_bytes"
            ),
            "auto_provision_isolation": bool(
                plan_step.parameters.get("auto_provision_isolation")
            ),
            "allowed_side_effects": list(
                plan_step.parameters.get("allowed_side_effects") or []
            ),
            "protected_paths": list(
                plan_step.parameters.get("protected_paths") or []
            ),
        }
        started_at = self.clock()
        try:
            policy = EffectPolicy.build(parameters)
            workspace_path = resolve_workspace(
                self.project_root, str(parameters["workspace"])
            )
        except (SnapshotError, TestExecutionError) as exc:
            return self._finish_tool_failure(
                task,
                lease_token,
                exc.code,
                exc.message,
                started_at,
                tool_name=RUN_TESTS_TOOL,
            )
        cancelled = self._cancel_if_requested(
            task,
            lease_token,
            checkpoint="before_tool",
            side_effect_observed=False,
        )
        if cancelled is not None:
            return cancelled
        self._assert_execution_authority(task, lease_token)
        prior_starts = sum(
            1
            for event in self.store.list_events(task.task_id)
            if event.event_type == EventType.TOOL_STARTED.value
        )
        if prior_starts > 0:
            # Recovery must never re-run a test whose outcome is unknown.
            persisted = (
                task.tool_result.test_evidence
                if task.tool_result is not None
                else None
            )
            if persisted is None:
                return self._block(
                    task,
                    lease_token,
                    code="test_execution_unknown",
                    reason=(
                        "a previous test run started but its result is "
                        "unknown; refusing to re-run"
                    ),
                    phase="blocked_test_unknown",
                    mark_not_executed=False,
                )
            verifying, _ = self.store.transition(
                task.task_id,
                TaskStatus.VERIFYING,
                EventType.VERIFICATION_STARTED,
                self.clock(),
                payload={
                    "resume": True,
                    "source": "persisted_test_evidence",
                    "attempt_id": persisted.attempt_id,
                },
                updates={"phase": "resumed_test_verification", "error": None},
                lease_token=lease_token,
            )
            verification_parameters = {
                **parameters,
                "runner": persisted.runner,
                "target": persisted.target,
                "workspace": persisted.workspace,
                "evidence_id": persisted.evidence_id,
                "config_digest": persisted.config_digest,
                "command_digest": persisted.command_digest,
            }
            return self._verify_test_and_finish(
                verifying, lease_token, verification_parameters
            )

        def cancel_check() -> bool:
            try:
                current = self.store.require_task(task.task_id)
            except Exception:
                return True
            return current.cancel_requested_at is not None

        if (
            parameters.get("execution_backend", "host") == "container"
            and not parameters.get("evidence_id")
            and parameters.get("auto_provision_isolation")
        ):
            from .evidence_provisioning import provision_evidence

            evidence_id = f"auto-{task.task_id}"
            try:
                provision_evidence(
                    self.project_root,
                    image=str(
                        parameters.get("image") or "python:3.13-alpine"
                    ),
                    workspace_name=str(parameters["workspace"]),
                    target=str(parameters["target"]),
                    request_id=task.task_id,
                    evidence_id=evidence_id,
                )
            except Exception as exc:
                code = getattr(exc, "code", None) or "provisioning_failed"
                message = getattr(exc, "message", None) or (
                    f"provisioning raised {type(exc).__name__}"
                )
                return self._finish_tool_failure(
                    task,
                    lease_token,
                    "provisioning_failed",
                    f"{code}: {message}"[:500],
                    started_at,
                    tool_name=RUN_TESTS_TOOL,
                )
            parameters["evidence_id"] = evidence_id
            plan_step.parameters["evidence_id"] = evidence_id

        if (
            parameters.get("execution_backend", "host") == "container"
            and not parameters.get("evidence_id")
        ):
            return self._finish_tool_failure(
                task,
                lease_token,
                "isolation_unverified",
                "evidence_id_missing",
                started_at,
                tool_name=RUN_TESTS_TOOL,
            )

        try:
            identity: dict[str, Any] | None = None
            if parameters.get("execution_backend", "host") == "container":
                identity = self._derive_test_execution_identity(parameters)
                parameters["image"] = identity["image"]
                parameters["config_digest"] = identity["config_digest"]
                parameters["command_digest"] = identity["command_digest"]
                plan_step.parameters["image"] = identity["image"]
                plan_step.parameters["config_digest"] = identity["config_digest"]
                plan_step.parameters["command_digest"] = identity["command_digest"]

            final_execution_plan = self._build_final_execution_plan(
                task, parameters, identity
            )
        except Exception as exc:
            return self._finish_tool_failure(
                task,
                lease_token,
                "execution_plan_preparation_failed",
                (
                    "final execution plan could not be prepared: "
                    f"{type(exc).__name__}"
                ),
                started_at,
                tool_name=RUN_TESTS_TOOL,
            )
        try:
            task, _ = self.store.record_event_and_update(
                task.task_id,
                EventType.EXECUTION_PLAN_FINALIZED,
                self.clock(),
                payload={"final_execution_plan": final_execution_plan},
                updates={
                    "execution_plan_version": task.plan_version,
                    "execution_plan_snapshot": final_execution_plan,
                    "phase": "execution_plan_finalized",
                },
                lease_token=lease_token,
            )
        except Exception as exc:
            return self._finish_tool_failure(
                task,
                lease_token,
                "execution_plan_persistence_failed",
                (
                    "final execution plan could not be persisted: "
                    f"{type(exc).__name__}"
                ),
                started_at,
                tool_name=RUN_TESTS_TOOL,
            )

        before = scan_workspace(
            workspace_path,
            workspace_id=str(parameters["workspace"]),
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            claim_generation=task.claim_generation,
            captured_at=self.clock(),
        )
        if not before.complete:
            # Fail closed: never claim a complete audit from a partial scan.
            return self._fail(
                task,
                lease_token,
                code="snapshot_incomplete_before",
                message="workspace snapshot was incomplete before execution",
                phase="snapshot_incomplete",
                payload_extra={
                    "limits_hit": list(before.limits_hit),
                    "unreadable": list(before.unreadable),
                },
            )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_STARTED,
            started_at,
            payload={
                "tool_name": RUN_TESTS_TOOL,
                "runner": parameters["runner"],
                "target": parameters["target"],
                "workspace": parameters["workspace"],
                "attempt_id": task.attempt_id,
                "claim_generation": task.claim_generation,
                "plan_version": task.plan_version,
                "intended_execution_mode": ExecutionMode.REAL.value,
                "before_snapshot_digest": before.to_dict()["digest"],
                "before_snapshot_entries": len(before.entries),
            },
            updates={
                "execution_mode": ExecutionMode.REAL,
                "phase": "test_started",
                "execution_plan_version": task.plan_version,
            },
            lease_token=lease_token,
        )

        try:
            if parameters.get("execution_backend", "host") == "container":
                try:
                    raw = self._run_container_test(
                        task, parameters, cancel_check
                    )
                except ContainerBackendUnavailable as exc:
                    return self._finish_tool_failure(
                        task,
                        lease_token,
                        exc.code,
                        exc.message,
                        started_at,
                        tool_name=RUN_TESTS_TOOL,
                    )
            else:
                raw = self.test_runner(
                    project_root=self.project_root,
                    parameters=parameters,
                    cancel_check=cancel_check,
                    grant_store=self.host_grant_store,
                )
        except TestExecutionError as exc:
            return self._finish_tool_failure(
                task,
                lease_token,
                exc.code,
                exc.message,
                started_at,
                tool_name=RUN_TESTS_TOOL,
                payload_extra=_grant_failure_context(exc),
            )
        except Exception as exc:
            return self._finish_tool_failure(
                task,
                lease_token,
                "test_runner_error",
                f"test runner raised {type(exc).__name__}",
                started_at,
                tool_name=RUN_TESTS_TOOL,
            )
        if raw.get("collection_status") == "failed":
            return self._fail(
                task,
                lease_token,
                code="collection_failed",
                message="required test artifacts were not collected",
                phase="collection_failed",
                payload_extra={"artifacts": raw.get("artifacts", [])},
            )
        evidence = TestExecutionEvidence(
            runner=str(raw["runner"]),
            target=str(raw["target"]),
            workspace=str(raw["workspace"]),
            attempt_id=task.attempt_id,
            claim_generation=task.claim_generation,
            exit_code=raw["exit_code"],
            started_at=started_at,
            finished_at=self.clock(),
            duration_ms=float(raw["duration_ms"]),
            timed_out=bool(raw["timed_out"]),
            cancelled=bool(raw["cancelled"]),
            truncated=bool(raw["truncated"]),
            stdout_sha256=str(raw["stdout_sha256"]),
            stderr_sha256=str(raw["stderr_sha256"]),
            stdout_bytes=int(raw["stdout_bytes"]),
            stderr_bytes=int(raw["stderr_bytes"]),
            stdout_excerpt=excerpt(raw["stdout"]),
            stderr_excerpt=excerpt(raw["stderr"]),
            terminated_confirmed=bool(raw["terminated_confirmed"]),
            output_limit_bytes=int(raw["output_limit_bytes"]),
            timeout_seconds=float(raw["timeout_seconds"]),
            before_snapshot=before.to_dict(),
            evidence_id=(
                str(parameters["evidence_id"])
                if parameters.get("evidence_id")
                else None
            ),
            container_name=(
                str(raw["container_name"])
                if raw.get("container_name")
                else None
            ),
            config_digest=(
                str(parameters["config_digest"])
                if parameters.get("config_digest")
                else None
            ),
            command_digest=(
                str(parameters["command_digest"])
                if parameters.get("command_digest")
                else None
            ),
            host_grant_id=(
                str(raw["host_grant_id"]) if raw.get("host_grant_id") else None
            ),
            host_grant_issuer=(
                str(raw["host_grant_issuer"])
                if raw.get("host_grant_issuer")
                else None
            ),
        )
        after = scan_workspace(
            workspace_path,
            workspace_id=str(parameters["workspace"]),
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            claim_generation=task.claim_generation,
            captured_at=self.clock(),
        )
        workspace_diff = diff_snapshots(before, after, policy)
        evidence.after_snapshot = after.to_dict()
        evidence.workspace_diff = workspace_diff
        result = ToolResult(
            attempt_id=task.attempt_id,
            tool_name=RUN_TESTS_TOOL,
            execution_mode=ExecutionMode.REAL,
            success=True,
            started_at=started_at,
            finished_at=self.clock(),
            test_evidence=evidence,
            workspace_effects=build_workspace_effects(
                operation=RUN_TESTS_TOOL,
                source="local_test_runner",
                workspace=str(parameters["workspace"]),
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                claim_generation=task.claim_generation,
                before=before,
                after=after,
                policy_parameters=parameters,
            ),
        )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_SUCCEEDED,
            self.clock(),
            payload={"tool_result": result.to_dict()},
            updates={
                "tool_result": result,
                "execution_mode": ExecutionMode.REAL,
                "phase": "test_completed",
            },
            lease_token=lease_token,
        )
        verifying, _ = self.store.transition(
            task.task_id,
            TaskStatus.VERIFYING,
            EventType.VERIFICATION_STARTED,
            self.clock(),
            payload={
                "test_evidence": evidence.to_dict(),
                "tool_result_trusted": False,
                "attempt_id": evidence.attempt_id,
            },
            updates={"phase": "verification_started"},
            lease_token=lease_token,
        )
        if self.fault_hook is not None:
            self.fault_hook("after_test_before_verification", verifying)
        return self._verify_test_and_finish(verifying, lease_token, parameters)

    def _verify_test_and_finish(
        self,
        task: Task,
        lease_token: str,
        parameters: dict[str, Any],
    ) -> Task:
        self._assert_execution_authority(task, lease_token)
        evidence = (
            task.tool_result.test_evidence
            if task.tool_result is not None
            else None
        )
        mismatch = (
            _test_execution_plan_mismatch(task, evidence)
            if evidence is not None
            else None
        )
        revalidation_reason = (
            _test_evidence_revalidation_reason(
                self.project_root, task, evidence
            )
            if evidence is not None
            else None
        )
        if mismatch:
            verification = VerificationResult(
                verifier_name=self.test_verifier.name,
                passed=False,
                exists=True,
                path_safe=True,
                content_matches=False,
                evidence_matches=False,
                reason=mismatch,
                artifact=None,
                checked_at=self.clock(),
            )
        elif revalidation_reason:
            verification = VerificationResult(
                verifier_name=self.test_verifier.name,
                passed=False,
                exists=True,
                path_safe=True,
                content_matches=False,
                evidence_matches=False,
                reason=revalidation_reason,
                artifact=None,
                checked_at=self.clock(),
            )
        else:
            verification = self.test_verifier.verify(
                task=task,
                evidence=evidence,
                expected_parameters=parameters,
                checked_at=self.clock(),
            )
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.VERIFICATION_COMPLETED,
            self.clock(),
            payload={
                "verification": verification.to_dict(),
                "attempt_id": task.attempt_id,
            },
            updates={
                "verification": verification,
                "phase": (
                    "verified"
                    if verification.passed
                    else "verification_failed"
                ),
            },
            lease_token=lease_token,
        )
        self._assert_execution_authority(task, lease_token)
        if evidence is not None and evidence.cancelled:
            cancelled_task, _ = self.store.transition(
                task.task_id,
                TaskStatus.CANCELLED,
                EventType.TASK_CANCELLED,
                self.clock(),
                payload={
                    "reason": "test run was cancelled",
                    "test_evidence": evidence.to_dict(),
                },
                updates={
                    **self._clear_claim_updates(),
                    "phase": "cancelled_test_run",
                    "error": TaskError(
                        "cancelled", "test execution was cancelled"
                    ),
                },
                lease_token=lease_token,
            )
            return cancelled_task
        if verification.passed:
            succeeded, _ = self.store.transition(
                task.task_id,
                TaskStatus.SUCCEEDED,
                EventType.TASK_SUCCEEDED,
                self.clock(),
                payload={
                    "verifier": verification.verifier_name,
                    "target": parameters["target"],
                },
                updates=self._clear_claim_updates(),
                lease_token=lease_token,
            )
            return succeeded
        code = (
            verification.reason
            if verification.reason
            in {
                "test_timeout",
                "tests_failed",
                "container_backend_unavailable",
                "execution_plan_snapshot_mismatch",
                "execution_plan_snapshot_missing",
                "evidence_expired",
                "evidence_invalidated",
                "evidence_revalidation_failed",
            }
            else "test_execution_failed"
        )
        return self._fail(
            task,
            lease_token,
            code=code,
            message=f"test verification failed: {verification.reason}",
            phase="test_failed",
            payload_extra={"reason": verification.reason},
        )

    def _finish_tool_failure(
        self,
        task: Task,
        lease_token: str,
        code: str,
        message: str,
        started_at: str,
        *,
        result: ToolResult | None = None,
        tool_name: str | None = None,
        payload_extra: Mapping[str, object] | None = None,
    ) -> Task:
        resolved_tool = tool_name or "unknown_tool"
        failed_result = result or ToolResult(
            attempt_id=task.attempt_id,
            tool_name=resolved_tool,
            execution_mode=getattr(
                self.writer, "execution_mode", ExecutionMode.REAL
            ),
            success=False,
            started_at=started_at,
            finished_at=self.clock(),
            artifact=None,
            error_code=code,
            error_message=message,
        )
        failed, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_FAILED,
            self.clock(),
            payload={
                "tool_result": failed_result.to_dict(),
                "code": code,
                "reason": message,
                **dict(payload_extra or {}),
            },
            updates={
                "tool_result": failed_result,
                "execution_mode": failed_result.execution_mode,
                "phase": "tool_failed",
                "error": TaskError(code, message),
            },
            lease_token=lease_token,
        )
        failed, _ = self.store.transition(
            failed.task_id,
            TaskStatus.FAILED,
            EventType.TASK_FAILED,
            self.clock(),
            payload={"code": code, "verification_performed": False},
            updates=self._clear_claim_updates(),
            lease_token=lease_token,
        )
        return failed

    def _persist_tool_result_before_cancel(
        self,
        task: Task,
        lease_token: str,
        result: ToolResult,
    ) -> Task:
        event_type = (
            EventType.TOOL_SUCCEEDED
            if result.success
            else EventType.TOOL_FAILED
        )
        updates: dict[str, object] = {
            "tool_result": result,
            "execution_mode": result.execution_mode,
            "phase": "tool_completed_before_cancellation",
        }
        if result.artifact is not None:
            updates["artifact"] = result.artifact
        if not result.success:
            updates["error"] = TaskError(
                result.error_code or "tool_failed",
                result.error_message or "tool reported failure",
            )
        updated, _ = self.store.record_event_and_update(
            task.task_id,
            event_type,
            self.clock(),
            payload={
                "tool_result": result.to_dict(),
                "cancellation_observed": True,
            },
            updates=updates,
            lease_token=lease_token,
        )
        return updated

    def _verify_and_finish(self, task: Task, lease_token: str) -> Task:
        self._assert_execution_authority(task, lease_token)
        cancelled = self._cancel_if_requested(
            task,
            lease_token,
            checkpoint="before_verification",
            side_effect_observed=task.artifact is not None,
        )
        if cancelled is not None:
            return cancelled
        parameters = task.request.parameters
        source_error: ArtifactToolError | None = None
        if self._is_modify_plan(task):
            try:
                content, _ = self._derive_modify_content(parameters)
                artifact_path = str(
                    parameters.get(
                        "output_path", parameters.get("artifact_path")
                    )
                )
            except ArtifactToolError as exc:
                source_error = exc
                artifact_path = str(
                    parameters.get(
                        "output_path", parameters.get("artifact_path")
                    )
                )
                content = ""
        else:
            artifact_path = parameters["artifact_path"]
            content = parameters["content"]
        assert isinstance(artifact_path, str)
        assert isinstance(content, str)
        result = task.tool_result
        snapshot_matches = (
            task.execution_plan_version == task.plan_version
            and task.execution_plan_snapshot
            == (task.plan.to_dict() if task.plan is not None else None)
        )
        if source_error is not None:
            verification = VerificationResult(
                verifier_name=self.verifier.name,
                passed=False,
                exists=False,
                path_safe=False,
                content_matches=False,
                evidence_matches=False,
                reason=source_error.code,
                artifact=None,
                checked_at=self.clock(),
            )
        elif not snapshot_matches:
            verification = VerificationResult(
                verifier_name=self.verifier.name,
                passed=False,
                exists=False,
                path_safe=False,
                content_matches=False,
                evidence_matches=False,
                reason="execution_plan_snapshot_mismatch",
                artifact=None,
                checked_at=self.clock(),
            )
        elif result is None:
            verification = VerificationResult(
                verifier_name=self.verifier.name,
                passed=False,
                exists=False,
                path_safe=False,
                content_matches=False,
                evidence_matches=False,
                reason="missing_tool_result",
                artifact=None,
                checked_at=self.clock(),
            )
        else:
            try:
                verification = self.verifier.verify(
                    task_id=task.task_id,
                    artifact_path=artifact_path,
                    expected_content=content,
                    tool_result=result,
                    checked_at=self.clock(),
                    expected_attempt_id=result.attempt_id,
                    require_provenance=True,
                    maximum_claim_generation=task.claim_generation,
                )
            except Exception as exc:
                verification = VerificationResult(
                    verifier_name=self.verifier.name,
                    passed=False,
                    exists=False,
                    path_safe=False,
                    content_matches=False,
                    evidence_matches=False,
                    reason=f"verifier_exception_{type(exc).__name__}",
                    artifact=None,
                    checked_at=self.clock(),
                )
        if not isinstance(verification, VerificationResult):
            verification = VerificationResult(
                verifier_name=self.verifier.name,
                passed=False,
                exists=False,
                path_safe=False,
                content_matches=False,
                evidence_matches=False,
                reason=(
                    "verifier_invalid_result_"
                    f"{type(verification).__name__}"
                ),
                artifact=None,
                checked_at=self.clock(),
            )
        effects = (
            result.workspace_effects
            if result is not None and result.workspace_effects
            else None
        )
        if effects is not None:
            try:
                validate_workspace_effects(effects)
            except SnapshotError as exc:
                verification = VerificationResult(
                    verifier_name=self.verifier.name,
                    passed=False,
                    exists=False,
                    path_safe=False,
                    content_matches=False,
                    evidence_matches=False,
                    reason=f"workspace_effects_invalid_{exc.code}",
                    artifact=None,
                    checked_at=self.clock(),
                )
            else:
                counts = effects["diff"]["counts"]
                if not effects["complete"] or effects["truncated"]:
                    reason = "workspace_audit_incomplete"
                elif counts["unexpected"] or counts["unknown"]:
                    reason = "unexpected_workspace_change"
                else:
                    added = [
                        item
                        for item in effects["diff"]["changes"]
                        if item["change"] == "added"
                        and item["path"] != ".staging"
                    ]
                    if len(added) != 1 or added[0]["path"] != artifact_path:
                        reason = "unexpected_workspace_change"
                    else:
                        reason = ""
                if reason:
                    verification = VerificationResult(
                        verifier_name=self.verifier.name,
                        passed=False,
                        exists=False,
                        path_safe=False,
                        content_matches=False,
                        evidence_matches=False,
                        reason=reason,
                        artifact=None,
                        checked_at=self.clock(),
                    )
        self._assert_execution_authority(task, lease_token)
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.VERIFICATION_COMPLETED,
            self.clock(),
            payload={
                "verification": verification.to_dict(),
                "attempt_id": task.attempt_id,
            },
            updates={
                "verification": verification,
                "artifact": verification.artifact or task.artifact,
                "phase": "verified" if verification.passed else "verification_failed",
            },
            lease_token=lease_token,
        )
        self._assert_execution_authority(task, lease_token)
        cancelled = self._cancel_if_requested(
            task,
            lease_token,
            checkpoint="before_success",
            side_effect_observed=task.artifact is not None,
        )
        if cancelled is not None:
            return cancelled
        if verification.passed:
            if (
                task.execution_plan_version != task.plan_version
                or task.execution_plan_snapshot
                != (task.plan.to_dict() if task.plan is not None else None)
            ):
                return self._fail(
                    task,
                    lease_token,
                    code="execution_plan_snapshot_changed",
                    message=(
                        "execution evidence no longer matches the current plan"
                    ),
                    phase="execution_plan_snapshot_changed",
                )
            succeeded, _ = self.store.transition(
                task.task_id,
                TaskStatus.SUCCEEDED,
                EventType.TASK_SUCCEEDED,
                self.clock(),
                payload={
                    "artifact": (
                        verification.artifact.to_dict()
                        if verification.artifact
                        else None
                    ),
                    "verifier": verification.verifier_name,
                },
                updates=self._clear_claim_updates(),
                lease_token=lease_token,
            )
            return succeeded
        return self._fail(
            task,
            lease_token,
            code="verification_failed",
            message=f"artifact verification failed: {verification.reason}",
            phase="verification_failed",
            payload_extra={"reason": verification.reason},
        )

    def _block(
        self,
        task: Task,
        lease_token: str,
        *,
        code: str,
        reason: str,
        phase: str,
        mark_not_executed: bool,
    ) -> Task:
        updates = {
            **self._clear_claim_updates(),
            "phase": phase,
            "error": TaskError(code, reason),
        }
        if mark_not_executed:
            updates["execution_mode"] = ExecutionMode.NOT_EXECUTED
        blocked, _ = self.store.transition(
            task.task_id,
            TaskStatus.BLOCKED,
            EventType.POLICY_REJECTED,
            self.clock(),
            payload={"code": code, "reason": reason},
            updates=updates,
            lease_token=lease_token,
        )
        return blocked

    def _fail(
        self,
        task: Task,
        lease_token: str,
        *,
        code: str,
        message: str,
        phase: str,
        payload_extra: Mapping[str, object] | None = None,
    ) -> Task:
        failed, _ = self.store.transition(
            task.task_id,
            TaskStatus.FAILED,
            EventType.TASK_FAILED,
            self.clock(),
            payload={"code": code, **dict(payload_extra or {})},
            updates={
                **self._clear_claim_updates(),
                "phase": phase,
                "error": TaskError(code, message),
            },
            lease_token=lease_token,
        )
        return failed

    def _release_claim(self, task: Task, lease_token: str, *, reason: str) -> Task:
        released, _ = self.store.release_claim(
            task.task_id,
            lease_token=lease_token,
            now=self.clock(),
            payload={"reason": reason},
        )
        return released

    @staticmethod
    def _clear_claim_updates() -> dict[str, object]:
        return {
            "execution_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
        }
