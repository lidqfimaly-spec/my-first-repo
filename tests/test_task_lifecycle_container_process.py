"""Phase 3.60: real container recovery after pre-verification crash."""

from __future__ import annotations

import json
import os
import selectors
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.test_container_timeout_cancel import (  # noqa: E402
    IMAGE,
    _bind_evidence,
    _probe_evidence,
    _request,
)
from zevorinth.evidence_store import EvidenceStore  # noqa: E402
from zevorinth.models import TaskStatus  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


WORKER = PROJECT_ROOT / "tests" / "task_lifecycle_container_worker.py"
NOW = "2026-10-20T00:00:00.000+00:00"
LATER = "2026-10-20T00:02:00.000+00:00"


class ContainerTaskLifecycleProcessTests(unittest.TestCase):
    def test_crash_after_container_test_resumes_without_rerun(self) -> None:
        if shutil.which("docker") is None:
            self.skipTest("docker is not available")
        root = Path(tempfile.mkdtemp(prefix="zv-container-recovery-"))
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / "test_ok.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        service = TaskService(root, clock=lambda: NOW)
        task = service.enqueue(_request("test_ok", timeout_seconds=60))
        evidence = _probe_evidence(
            root, workspace, task.task_id, IMAGE
        )
        _bind_evidence(evidence, workspace, "test_ok", task.task_id)
        EvidenceStore(root).save_evidence(evidence)
        task.request.parameters["evidence_id"] = evidence["evidence_id"]
        service.store.record_event_and_update(
            task.task_id,
            "test_fixture",
            NOW,
            updates={"request": task.request},
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
            if not selector.select(timeout=20):
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
        self.assertEqual(
            [event.event_type for event in events].count("tool_succeeded"),
            1,
        )
        self.assertIn("task_succeeded", [event.event_type for event in events])


if __name__ == "__main__":
    unittest.main()
