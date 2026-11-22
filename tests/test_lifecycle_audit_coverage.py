"""Phase 3.58: lifecycle audit coverage and recovery decisions."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.host_grant_support import authorized_service  # noqa: E402
from zevorinth.models import Event, TaskRequest  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.task_lifecycle import audit_task_lifecycle  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


def _host_test_task() -> tuple[TaskService, object, list[Event]]:
    root = Path(tempfile.mkdtemp(prefix="zv-audit-"))
    workspace = root / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    (workspace / "test_ok.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    service, _, token = authorized_service(
        root, workspace="demo", target="test_ok"
    )
    task = service.submit(
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
    return service, task, service.store.list_events(task.task_id)


class LifecycleAuditCoverageTests(unittest.TestCase):
    def test_success_is_consistent_and_not_retryable(self) -> None:
        service, task, _ = _host_test_task()
        report = service.audit_lifecycle(task.task_id)
        self.assertTrue(report["ok"], report["issues"])
        self.assertFalse(report["task_recovery"]["safe_to_retry"])
        self.assertEqual(report["task_recovery"]["recommendation"], "none")
        self.assertEqual(report["auditor_version"], "lifecycle-audit-v2")
        self.assertFalse(report["security_certification"])

    def test_audit_is_read_only(self) -> None:
        service, task, events = _host_test_task()
        workspace = service.project_root / "workspaces" / "demo"
        probe = workspace / "probe-written.txt"
        probe.write_text("keep\n", encoding="utf-8")
        before_task = task.to_dict()
        before_events = [event.to_dict() for event in events]
        before_probe = probe.read_bytes()
        service.audit_lifecycle(task.task_id, now=NOW)
        self.assertEqual(task.to_dict(), before_task)
        self.assertEqual(
            [event.to_dict() for event in service.store.list_events(task.task_id)],
            before_events,
        )
        self.assertEqual(probe.read_bytes(), before_probe)

    def test_missing_terminal_event_is_detected(self) -> None:
        service, task, events = _host_test_task()
        filtered = [
            event for event in events if event.event_type != "task_succeeded"
        ]
        report = audit_task_lifecycle(task, filtered)
        self.assertFalse(report["ok"])
        self.assertIn(
            "succeeded_without_task_event",
            [item["code"] for item in report["issues"]],
        )

    def test_duplicate_terminal_event_is_detected(self) -> None:
        service, task, events = _host_test_task()
        duplicate = Event.new(task.task_id, "task_succeeded", NOW)
        report = audit_task_lifecycle(task, [*events, duplicate])
        self.assertFalse(report["ok"])
        self.assertIn(
            "duplicate_terminal_event",
            [item["code"] for item in report["issues"]],
        )

    def test_orphan_cleanup_terminal_is_detected(self) -> None:
        service, task, events = _host_test_task()
        orphan = Event.new(
            task.task_id,
            "cleanup_completed",
            NOW,
            {"operation_id": "orphan", "relative_path": "probe-written.txt"},
        )
        report = audit_task_lifecycle(task, [*events, orphan])
        self.assertFalse(report["ok"])
        self.assertIn(
            "cleanup_terminal_without_intent",
            [item["code"] for item in report["issues"]],
        )

    def test_invalid_cleanup_timestamp_is_detected(self) -> None:
        service, task, events = _host_test_task()
        invalid = Event.new(
            task.task_id,
            "cleanup_intent",
            "not-a-time",
            {
                "operation_id": "bad-time",
                "relative_path": "probe-written.txt",
                "source": "provisioning",
            },
        )
        report = audit_task_lifecycle(task, [*events, invalid])
        self.assertFalse(report["ok"])
        self.assertIn(
            "invalid_cleanup_timestamp",
            [item["code"] for item in report["issues"]],
        )
        self.assertTrue(report["cleanup_operations"][0]["manual_intervention"])

    def test_succeeded_run_tests_with_invalid_evidence_is_detected(self) -> None:
        service, task, events = _host_test_task()
        report = audit_task_lifecycle(
            task,
            events,
            evidence_state={"state": "invalidated", "reasons": ["test"]},
        )
        self.assertFalse(report["ok"])
        self.assertIn(
            "succeeded_with_unverified_evidence",
            [item["code"] for item in report["issues"]],
        )

    def test_old_attempt_plan_identity_is_detected(self) -> None:
        service, task, events = _host_test_task()
        snapshot = dict(task.execution_plan_snapshot)
        snapshot["attempt_id"] = "old-attempt"
        data = task.to_dict()
        data["execution_plan_snapshot"] = snapshot
        changed = type(task).from_dict(data)
        report = audit_task_lifecycle(changed, events)
        self.assertFalse(report["ok"])
        self.assertIn(
            "execution_plan_attempt_id_mismatch",
            [item["code"] for item in report["issues"]],
        )

    def test_interrupted_task_requires_manual_review(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="zv-audit-queued-"))
        service = TaskService(root)
        task = service.enqueue(
            TaskRequest(
                goal="write",
                operation="write_text_artifact",
                parameters={"artifact_path": "a.txt", "content": "x"},
            )
        )
        data = task.to_dict()
        data["status"] = "interrupted"
        data["phase"] = "lease_expired"
        interrupted = type(task).from_dict(data)
        report = audit_task_lifecycle(
            interrupted, service.store.list_events(task.task_id)
        )
        self.assertTrue(report["task_recovery"]["manual_intervention"])
        self.assertFalse(report["task_recovery"]["safe_to_retry"])
        self.assertEqual(
            report["task_recovery"]["recommendation"], "manual_review"
        )

    def test_blocked_unknown_execution_requires_manual_review(self) -> None:
        service, task, events = _host_test_task()
        data = task.to_dict()
        data["status"] = "blocked"
        data["phase"] = "blocked_test_unknown"
        data["error"] = {
            "code": "test_execution_unknown",
            "message": "unknown execution state",
        }
        blocked = type(task).from_dict(data)
        filtered = [
            event for event in events if event.event_type != "task_succeeded"
        ]
        report = audit_task_lifecycle(blocked, filtered)
        self.assertTrue(report["task_recovery"]["manual_intervention"])
        self.assertFalse(report["task_recovery"]["safe_to_retry"])
        self.assertEqual(
            report["task_recovery"]["recommendation"], "manual_review"
        )

    def test_evidence_read_failure_is_reported_without_audit_crash(self) -> None:
        service, task, _ = _host_test_task()
        evidence = replace(
            task.tool_result.test_evidence,
            evidence_id="auto-test",
            config_digest="a" * 64,
            command_digest="b" * 64,
        )
        tool_result = replace(task.tool_result, test_evidence=evidence)
        snapshot = dict(task.execution_plan_snapshot)
        snapshot.update(
            {
                "evidence_id": "auto-test",
                "config_digest": "a" * 64,
                "command_digest": "b" * 64,
            }
        )
        service.store.record_event_and_update(
            task.task_id,
            "evidence_fixture",
            NOW,
            updates={
                "tool_result": tool_result,
                "execution_plan_version": task.plan_version,
                "execution_plan_snapshot": snapshot,
            },
        )
        with patch(
            "zevorinth.evidence_store.EvidenceStore.verify_stored_evidence",
            side_effect=OSError("unreadable"),
        ):
            report = service.audit_lifecycle(task.task_id)
        self.assertEqual(report["evidence_state"]["state"], "unverified")
        self.assertIn(
            "succeeded_with_unverified_evidence",
            [item["code"] for item in report["issues"]],
        )


if __name__ == "__main__":
    unittest.main()
