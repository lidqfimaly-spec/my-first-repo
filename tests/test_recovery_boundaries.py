"""Phase 3.61: result persistence boundaries and recovery fencing."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.host_grant_support import authorized_service  # noqa: E402
from zevorinth.event_store import ClaimLost, EventStore  # noqa: E402
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"
LATER = "2026-10-20T00:02:00.000+00:00"
PAUSE_WORKER = PROJECT_ROOT / "tests" / "task_lifecycle_worker.py"
RESUME_WORKER = PROJECT_ROOT / "tests" / "recovery_resume_worker.py"


def _host_task() -> tuple[Path, TaskService, object]:
    root = Path(tempfile.mkdtemp(prefix="zv-recovery-boundary-"))
    workspace = root / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    (workspace / "test_ok.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    service, _, token = authorized_service(
        root, workspace="demo", target="test_ok", clock=lambda: NOW
    )
    task = service.enqueue(
        TaskRequest(
            goal="run test_ok",
            operation="run_tests",
            parameters={
                "runner": "unittest",
                "target": "test_ok",
                "workspace": "demo",
                "execution_backend": "host",
                "host_authorization": token,
            },
        )
    )
    return root, service, task


class FailingEventStore(EventStore):
    def __init__(self, path, event_type: str) -> None:
        super().__init__(path)
        self.event_type = event_type

    def _insert_event(self, connection, event):
        if event.event_type == self.event_type:
            raise RuntimeError(f"injected {self.event_type} failure")
        return super()._insert_event(connection, event)


class PersistenceBoundaryTests(unittest.TestCase):
    def _fail_and_resume(self, event_type: str):
        root, service, task = _host_task()
        service.store = FailingEventStore(service.database_path, event_type)
        with self.assertRaises(RuntimeError):
            service.execute_task(task.task_id)
        recovery = TaskService(root, clock=lambda: LATER)
        recovery.store.expire_claim(task.task_id, now=LATER)
        return task.task_id, recovery, recovery.resume(task.task_id)

    def test_tool_started_failure_is_unknown(self) -> None:
        task_id, recovery, resumed = self._fail_and_resume("tool_started")
        self.assertEqual(resumed.status, TaskStatus.BLOCKED)
        self.assertEqual(resumed.error.code, "test_execution_unknown")
        events = [event.event_type for event in recovery.events(task_id)]
        self.assertNotIn("task_succeeded", events)
        self.assertNotIn("tool_succeeded", events)

    def test_verification_started_failure_resumes_without_rerun(self) -> None:
        task_id, recovery, resumed = self._fail_and_resume(
            "verification_started"
        )
        self.assertEqual(resumed.status, TaskStatus.SUCCEEDED)
        events = [event.event_type for event in recovery.events(task_id)]
        self.assertEqual(events.count("tool_started"), 1)
        self.assertEqual(events.count("task_succeeded"), 1)

    def test_terminal_event_failure_resumes_idempotently(self) -> None:
        task_id, recovery, resumed = self._fail_and_resume("task_succeeded")
        self.assertEqual(resumed.status, TaskStatus.SUCCEEDED)
        events = [event.event_type for event in recovery.events(task_id)]
        self.assertEqual(events.count("tool_started"), 1)
        self.assertEqual(events.count("task_succeeded"), 1)

    def test_late_worker_lease_cannot_write(self) -> None:
        root, service, task = _host_task()
        first = service.store.claim_task(
            task.task_id,
            owner_id="worker-a",
            lease_token="lease-a",
            attempt_id="attempt-a",
            now=NOW,
            expires_at="2026-10-20T00:00:10.000+00:00",
            allowed_statuses=TaskService.RUNNABLE_STATUSES,
        )
        self.assertTrue(first.acquired)
        service.store.expire_claim(
            task.task_id, now="2026-10-20T00:02:00.000+00:00"
        )
        second = service.store.claim_task(
            task.task_id,
            owner_id="worker-b",
            lease_token="lease-b",
            attempt_id="attempt-b",
            now="2026-10-20T00:02:01.000+00:00",
            expires_at="2026-10-20T00:03:00.000+00:00",
            allowed_statuses=TaskService.RESUMABLE_STATUSES,
        )
        self.assertTrue(second.acquired)
        with self.assertRaises(ClaimLost):
            service.store.record_event_and_update(
                task.task_id,
                "stale_event",
                "2026-10-20T00:02:02.000+00:00",
                updates={"phase": "stale_worker"},
                lease_token="lease-a",
            )
        current = service.store.require_task(task.task_id)
        self.assertEqual(current.phase, second.task.phase)
        self.assertNotIn(
            "stale_event",
            [event.event_type for event in service.events(task.task_id)],
        )

    def test_repeated_resume_after_success_is_idempotent(self) -> None:
        _, service, task = _host_task()
        first = service.execute_task(task.task_id)
        self.assertEqual(first.status, TaskStatus.SUCCEEDED)
        second = service.resume(task.task_id)
        third = service.resume(task.task_id)
        self.assertEqual(second.status, TaskStatus.SUCCEEDED)
        self.assertEqual(third.status, TaskStatus.SUCCEEDED)
        events = [event.event_type for event in service.events(task.task_id)]
        self.assertEqual(events.count("tool_started"), 1)
        self.assertEqual(events.count("task_succeeded"), 1)


class ConcurrentResumeTests(unittest.TestCase):
    def test_two_processes_resume_without_duplicate_execution(self) -> None:
        root, service, task = _host_task()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        worker = subprocess.Popen(
            [
                sys.executable,
                str(PAUSE_WORKER),
                str(root),
                task.task_id,
                NOW,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        selector = selectors.DefaultSelector()
        try:
            assert worker.stdout is not None
            selector.register(worker.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=15):
                worker.kill()
                _, stderr = worker.communicate(timeout=5)
                self.fail(f"worker did not reach checkpoint: {stderr}")
            json.loads(worker.stdout.readline())
            os.kill(worker.pid, signal.SIGKILL)
            worker.wait(timeout=10)
            worker.communicate(timeout=5)
        finally:
            selector.close()
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
            if worker.stdout is not None:
                worker.stdout.close()
            if worker.stderr is not None:
                worker.stderr.close()

        TaskService(root, clock=lambda: LATER).store.expire_claim(
            task.task_id, now=LATER
        )
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(RESUME_WORKER),
                    str(root),
                    task.task_id,
                    LATER,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(2)
        ]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout.strip()))

        self.assertEqual(
            service.store.require_task(task.task_id).status,
            TaskStatus.SUCCEEDED,
        )
        events = [event.event_type for event in service.events(task.task_id)]
        self.assertEqual(events.count("tool_started"), 1)
        self.assertEqual(events.count("task_succeeded"), 1)
        self.assertIn(
            "succeeded", {item["status"] for item in outputs}
        )


if __name__ == "__main__":
    unittest.main()
