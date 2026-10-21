"""Phase 3.73: event completeness and causal-attempt audit."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.test_acceptance import FailingWriter, request  # noqa: E402
from tests.test_lifecycle_audit_coverage import _host_test_task  # noqa: E402
from zevorinth.cli import main  # noqa: E402
from zevorinth.models import TaskRequest  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.task_lifecycle import audit_task_lifecycle  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


class EventCompletenessCausalityTests(unittest.TestCase):
    def test_success_has_complete_required_event_chain(self) -> None:
        _, task, events = _host_test_task()
        audit = audit_task_lifecycle(task, events, now=NOW)
        completeness = audit["event_completeness"]
        self.assertTrue(audit["ok"], audit["issues"])
        self.assertEqual(completeness["state"], "complete")
        self.assertEqual(completeness["missing_event_types"], [])
        self.assertEqual(completeness["causality"], "verified")
        self.assertIn("task_created", completeness["required_event_types"])
        self.assertIn("task_succeeded", completeness["required_event_types"])

    def test_missing_required_event_is_confirmed_incomplete(self) -> None:
        _, task, events = _host_test_task()
        filtered = [
            event
            for event in events
            if event.event_type != "planning_completed"
        ]
        audit = audit_task_lifecycle(task, filtered, now=NOW)
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["assurance_level"], "L0")
        self.assertEqual(audit["event_completeness"]["state"], "incomplete")
        self.assertIn(
            "planning_completed",
            audit["event_completeness"]["missing_event_types"],
        )
        self.assertIn(
            "missing_required_lifecycle_event",
            {item["code"] for item in audit["issues"]},
        )

    def test_success_before_verification_is_causal_error(self) -> None:
        _, task, events = _host_test_task()
        success = next(
            event for event in events if event.event_type == "task_succeeded"
        )
        rest = [
            event for event in events if event.event_type != "task_succeeded"
        ]
        verification_index = next(
            index
            for index, event in enumerate(rest)
            if event.event_type == "verification_started"
        )
        reordered = [
            *rest[:verification_index],
            success,
            *rest[verification_index:],
        ]
        audit = audit_task_lifecycle(task, reordered, now=NOW)
        self.assertFalse(audit["ok"])
        self.assertIn(
            "task_succeeded_before_verification",
            {item["code"] for item in audit["issues"]},
        )

    def test_tool_started_attempt_mismatch_is_detected(self) -> None:
        _, task, events = _host_test_task()
        changed = [
            replace(
                event,
                payload={**event.payload, "attempt_id": "old-attempt"},
            )
            if event.event_type == "tool_started"
            else event
            for event in events
        ]
        audit = audit_task_lifecycle(task, changed, now=NOW)
        self.assertEqual(
            audit["event_completeness"]["causality"], "mismatch"
        )
        self.assertIn(
            "tool_started_attempt_mismatch",
            {item["code"] for item in audit["issues"]},
        )

    def test_old_attempt_verification_event_is_not_adopted(self) -> None:
        _, task, events = _host_test_task()
        changed = [
            replace(
                event,
                payload={**event.payload, "attempt_id": "old-attempt"},
            )
            if event.event_type
            in {"verification_started", "verification_completed"}
            else event
            for event in events
        ]
        audit = audit_task_lifecycle(task, changed, now=NOW)
        self.assertEqual(
            audit["event_completeness"]["causality"], "mismatch"
        )
        self.assertIn(
            "verification_event_attempt_mismatch",
            {item["code"] for item in audit["issues"]},
        )

    def test_unbound_attempt_is_unknown_historical_not_mismatch(self) -> None:
        _, task, events = _host_test_task()
        unbound = []
        for event in events:
            if event.event_type not in {
                "verification_started",
                "verification_completed",
            }:
                unbound.append(event)
                continue
            payload = {
                key: value
                for key, value in event.payload.items()
                if key != "attempt_id"
            }
            nested = payload.get("test_evidence")
            if isinstance(nested, dict):
                payload["test_evidence"] = {
                    key: value
                    for key, value in nested.items()
                    if key != "attempt_id"
                }
            unbound.append(replace(event, payload=payload))
        audit = audit_task_lifecycle(task, unbound, now=NOW)
        self.assertEqual(
            audit["event_completeness"]["causality"],
            "unknown_historical",
        )
        self.assertIn(
            "event_attempt_binding_unknown",
            {item["code"] for item in audit["issues"]},
        )
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["assurance_level"], "L0")

    def test_old_missing_event_is_unknown_historical(self) -> None:
        _, task, events = _host_test_task()
        historical = []
        for event in events:
            if event.event_type == "planning_completed":
                continue
            if event.event_type not in {
                "verification_started",
                "verification_completed",
            }:
                historical.append(event)
                continue
            payload = {
                key: value
                for key, value in event.payload.items()
                if key != "attempt_id"
            }
            nested = payload.get("test_evidence")
            if isinstance(nested, dict):
                payload["test_evidence"] = {
                    key: value
                    for key, value in nested.items()
                    if key != "attempt_id"
                }
            historical.append(replace(event, payload=payload))

        audit = audit_task_lifecycle(task, historical, now=NOW)

        self.assertEqual(
            audit["event_completeness"]["state"], "unknown_historical"
        )
        self.assertEqual(
            audit["event_completeness"]["causality"],
            "unknown_historical",
        )
        self.assertIn(
            "planning_completed",
            audit["event_completeness"]["missing_event_types"],
        )
        self.assertFalse(audit["ok"])

    def test_failure_and_cancellation_have_minimal_completeness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            failing = TaskService(root, writer=FailingWriter())
            failed = failing.submit(request())
            failed_audit = audit_task_lifecycle(
                failed,
                failing.events(failed.task_id),
                now=NOW,
            )
            self.assertEqual(
                failed_audit["event_completeness"]["state"],
                "minimal_complete",
            )
            self.assertEqual(
                failed_audit["event_completeness"]["causality"],
                "tool_verified",
            )

            cancellation_root = Path(temp_dir) / "cancel"
            cancel_service = TaskService(cancellation_root)
            waiting = cancel_service.submit(
                TaskRequest(
                    goal="write",
                    operation=None,
                    parameters={},
                )
            )
            cancelled = cancel_service.cancel(waiting.task_id)
            cancelled_audit = audit_task_lifecycle(
                cancelled,
                cancel_service.events(cancelled.task_id),
                now=NOW,
            )
            self.assertEqual(
                cancelled_audit["event_completeness"]["state"],
                "minimal_complete",
            )

    def test_task_audit_cli_surfaces_completeness(self) -> None:
        service, task, _ = _host_test_task()
        output = StringIO()
        with redirect_stdout(output):
            code = main(
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
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(
            report["event_completeness"]["state"], "complete"
        )
        self.assertEqual(
            report["event_completeness"]["causality"], "verified"
        )


if __name__ == "__main__":
    unittest.main()
