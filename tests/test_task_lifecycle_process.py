"""Phase 3.57: process crash after test before verification."""

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

from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


WORKER = PROJECT_ROOT / "tests" / "task_lifecycle_worker.py"
NOW = "2026-10-20T00:00:00.000+00:00"
LATER = "2026-10-20T00:02:00.000+00:00"


class TaskLifecycleProcessTests(unittest.TestCase):
    def test_crash_after_test_resumes_without_rerun(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="zv-task-lifecycle-"))
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
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.Popen(
            [sys.executable, str(WORKER), str(root), task.task_id, NOW],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        selector = selectors.DefaultSelector()
        try:
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=15):
                process.kill()
                _, stderr = process.communicate(timeout=5)
                self.fail(f"worker did not reach checkpoint: {stderr}")
            checkpoint = json.loads(process.stdout.readline())
            self.assertEqual(
                checkpoint["stage"], "after_test_before_verification"
            )
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            process.communicate(timeout=5)
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

        recovery = TaskService(root, clock=lambda: LATER)
        recovery.store.expire_claim(task.task_id, now=LATER)
        resumed = recovery.resume(task.task_id)

        self.assertEqual(resumed.status, TaskStatus.SUCCEEDED)
        self.assertTrue(resumed.verification.passed)
        events = recovery.events(task.task_id)
        self.assertEqual(
            [event.event_type for event in events].count("tool_started"),
            1,
        )
        self.assertIn("task_succeeded", [event.event_type for event in events])


if __name__ == "__main__":
    unittest.main()
