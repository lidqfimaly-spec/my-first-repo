from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.event_store import EventStore, EventStoreError  # noqa: E402
from zevorinth.models import (  # noqa: E402
    EventType,
    ExecutionMode,
    Task,
    TaskRequest,
    TaskStatus,
    ToolResult,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


class NoWriteWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self) -> None:
        self.calls = 0

    def write(self, **_: Any) -> ToolResult:
        self.calls += 1
        raise AssertionError("recovery must not rewrite an existing artifact")


class FailToolEvidenceStore(EventStore):
    def record_event_and_update(
        self,
        task_id: str,
        event_type: EventType | str,
        occurred_at: str,
        payload=None,
        updates=None,
        lease_token=None,
    ):
        if event_type == EventType.TOOL_SUCCEEDED:
            raise EventStoreError("injected failure after file write")
        return super().record_event_and_update(
            task_id,
            event_type,
            occurred_at,
            payload=payload,
            updates=updates,
            lease_token=lease_token,
        )


def _request(path: str = "hardening.txt", content: str = "hardening"):
    return TaskRequest(
        goal="hardening proof",
        operation=WRITE_TEXT_ARTIFACT,
        parameters={"artifact_path": path, "content": content},
    )


class HardeningRecoveryTests(unittest.TestCase):
    def test_database_failure_after_write_is_diagnosed_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_time = "2026-10-19T00:00:00.000+00:00"
            future_time = "2026-10-19T00:01:01.000+00:00"
            seed = TaskService(root, clock=lambda: base_time)
            task = seed.enqueue(_request("db-gap.txt", "already written"))

            runner = TaskService(root, clock=lambda: base_time)
            runner.store = FailToolEvidenceStore(root / "data" / "zevorinth.sqlite3")
            with self.assertRaises(EventStoreError):
                runner.execute_task(task.task_id)

            checkpoint = EventStore(
                root / "data" / "zevorinth.sqlite3"
            ).require_task(task.task_id)
            self.assertEqual(checkpoint.status, TaskStatus.RUNNING)
            self.assertEqual(checkpoint.execution_mode, ExecutionMode.REAL)
            self.assertIsNone(checkpoint.tool_result)
            artifact = root / "artifacts" / task.task_id / "db-gap.txt"
            self.assertEqual(artifact.read_text(), "already written")

            no_write = NoWriteWriter()
            restarted = TaskService(
                root,
                writer=no_write,
                clock=lambda: future_time,
            )
            self.assertEqual(
                restarted.require_task(task.task_id).status,
                TaskStatus.INTERRUPTED,
            )
            result = restarted.resume(task.task_id)

            self.assertEqual(result.status, TaskStatus.BLOCKED)
            self.assertEqual(result.error.code, "artifact_exists")
            self.assertEqual(result.execution_mode, ExecutionMode.REAL)
            self.assertEqual(no_write.calls, 0)
            self.assertEqual(artifact.read_text(), "already written")
            event_types = [
                event.event_type for event in restarted.events(task.task_id)
            ]
            self.assertNotIn("tool_succeeded", event_types)
            self.assertNotIn("task_succeeded", event_types)

    def test_deleted_existing_artifact_fails_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_time = "2026-10-19T00:00:00.000+00:00"
            future_time = "2026-10-19T00:01:01.000+00:00"
            task_ids: list[str] = []

            def interrupt(_: str, task: Task) -> None:
                task_ids.append(task.task_id)
                raise RuntimeError("interrupt")

            crashing = TaskService(
                root,
                fault_hook=interrupt,
                clock=lambda: base_time,
            )
            with self.assertRaises(RuntimeError):
                crashing.submit(_request("deleted.txt", "will be deleted"))
            task_id = task_ids[0]
            (root / "artifacts" / task_id / "deleted.txt").unlink()

            no_write = NoWriteWriter()
            restarted = TaskService(
                root,
                writer=no_write,
                clock=lambda: future_time,
            )
            failed = restarted.resume(task_id)

            self.assertEqual(failed.status, TaskStatus.FAILED)
            self.assertEqual(failed.verification.reason, "artifact_missing")
            self.assertEqual(no_write.calls, 0)

    def test_successful_task_ignores_repeated_run_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            task = service.submit(_request("once.txt", "once"))
            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            event_count = len(service.events(task.task_id))

            first = service.execute_task(task.task_id)
            second = service.resume(task.task_id)

            self.assertEqual(first.status, TaskStatus.SUCCEEDED)
            self.assertEqual(second.status, TaskStatus.SUCCEEDED)
            self.assertEqual(len(service.events(task.task_id)), event_count)
            self.assertEqual(
                [event.event_type for event in service.events(task.task_id)].count(
                    "task_succeeded"
                ),
                1,
            )

    def test_repeated_submission_creates_distinct_task_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            first = service.submit(_request("same-request.txt", "same request"))
            second = service.submit(_request("same-request.txt", "same request"))

            self.assertNotEqual(first.task_id, second.task_id)
            self.assertEqual(first.status, TaskStatus.SUCCEEDED)
            self.assertEqual(second.status, TaskStatus.SUCCEEDED)
            self.assertTrue(
                (root / "artifacts" / first.task_id / "same-request.txt").is_file()
            )
            self.assertTrue(
                (root / "artifacts" / second.task_id / "same-request.txt").is_file()
            )


if __name__ == "__main__":
    unittest.main()
