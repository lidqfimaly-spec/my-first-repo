"""Bounded end-to-end task orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from uuid import uuid4

from .artifact_tool import (
    ArtifactToolError,
    ArtifactWriter,
    RealArtifactWriter,
)
from .event_store import EventStore
from .models import (
    Event,
    EventType,
    ExecutionMode,
    Task,
    TaskError,
    TaskRequest,
    TaskStatus,
    ToolResult,
    VerificationResult,
)
from .planner import DeterministicPlanner, PlanningError
from .policy import ArtifactPolicy, missing_required_fields
from .state_machine import is_terminal
from .verifier import ArtifactVerifier


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


FaultHook = Callable[[str, Task], None]


class TaskService:
    """Single-process coordinator for the Phase 1 artifact task."""

    def __init__(
        self,
        project_root: Path,
        *,
        writer: ArtifactWriter | None = None,
        fault_hook: FaultHook | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.project_root = project_root.resolve()
        self.artifacts_root = self.project_root / "artifacts"
        self.database_path = self.project_root / "data" / "zevorinth.sqlite3"
        self.clock = clock
        self.writer = writer or RealArtifactWriter(self.artifacts_root)
        self.fault_hook = fault_hook
        self.planner = DeterministicPlanner()
        self.policy = ArtifactPolicy()
        self.verifier = ArtifactVerifier(self.artifacts_root)
        self.store = EventStore(self.database_path)
        self.store.initialize()
        self.recover_in_progress_tasks()

    def submit(self, request: TaskRequest) -> Task:
        now = self.clock()
        task = Task(
            task_id=str(uuid4()),
            status=TaskStatus.QUEUED,
            request=request,
            execution_mode=ExecutionMode.NOT_EXECUTED,
            attempt_id=str(uuid4()),
            phase="queued",
            created_at=now,
            updated_at=now,
        )
        task, _ = self.store.create_task(
            task,
            Event.new(
                task.task_id,
                EventType.TASK_CREATED,
                now,
                {"operation": request.operation, "goal_present": bool(request.goal)},
            ),
        )
        missing = missing_required_fields(request)
        if missing:
            task, _ = self.store.transition(
                task.task_id,
                TaskStatus.AWAITING_CLARIFICATION,
                EventType.CLARIFICATION_REQUIRED,
                self.clock(),
                payload={"missing_fields": missing},
                updates={
                    "missing_fields": missing,
                    "phase": "awaiting_clarification",
                },
            )
            return task
        return self._execute_queued_task(task.task_id)

    def get_task(self, task_id: str) -> Task | None:
        return self.store.get_task(task_id)

    def require_task(self, task_id: str) -> Task:
        return self.store.require_task(task_id)

    def events(self, task_id: str) -> list[Event]:
        return self.store.list_events(task_id)

    def recover_in_progress_tasks(self) -> list[Task]:
        """Treat stale RUNNING/VERIFYING rows as interrupted on process start."""
        recovered: list[Task] = []
        for task in self.store.find_tasks_by_statuses(
            {TaskStatus.RUNNING, TaskStatus.VERIFYING}
        ):
            interrupted, _ = self.store.transition(
                task.task_id,
                TaskStatus.INTERRUPTED,
                EventType.TASK_INTERRUPTED,
                self.clock(),
                payload={
                    "reason": "process_restart_before_terminal_state",
                    "previous_status": task.status.value,
                    "previous_phase": task.phase,
                },
                updates={
                    "phase": "recovered_after_restart",
                    "error": TaskError(
                        "interrupted",
                        "task was in progress when a new runtime started",
                    ),
                },
            )
            recovered.append(interrupted)
        return recovered

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

        if task.status in {TaskStatus.AWAITING_CLARIFICATION, TaskStatus.BLOCKED}:
            if request_patch is not None:
                unknown = set(request_patch) - {"goal", "operation", "parameters"}
                if unknown:
                    raise ValueError(f"unsupported request fields: {sorted(unknown)}")
                updated_request = task.request.merged(request_patch)
            else:
                updated_request = task.request
            missing = missing_required_fields(updated_request)
            if missing:
                updated, _ = self.store.record_event_and_update(
                    task.task_id,
                    EventType.CLARIFICATION_UPDATED,
                    self.clock(),
                    payload={"missing_fields": missing},
                    updates={
                        "request": updated_request,
                        "missing_fields": missing,
                        "phase": "awaiting_clarification",
                    },
                )
                return updated
            queued, _ = self.store.transition(
                task.task_id,
                TaskStatus.QUEUED,
                EventType.CLARIFICATION_UPDATED,
                self.clock(),
                payload={"missing_fields": []},
                updates={
                    "request": updated_request,
                    "missing_fields": [],
                    "error": None,
                    "phase": "clarification_completed",
                    "attempt_id": str(uuid4()),
                },
            )
            return self._execute_queued_task(queued.task_id)

        if task.status is TaskStatus.INTERRUPTED:
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
                    updates={
                        "phase": "resumed_verification",
                        "attempt_id": str(uuid4()),
                        "error": None,
                    },
                )
                return self._verify_and_finish(verifying)
            plan = task.plan
            if plan is None:
                failed, _ = self.store.transition(
                    task.task_id,
                    TaskStatus.FAILED,
                    EventType.TASK_FAILED,
                    self.clock(),
                    payload={"reason": "missing_plan_checkpoint"},
                    updates={
                        "phase": "failed",
                        "error": TaskError(
                            "missing_plan_checkpoint",
                            "interrupted task has no persisted plan",
                        ),
                    },
                )
                return failed
            decision = self.policy.evaluate(
                request=task.request,
                plan=plan,
                task_id=task.task_id,
                artifacts_root=self.artifacts_root,
            )
            if not decision.allowed:
                blocked, _ = self.store.transition(
                    task.task_id,
                    TaskStatus.BLOCKED,
                    EventType.POLICY_REJECTED,
                    self.clock(),
                    payload={"code": decision.code, "reason": decision.reason},
                    updates={
                        "phase": "blocked",
                        "error": TaskError(decision.code, decision.reason),
                    },
                )
                return blocked
            running, _ = self.store.transition(
                task.task_id,
                TaskStatus.RUNNING,
                EventType.TASK_RESUMED,
                self.clock(),
                payload={"resume": True, "source": "no_persisted_artifact"},
                updates={
                    "phase": "resumed_before_tool",
                    "attempt_id": str(uuid4()),
                    "error": None,
                },
            )
            return self._execute_tool(running)

        return task

    def _execute_queued_task(self, task_id: str) -> Task:
        task = self.store.require_task(task_id)
        if task.status is not TaskStatus.QUEUED:
            return task
        try:
            plan = self.planner.plan(task.request)
        except PlanningError as exc:
            blocked, _ = self.store.transition(
                task.task_id,
                TaskStatus.BLOCKED,
                EventType.POLICY_REJECTED,
                self.clock(),
                payload={"code": exc.code, "reason": exc.message},
                updates={
                    "phase": "planning_rejected",
                    "error": TaskError(exc.code, exc.message),
                },
            )
            return blocked
        running, _ = self.store.transition(
            task.task_id,
            TaskStatus.RUNNING,
            EventType.TASK_PLANNED,
            self.clock(),
            payload={"plan": plan.to_dict()},
            updates={"plan": plan, "phase": "planned"},
        )
        decision = self.policy.evaluate(
            request=running.request,
            plan=plan,
            task_id=running.task_id,
            artifacts_root=self.artifacts_root,
        )
        if not decision.allowed:
            blocked, _ = self.store.transition(
                running.task_id,
                TaskStatus.BLOCKED,
                EventType.POLICY_REJECTED,
                self.clock(),
                payload={"code": decision.code, "reason": decision.reason},
                updates={
                    "phase": "blocked",
                    "execution_mode": ExecutionMode.NOT_EXECUTED,
                    "error": TaskError(decision.code, decision.reason),
                },
            )
            return blocked
        return self._execute_tool(running)

    def _execute_tool(self, task: Task) -> Task:
        parameters = task.request.parameters
        artifact_path = parameters["artifact_path"]
        content = parameters["content"]
        assert isinstance(artifact_path, str)
        assert isinstance(content, str)

        intended_mode = getattr(self.writer, "execution_mode", ExecutionMode.REAL)
        started_at = self.clock()
        task, _ = self.store.record_event_and_update(
            task.task_id,
            EventType.TOOL_STARTED,
            started_at,
            payload={
                "tool_name": self.writer.tool_name,
                "artifact_path": artifact_path,
                "attempt_id": task.attempt_id,
                "intended_execution_mode": intended_mode.value,
            },
            updates={
                "execution_mode": intended_mode,
                "phase": "tool_started",
            },
        )
        try:
            result = self.writer.write(
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                artifact_path=artifact_path,
                content=content,
                started_at=started_at,
                finished_at=self.clock(),
            )
        except ArtifactToolError as exc:
            return self._finish_tool_failure(task, exc.code, exc.message, started_at)
        except Exception as exc:
            return self._finish_tool_failure(
                task,
                "tool_exception",
                f"tool execution raised {type(exc).__name__}: {exc}",
                started_at,
            )

        if not result.success:
            return self._finish_tool_failure(
                task,
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
            )
            simulated, _ = self.store.transition(
                simulated.task_id,
                TaskStatus.SIMULATED,
                EventType.SIMULATION_COMPLETED,
                self.clock(),
                payload={
                    "reason": "simulated tool produced no real artifact",
                    "real_artifact_created": False,
                },
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
            )
            failed, _ = self.store.transition(
                failed.task_id,
                TaskStatus.FAILED,
                EventType.TASK_FAILED,
                self.clock(),
                payload={"code": "invalid_tool_result"},
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
        )
        if self.fault_hook is not None:
            self.fault_hook("after_write_before_verification", verifying)
        return self._verify_and_finish(verifying)

    def _finish_tool_failure(
        self,
        task: Task,
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
        )
        failed, _ = self.store.transition(
            failed.task_id,
            TaskStatus.FAILED,
            EventType.TASK_FAILED,
            self.clock(),
            payload={"code": code, "verification_performed": False},
        )
        return failed

    def _verify_and_finish(self, task: Task) -> Task:
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
            verification = self.verifier.verify(
                task_id=task.task_id,
                artifact_path=artifact_path,
                expected_content=content,
                tool_result=result,
                checked_at=self.clock(),
            )
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
        )
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
            )
            return succeeded
        failed, _ = self.store.transition(
            task.task_id,
            TaskStatus.FAILED,
            EventType.TASK_FAILED,
            self.clock(),
            payload={
                "code": "verification_failed",
                "reason": verification.reason,
            },
            updates={
                "error": TaskError(
                    "verification_failed",
                    f"artifact verification failed: {verification.reason}",
                )
            },
        )
        return failed
