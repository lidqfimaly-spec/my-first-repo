"""Phase 3.57: read-only lifecycle consistency rules."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import Event, Task, TaskRequest  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.task_lifecycle import audit_task_lifecycle  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"
LATER = "2026-10-23T00:00:00.000+00:00"


class TaskLifecycleAuditTests(unittest.TestCase):
    def _write_task(self) -> tuple[Path, TaskService, Task]:
        root = Path(tempfile.mkdtemp(prefix="zv-lifecycle-write-"))
        service = TaskService(root)
        task = service.submit(
            TaskRequest(
                goal="write",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "result.txt",
                    "content": "hello\n",
                },
            )
        )
        return root, service, task

    def _cleanup_task(self) -> tuple[Path, TaskService, Task, Path]:
        root = Path(tempfile.mkdtemp(prefix="zv-lifecycle-cleanup-"))
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        target = workspace / "probe-written.txt"
        target.write_text("ok\n", encoding="utf-8")
        target_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        service = TaskService(root)
        task = service.enqueue(
            TaskRequest(
                goal="cleanup",
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
                    "path": "probe-written.txt",
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

    def test_successful_write_task_is_consistent(self) -> None:
        _, service, task = self._write_task()
        report = service.audit_lifecycle(task.task_id)
        self.assertTrue(report["ok"], report["issues"])

    def test_succeeded_without_verification_is_rejected(self) -> None:
        _, service, task = self._write_task()
        data = task.to_dict()
        data["status"] = "succeeded"
        data["verification"] = None
        data["error"] = None
        tampered = Task.from_dict(data)
        report = audit_task_lifecycle(
            tampered, service.store.list_events(task.task_id)
        )
        self.assertFalse(report["ok"])
        self.assertIn(
            "succeeded_without_passed_verification",
            [item["code"] for item in report["issues"]],
        )

    def test_cleanup_completed_is_consistent(self) -> None:
        _, service, task, _ = self._cleanup_task()
        result = service.cleanup_provisioning_artifact(
            task.task_id,
            "probe-written.txt",
            enabled=True,
            authorized=True,
        )
        self.assertEqual(result["status"], "completed")
        report = service.audit_lifecycle(task.task_id)
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(
            report["cleanup_operations"][0]["status"], "completed"
        )

    def test_old_pending_cleanup_intent_is_expired(self) -> None:
        _, service, task, _ = self._cleanup_task()
        service.store.record_event_and_update(
            task.task_id,
            "cleanup_intent",
            NOW,
            payload={
                "operation_id": "stale-op",
                "task_id": task.task_id,
                "attempt_id": task.attempt_id,
                "claim_generation": task.claim_generation,
                "relative_path": "probe-written.txt",
                "expected_sha256": "a" * 64,
                "source": "provisioning",
            },
        )
        report = service.audit_lifecycle(task.task_id, now=LATER)
        state = report["cleanup_operations"][0]
        self.assertEqual(state["status"], "expired")
        self.assertFalse(state["retryable"])
        self.assertTrue(state["manual_intervention"])

    def test_awaiting_clarification_does_not_require_final_test_plan(
        self,
    ) -> None:
        root = Path(tempfile.mkdtemp(prefix="zv-lifecycle-clarification-"))
        service = TaskService(root)
        task = service.submit(
            TaskRequest(
                goal="run tests before workspace is known",
                operation="run_tests",
                parameters={
                    "runner": "unittest",
                    "target": "tests.test_demo",
                },
            )
        )
        report = service.audit_lifecycle(task.task_id)
        self.assertEqual(task.status.value, "awaiting_clarification")
        self.assertTrue(report["ok"], report["issues"])
        self.assertNotIn(
            "missing_final_execution_plan",
            [item["code"] for item in report["issues"]],
        )
        self.assertTrue(report["task_recovery"]["manual_intervention"])

        preview = service.recovery_preview(task.task_id)
        action = preview["actions"][0]
        self.assertEqual(action["action"], "none")
        self.assertEqual(action["current_state"], "awaiting_clarification")
        self.assertEqual(action["risk"], "low")
        self.assertFalse(action["requires_approval"])
        self.assertNotIn("missing_final_execution_plan", preview["unknown"])

    def test_other_pre_execution_states_do_not_require_final_plan(
        self,
    ) -> None:
        root = Path(tempfile.mkdtemp(prefix="zv-lifecycle-open-"))
        service = TaskService(root)
        queued = service.enqueue(
            TaskRequest(
                goal="queued test",
                operation="run_tests",
                parameters={
                    "runner": "unittest",
                    "target": "test_ok",
                    "workspace": "missing",
                },
            )
        )
        queued_audit = service.audit_lifecycle(queued.task_id)
        self.assertTrue(queued_audit["ok"], queued_audit["issues"])
        self.assertNotIn(
            "missing_final_execution_plan",
            [item["code"] for item in queued_audit["issues"]],
        )

        blocked = service.submit(
            TaskRequest(
                goal="blocked test",
                operation="run_tests",
                parameters={
                    "runner": "unittest",
                    "target": "test_ok",
                    "workspace": "missing",
                    "execution_backend": "host",
                    # Rejected by the workspace check before any grant
                    # is consulted; never a usable authorization.
                    "host_authorization": "0" * 32 + "." + "0" * 64,
                },
            )
        )
        blocked_audit = service.audit_lifecycle(blocked.task_id)
        self.assertEqual(blocked.status.value, "blocked")
        self.assertTrue(blocked_audit["ok"], blocked_audit["issues"])
        self.assertNotIn(
            "missing_final_execution_plan",
            [item["code"] for item in blocked_audit["issues"]],
        )
        self.assertTrue(
            blocked_audit["task_recovery"]["manual_intervention"]
        )

    def test_executed_run_tests_without_final_plan_is_flagged(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="zv-lifecycle-history-"))
        service = TaskService(root)
        task = service.enqueue(
            TaskRequest(
                goal="historical",
                operation="run_tests",
                parameters={
                    "runner": "unittest",
                    "target": "test_ok",
                    "workspace": "demo",
                },
            )
        )
        events = [
            *service.store.list_events(task.task_id),
            Event.new(
                task.task_id,
                "execution_plan_finalized",
                NOW,
                payload={"final_execution_plan": None},
            ),
        ]
        data = task.to_dict()
        data["status"] = "verifying"
        data["phase"] = "verification_started"
        verifying = Task.from_dict(data)
        report = audit_task_lifecycle(verifying, events, now=NOW)
        self.assertFalse(report["ok"])
        self.assertIn(
            "missing_final_execution_plan",
            [item["code"] for item in report["issues"]],
        )


if __name__ == "__main__":
    unittest.main()
