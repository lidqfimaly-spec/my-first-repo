from __future__ import annotations

import sys
import os
import json
import selectors
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import (  # noqa: E402
    EventType,
    TaskRequest,
    TaskStatus,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"
WORKER = PROJECT_ROOT / "tests" / "staging_cleanup_worker.py"


def _request() -> TaskRequest:
    return TaskRequest(
        goal="staging cleanup",
        operation=WRITE_TEXT_ARTIFACT,
        parameters={"artifact_path": "result.txt", "content": "result"},
    )


class StagingCleanupTests(unittest.TestCase):
    def _run_cleanup_and_kill(self, root: Path, timestamp: str) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.Popen(
            [sys.executable, str(WORKER), str(root), timestamp],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        selector = selectors.DefaultSelector()
        try:
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=10):
                process.kill()
                _, stderr = process.communicate(timeout=5)
                self.fail(f"cleanup worker did not reach checkpoint: {stderr}")
            checkpoint = json.loads(process.stdout.readline())
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(stderr, "")
            return checkpoint
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_cleanup_preserves_active_and_unknown_and_deletes_stale(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, clock=lambda: NOW)
            task = service.enqueue(_request())
            old_attempt = str(uuid4())
            current_attempt = str(uuid4())
            unknown_attempt = str(uuid4())

            first = service.store.claim_task(
                task.task_id,
                owner_id="owner-a",
                lease_token="lease-a",
                attempt_id=old_attempt,
                now=NOW,
                expires_at="2026-10-20T00:00:10.000+00:00",
                allowed_statuses=TaskService.RUNNABLE_STATUSES,
            )
            self.assertTrue(first.acquired)
            service.store.transition(
                task.task_id,
                TaskStatus.RUNNING,
                EventType.TASK_PLANNED,
                NOW,
                lease_token="lease-a",
            )
            service.store.record_event_and_update(
                task.task_id,
                EventType.TOOL_STARTED,
                NOW,
                payload={"attempt_id": old_attempt},
                lease_token="lease-a",
            )
            service.store.expire_claim(
                task.task_id,
                now="2026-10-20T00:00:11.000+00:00",
            )
            second = service.store.claim_task(
                task.task_id,
                owner_id="owner-b",
                lease_token="lease-b",
                attempt_id=current_attempt,
                now="2026-10-20T00:00:12.000+00:00",
                expires_at="2026-10-20T00:01:00.000+00:00",
                allowed_statuses=TaskService.RESUMABLE_STATUSES,
            )
            self.assertTrue(second.acquired)

            task_dir = root / "artifacts" / task.task_id
            staging = task_dir / ".staging"
            staging.mkdir(parents=True)
            old_file = staging / f"{old_attempt}.part"
            current_file = staging / f"{current_attempt}.part"
            unknown_file = staging / f"{unknown_attempt}.part"
            old_file.write_text("stale")
            current_file.write_text("active")
            unknown_file.write_text("unknown")
            final_file = task_dir / "result.txt"
            final_file.write_text("published")

            report = service.cleanup_staging(
                now="2026-10-20T00:00:20.000+00:00"
            )

            self.assertTrue(final_file.exists())
            self.assertFalse(old_file.exists())
            self.assertTrue(current_file.exists())
            self.assertTrue(unknown_file.exists())
            actions = {
                (entry.get("attempt_id"), entry["action"])
                for entry in report["entries"]
                if entry.get("attempt_id") is not None
            }
            self.assertIn((old_attempt, "deleted"), actions)
            self.assertIn((current_attempt, "retained"), actions)
            retained_reasons = {
                (entry.get("attempt_id"), entry.get("reason"))
                for entry in report["entries"]
                if entry["action"] == "retained"
            }
            self.assertIn((current_attempt, "active_attempt"), retained_reasons)
            self.assertIn((unknown_attempt, "unknown_attempt"), retained_reasons)
            events = service.events(task.task_id)
            cleaned = [
                event
                for event in events
                if event.event_type == EventType.STAGING_CLEANED.value
            ]
            self.assertEqual(len(cleaned), 1)
            self.assertEqual(cleaned[0].payload["attempt_id"], old_attempt)
            self.assertEqual(cleaned[0].payload["byte_count"], 5)

            repeated = service.cleanup_staging(
                now="2026-10-20T00:00:20.000+00:00"
            )
            self.assertEqual(repeated["deleted"], 0)
            self.assertTrue(current_file.exists())
            self.assertTrue(unknown_file.exists())
            self.assertEqual(
                len(
                    [
                        event
                        for event in service.events(task.task_id)
                        if event.event_type == EventType.STAGING_CLEANED.value
                    ]
                ),
                1,
            )

    def test_cleanup_leaves_staging_when_task_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, clock=lambda: NOW)
            task_id = str(uuid4())
            attempt_id = str(uuid4())
            staging = root / "artifacts" / task_id / ".staging"
            staging.mkdir(parents=True)
            staged = staging / f"{attempt_id}.part"
            staged.write_text("orphan")

            report = service.cleanup_staging(now=NOW)

            self.assertTrue(staged.exists())
            self.assertEqual(report["deleted"], 0)
            self.assertIn(
                "task_missing",
                {entry["reason"] for entry in report["entries"]},
            )

    def test_sigkill_after_unlink_before_event_is_safe_to_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, clock=lambda: NOW)
            task = service.enqueue(_request())
            old_attempt = str(uuid4())
            current_attempt = str(uuid4())
            first = service.store.claim_task(
                task.task_id,
                owner_id="owner-a",
                lease_token="lease-a",
                attempt_id=old_attempt,
                now=NOW,
                expires_at="2026-10-20T00:00:10.000+00:00",
                allowed_statuses=TaskService.RUNNABLE_STATUSES,
            )
            self.assertTrue(first.acquired)
            service.store.transition(
                task.task_id,
                TaskStatus.RUNNING,
                EventType.TASK_PLANNED,
                NOW,
                lease_token="lease-a",
            )
            service.store.record_event_and_update(
                task.task_id,
                EventType.TOOL_STARTED,
                NOW,
                payload={"attempt_id": old_attempt},
                lease_token="lease-a",
            )
            service.store.expire_claim(
                task.task_id,
                now="2026-10-20T00:00:11.000+00:00",
            )
            second = service.store.claim_task(
                task.task_id,
                owner_id="owner-b",
                lease_token="lease-b",
                attempt_id=current_attempt,
                now="2026-10-20T00:00:12.000+00:00",
                expires_at="2026-10-20T00:01:00.000+00:00",
                allowed_statuses=TaskService.RESUMABLE_STATUSES,
            )
            self.assertTrue(second.acquired)
            task_dir = root / "artifacts" / task.task_id
            staging = task_dir / ".staging"
            staging.mkdir(parents=True)
            old_file = staging / f"{old_attempt}.part"
            current_file = staging / f"{current_attempt}.part"
            old_file.write_text("stale")
            current_file.write_text("active")
            final_file = task_dir / "result.txt"
            final_file.write_text("published")

            checkpoint = self._run_cleanup_and_kill(
                root,
                "2026-10-20T00:00:20.000+00:00",
            )

            self.assertEqual(checkpoint["attempt_id"], old_attempt)
            self.assertFalse(old_file.exists())
            self.assertTrue(current_file.exists())
            self.assertTrue(final_file.exists())
            events = service.events(task.task_id)
            self.assertNotIn(
                EventType.STAGING_CLEANED.value,
                [event.event_type for event in events],
            )
            retry_service = TaskService(
                root,
                clock=lambda: "2026-10-20T00:00:20.000+00:00",
            )
            repeated = retry_service.cleanup_staging(
                now="2026-10-20T00:00:20.000+00:00"
            )
            self.assertEqual(repeated["deleted"], 0)
            self.assertTrue(current_file.exists())
            self.assertTrue(final_file.exists())


if __name__ == "__main__":
    unittest.main()
