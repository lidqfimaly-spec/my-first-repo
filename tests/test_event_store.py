from __future__ import annotations

import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.event_store import ClaimLost, StoreBusyError  # noqa: E402
from zevorinth.event_store import EventStore, EventStoreError  # noqa: E402
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.state_machine import InvalidTransition  # noqa: E402


class EventStoreTests(unittest.TestCase):
    def _request(self) -> TaskRequest:
        return TaskRequest(
            goal="claim test",
            operation=WRITE_TEXT_ARTIFACT,
            parameters={"artifact_path": "claim.txt", "content": "claim"},
        )

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

    def test_sqlite_claim_is_exclusive_and_fenced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            t0 = "2026-10-19T00:00:00.000+00:00"
            t1 = "2099-01-01T00:00:00.000+00:00"
            service = TaskService(root, clock=lambda: t0, lease_seconds=30)
            task = service.enqueue(self._request())
            allowed = TaskService.RUNNABLE_STATUSES

            first = service.store.claim_task(
                task.task_id,
                owner_id="owner-a",
                lease_token="lease-a",
                attempt_id="attempt-a",
                now=t0,
                expires_at=t1,
                allowed_statuses=allowed,
            )
            second = service.store.claim_task(
                task.task_id,
                owner_id="owner-b",
                lease_token="lease-b",
                attempt_id="attempt-b",
                now=t0,
                expires_at=t1,
                allowed_statuses=allowed,
            )

            self.assertTrue(first.acquired)
            self.assertFalse(second.acquired)
            self.assertEqual(second.reason, "active_lease")
            self.assertEqual(second.task.execution_owner, "owner-a")
            self.assertEqual(second.task.claim_generation, 1)
            event_types = [event.event_type for event in service.events(task.task_id)]
            self.assertEqual(event_types.count("task_claimed"), 1)
            self.assertEqual(event_types.count("claim_rejected"), 1)

            after_expiry = "2099-01-01T00:00:01.000+00:00"
            third = service.store.claim_task(
                task.task_id,
                owner_id="owner-b",
                lease_token="lease-b",
                attempt_id="attempt-b",
                now=after_expiry,
                expires_at="2099-01-01T00:01:00.000+00:00",
                allowed_statuses=allowed,
            )
            self.assertTrue(third.acquired)
            self.assertEqual(third.task.claim_generation, 2)
            event_types_after_expiry = [
                event.event_type for event in service.events(task.task_id)
            ]
            self.assertIn("lease_expired", event_types_after_expiry)

            with self.assertRaises(ClaimLost):
                service.store.transition(
                    task.task_id,
                    TaskStatus.RUNNING,
                    "stale_transition",
                    after_expiry,
                    lease_token="lease-a",
                )
            self.assertEqual(
                service.require_task(task.task_id).status,
                TaskStatus.QUEUED,
            )

    def test_new_service_does_not_interrupt_an_active_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            t0 = "2026-10-19T00:00:00.000+00:00"
            expires = "2026-10-19T00:01:00.000+00:00"
            owner = TaskService(root, clock=lambda: t0)
            task = owner.enqueue(self._request())
            claim = owner.store.claim_task(
                task.task_id,
                owner_id="active-owner",
                lease_token="active-lease",
                attempt_id="active-attempt",
                now=t0,
                expires_at=expires,
                allowed_statuses=TaskService.RUNNABLE_STATUSES,
            )
            self.assertTrue(claim.acquired)

            observer = TaskService(root, clock=lambda: t0)
            observed = observer.require_task(task.task_id)
            self.assertEqual(observed.status, TaskStatus.QUEUED)
            self.assertEqual(observed.execution_owner, "active-owner")
            self.assertEqual(observed.lease_token, "active-lease")

    def test_sqlite_busy_timeout_raises_typed_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, busy_timeout_ms=50)
            database = root / "data" / "zevorinth.sqlite3"
            blocker = sqlite3.connect(database, timeout=0.05)
            blocker.execute("PRAGMA journal_mode = WAL")
            blocker.execute("BEGIN IMMEDIATE")
            try:
                with self.assertRaises(StoreBusyError):
                    service.enqueue(self._request())
            finally:
                blocker.rollback()
                blocker.close()

    def test_event_insert_failure_rolls_back_state_update(self) -> None:
        class FailingEventStore(EventStore):
            def _insert_event(self, connection, event):
                raise sqlite3.OperationalError("injected event insert failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            task = service.enqueue(self._request())
            before_events = len(service.events(task.task_id))
            failing = FailingEventStore(root / "data" / "zevorinth.sqlite3")

            with self.assertRaises(EventStoreError):
                failing.transition(
                    task.task_id,
                    TaskStatus.RUNNING,
                    "task_planned",
                    "2026-10-19T00:00:00.000+00:00",
                )

            self.assertEqual(
                service.require_task(task.task_id).status,
                TaskStatus.QUEUED,
            )
            self.assertEqual(len(service.events(task.task_id)), before_events)


if __name__ == "__main__":
    unittest.main()
