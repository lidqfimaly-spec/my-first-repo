"""Phase 3.63: read-only recovery preview and approval binding."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.test_lifecycle_audit_coverage import _host_test_task  # noqa: E402
from zevorinth.models import TaskRequest  # noqa: E402
from zevorinth.recovery_preview import (  # noqa: E402
    approve_recovery_preview,
    build_recovery_preview,
    validate_recovery_approval,
)
from zevorinth.service import TaskService  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"
LATER = "2026-10-20T00:05:00.000+00:00"


class RecoveryPreviewTests(unittest.TestCase):
    def test_success_preview_is_read_only_and_non_executable(self) -> None:
        service, task, events = _host_test_task()
        workspace = service.project_root / "workspaces" / "demo"
        target = workspace / "probe-written.txt"
        target.write_text("keep\n", encoding="utf-8")
        before_task = task.to_dict()
        before_events = [event.to_dict() for event in events]
        before_file = target.read_bytes()

        preview = service.recovery_preview(task.task_id, now=NOW)

        self.assertEqual(preview["preview_version"], "recovery-preview-v1")
        self.assertFalse(preview["execution_authorized"])
        self.assertEqual(preview["actions"][0]["action"], "none")
        self.assertFalse(preview["actions"][0]["executable"])
        self.assertEqual(task.to_dict(), before_task)
        self.assertEqual(
            [event.to_dict() for event in service.store.list_events(task.task_id)],
            before_events,
        )
        self.assertEqual(target.read_bytes(), before_file)

    def test_interrupted_task_preview_requires_manual_review(self) -> None:
        service, task, events = _host_test_task()
        data = task.to_dict()
        data["status"] = "interrupted"
        data["phase"] = "lease_expired"
        interrupted = type(task).from_dict(data)
        preview = build_recovery_preview(
            interrupted, events, now=NOW
        )
        action = preview.to_dict()["actions"][0]
        self.assertEqual(action["action"], "manual_review")
        self.assertFalse(action["executable"])
        self.assertTrue(action["requires_approval"])
        self.assertFalse(preview.to_dict()["execution_authorized"])

    def test_cleanup_pending_preview_has_manual_reconcile_action(self) -> None:
        service, task, _ = _host_test_task()
        service.store.record_event_and_update(
            task.task_id,
            "cleanup_intent",
            NOW,
            payload={
                "operation_id": "preview-cleanup",
                "relative_path": "probe-written.txt",
                "source": "provisioning",
                "expected_sha256": "a" * 64,
            },
        )
        preview = service.recovery_preview(task.task_id, now=NOW)
        actions = [item["action"] for item in preview["actions"]]
        self.assertIn("manual_reconcile_cleanup", actions)
        cleanup = next(
            item
            for item in preview["actions"]
            if item["action"] == "manual_reconcile_cleanup"
        )
        self.assertFalse(cleanup["executable"])
        self.assertEqual(cleanup["current_state"], "pending")

    def test_approval_binds_preview_and_becomes_stale(self) -> None:
        service, task, events = _host_test_task()
        preview = build_recovery_preview(task, events, now=NOW)
        action = preview.actions[0]
        approval = approve_recovery_preview(
            preview,
            approver="operator",
            action=action.action,
            target=action.target,
            now=NOW,
        )
        current = build_recovery_preview(
            task,
            service.store.list_events(task.task_id),
            now=LATER,
        )
        self.assertTrue(
            validate_recovery_approval(approval, current)["valid"]
        )
        self.assertFalse(approval.execution_authorized)

        service.store.record_event_and_update(
            task.task_id,
            "state_changed",
            LATER,
            payload={"reason": "authorized test change"},
            updates={"phase": "changed_after_approval"},
        )
        changed = service.recovery_preview(task.task_id, now=LATER)
        changed_obj = build_recovery_preview(
            service.store.require_task(task.task_id),
            service.store.list_events(task.task_id),
            audit=changed["audit"],
            now=LATER,
        )
        verdict = validate_recovery_approval(approval, changed_obj)
        self.assertFalse(verdict["valid"])
        self.assertEqual(verdict["state"], "stale")
        self.assertFalse(verdict["execution_authorized"])

    def test_unreadable_storage_preview_is_manual_only(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="zv-preview-corrupt-"))
        service = TaskService(root)
        task = service.enqueue(
            TaskRequest(goal="write", operation=None, parameters={})
        )
        with sqlite3.connect(service.database_path) as connection:
            connection.execute(
                "UPDATE tasks SET task_json=? WHERE task_id=?",
                ("not-json", task.task_id),
            )
            connection.commit()
        preview = service.recovery_preview(task.task_id, now=NOW)
        self.assertEqual(preview["actions"][0]["action"], "manual_review")
        self.assertFalse(preview["execution_authorized"])
        self.assertIn("storage_unreadable", preview["unknown"])


if __name__ == "__main__":
    unittest.main()
