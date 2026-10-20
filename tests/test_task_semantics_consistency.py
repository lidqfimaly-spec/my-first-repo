"""Phase 3.72: task status, events, tool and verification semantics."""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.test_lifecycle_audit_coverage import _host_test_task  # noqa: E402
from zevorinth.cli import main  # noqa: E402
from zevorinth.recovery_preview import build_recovery_preview  # noqa: E402
from zevorinth.task_lifecycle import audit_task_lifecycle  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


class TaskSemanticsConsistencyTests(unittest.TestCase):
    def _filtered_events(self, event_type: str):
        service, task, events = _host_test_task()
        filtered = [
            event for event in events if event.event_type != event_type
        ]
        return service, task, filtered

    def test_normal_event_chain_is_consistent(self) -> None:
        _, task, events = _host_test_task()
        audit = audit_task_lifecycle(task, events, now=NOW)
        self.assertTrue(audit["ok"], audit["issues"])
        self.assertEqual(audit["assurance_level"], "L1")
        self.assertIn("tool_succeeded", audit["event_types"])
        self.assertIn("verification_completed", audit["event_types"])

    def test_missing_tool_terminal_event_forces_manual_preview(self) -> None:
        _, task, events = self._filtered_events("tool_succeeded")
        audit = audit_task_lifecycle(task, events, now=NOW)
        codes = {item["code"] for item in audit["issues"]}
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["assurance_level"], "L0")
        self.assertIn("tool_result_without_matching_event", codes)
        preview = build_recovery_preview(
            task, events, audit=audit, now=NOW
        ).to_dict()
        self.assertEqual(preview["actions"][0]["action"], "manual_review")
        self.assertEqual(
            preview["actions"][0]["current_state"],
            "lifecycle_inconsistent",
        )
        self.assertEqual(preview["assurance_level"], "L0")
        self.assertIn(
            "tool_result_without_matching_event", preview["unknown"]
        )
        self.assertFalse(preview["execution_authorized"])

    def test_missing_verification_event_forces_manual_review(self) -> None:
        _, task, events = self._filtered_events("verification_completed")
        audit = audit_task_lifecycle(task, events, now=NOW)
        codes = {item["code"] for item in audit["issues"]}
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["assurance_level"], "L0")
        self.assertIn("verification_without_matching_event", codes)

    def test_tool_and_verification_field_mismatches_are_detected(self) -> None:
        _, task, events = _host_test_task()
        failed_tool = replace(task.tool_result, success=False)
        mismatch_task = replace(task, tool_result=failed_tool)
        tool_audit = audit_task_lifecycle(
            mismatch_task, events, now=NOW
        )
        tool_codes = {item["code"] for item in tool_audit["issues"]}
        self.assertIn(
            "tool_result_event_success_mismatch", tool_codes
        )
        self.assertEqual(tool_audit["assurance_level"], "L0")

        changed_events = []
        for event in events:
            if event.event_type == "verification_completed":
                verification = dict(event.payload["verification"])
                verification["passed"] = False
                event = replace(
                    event,
                    payload={
                        **event.payload,
                        "verification": verification,
                    },
                )
            changed_events.append(event)
        verification_audit = audit_task_lifecycle(
            task, changed_events, now=NOW
        )
        verification_codes = {
            item["code"] for item in verification_audit["issues"]
        }
        self.assertIn(
            "verification_without_matching_event", verification_codes
        )
        self.assertEqual(
            verification_audit["assurance_level"], "L0"
        )

    def test_duplicate_tool_and_verification_events_are_detected(self) -> None:
        _, task, events = _host_test_task()
        duplicated = list(events)
        for event in events:
            if event.event_type in {
                "tool_succeeded",
                "verification_completed",
            }:
                duplicated.append(event)
        audit = audit_task_lifecycle(task, duplicated, now=NOW)
        codes = {item["code"] for item in audit["issues"]}
        self.assertIn("duplicate_tool_terminal_event", codes)
        self.assertIn(
            "duplicate_verification_completed_event", codes
        )

    def test_status_and_task_audit_fail_closed_on_event_semantic_break(
        self,
    ) -> None:
        service, task, _ = _host_test_task()
        with sqlite3.connect(service.database_path) as connection:
            connection.execute(
                "DELETE FROM events WHERE task_id=? AND event_type=?",
                (task.task_id, "tool_succeeded"),
            )
            connection.commit()
        before_task = service.store.require_task(task.task_id).to_dict()
        before_events = [
            event.to_dict()
            for event in service.store.list_events(task.task_id)
        ]
        lifecycle = service.audit_lifecycle(task.task_id, now=NOW)
        preview = service.recovery_preview(task.task_id, now=NOW)
        self.assertFalse(lifecycle["ok"])
        self.assertFalse(preview["audit"]["ok"])
        self.assertEqual(
            preview["audit"]["issues"], lifecycle["issues"]
        )
        self.assertEqual(
            preview["assurance_level"], lifecycle["assurance_level"]
        )
        self.assertEqual(preview["actions"][0]["action"], "manual_review")

        status_output = StringIO()
        with redirect_stdout(status_output):
            status_code = main(
                [
                    "--home",
                    str(service.project_root),
                    "status",
                    task.task_id,
                    "--json",
                ]
            )
        self.assertEqual(status_code, 5)
        status_payload = json.loads(status_output.getvalue())
        self.assertEqual(status_payload["status"], "succeeded")
        self.assertFalse(status_payload["lifecycle_audit"]["ok"])
        self.assertEqual(
            status_payload["lifecycle_audit"]["assurance_level"], "L0"
        )

        audit_output = StringIO()
        with redirect_stdout(audit_output):
            audit_code = main(
                [
                    "--home",
                    str(service.project_root),
                    "task-audit",
                    task.task_id,
                    "--checked-at",
                    NOW,
                    "--json",
                ]
            )
        self.assertEqual(audit_code, 5)
        audit = json.loads(audit_output.getvalue())
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["assurance_level"], "L0")
        self.assertFalse(audit["security_certification"])
        self.assertEqual(
            service.store.require_task(task.task_id).to_dict(),
            before_task,
        )
        self.assertEqual(
            [
                event.to_dict()
                for event in service.store.list_events(task.task_id)
            ],
            before_events,
        )

    def test_human_status_does_not_hide_unverified_lifecycle(self) -> None:
        service, task, _ = _host_test_task()
        with sqlite3.connect(service.database_path) as connection:
            connection.execute(
                "DELETE FROM events WHERE task_id=? AND event_type=?",
                (task.task_id, "verification_completed"),
            )
            connection.commit()
        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "--home",
                    str(service.project_root),
                    "status",
                    task.task_id,
                ]
            )
        self.assertEqual(code, 5)
        self.assertIn("Lifecycle audit: unverified", output.getvalue())
        self.assertIn("authenticity=not_authenticated", output.getvalue())
        self.assertIn("security_certification=False", output.getvalue())


if __name__ == "__main__":
    unittest.main()
