from __future__ import annotations

import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.state_machine import InvalidTransition  # noqa: E402


class EventStoreTests(unittest.TestCase):
    def test_state_and_ordered_events_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            task = service.submit(
                TaskRequest(
                    goal="persist",
                    operation=WRITE_TEXT_ARTIFACT,
                    parameters={"artifact_path": "event.txt", "content": "event"},
                )
            )
            reopened = TaskService(root)

            self.assertEqual(reopened.require_task(task.task_id).status, TaskStatus.SUCCEEDED)
            sequences = [event.sequence for event in reopened.events(task.task_id)]
            self.assertEqual(sequences, sorted(sequences))
            self.assertEqual(len(sequences), len(set(sequences)))

            with sqlite3.connect(root / "data" / "zevorinth.sqlite3") as connection:
                task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            self.assertEqual(task_count, 1)
            self.assertGreater(event_count, 0)
            db_mode = stat.S_IMODE(
                (root / "data" / "zevorinth.sqlite3").stat().st_mode
            )
            data_mode = stat.S_IMODE((root / "data").stat().st_mode)
            self.assertEqual(db_mode, 0o600)
            self.assertEqual(data_mode, 0o700)

    def test_invalid_transition_does_not_append_an_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            task = service.submit(
                TaskRequest(
                    goal="terminal",
                    operation=WRITE_TEXT_ARTIFACT,
                    parameters={"artifact_path": "terminal.txt", "content": "done"},
                )
            )
            before = len(service.events(task.task_id))

            with self.assertRaises(InvalidTransition):
                service.store.transition(
                    task.task_id,
                    TaskStatus.RUNNING,
                    "illegal_transition",
                    "now",
                )

            self.assertEqual(len(service.events(task.task_id)), before)
            self.assertEqual(
                service.require_task(task.task_id).status,
                TaskStatus.SUCCEEDED,
            )


if __name__ == "__main__":
    unittest.main()
