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
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.event_store import EventStore  # noqa: E402
from zevorinth.models import ExecutionMode, TaskStatus, ToolResult  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


class NoWriteWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self) -> None:
        self.calls = 0

    def write(self, **_: Any) -> ToolResult:
        self.calls += 1
        raise AssertionError("SIGKILL recovery must not rewrite the artifact")


class SigkillRecoveryTests(unittest.TestCase):
    def _run_and_kill(
        self, root: Path
    ) -> tuple[dict[str, Any], int, str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        process = subprocess.Popen(
            [
                sys.executable,
                str(PROJECT_ROOT / "tests" / "sigkill_worker.py"),
                str(root),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        selector = selectors.DefaultSelector()
        try:
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=10):
                process.kill()
                _, stderr = process.communicate(timeout=5)
                self.fail(f"worker did not reach post-write checkpoint: {stderr}")
            line = process.stdout.readline()
            if not line:
                _, stderr = process.communicate(timeout=5)
                self.fail(f"worker exited before checkpoint: {stderr}")
            checkpoint = json.loads(line)

            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            _, stderr = process.communicate(timeout=5)
            return checkpoint, process.returncode, stderr
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def _recovery_time(self, checkpoint: dict[str, Any]) -> str:
        from datetime import datetime, timedelta

        lease_expires_at = datetime.fromisoformat(checkpoint["lease_expires_at"])
        return (lease_expires_at + timedelta(seconds=1)).isoformat(
            timespec="milliseconds"
        )

    def test_real_sigkill_after_write_recovers_by_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint, _, stderr = self._run_and_kill(root)
            self.assertEqual(stderr, "")
            task_id = checkpoint["task_id"]
            artifact = root / "artifacts" / task_id / "sigkill-proof.txt"
            self.assertTrue(artifact.is_file())

            raw_store = EventStore(root / "data" / "zevorinth.sqlite3")
            before_recovery = raw_store.require_task(task_id)
            self.assertEqual(before_recovery.status, TaskStatus.VERIFYING)
            self.assertIsNotNone(before_recovery.lease_token)
            self.assertIsNotNone(before_recovery.tool_result)
            self.assertIsNotNone(before_recovery.artifact)

            no_write = NoWriteWriter()
            recovery_time = self._recovery_time(checkpoint)
            restarted = TaskService(
                root,
                writer=no_write,
                clock=lambda: recovery_time,
            )
            self.assertEqual(restarted.require_task(task_id).status, TaskStatus.INTERRUPTED)
            resumed = restarted.resume(task_id)

            self.assertEqual(resumed.status, TaskStatus.SUCCEEDED)
            self.assertEqual(resumed.execution_mode, ExecutionMode.REAL)
            self.assertEqual(no_write.calls, 0)
            event_types = [
                event.event_type for event in restarted.events(task_id)
            ]
            self.assertIn("lease_expired", event_types)
            self.assertIn("task_interrupted", event_types)
            self.assertIn("task_claimed", event_types)
            self.assertEqual(event_types.count("task_succeeded"), 1)
            self.assertEqual(
                artifact.read_text(),
                "survives SIGKILL before verification\n",
            )

    def test_real_sigkill_with_modified_file_fails_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint, _, _ = self._run_and_kill(root)
            task_id = checkpoint["task_id"]
            artifact = root / "artifacts" / task_id / "sigkill-proof.txt"
            artifact.write_text("tampered after SIGKILL")

            no_write = NoWriteWriter()
            restarted = TaskService(
                root,
                writer=no_write,
                clock=lambda: self._recovery_time(checkpoint),
            )
            failed = restarted.resume(task_id)

            self.assertEqual(failed.status, TaskStatus.FAILED)
            self.assertEqual(failed.error.code, "verification_failed")
            self.assertEqual(failed.verification.reason, "artifact_content_mismatch")
            self.assertEqual(no_write.calls, 0)
            self.assertEqual(artifact.read_text(), "tampered after SIGKILL")


if __name__ == "__main__":
    unittest.main()
