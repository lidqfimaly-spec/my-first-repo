"""Phase 3.50: controlled provisioning file cleanup lifecycle."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
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
        status_before = task.status
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
        self.assertEqual(
            service.store.require_task(task.task_id).status,
            status_before,
        )

    def test_concurrent_requests_delete_at_most_once(self) -> None:
        service, task, _, target = self._task()
        barrier = threading.Barrier(2)
        results: list[dict] = []

        def worker() -> None:
            barrier.wait(timeout=5)
            results.append(
                service.cleanup_provisioning_artifact(
                    task.task_id,
                    "probe-written.txt",
                    enabled=True,
                    authorized=True,
                )
            )

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        statuses = sorted(item["status"] for item in results)
        self.assertEqual(statuses.count("completed"), 1)
        self.assertEqual(statuses.count("rejected"), 1)
        self.assertIn(
            "already_cleaned",
            [item.get("code") for item in results],
        )
        self.assertFalse(target.exists())
        self.assertEqual(
            [
                event.event_type
                for event in service.events(task.task_id)
            ].count("cleanup_completed"),
            1,
        )

    def test_content_change_before_verification_is_rejected(self) -> None:
        service, task, _, target = self._task()

        def checkpoint(stage: str) -> None:
            if stage == "opened":
                target.write_text("changed\n", encoding="utf-8")

        record = service.cleanup_provisioning_artifact(
            task.task_id,
            "probe-written.txt",
            enabled=True,
            authorized=True,
            checkpoint=checkpoint,
        )
        self.assertEqual(record["code"], "hash_mismatch")
        self.assertTrue(target.exists())

    def test_symlink_or_regular_replacement_is_rejected(self) -> None:
        for replacement in ("symlink", "regular"):
            with self.subTest(replacement=replacement):
                service, task, _, target = self._task()
                outside = target.parent / "outside.txt"
                outside.write_text("outside\n", encoding="utf-8")

                def checkpoint(stage: str) -> None:
                    if stage != "before_quarantine":
                        return
                    target.unlink()
                    if replacement == "symlink":
                        os.symlink(outside, target)
                    else:
                        replacement_file = target.parent / "replacement.txt"
                        replacement_file.write_text(
                            "ok\n", encoding="utf-8"
                        )
                        os.replace(replacement_file, target)

                record = service.cleanup_provisioning_artifact(
                    task.task_id,
                    "probe-written.txt",
                    enabled=True,
                    authorized=True,
                    checkpoint=checkpoint,
                )
                self.assertIn(
                    record["code"], {"target_replaced", "unsafe_path"}
                )
                self.assertTrue(target.exists() or target.is_symlink())

    def test_interruption_after_quarantine_is_reconcilable(self) -> None:
        service, task, _, target = self._task()

        def checkpoint(stage: str) -> None:
            if stage == "after_quarantine":
                raise RuntimeError("crash after quarantine")

        with self.assertRaises(RuntimeError):
            service.cleanup_provisioning_artifact(
                task.task_id,
                "probe-written.txt",
                enabled=True,
                authorized=True,
                checkpoint=checkpoint,
            )
        intent = next(
            event
            for event in service.events(task.task_id)
            if event.event_type == "cleanup_intent"
        )
        resumed = service.reconcile_provisioning_cleanup(
            task.task_id, intent.payload["operation_id"]
        )
        self.assertEqual(resumed["state"], "reconciled_pending")
        self.assertFalse(target.exists())

    def test_interruption_after_unlink_is_unverified(self) -> None:
        service, task, _, target = self._task()

        def checkpoint(stage: str) -> None:
            if stage == "after_unlink":
                raise RuntimeError("crash after unlink")

        with self.assertRaises(RuntimeError):
            service.cleanup_provisioning_artifact(
                task.task_id,
                "probe-written.txt",
                enabled=True,
                authorized=True,
                checkpoint=checkpoint,
            )
        intent = next(
            event
            for event in service.events(task.task_id)
            if event.event_type == "cleanup_intent"
        )
        resumed = service.reconcile_provisioning_cleanup(
            task.task_id, intent.payload["operation_id"]
        )
        self.assertEqual(resumed["state"], "reconciled_unverified")
        self.assertFalse(target.exists())

    def test_completion_event_failure_is_not_reported_completed(self) -> None:
        service, task, _, target = self._task()

        def fail_event(_task, record: dict) -> None:
            record["evidence_persisted"] = False

        with patch.object(service, "_record_cleanup_event", side_effect=fail_event):
            record = service.cleanup_provisioning_artifact(
                task.task_id,
                "probe-written.txt",
                enabled=True,
                authorized=True,
            )
        self.assertEqual(record["status"], "unverified")
        self.assertEqual(record["code"], "completion_event_not_persisted")
        self.assertTrue(record["removed"])
        self.assertFalse(target.exists())

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
        status_before = task.status
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
        self.assertEqual(
            service.store.require_task(task.task_id).status,
            status_before,
        )

    def test_isolation_failure_is_recorded_without_success(self) -> None:
        service, task, _, target = self._task()
        status_before = task.status
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
        self.assertEqual(
            service.store.require_task(task.task_id).status,
            status_before,
        )
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
