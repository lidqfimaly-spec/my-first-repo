"""Bounded artifact task orchestration with SQLite-backed execution leases."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import time
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from .artifact_tool import (
    MAX_ARTIFACT_BYTES,
    ArtifactToolError,
    ArtifactWriter,
    RealArtifactWriter,
    task_publication_lock,
)
from .event_store import (
    ClaimLost,
    EventStore,
    PlanRevisionConflict,
    StoreBusyError,
)
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
    TestExecutionEvidence,
    VerificationResult,
    PlanningClassification,
    PlanningOutcome,
    PlanRevision,
)
from .local_test_runner import (
    RUN_TESTS_TOOL,
    TestExecutionError,
    excerpt,
    resolve_workspace,
    run_unittest,
)
from .planner import WRITE_TEXT_ARTIFACT, DeterministicPlanner, Planner
from .policy import ArtifactPolicy, missing_required_fields
from .state_machine import is_terminal
from .verifier import ArtifactVerifier, TestResultVerifier
from .task_lifecycle import audit_task_lifecycle
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


FaultHook = Callable[[str, Task], None]
_ACTIVE_RENEWER: ContextVar[LeaseRenewer | None] = ContextVar(
    "zevorinth_active_lease_renewer",
    default=None,
)


class CancellationRequested(Exception):
    pass


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


def _test_evidence_revalidation_reason(
    project_root: Path,
    task: Task,
    evidence: TestExecutionEvidence,
) -> str | None:
    """Re-read persisted isolation evidence before accepting test success."""
    if not evidence.evidence_id:
        return None
    from .evidence_store import EvidenceStore, EvidenceStoreError
    from .isolation import _live_image_digest, _live_runtime_identity

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
        self.test_verifier = TestResultVerifier()
        self.test_runner = run_unittest
        self.container_popen = subprocess.Popen
        self.container_terminator = None
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

    def audit_lifecycle(
        self, task_id: str, *, now: str | None = None
    ) -> dict[str, Any]:
        """Read-only audit of task, evidence and cleanup consistency."""
        task = self.store.require_task(task_id)
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
            from .isolation import _live_image_digest, _live_runtime_identity

            image = str(snapshot.get("image") or "python:3.13-alpine")
            try:
                evidence_state = EvidenceStore(
                    self.project_root
                ).verify_stored_evidence(
                    evidence.evidence_id,
                    expected_runtime=_live_runtime_identity(),
                    expected_image_digest=_live_image_digest(image),
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
        return audit_task_lifecycle(
            task,
            self.store.list_events(task_id),
            now=now,
            evidence_state=evidence_state,
        )

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

    def cancel(self, task_id: str) -> Task:
        return self.store.cancel_task(task_id, now=self.clock())

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
        if (
            current.status
            not in {
                TaskStatus.QUEUED,
                TaskStatus.AWAITING_CLARIFICATION,
                TaskStatus.BLOCKED,
                TaskStatus.INTERRUPTED,
            }
            or current.tool_result is not None
            or current.artifact is not None
            or current.execution_mode is not ExecutionMode.NOT_EXECUTED
        ):
            raise PlanRevisionConflict(
                "task cannot be revised after execution evidence"
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
        if self._is_test_plan(task):
            return self._resume_test_task(task, lease_token)
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

    @staticmethod
    def _is_test_plan(task: Task) -> bool:
        return bool(
            task.plan is not None
            and task.plan.steps
            and task.plan.steps[0].tool_name == RUN_TESTS_TOOL
        )

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

    def _execute_tool(self, task: Task, lease_token: str) -> Task:
        plan_step = (
            task.plan.steps[0]
            if task.plan is not None and task.plan.steps
            else None
        )
        if plan_step is not None and plan_step.tool_name == RUN_TESTS_TOOL:
            return self._execute_test_tool(task, lease_token, plan_step)
        parameters = task.request.parameters
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
                "tool_name": self.writer.tool_name,
                "artifact_path": artifact_path,
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
                tool_name=self.writer.tool_name,
            )
        except Exception as exc:
            return self._finish_tool_failure(
                task,
                lease_token,
                "tool_exception",
                f"tool execution raised {type(exc).__name__}: {exc}",
                started_at,
                tool_name=self.writer.tool_name,
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
            operation=WRITE_TEXT_ARTIFACT,
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
            },
            updates={"phase": "verification_started"},
            lease_token=lease_token,
        )
        if self.fault_hook is not None:
            self.fault_hook("after_write_before_verification", verifying)
        return self._verify_and_finish(verifying, lease_token)

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

    def _blocked_container_execution(
        self, task: Task, lease_token: str, parameters: dict[str, Any]
    ) -> Task:
        """run_tests defaults to the container backend; never fall back to host."""
        from .evidence_store import EvidenceStore
        from .isolation import _live_image_digest, _live_runtime_identity

        image = str(parameters.get("image", "python:3.13-slim"))
        evidence_id = parameters.get("evidence_id")
        store = EvidenceStore(self.project_root)
        reasons: list[str] = []
        state = "blocked"
        if not evidence_id:
            reasons.append("evidence_id_missing")
        else:
            verdict = store.verify_stored_evidence(
                str(evidence_id),
                expected_runtime=_live_runtime_identity(),
                expected_image_digest=_live_image_digest(image),
                expected_config_digest=str(parameters.get("config_digest", "")),
                expected_request_id=task.task_id,
                expected_command_digest=str(
                    parameters.get("command_digest", "")
                ),
            )
            state = verdict["state"]
            reasons = list(verdict["reasons"])
        return self._fail(
            task,
            lease_token,
            code="test_isolation_unverified",
            message="run_tests requires verified container isolation: "
            + "; ".join(reasons or [state]),
            phase="blocked_isolation",
            payload_extra={"isolation_state": state, "reasons": reasons},
        )

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
                raw = self._run_container_test(task, parameters, cancel_check)
            else:
                raw = self.test_runner(
                    project_root=self.project_root,
                    parameters=parameters,
                    cancel_check=cancel_check,
                )
        except TestExecutionError as exc:
            return self._finish_tool_failure(
                task,
                lease_token,
                exc.code,
                exc.message,
                started_at,
                tool_name=RUN_TESTS_TOOL,
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
            payload={"verification": verification.to_dict()},
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
            payload={"verification": verification.to_dict()},
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
