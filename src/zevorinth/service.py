"""Bounded artifact task orchestration with SQLite-backed execution leases."""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping
from uuid import UUID, uuid4

from .artifact_tool import (
    MAX_ARTIFACT_BYTES,
    ArtifactToolError,
    ArtifactWriter,
    RealArtifactWriter,
    task_publication_lock,
)
from .event_store import ClaimLost, EventStore, StoreBusyError
from .lease_renewal import LeaseRenewer
from .models import (
    ClaimResult,
    Event,
    EventType,
    ExecutionMode,
    Task,
    TaskError,
    TaskRequest,
    TaskStatus,
    ToolResult,
    VerificationResult,
    PlanningClassification,
    PlanningOutcome,
)
from .planner import DeterministicPlanner, Planner
from .policy import ArtifactPolicy, missing_required_fields
from .state_machine import is_terminal
from .verifier import ArtifactVerifier


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


FaultHook = Callable[[str, Task], None]
_ACTIVE_RENEWER: ContextVar[LeaseRenewer | None] = ContextVar(
    "zevorinth_active_lease_renewer",
    default=None,
)


class TaskService:
    """Single-host coordinator using SQLite transactions for task ownership."""

    RUNNABLE_STATUSES = frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.INTERRUPTED,
            TaskStatus.RUNNING,
            TaskStatus.VERIFYING,
        }
    )
    RESUMABLE_STATUSES = frozenset(
        {
            TaskStatus.QUEUED,
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
        self.policy = ArtifactPolicy()
        self.verifier = ArtifactVerifier(self.artifacts_root)
        self.store = EventStore(
            self.database_path,
            busy_timeout_ms=busy_timeout_ms,
        )
        self.store.initialize()
        self.recover_expired_leases()

    def enqueue(
        self,
        request: TaskRequest,
        *,
        conversation_id: str | None = None,
        turn_id: str | None = None,
    ) -> Task:
        now = self.clock()
        task = Task(
            task_id=str(uuid4()),
            status=TaskStatus.QUEUED,
            request=request,
            execution_mode=ExecutionMode.NOT_EXECUTED,
            attempt_id=str(uuid4()),
            phase="queued",
            planner_mode=self.planner.mode,
            created_at=now,
            updated_at=now,
            conversation_id=conversation_id,
        )
        task, _ = self.store.create_task(
            task,
            Event.new(
                task.task_id,
                EventType.TASK_CREATED,
                now,
                {
                    "operation": request.operation,
                    "goal_present": bool(request.goal),
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                },
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

    def execute_task(self, task_id: str) -> Task:
        task = self.store.require_task(task_id)
        if is_terminal(task.status):
            return task
        claim = self._claim(task_id, allowed=self.RUNNABLE_STATUSES)
        if not claim.acquired:
            return claim.task
        return self._run_claimed(claim.task, claim.task.lease_token)

    def resume(
        self,
        task_id: str,
        request_patch: Mapping[str, object] | None = None,
    ) -> Task:
        task = self.store.require_task(task_id)
        if is_terminal(task.status):
            return task
        if task.status is TaskStatus.BLOCKED and request_patch is None:
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

        claim = self._claim(task_id, allowed=self.RESUMABLE_STATUSES)
        if not claim.acquired:
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
                    payload={"missing_fields": missing},
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
                payload={"missing_fields": []},
                updates={
                    "request": updated_request,
                    "missing_fields": [],
                    "error": None,
                    "phase": "clarification_completed",
                },
                lease_token=lease_token,
            )

        return self._run_claimed(claimed, lease_token)

    def get_task(self, task_id: str) -> Task | None:
        return self.store.get_task(task_id)

    def require_task(self, task_id: str) -> Task:
        return self.store.require_task(task_id)

    def events(self, task_id: str) -> list[Event]:
        return self.store.list_events(task_id)

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
            {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.VERIFYING}
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

    def _claim(self, task_id: str, *, allowed: frozenset[TaskStatus]) -> ClaimResult:
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
        if task.status is TaskStatus.QUEUED:
            return self._run_with_renewal(
                task,
                lease_token,
                lambda: self._plan_and_execute(task, lease_token),
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
                payload={"resume": True, "source": "persisted_tool_evidence"},
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

    def _execute_tool(self, task: Task, lease_token: str) -> Task:
        parameters = task.request.parameters
        artifact_path = parameters["artifact_path"]
        content = parameters["content"]
        assert isinstance(artifact_path, str)
        assert isinstance(content, str)

        intended_mode = getattr(self.writer, "execution_mode", ExecutionMode.REAL)
        started_at = self.clock()
        self._assert_execution_authority(task, lease_token)
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_STARTED,
            started_at,
            payload={
                "tool_name": self.writer.tool_name,
                "artifact_path": artifact_path,
                "attempt_id": task.attempt_id,
                "intended_execution_mode": intended_mode.value,
                "claim_generation": task.claim_generation,
            },
            updates={
                "execution_mode": intended_mode,
                "phase": "tool_started",
            },
            lease_token=lease_token,
        )
        try:
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
        except ClaimLost:
            raise
        except ArtifactToolError as exc:
            return self._finish_tool_failure(
                task,
                lease_token,
                exc.code,
                exc.message,
                started_at,
            )
        except Exception as exc:
            return self._finish_tool_failure(
                task,
                lease_token,
                "tool_exception",
                f"tool execution raised {type(exc).__name__}: {exc}",
                started_at,
            )

        self._assert_execution_authority(task, lease_token)
        if not result.success:
            return self._finish_tool_failure(
                task,
                lease_token,
                result.error_code or "tool_failed",
                result.error_message or "tool reported failure",
                started_at,
                result=result,
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
            },
            updates={"phase": "verification_started"},
            lease_token=lease_token,
        )
        if self.fault_hook is not None:
            self.fault_hook("after_write_before_verification", verifying)
        return self._verify_and_finish(verifying, lease_token)

    def _finish_tool_failure(
        self,
        task: Task,
        lease_token: str,
        code: str,
        message: str,
        started_at: str,
        *,
        result: ToolResult | None = None,
    ) -> Task:
        failed_result = result or ToolResult(
            attempt_id=task.attempt_id,
            tool_name=self.writer.tool_name,
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

    def _verify_and_finish(self, task: Task, lease_token: str) -> Task:
        self._assert_execution_authority(task, lease_token)
        parameters = task.request.parameters
        artifact_path = parameters["artifact_path"]
        content = parameters["content"]
        assert isinstance(artifact_path, str)
        assert isinstance(content, str)
        result = task.tool_result
        if result is None:
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
        self._assert_execution_authority(task, lease_token)
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.VERIFICATION_COMPLETED,
            self.clock(),
            payload={"verification": verification.to_dict()},
            updates={
                "verification": verification,
                "artifact": verification.artifact or task.artifact,
                "phase": "verified" if verification.passed else "verification_failed",
            },
            lease_token=lease_token,
        )
        self._assert_execution_authority(task, lease_token)
        if verification.passed:
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
