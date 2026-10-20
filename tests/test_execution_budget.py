from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import RealArtifactWriter  # noqa: E402
from zevorinth.event_store import LeaseRenewalError  # noqa: E402
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import (  # noqa: E402
    WRITE_TEXT_ARTIFACT,
    DeterministicPlanner,
)
from zevorinth.service import TaskService  # noqa: E402
import zevorinth.service as service_module  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


class BudgetWaitingPlanner:
    mode = DeterministicPlanner.mode
    name = "budget-waiting-planner"

    def plan_request(self, request: TaskRequest):
        renewer = service_module._ACTIVE_RENEWER.get()
        if renewer is None or not renewer.failed_event.wait(timeout=5):
            raise AssertionError("execution budget did not expire")
        return DeterministicPlanner().plan_request(request)


class CountingWriter:
    tool_name = "write_text_artifact"

    def __init__(self, artifacts_root: Path) -> None:
        self.delegate = RealArtifactWriter(artifacts_root)
        self.execution_mode = self.delegate.execution_mode
        self.calls = 0

    def write(self, **kwargs: Any):
        self.calls += 1
        return self.delegate.write(**kwargs)


def _request() -> TaskRequest:
    return TaskRequest(
        goal="execution budget",
        operation=WRITE_TEXT_ARTIFACT,
        parameters={
            "artifact_path": "budget.txt",
            "content": "budgeted\n",
        },
    )


class ExecutionBudgetTests(unittest.TestCase):
    def test_claim_persists_deadline_and_renewal_does_not_reset_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(
                Path(temp_dir),
                clock=lambda: NOW,
                lease_seconds=5,
                max_execution_seconds=10,
            )
            task = service.enqueue(_request())
            claim = service._claim(
                task.task_id,
                allowed=TaskService.RUNNABLE_STATUSES,
            )
            self.assertTrue(claim.acquired)
            original_deadline = claim.task.execution_deadline_at
            self.assertEqual(
                original_deadline,
                "2026-10-20T00:00:10.000+00:00",
            )
            service.store.renew_lease(
                task.task_id,
                lease_token=str(claim.task.lease_token),
                claim_generation=claim.task.claim_generation,
                now=NOW,
                expires_at="2026-10-20T00:00:30.000+00:00",
            )
            renewed = service.require_task(task.task_id)
            self.assertEqual(
                renewed.execution_deadline_at,
                original_deadline,
            )

    def test_restart_preserves_original_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            clock = [NOW]
            service = TaskService(
                root,
                clock=lambda: clock[0],
                lease_seconds=1,
                max_execution_seconds=10,
            )
            task = service.enqueue(_request())
            claim = service._claim(
                task.task_id,
                allowed=TaskService.RUNNABLE_STATUSES,
            )
            self.assertTrue(claim.acquired)
            deadline = claim.task.execution_deadline_at
            service.store.transition(
                task.task_id,
                TaskStatus.RUNNING,
                "task_planned",
                NOW,
                lease_token=str(claim.task.lease_token),
            )
            clock[0] = "2026-10-20T00:00:02.000+00:00"
            service.store.expire_claim(task.task_id, now=clock[0])

            writer = CountingWriter(root / "artifacts")
            restarted = TaskService(
                root,
                writer=writer,
                clock=lambda: clock[0],
                lease_seconds=1,
                max_execution_seconds=100,
            )
            result = restarted.execute_task(task.task_id)

            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertEqual(result.execution_deadline_at, deadline)
            self.assertEqual(writer.calls, 1)

    def test_budget_timeout_before_planner_completion_starts_no_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(
                root,
                clock=lambda: NOW,
                lease_seconds=1,
                lease_renewal_interval_seconds=0.02,
                max_execution_seconds=0.1,
            )
            service.planner = BudgetWaitingPlanner()
            task = service.enqueue(_request())

            with self.assertRaises(LeaseRenewalError) as raised:
                service.execute_task(task.task_id)

            self.assertEqual(
                raised.exception.code,
                "lease_renewal_limit_exceeded",
            )
            event_types = [
                event.event_type for event in service.events(task.task_id)
            ]
            self.assertNotIn("tool_started", event_types)
            self.assertFalse(
                (root / "artifacts" / task.task_id / "budget.txt").exists()
            )

    def test_expired_budget_fails_claim_without_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            clock = [NOW]
            service = TaskService(
                root,
                clock=lambda: clock[0],
                lease_seconds=1,
                max_execution_seconds=2,
            )
            task = service.enqueue(_request())
            claim = service._claim(
                task.task_id,
                allowed=TaskService.RUNNABLE_STATUSES,
            )
            self.assertTrue(claim.acquired)
            service.store.transition(
                task.task_id,
                TaskStatus.RUNNING,
                "task_planned",
                NOW,
                lease_token=str(claim.task.lease_token),
            )
            clock[0] = "2026-10-20T00:00:03.000+00:00"
            service.store.expire_claim(task.task_id, now=clock[0])
            writer = CountingWriter(root / "artifacts")
            restarted = TaskService(
                root,
                writer=writer,
                clock=lambda: clock[0],
                max_execution_seconds=100,
            )

            result = restarted.execute_task(task.task_id)

            self.assertEqual(result.status, TaskStatus.FAILED)
            self.assertEqual(
                result.error.code,
                "execution_budget_exceeded",
            )
            self.assertEqual(writer.calls, 0)
            event_types = [
                event.event_type
                for event in restarted.events(task.task_id)
            ]
            self.assertNotIn("tool_started", event_types)
            self.assertIn("task_failed", event_types)


if __name__ == "__main__":
    unittest.main()
