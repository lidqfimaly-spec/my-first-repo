"""Phase 3.53: process-level cleanup concurrency and crash recovery."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.event_store import EventStore  # noqa: E402
from zevorinth.models import TaskRequest  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"
WORKER = PROJECT_ROOT / "tests" / "provisioning_cleanup_worker.py"


def _prepare() -> tuple[Path, TaskService, object, Path]:
    root = Path(tempfile.mkdtemp(prefix="zv-cleanup-process-"))
    workspace = root / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    target = workspace / "probe-written.txt"
    target.write_text("ok\n", encoding="utf-8")
    target_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    service = TaskService(root)
    task = service.enqueue(
        TaskRequest(
            goal="cleanup process fixture",
            operation="run_tests",
            parameters={
                "runner": "unittest",
                "target": "test_ok",
                "workspace": "demo",
            },
        )
    )
    snapshot = {
        "kind": "run_tests",
        "task_id": task.task_id,
        "attempt_id": task.attempt_id,
        "claim_generation": task.claim_generation,
        "plan_version": task.plan_version,
        "workspace": "demo",
        "provisioning_artifacts": [
            {
                "path": "probe-written.txt",
                "role": "write_probe_output",
                "ownership": "provisioning",
                "retention": "workspace_baseline",
                "cleanup": "deferred_to_controlled_cleanup",
                "bytes": 3,
                "sha256": target_sha,
            }
        ],
    }
    service.store.record_event_and_update(
        task.task_id,
        "cleanup_fixture",
        NOW,
        updates={
            "execution_plan_version": task.plan_version,
            "execution_plan_snapshot": snapshot,
        },
    )
    return root, service, service.store.require_task(task.task_id), target


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class ProcessCleanupTests(unittest.TestCase):
    def test_independent_processes_delete_at_most_once(self) -> None:
        root, service, task, target = _prepare()
        start = root / "start"
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(WORKER),
                    str(root),
                    task.task_id,
                    "race",
                    str(start),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_environment(),
            )
            for _ in range(2)
        ]
        start.write_text("go", encoding="utf-8")
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout.strip()))

        statuses = sorted(item["status"] for item in outputs)
        self.assertEqual(statuses.count("completed"), 1)
        self.assertEqual(
            statuses.count("rejected") + statuses.count("unverified"), 1
        )
        self.assertTrue(
            any(
                item.get("code")
                in {"already_cleaned", "cleanup_in_progress"}
                for item in outputs
            ),
            outputs,
        )
        self.assertFalse(target.exists())
        self.assertEqual(
            [
                event.event_type
                for event in service.events(task.task_id)
            ].count("cleanup_completed"),
            1,
        )

    def test_sigkill_checkpoint_is_reconciled_by_new_process(self) -> None:
        expectations = {
            "after_quarantine": "reconciled_pending",
            "after_unlink": "reconciled_unverified",
        }
        for stage, expected in expectations.items():
            with self.subTest(stage=stage):
                root, _service, task, target = _prepare()
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(WORKER),
                        str(root),
                        task.task_id,
                        "checkpoint",
                        stage,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=_environment(),
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

                resumed = TaskService(root).reconcile_provisioning_cleanup(
                    task.task_id, checkpoint["operation_id"]
                )
                self.assertEqual(resumed["state"], expected)
                if stage == "after_quarantine":
                    self.assertFalse(target.exists())
                    self.assertTrue(
                        (
                            target.parent
                            / ".quarantine"
                            / checkpoint["operation_id"]
                            / "probe-written.txt"
                        ).exists()
                    )
                else:
                    self.assertFalse(target.exists())

    def test_event_and_state_update_are_atomic(self) -> None:
        class FailingEvents(EventStore):
            def _insert_event(self, connection, event):
                raise RuntimeError("event insert failed")

        root = Path(tempfile.mkdtemp(prefix="zv-event-atomic-"))
        service = TaskService(root)
        task = service.enqueue(
            TaskRequest(goal="atomic", operation=None, parameters={})
        )
        before = service.store.require_task(task.task_id)
        service.store = FailingEvents(service.database_path)
        with self.assertRaises(RuntimeError):
            service.store.record_event_and_update(
                task.task_id,
                "atomic_event",
                NOW,
                updates={"phase": "should_not_persist"},
            )
        after = TaskService(root).store.require_task(task.task_id)
        self.assertEqual(after.phase, before.phase)
        self.assertNotIn(
            "atomic_event",
            [event.event_type for event in TaskService(root).events(task.task_id)],
        )


if __name__ == "__main__":
    unittest.main()
