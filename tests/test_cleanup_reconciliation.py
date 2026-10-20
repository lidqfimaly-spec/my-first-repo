"""Intent persistence, quarantine recovery and read-only reconciliation."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.event_store import EventStore  # noqa: E402
from zevorinth.models import TaskRequest  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.workspace_cleanup import (  # noqa: E402
    execute_cleanup,
    plan_cleanup,
    reconcile_cleanup,
)


def _written(root: Path):
    service = TaskService(root)
    task = service.submit(
        TaskRequest(
            goal="write",
            operation="write_text_artifact",
            parameters={"artifact_path": "a.txt", "content": "hi\n"},
        )
    )
    return service, task, root / "artifacts" / task.task_id


class IntentPersistenceTests(unittest.TestCase):
    def test_intent_is_persisted_before_the_filesystem_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, task_dir = _written(root)
            service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            events = [e.event_type for e in service.events(task.task_id)]
            self.assertLess(
                events.index("cleanup_intent"),
                events.index("cleanup_completed"),
            )

    def test_intent_failure_is_reported(self) -> None:
        class FailingStore(EventStore):
            def record_event_and_update(self, *args, **kwargs):
                raise RuntimeError("store down")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, task_dir = _written(root)
            service.store = FailingStore(service.database_path)
            record = service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            self.assertFalse(record["intent_persisted"])
            self.assertFalse(record["evidence_persisted"])

    def test_crash_before_quarantine_leaves_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, task_dir = _written(root)
            decision = plan_cleanup(
                effects=task.tool_result.workspace_effects,
                workspace_root=task_dir,
                relative_path="a.txt",
                current_generation=task.claim_generation,
                enabled=True,
                authorized=True,
            )

            def checkpoint(stage: str) -> None:
                if stage == "before_quarantine":
                    raise RuntimeError("crash")

            with self.assertRaises(RuntimeError):
                execute_cleanup(
                    decision,
                    workspace_root=task_dir,
                    checkpoint=checkpoint,
                )
            self.assertTrue((task_dir / "a.txt").exists())
            self.assertFalse((task_dir / ".quarantine").exists())


class ReconciliationTests(unittest.TestCase):
    def _state(self, root: Path, task, **overrides):
        effects = task.tool_result.workspace_effects
        base = {
            "workspace_root": root / "artifacts" / task.task_id,
            "relative_path": "a.txt",
            "operation_id": "op-1",
            "expected_sha256": next(
                change["after_sha256"]
                for change in effects["diff"]["changes"]
                if change["path"] == "a.txt"
            ),
            "intent_persisted": True,
            "completed_event": False,
            "current_generation": task.claim_generation,
            "record_generation": task.claim_generation,
        }
        base.update(overrides)
        return reconcile_cleanup(**base)

    def test_intent_only_is_not_executed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, _task_dir = _written(root)
            result = self._state(root, task)
        self.assertEqual(result["state"], "reconciled_not_executed")

    def test_missing_intent_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, _task_dir = _written(root)
            result = self._state(root, task, intent_persisted=False)
        self.assertEqual(result["state"], "reconciled_unverified")

    def test_quarantined_but_not_deleted_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, task_dir = _written(root)
            target = task_dir / "a.txt"
            quarantine = task_dir / ".quarantine" / "op-1"
            quarantine.mkdir(parents=True)
            shutil.move(str(target), str(quarantine / "a.txt"))
            result = self._state(root, task)
        self.assertEqual(result["state"], "reconciled_pending")

    def test_quarantine_content_mismatch_is_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, task_dir = _written(root)
            target = task_dir / "a.txt"
            quarantine = task_dir / ".quarantine" / "op-1"
            quarantine.mkdir(parents=True)
            shutil.move(str(target), str(quarantine / "a.txt"))
            (quarantine / "a.txt").write_text("changed\n")
            result = self._state(root, task)
        self.assertEqual(result["state"], "reconciled_conflict")

    def test_completed_event_with_missing_files_is_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, task_dir = _written(root)
            (task_dir / "a.txt").unlink()
            result = self._state(root, task, completed_event=True)
        self.assertEqual(result["state"], "reconciled_completed")

    def test_completed_event_with_existing_file_is_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, _task_dir = _written(root)
            result = self._state(root, task, completed_event=True)
        self.assertEqual(result["state"], "reconciled_conflict")

    def test_stale_generation_is_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, _task_dir = _written(root)
            result = self._state(
                root, task, current_generation=task.claim_generation + 1
            )
        self.assertEqual(result["state"], "reconciled_conflict")

    def test_service_reconcile_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, _task_dir = _written(root)
            record = service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            result = service.reconcile_cleanup_operation(
                task.task_id, record["operation_id"]
            )
        self.assertEqual(result["state"], "reconciled_completed")

    def test_unknown_operation_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, _task_dir = _written(root)
            result = service.reconcile_cleanup_operation(
                task.task_id, "does-not-exist"
            )
        self.assertEqual(result["state"], "reconciled_unverified")


if __name__ == "__main__":
    unittest.main()
