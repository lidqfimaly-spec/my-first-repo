"""R-08: approval must bind to step *content*, not to a step id.

Before this change, ``approved_step_ids`` held bare identifiers and
``replan_pending_steps`` did not clear them. A step redefined under the same id
inherited the old approval, so the thing executed was not the thing approved.

Honest scope note: today's public API cannot reach "approved, then content
rewritten under the same id" — approval executes the step immediately, and a
completed step cannot be replanned. So the discriminating test below builds the
record state directly, because that is exactly the state an older persisted
record (or a future code path) can hold, and the gate has to fail closed on it
rather than assume it never happens.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import (  # noqa: E402
    ExecutionMode,
    Plan,
    PlanStep,
    Task,
    TaskRequest,
    TaskStatus,
)
from zevorinth.multi_step import (  # noqa: E402
    step_approval_digest,
    step_is_approved,
)
from zevorinth.service import TaskService  # noqa: E402


def write_step(
    step_id: str,
    artifact_path: str,
    content: object,
    *,
    requires_approval: bool = True,
) -> dict[str, object]:
    return {
        "step_id": step_id,
        "tool_name": "write_text_artifact",
        "expected_result": f"create {artifact_path}",
        "parameters": {"artifact_path": artifact_path, "content": content},
        "depends_on": [],
        "requires_approval": requires_approval,
        "max_attempts": 1,
    }


PLAN_ID = "plan-fixture"


def _plan(*steps: PlanStep) -> Plan:
    return Plan(
        plan_id=PLAN_ID,
        planner_name="deterministic",
        deterministic=True,
        steps=list(steps),
    )


def _step(step_id: str, content: str) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        tool_name="write_text_artifact",
        expected_result=f"create {step_id}.txt",
        parameters={"artifact_path": f"{step_id}.txt", "content": content},
        requires_approval=True,
    )


class ApprovalBindingTests(unittest.TestCase):
    def _task_with_approval(self, step: PlanStep, digest: str | None) -> Task:
        return Task(
            task_id="t1",
            status=TaskStatus.RUNNING,
            request=TaskRequest(goal="g", operation="execute_plan"),
            execution_mode=ExecutionMode.REAL,
            attempt_id="a1",
            phase="running",
            planner_mode="deterministic",
            created_at="2026-11-22T00:00:00+00:00",
            updated_at="2026-11-22T00:00:00+00:00",
            plan=_plan(step),
            approved_step_ids=[step.step_id],
            approved_step_digests=(
                {step.step_id: digest} if digest is not None else {}
            ),
        )

    def test_identical_content_is_still_approved(self) -> None:
        step = _step("s1", "hello")
        task = self._task_with_approval(step, step_approval_digest(step, PLAN_ID))
        self.assertTrue(step_is_approved(step, task))

    def test_changing_a_parameter_under_the_same_id_revokes_approval(self) -> None:
        approved = _step("s1", "hello")
        changed = _step("s1", "something else")
        task = self._task_with_approval(
            approved, step_approval_digest(approved, PLAN_ID)
        )
        self.assertFalse(step_is_approved(changed, task))

    def test_changing_the_tool_under_the_same_id_revokes_approval(self) -> None:
        approved = _step("s1", "hello")
        changed = PlanStep(
            step_id="s1",
            tool_name="read_text_artifact",
            expected_result="create s1.txt",
            parameters={"artifact_path": "s1.txt"},
            requires_approval=True,
        )
        task = self._task_with_approval(
            approved, step_approval_digest(approved, PLAN_ID)
        )
        self.assertFalse(step_is_approved(changed, task))

    def test_a_legacy_approval_without_a_digest_is_not_trusted(self) -> None:
        # Records written before this fix have ids but no digests. They must
        # re-ask rather than inherit a blank cheque.
        step = _step("s1", "hello")
        task = self._task_with_approval(step, None)
        self.assertIn("s1", task.approved_step_ids)
        self.assertFalse(step_is_approved(step, task))

    def test_the_digest_is_stable_and_content_sensitive(self) -> None:
        step = _step("s1", "hello")
        self.assertEqual(
            step_approval_digest(step, PLAN_ID), step_approval_digest(_step("s1", "hello"), PLAN_ID)
        )
        self.assertNotEqual(
            step_approval_digest(step, PLAN_ID), step_approval_digest(_step("s1", "x"), PLAN_ID)
        )
        self.assertEqual(len(step_approval_digest(step, PLAN_ID)), 64)


class ApprovalPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.service = TaskService(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_approval_records_the_digest_it_approved(self) -> None:
        task = self.service.submit_plan(
            "two approvals",
            [write_step("s1", "a.txt", "a"), write_step("s2", "b.txt", "b")],
        )
        self.assertIs(task.status, TaskStatus.AWAITING_APPROVAL)
        approved = self.service.approve_step(
            task.task_id, "s1", approved=True
        )
        self.assertIn("s1", approved.approved_step_digests)
        plan_step = next(
            step for step in approved.plan.steps if step.step_id == "s1"
        )
        self.assertEqual(
            approved.approved_step_digests["s1"],
            step_approval_digest(plan_step, approved.plan.plan_id),
        )
        events = [
            event
            for event in self.service.store.list_events(task.task_id)
            if event.event_type == "step_approved"
        ]
        self.assertTrue(events)
        self.assertEqual(
            events[-1].payload.get("step_digest"),
            approved.approved_step_digests["s1"],
        )

    def test_replan_keeps_unchanged_approvals_and_drops_the_rest(self) -> None:
        task = self.service.submit_plan(
            "two approvals",
            [write_step("s1", "a.txt", "a"), write_step("s2", "b.txt", "b")],
        )
        running = self.service.approve_step(
            task.task_id, "s1", approved=True
        )
        self.assertIs(running.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(running.pending_approval_step_id, "s2")

        replanned = self.service.replan_pending_steps(
            task.task_id,
            [write_step("s2", "b2.txt", "b2")],
            expected_plan_version=running.plan_version,
        )
        # s1's approved content is untouched, so its approval survives.
        self.assertEqual(replanned.approved_step_ids, ["s1"])
        self.assertEqual(list(replanned.approved_step_digests), ["s1"])

        resumed = self.service.resume(task.task_id)
        # The redefined s2 has no approval, so the task stops and asks again.
        self.assertIs(resumed.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(resumed.pending_approval_step_id, "s2")

    def test_a_failed_approved_step_keeps_its_digest_for_the_record(self) -> None:
        # Evidence value: after a failure we can still say which content the
        # human approved, even though the step did not succeed.
        task = self.service.submit_plan(
            "approve then fail",
            [
                {
                    "step_id": "s1",
                    "tool_name": "read_text_artifact",
                    "expected_result": "read nope.txt",
                    "parameters": {"artifact_path": "nope.txt"},
                    "depends_on": [],
                    "requires_approval": True,
                    "max_attempts": 1,
                }
            ],
        )
        failed = self.service.approve_step(task.task_id, "s1", approved=True)
        self.assertIs(failed.status, TaskStatus.FAILED)
        self.assertEqual(
            [result.status for result in failed.step_results], ["failed"]
        )
        self.assertIn("s1", failed.approved_step_digests)


if __name__ == "__main__":
    unittest.main()
