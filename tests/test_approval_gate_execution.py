"""R-08 at the execution gate, not just at the predicate (phase B).

``test_approval_binding.py`` proves the *predicate* revokes approval when the
content changes. That is necessary and not sufficient: the requirement is that
the **execution path** refuses before any side effect. These tests drive the
real service through ``resume`` into ``_execute_multi_step_plan`` and observe:

* the task goes back to waiting for approval,
* the event says why (``approval_required`` / ``approval_content_changed``),
* **no child task ran and no file was written** for the denied step.

Reachability, stated plainly and not dressed up:

* "approved, then the same step id is redefined" is **not reachable** through
  the product. ``approve_step`` executes the step immediately, and a completed
  step cannot be replanned. The window only exists if a persisted record was
  written by an older build, or if the process died between the approval
  transition and the execution (the crash-recovery path).
* Because that state is real for persisted data even when the UI cannot mint
  it, the fixture below writes it deliberately: it sets ``approved_step_ids``
  plus a digest for *different* content, with the task in a genuinely resumable
  state (PAUSED, no lease). Everything after that point runs through the real
  gate.
* What these tests therefore verify: **the gate re-computes and compares at
  execution time, and refuses before the tool runs.**
* What they do not verify: that a human was shown the old content. That is the
  approval-form tests' job, and no automated test can prove it.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import (  # noqa: E402
    EventType,
    PlanStep,
    TaskRequest,
    TaskStatus,
)
from zevorinth.multi_step import (  # noqa: E402
    step_approval_digest,
    step_is_approved,
)
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.workbench import ReadOnlyWorkbench  # noqa: E402


def write_step(
    step_id: str,
    artifact_path: str,
    content: str,
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


class _GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.service = TaskService(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _waiting(self):
        task = self.service.submit_plan(
            "two steps, second needs approval",
            [
                write_step("s1", "s1.txt", "one\n", requires_approval=False),
                write_step("s2", "s2.txt", "two\n"),
            ],
        )
        self.assertIs(task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(task.pending_approval_step_id, "s2")
        return task

    def _task_ids(self) -> set[str]:
        listing = ReadOnlyWorkbench(self.root).list_tasks(limit=100)
        return {item["task_id"] for item in listing["tasks"]}

    def _s2_artifact_exists(self, task_id: str) -> bool:
        return (self.root / "artifacts" / task_id / "s2.txt").exists()

    def _approval_reasons(self, task_id: str) -> list[str]:
        return [
            event.payload.get("reason")
            for event in self.service.store.list_events(task_id)
            if event.event_type == EventType.STEP_APPROVAL_REQUIRED.value
        ]

    def _force_approval_record(
        self,
        task_id: str,
        step_id: str,
        digest: str,
    ):
        """Write a persisted approval record, as a legacy or crashed run would."""
        task = self.service.store.require_task(task_id)
        return self.service.store.record_event_and_update(
            task_id,
            EventType.TASK_NOTE_ADDED,
            self.service.clock(),
            payload={"message": "fixture: persisted approval record"},
            updates={
                "approved_step_ids": [step_id],
                "approved_step_digests": {step_id: digest},
                "phase": "fixture_approved",
            },
            require_no_lease=True,
            expected_updated_at=task.updated_at,
            expected_plan_version=task.plan_version,
            expected_status=TaskStatus.PAUSED,
        )


class GateAllowsTheApprovedContentTests(_GateCase):
    def test_a_real_approval_still_executes(self) -> None:
        task = self._waiting()
        done = self.service.approve_step(task.task_id, "s2", approved=True)
        self.assertIs(done.status, TaskStatus.SUCCEEDED)
        self.assertTrue(self._s2_artifact_exists(task.task_id))

    def test_resume_with_a_matching_record_executes(self) -> None:
        """Positive control: the gate is not a blanket refusal."""
        task = self._waiting()
        self.service.request_pause(
            task.task_id, requested_by="test", reason="fixture"
        )
        step = next(
            item for item in task.plan.steps if item.step_id == "s2"
        )
        self._force_approval_record(
            task.task_id,
            "s2",
            step_approval_digest(step, task.plan.plan_id),
        )
        resumed = self.service.resume(task.task_id)
        self.assertIs(resumed.status, TaskStatus.SUCCEEDED)
        self.assertTrue(self._s2_artifact_exists(task.task_id))


class GateRefusesChangedContentTests(_GateCase):
    def test_a_record_for_different_content_is_refused_before_side_effect(
        self,
    ) -> None:
        task = self._waiting()
        self.service.request_pause(
            task.task_id, requested_by="test", reason="fixture"
        )
        # A record that says "s2 was approved" but for content that is not the
        # content in the plan. This is the R-08 shape.
        other = PlanStep(
            step_id="s2",
            tool_name="write_text_artifact",
            expected_result="create s2.txt",
            parameters={"artifact_path": "s2.txt", "content": "OTHER\n"},
            requires_approval=True,
        )
        before_tasks = self._task_ids()
        self._force_approval_record(
            task.task_id,
            "s2",
            step_approval_digest(other, task.plan.plan_id),
        )
        resumed = self.service.resume(task.task_id)

        self.assertIs(resumed.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(resumed.pending_approval_step_id, "s2")
        self.assertEqual(
            self._approval_reasons(task.task_id)[-1],
            "approval_content_changed",
        )
        # The proof that matters: nothing ran.
        self.assertFalse(self._s2_artifact_exists(task.task_id))
        self.assertNotIn(
            "s2", [result.step_id for result in resumed.step_results]
        )
        self.assertEqual(self._task_ids(), before_tasks)

    def test_a_record_from_another_plan_is_refused(self) -> None:
        task = self._waiting()
        self.service.request_pause(
            task.task_id, requested_by="test", reason="fixture"
        )
        step = next(
            item for item in task.plan.steps if item.step_id == "s2"
        )
        before_tasks = self._task_ids()
        self._force_approval_record(
            task.task_id,
            "s2",
            step_approval_digest(step, "some-other-plan"),
        )
        resumed = self.service.resume(task.task_id)
        self.assertIs(resumed.status, TaskStatus.AWAITING_APPROVAL)
        self.assertFalse(self._s2_artifact_exists(task.task_id))
        self.assertEqual(self._task_ids(), before_tasks)

    def test_replanning_the_pending_step_drops_its_approval(self) -> None:
        """Reachable path: replan a pending step, then resume."""
        task = self._waiting()
        self.service.request_pause(
            task.task_id, requested_by="test", reason="change my mind"
        )
        replanned = self.service.replan_pending_steps(
            task.task_id,
            [write_step("s2", "s2.txt", "two, but different\n")],
            expected_plan_version=task.plan_version,
        )
        self.assertIs(replanned.status, TaskStatus.PAUSED)
        self.assertEqual(replanned.approved_step_ids, [])

        resumed = self.service.resume(task.task_id)
        self.assertIs(resumed.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(
            self._approval_reasons(task.task_id)[-1], "approval_required"
        )
        self.assertFalse(self._s2_artifact_exists(task.task_id))


class PredicateFailClosedTests(_GateCase):
    def test_a_step_with_undumpable_parameters_never_inherits_approval(
        self,
    ) -> None:
        step = PlanStep(
            step_id="s1",
            tool_name="write_text_artifact",
            expected_result="x",
            parameters={"artifact_path": "a.txt", "content": object()},
            requires_approval=True,
        )
        task = self.service.enqueue(
            TaskRequest(goal="g", operation="execute_plan")
        )
        self.assertIsNone(task.plan)
        self.assertFalse(step_is_approved(step, task))

    def test_a_missing_plan_never_inherits_approval(self) -> None:
        queued = self.service.enqueue(
            TaskRequest(goal="g", operation="execute_plan")
        )
        step = PlanStep(
            step_id="s1",
            tool_name="write_text_artifact",
            expected_result="x",
            parameters={"artifact_path": "a.txt", "content": "x"},
            requires_approval=True,
        )
        # An id recorded without a plan to bind it to is not an approval.
        task = replace(queued, approved_step_ids=["s1"])
        self.assertFalse(step_is_approved(step, task))

    def test_the_digest_changes_when_plan_identity_changes(self) -> None:
        step = PlanStep(
            step_id="s1",
            tool_name="write_text_artifact",
            expected_result="x",
            parameters={"artifact_path": "a.txt", "content": "x"},
            requires_approval=True,
        )
        self.assertNotEqual(
            step_approval_digest(step, "plan-a"),
            step_approval_digest(step, "plan-b"),
        )
        self.assertEqual(
            step_approval_digest(step, "plan-a"),
            step_approval_digest(step, "plan-a"),
        )

    def test_the_digest_is_insensitive_to_parameter_order(self) -> None:
        first = PlanStep(
            step_id="s1",
            tool_name="write_text_artifact",
            expected_result="x",
            parameters={"artifact_path": "a.txt", "content": "x"},
            requires_approval=True,
        )
        second = PlanStep(
            step_id="s1",
            tool_name="write_text_artifact",
            expected_result="x",
            parameters={"content": "x", "artifact_path": "a.txt"},
            requires_approval=True,
        )
        self.assertEqual(
            step_approval_digest(first, "p"),
            step_approval_digest(second, "p"),
        )


if __name__ == "__main__":
    unittest.main()
