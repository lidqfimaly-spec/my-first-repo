"""Phase 3.60: tool success without persisted evidence is unknown."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.event_store import EventStore  # noqa: E402
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"
LATER = "2026-10-20T00:02:00.000+00:00"


class ToolSuccessEventFailureStore(EventStore):
    def _insert_event(self, connection, event):
        if event.event_type == "tool_succeeded":
            raise RuntimeError("tool_succeeded event write failed")
        return super()._insert_event(connection, event)


class TaskPersistenceFaultTests(unittest.TestCase):
    def test_tool_success_event_failure_leaves_unknown_state(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="zv-persistence-fault-"))
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / "test_ok.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        service = TaskService(root, clock=lambda: NOW)
        task = service.enqueue(
            TaskRequest(
                goal="run test_ok",
                operation="run_tests",
                parameters={
                    "runner": "unittest",
                    "target": "test_ok",
                    "workspace": "demo",
                    "execution_backend": "host",
                    "host_authorization": "local-maintenance",
                },
            )
        )
        service.store = ToolSuccessEventFailureStore(service.database_path)
        with self.assertRaises(RuntimeError):
            service.execute_task(task.task_id)

        interrupted = TaskService(root, clock=lambda: NOW).store.require_task(
            task.task_id
        )
        self.assertNotEqual(interrupted.status, TaskStatus.SUCCEEDED)
        self.assertIsNone(interrupted.verification)

        recovery = TaskService(root, clock=lambda: LATER)
        recovery.store.expire_claim(task.task_id, now=LATER)
        resumed = recovery.resume(task.task_id)
        self.assertEqual(resumed.status, TaskStatus.BLOCKED)
        self.assertEqual(resumed.error.code, "test_execution_unknown")
        events = recovery.events(task.task_id)
        self.assertEqual(
            [event.event_type for event in events].count("tool_started"), 1
        )
        self.assertNotIn(
            "task_succeeded",
            [event.event_type for event in events],
        )


if __name__ == "__main__":
    unittest.main()
