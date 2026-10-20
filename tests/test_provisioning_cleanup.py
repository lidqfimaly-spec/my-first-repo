"""Phase 3.50: controlled provisioning file cleanup lifecycle."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import TaskRequest  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


class ProvisioningCleanupTests(unittest.TestCase):
    def _task(self, *, path: str = "probe-written.txt", ownership: str = "provisioning"):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        target = workspace / path
        target.write_text("ok\n", encoding="utf-8")
        content_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        service = TaskService(root)
        task = service.enqueue(
            TaskRequest(
                goal="cleanup fixture",
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
            "evidence_id": "auto-fixture",
            "config_digest": "a" * 64,
            "command_digest": "b" * 64,
            "provisioning_artifacts": [
                {
                    "path": path,
                    "role": "write_probe_output",
                    "ownership": ownership,
                    "retention": "workspace_baseline",
                    "cleanup": "deferred_to_controlled_cleanup",
                    "bytes": 3,
                    "sha256": content_sha,
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
        return service, service.store.require_task(task.task_id), workspace, target

    def test_cleanup_is_disabled_and_unauthorized_by_default(self) -> None:
        service, task, _, target = self._task()
        record = service.cleanup_provisioning_artifact(
            task.task_id, "probe-written.txt"
        )
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["code"], "cleanup_disabled")
        self.assertTrue(target.exists())

        record = service.cleanup_provisioning_artifact(
            task.task_id, "probe-written.txt", enabled=True
        )
        self.assertEqual(record["code"], "cleanup_unauthorized")
        self.assertTrue(target.exists())

    def test_completed_cleanup_is_idempotent(self) -> None:
        service, task, _, target = self._task()
        first = service.cleanup_provisioning_artifact(
            task.task_id,
            "probe-written.txt",
            enabled=True,
            authorized=True,
        )
        self.assertEqual(first["status"], "completed")
        self.assertTrue(first["removed"])
        self.assertFalse(target.exists())
        events = service.events(task.task_id)
        self.assertEqual(
            [e.event_type for e in events].count("cleanup_completed"), 1
        )
        second = service.cleanup_provisioning_artifact(
            task.task_id,
            "probe-written.txt",
            enabled=True,
            authorized=True,
        )
        self.assertEqual(second["code"], "already_cleaned")
        self.assertFalse(second["removed"])

    def test_modified_or_missing_target_is_rejected(self) -> None:
        service, task, _, target = self._task()
        target.write_text("changed\n", encoding="utf-8")
        modified = service.cleanup_provisioning_artifact(
            task.task_id,
            "probe-written.txt",
            enabled=True,
            authorized=True,
        )
        self.assertEqual(modified["code"], "target_modified")
        self.assertTrue(target.exists())

        target.unlink()
        missing = service.cleanup_provisioning_artifact(
            task.task_id,
            "probe-written.txt",
            enabled=True,
            authorized=True,
        )
        self.assertEqual(missing["code"], "missing_target")

    def test_ambiguous_inside_ownership_is_rejected(self) -> None:
        service, task, _, target = self._task(
            path="inside.txt",
            ownership="workspace_input_or_provisioning",
        )
        record = service.cleanup_provisioning_artifact(
            task.task_id,
            "inside.txt",
            enabled=True,
            authorized=True,
        )
        self.assertEqual(record["code"], "not_owned")
        self.assertTrue(target.exists())

    def test_intent_write_failure_blocks_quarantine(self) -> None:
        service, task, _, target = self._task()
        with patch.object(
            service, "_record_cleanup_intent", return_value=False
        ), patch.object(service, "_intent_exists", return_value=False):
            record = service.cleanup_provisioning_artifact(
                task.task_id,
                "probe-written.txt",
                enabled=True,
                authorized=True,
            )
        self.assertEqual(record["status"], "unverified")
        self.assertEqual(record["code"], "intent_not_persisted")
        self.assertTrue(target.exists())

    def test_isolation_failure_is_recorded_without_success(self) -> None:
        service, task, _, target = self._task()
        failed = {
            "status": "failed",
            "code": "quarantine_failed",
            "reason": "injected",
            "removed": False,
        }
        with patch(
            "zevorinth.service.execute_cleanup", return_value=failed
        ):
            record = service.cleanup_provisioning_artifact(
                task.task_id,
                "probe-written.txt",
                enabled=True,
                authorized=True,
            )
        self.assertEqual(record["status"], "failed")
        self.assertFalse(record["removed"])
        self.assertTrue(target.exists())
        events = service.events(task.task_id)
        self.assertIn("cleanup_rejected", [e.event_type for e in events])

    def test_historical_task_without_final_plan_is_rejected(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        service = TaskService(Path(temp_dir.name))
        task = service.enqueue(
            TaskRequest(goal="historical", operation="run_tests", parameters={})
        )
        record = service.cleanup_provisioning_artifact(
            task.task_id,
            "probe-written.txt",
            enabled=True,
            authorized=True,
        )
        self.assertEqual(record["code"], "no_final_execution_plan")

    def test_reconcile_completed_operation(self) -> None:
        service, task, _, _ = self._task()
        result = service.cleanup_provisioning_artifact(
            task.task_id,
            "probe-written.txt",
            enabled=True,
            authorized=True,
        )
        reconciled = service.reconcile_provisioning_cleanup(
            task.task_id, result["operation_id"]
        )
        self.assertEqual(reconciled["state"], "reconciled_completed")


if __name__ == "__main__":
    unittest.main()
