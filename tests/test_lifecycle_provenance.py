"""Fault injection across publish, evidence, verification and commit.

Every fault is injected through a test-only subclass.  Production defaults
stay untouched: ``TaskService`` never installs a fault hook by itself, and
``RealArtifactWriter.checkpoint_hook`` defaults to ``None``.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from zevorinth.artifact_tool import RealArtifactWriter  # noqa: E402
from zevorinth.event_store import EventStore  # noqa: E402
from zevorinth.models import (  # noqa: E402
    EventType,
    TaskRequest,
    TaskStatus,
    ToolResult,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.safety_gate import (  # noqa: E402
    build_effect,
    validate_case_effects,
)
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.verifier import ArtifactVerifier  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"
RECOVERY = "2026-10-20T00:01:00.000+00:00"
ARTIFACT_PATH = "lifecycle.txt"
ARTIFACT_CONTENT = "lifecycle proof\n"


class InjectedFault(RuntimeError):
    pass


def _request() -> TaskRequest:
    return TaskRequest(
        goal="lifecycle provenance",
        operation=WRITE_TEXT_ARTIFACT,
        parameters={
            "artifact_path": ARTIFACT_PATH,
            "content": ARTIFACT_CONTENT,
        },
    )


def _event_name(event_type: Any) -> str:
    return str(getattr(event_type, "value", event_type))


class FaultStore(EventStore):
    """Abort a transaction just before a named event is persisted."""

    def __init__(self, path: Path, stage: str) -> None:
        super().__init__(path)
        self.stage = stage
        self.trips: list[str] = []

    def _insert_event(self, connection, event):
        name = _event_name(event.event_type)
        if name == self.stage:
            self.trips.append(name)
            raise InjectedFault(f"injected before {name} commit")
        return super()._insert_event(connection, event)


class CheckpointWriter(RealArtifactWriter):
    """Raise at a named filesystem checkpoint."""

    def __init__(self, root: Path, stage: str) -> None:
        super().__init__(root)
        self.stage = stage
        self.trips: list[str] = []

    def _checkpoint(
        self,
        stage: str,
        task_id: str,
        attempt_id: str,
        claim_generation: int | None,
    ) -> None:
        if stage == self.stage:
            self.trips.append(stage)
            raise InjectedFault(f"injected at {stage}")
        super()._checkpoint(stage, task_id, attempt_id, claim_generation)


class NoWriteWriter:
    tool_name = "write_text_artifact"

    def __init__(self) -> None:
        self.calls = 0

    def write(self, **_: Any) -> ToolResult:
        self.calls += 1
        raise AssertionError("recovery must not publish again")


def _events(service: TaskService, task_id: str) -> list[str]:
    return [
        event.event_type for event in service.events(task_id)
    ]


class LifecycleProvenanceTests(unittest.TestCase):
    def _faulted_run(
        self,
        root: Path,
        stage: str,
    ) -> tuple[TaskService, FaultStore, str]:
        service = TaskService(root, clock=lambda: NOW)
        store = FaultStore(service.database_path, stage)
        service.store = store
        task = service.enqueue(_request())
        with self.assertRaises(InjectedFault):
            service.execute_task(task.task_id)
        self.assertEqual(store.trips, [stage])
        return service, store, task.task_id

    def _recover(
        self,
        root: Path,
        task_id: str,
        writer: Any | None = None,
    ):
        service = TaskService(
            root,
            writer=writer,
            clock=lambda: RECOVERY,
        )
        service.store.expire_claim(task_id, now=RECOVERY)
        interrupted = service.require_task(task_id)
        self.assertEqual(interrupted.status, TaskStatus.INTERRUPTED)
        return service, service.resume(task_id)

    # 1. failure before the file is published
    def test_failure_before_publish_leaves_no_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            writer = CheckpointWriter(root / "artifacts", "after_staging_write")
            service = TaskService(root, writer=writer, clock=lambda: NOW)
            task = service.enqueue(_request())
            failed = service.execute_task(task.task_id)

            self.assertEqual(writer.trips, ["after_staging_write"])
            self.assertEqual(failed.status, TaskStatus.FAILED)
            self.assertEqual(failed.error.code, "tool_exception")
            self.assertIsNone(failed.artifact)
            task_dir = root / "artifacts" / task.task_id
            self.assertFalse((task_dir / ARTIFACT_PATH).exists())
            staging = task_dir / ".staging"
            self.assertEqual(list(staging.glob("*.part")), [])
            event_types = _events(service, task.task_id)
            self.assertIn(EventType.TOOL_STARTED.value, event_types)
            self.assertNotIn(EventType.TOOL_SUCCEEDED.value, event_types)
            effects = build_effect(
                origin="none", tool_calls=1, file_writes=0
            )
            validate_case_effects({"id": "before_publish", "effects": effects})

    # 2. failure after publish, before evidence is recorded
    def test_failure_after_publish_leaves_unbacked_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            writer = CheckpointWriter(root / "artifacts", "after_publish")
            service = TaskService(root, writer=writer, clock=lambda: NOW)
            task = service.enqueue(_request())
            failed = service.execute_task(task.task_id)

            self.assertEqual(writer.trips, ["after_publish"])
            self.assertEqual(failed.status, TaskStatus.FAILED)
            self.assertEqual(failed.error.code, "tool_exception")
            self.assertIsNone(failed.artifact)
            artifact = root / "artifacts" / task.task_id / ARTIFACT_PATH
            self.assertEqual(artifact.read_text(), ARTIFACT_CONTENT)
            self.assertTrue(
                hashlib.sha256(artifact.read_bytes()).hexdigest()
            )
            event_types = _events(service, task.task_id)
            self.assertNotIn(EventType.TOOL_SUCCEEDED.value, event_types)
            self.assertNotIn(EventType.TASK_SUCCEEDED.value, event_types)
            # A file without persisted evidence is "unbacked", never written.
            effects = build_effect(
                origin="none",
                integrity="unbacked",
                tool_calls=1,
                file_writes=0,
            )
            validate_case_effects({"id": "after_publish", "effects": effects})
            # FAILED is terminal: a repeat execution must not publish again.
            before = artifact.stat().st_ino
            again = service.execute_task(task.task_id)
            self.assertEqual(again.status, TaskStatus.FAILED)
            self.assertEqual(artifact.stat().st_ino, before)

    # 3. published file, evidence transaction aborted, then recovery
    def test_evidence_transaction_failure_recovers_without_overwrite(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, store, task_id = self._faulted_run(
                root, "tool_succeeded"
            )
            artifact = root / "artifacts" / task_id / ARTIFACT_PATH
            original_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
            interrupted_state = service.require_task(task_id)
            self.assertEqual(interrupted_state.status, TaskStatus.RUNNING)
            self.assertIsNone(interrupted_state.artifact)
            self.assertIsNotNone(interrupted_state.lease_token)
            self.assertNotIn(
                EventType.TOOL_SUCCEEDED.value, _events(service, task_id)
            )

            writer = NoWriteWriter()
            recovered_service, resumed = self._recover(
                root, task_id, writer
            )
            self.assertEqual(resumed.status, TaskStatus.BLOCKED)
            self.assertEqual(resumed.error.code, "artifact_exists")
            self.assertEqual(writer.calls, 0)
            self.assertEqual(
                hashlib.sha256(artifact.read_bytes()).hexdigest(),
                original_sha,
            )
            event_types = _events(recovered_service, task_id)
            self.assertNotIn(EventType.TASK_SUCCEEDED.value, event_types)
            self.assertEqual(
                recovered_service.require_task(task_id).claim_generation, 2
            )

    # 4. evidence persisted, verification never started, then recovery
    def test_evidence_persisted_recovers_by_verifying_without_rewrite(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, task_id = self._faulted_run(
                root, "verification_started"
            )
            pending = service.require_task(task_id)
            self.assertEqual(pending.status, TaskStatus.RUNNING)
            self.assertIsNotNone(pending.artifact)
            self.assertIsNotNone(pending.tool_result)
            event_types = _events(service, task_id)
            self.assertEqual(
                event_types.count(EventType.TOOL_SUCCEEDED.value), 1
            )

            writer = NoWriteWriter()
            recovered_service, resumed = self._recover(
                root, task_id, writer
            )
            self.assertEqual(resumed.status, TaskStatus.SUCCEEDED)
            self.assertEqual(writer.calls, 0)
            self.assertTrue(resumed.verification.passed)
            final_events = _events(recovered_service, task_id)
            self.assertEqual(
                final_events.count(EventType.TOOL_STARTED.value), 1
            )
            self.assertEqual(
                final_events.count(EventType.TASK_SUCCEEDED.value), 1
            )
            effects = build_effect(
                origin="recovered",
                tool_calls=0,
                file_writes=0,
                evidence=resumed.artifact,
                artifact_root=root / "artifacts",
                task_id=task_id,
            )
            validate_case_effects(
                {"id": "recovered", "effects": effects}
            )
            self.assertEqual(effects["integrity"], "verified")
            self.assertEqual(effects["file_writes"], 0)

    # 5. success commit aborted, double recovery
    def test_success_commit_failure_recovers_once_on_double_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, task_id = self._faulted_run(
                root, "task_succeeded"
            )
            pending = service.require_task(task_id)
            self.assertEqual(pending.status, TaskStatus.VERIFYING)
            self.assertTrue(pending.verification.passed)
            self.assertNotIn(
                EventType.TASK_SUCCEEDED.value, _events(service, task_id)
            )

            writer = NoWriteWriter()
            recovered_service, first = self._recover(root, task_id, writer)
            second = recovered_service.resume(task_id)

            self.assertEqual(first.status, TaskStatus.SUCCEEDED)
            self.assertEqual(second.status, TaskStatus.SUCCEEDED)
            self.assertEqual(writer.calls, 0)
            final_events = _events(recovered_service, task_id)
            self.assertEqual(
                final_events.count(EventType.TASK_SUCCEEDED.value), 1
            )
            self.assertEqual(
                final_events.count(EventType.TOOL_STARTED.value), 1
            )

    # 6. recovery after the workspace content changed
    def test_recovery_with_modified_content_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, task_id = self._faulted_run(
                root, "verification_started"
            )
            artifact = root / "artifacts" / task_id / ARTIFACT_PATH
            # Evidence recorded before the workspace changed.
            original_evidence = service.require_task(task_id).artifact
            artifact.write_text("tampered after evidence\n")
            evidence_sha = original_evidence.sha256

            writer = NoWriteWriter()
            recovered_service, resumed = self._recover(
                root, task_id, writer
            )
            self.assertEqual(resumed.status, TaskStatus.FAILED)
            self.assertEqual(resumed.error.code, "verification_failed")
            self.assertEqual(writer.calls, 0)
            event_types = _events(recovered_service, task_id)
            self.assertNotIn(EventType.TASK_SUCCEEDED.value, event_types)
            self.assertEqual(
                artifact.read_text(), "tampered after evidence\n"
            )
            effects = build_effect(
                origin="recovered",
                integrity="mismatch",
                tool_calls=0,
                file_writes=0,
                evidence=original_evidence,
                artifact_root=root / "artifacts",
                task_id=task_id,
            )
            validate_case_effects(
                {"id": "modified", "effects": effects}
            )
            self.assertNotEqual(effects["file_sha256"], evidence_sha)
            self.assertNotEqual(
                effects["file_sha256"], effects["evidence_sha256"]
            )

    # 7. recovery after the artifact disappeared
    def test_recovery_with_missing_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, _store, task_id = self._faulted_run(
                root, "verification_started"
            )
            artifact = root / "artifacts" / task_id / ARTIFACT_PATH
            artifact.unlink()

            writer = NoWriteWriter()
            recovered_service, resumed = self._recover(
                root, task_id, writer
            )
            self.assertEqual(resumed.status, TaskStatus.FAILED)
            self.assertEqual(resumed.error.code, "verification_failed")
            self.assertEqual(writer.calls, 0)
            self.assertIn(
                resumed.verification.reason, {"artifact_missing"}
            )
            self.assertNotIn(
                EventType.TASK_SUCCEEDED.value,
                _events(recovered_service, task_id),
            )
            self.assertFalse(artifact.exists())

    # 8. evidence from a newer generation cannot validate an older claim
    def test_newer_generation_evidence_is_rejected_for_older_claim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            writer = RealArtifactWriter(root)
            task_id = str(uuid4())
            result = writer.write(
                task_id=task_id,
                attempt_id=str(uuid4()),
                artifact_path=ARTIFACT_PATH,
                content=ARTIFACT_CONTENT,
                started_at=NOW,
                finished_at=NOW,
                lease_token="lease",
                claim_generation=2,
                authorize=lambda _token, _generation: None,
            )
            verification = ArtifactVerifier(root).verify(
                task_id=task_id,
                artifact_path=ARTIFACT_PATH,
                expected_content=ARTIFACT_CONTENT,
                tool_result=result,
                checked_at=NOW,
                expected_attempt_id=result.attempt_id,
                require_provenance=True,
                maximum_claim_generation=1,
            )
            self.assertFalse(verification.passed)
            self.assertEqual(verification.reason, "tool_evidence_mismatch")


if __name__ == "__main__":
    unittest.main()
