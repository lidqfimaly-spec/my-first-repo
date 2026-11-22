"""S-D ⑤: continuing a failed multi-step plan without redoing finished work.

These tests are about *behaviour*, not status strings. Wherever it is possible
they assert on:

* how many times a tool actually started (``tool_started`` events across every
  task the run created),
* what is actually on disk (``artifacts/<task>/<name>``),
* which rejection code came back, because each code means something different
  to the person who has to decide what to do next.

Reachability, stated plainly
----------------------------
Every state exercised here is produced by the real service, with two
exceptions that are called out in the test that uses them:

* the table-driven cases in ``SideEffectDecisionTests`` call the decision
  function directly with hand-built ``StepResult`` values. They verify the
  branches of the rule, not a user path.
* ``test_a_recorded_child_that_vanished_is_not_retried`` cannot be reached
  through the product at all: it needs a step record whose child id no longer
  resolves, which only a corrupted or hand-edited store produces.

The "tool ran and failed" case is *not* fabricated: it is produced by making
the artifacts directory read-only, so the tool really starts and really fails.
That is the honest version of "the step may already have written something".

Why a continuation is a new task: ``PARTIAL`` and ``FAILED`` are terminal and
the lifecycle audit rejects a second terminal event of the same kind, so
reopening the failed task in place would corrupt the audit trail of every
continued plan. See ``TaskService.resume_failed_plan``.
"""

from __future__ import annotations

import inspect
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.capabilities import SUPPORTED_TASK_OPERATIONS  # noqa: E402
from zevorinth.models import (  # noqa: E402
    EventType,
    PlanStep,
    StepResult,
    TaskRequest,
    TaskStatus,
)
from zevorinth.multi_step import (  # noqa: E402
    MULTI_STEP_OPERATION,
    SUPPORTED_STEP_OPERATIONS,
)
from zevorinth.service import (  # noqa: E402
    PlanResumeRefused,
    TaskService,
)
from zevorinth.workbench import ReadOnlyWorkbench  # noqa: E402
from zevorinth.workbench_control import (  # noqa: E402
    CONTROL_STARTABLE_OPERATIONS,
)


def write_step(
    step_id: str,
    artifact_path: str,
    content: str,
    *,
    depends_on: list[str] | None = None,
    requires_approval: bool = False,
) -> dict[str, object]:
    return {
        "step_id": step_id,
        "tool_name": "write_text_artifact",
        "expected_result": f"create {artifact_path}",
        "parameters": {"artifact_path": artifact_path, "content": content},
        "depends_on": list(depends_on or []),
        "requires_approval": requires_approval,
        "max_attempts": 1,
    }


def inspect_step(
    step_id: str,
    workspace: str,
    *,
    depends_on: list[str] | None = None,
    requires_approval: bool = False,
) -> dict[str, object]:
    return {
        "step_id": step_id,
        "tool_name": "inspect_workspace",
        "expected_result": f"inspect workspace {workspace}",
        "parameters": {"workspace": workspace},
        "depends_on": list(depends_on or []),
        "requires_approval": requires_approval,
        "max_attempts": 1,
    }


class _ResumeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.service = TaskService(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- fixtures ---------------------------------------------------------

    def _register_workspace(self, name: str = "demo") -> Path:
        path = self.root / "workspaces" / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "hello.txt").write_text("hi\n", encoding="utf-8")
        return path

    def _three_step_plan(self, *, c_requires_approval: bool = False,
                         c_content: str = "${a.artifact_sha256}"):
        """a writes, b inspects a workspace that does not exist yet, c follows.

        ``fail_fast`` means ``b`` blocking leaves the parent PARTIAL with ``c``
        never attempted -- the shape a real failure produces.
        """
        return self.service.submit_plan(
            "write, inspect, write",
            [
                write_step("a", "a.txt", "A\n"),
                inspect_step("b", "demo", depends_on=["a"]),
                write_step(
                    "c",
                    "c.txt",
                    c_content,
                    depends_on=["a", "b"],
                    requires_approval=c_requires_approval,
                ),
            ],
        )

    # -- observations -----------------------------------------------------

    def _step(self, task, step_id: str) -> StepResult | None:
        return next(
            (r for r in task.step_results if r.step_id == step_id), None
        )

    def _statuses(self, task) -> list[tuple[str, str]]:
        return [(r.step_id, r.status) for r in task.step_results]

    def _plan_step_ids(self, task) -> list[str]:
        self.assertIsNotNone(task.plan)
        return [step.step_id for step in task.plan.steps]

    def _artifact(self, child_task_id: str, name: str) -> Path:
        return self.root / "artifacts" / child_task_id / name

    def _task_count(self) -> int:
        listing = ReadOnlyWorkbench(self.root).list_tasks(limit=100)
        return len(listing["tasks"])

    def _creation_preview(self, task_id: str) -> dict:
        """The preview recorded on the ``task_created`` event."""
        events = self._events_of(task_id, EventType.TASK_CREATED)
        self.assertEqual(len(events), 1)
        return events[0].payload.get("creation_preview") or {}

    def _known_task_ids(self, *tasks) -> set[str]:
        found: set[str] = set()
        for task in tasks:
            found.add(task.task_id)
            for result in task.step_results:
                if result.child_task_id is not None:
                    found.add(result.child_task_id)
        return found

    def _tool_starts(self, tool_name: str, *tasks) -> int:
        """How many times a tool was actually invoked, across every task."""
        total = 0
        for task_id in self._known_task_ids(*tasks):
            for event in self.service.store.list_events(task_id):
                if event.event_type != EventType.TOOL_STARTED.value:
                    continue
                if event.payload.get("tool_name") == tool_name:
                    total += 1
        return total

    def _events_of(self, task_id: str, event_type: EventType) -> list:
        return [
            event
            for event in self.service.store.list_events(task_id)
            if event.event_type == event_type.value
        ]


class ResumeDoesNotRedoFinishedWorkTests(_ResumeCase):
    def test_the_step_that_succeeded_is_not_run_again(self) -> None:
        parent = self._three_step_plan()
        self.assertIs(parent.status, TaskStatus.PARTIAL)
        self.assertEqual(
            self._statuses(parent), [("a", "succeeded"), ("b", "blocked")]
        )
        a_child = self._step(parent, "a").child_task_id
        a_path = self._artifact(a_child, "a.txt")
        self.assertTrue(a_path.exists())
        before = a_path.read_bytes()
        before_mtime = a_path.stat().st_mtime_ns

        continuation = self.service.resume_failed_plan(parent.task_id)

        # The continuation does not contain step a at all.
        self.assertEqual(self._plan_step_ids(continuation), ["b", "c"])
        # ...and it did not recreate the file the parent already produced.
        self.assertFalse(self._artifact(continuation.task_id, "a.txt").exists())
        self.assertTrue(a_path.exists())
        self.assertEqual(a_path.read_bytes(), before)
        self.assertEqual(a_path.stat().st_mtime_ns, before_mtime)
        # Only one write_text_artifact ever ran (step a, in the parent).
        self.assertEqual(
            self._tool_starts("write_text_artifact", parent, continuation), 1
        )

    def test_the_parent_keeps_its_verdict_and_records_the_link(self) -> None:
        parent = self._three_step_plan()
        continuation = self.service.resume_failed_plan(parent.task_id)

        reloaded = self.service.store.require_task(parent.task_id)
        self.assertIs(reloaded.status, TaskStatus.PARTIAL)
        self.assertEqual(reloaded.error.code, "multi_step_partial")
        # The parent's own step records are untouched by the continuation.
        self.assertEqual(
            self._statuses(reloaded), [("a", "succeeded"), ("b", "blocked")]
        )
        notes = [
            event
            for event in self._events_of(parent.task_id, EventType.TASK_NOTE_ADDED)
            if (event.payload or {}).get("kind") == "plan_continuation"
        ]
        self.assertEqual(len(notes), 1)
        self.assertEqual(
            notes[0].payload["continuation_task_id"], continuation.task_id
        )
        self.assertEqual(notes[0].payload["carried_step_ids"], ["a"])

    def test_the_continuation_has_its_own_plan_identity(self) -> None:
        parent = self._three_step_plan()
        continuation = self.service.resume_failed_plan(parent.task_id)
        self.assertNotEqual(continuation.plan.plan_id, parent.plan.plan_id)
        self.assertEqual(continuation.request.operation, MULTI_STEP_OPERATION)
        self.assertEqual(continuation.metadata.get("resume_of"), parent.task_id)
        preview = self._creation_preview(continuation.task_id)
        self.assertEqual(preview.get("resume_of"), parent.task_id)
        self.assertEqual(preview.get("carried_step_ids"), ["a"])
        self.assertEqual(preview.get("resumed_step_ids"), ["b", "c"])
        # No approval is inherited: the continuation starts clean.
        self.assertEqual(continuation.approved_step_ids, [])


class ResumeCarriesRecordedOutputTests(_ResumeCase):
    def test_upstream_output_is_baked_into_the_resumed_parameters(self) -> None:
        parent = self._three_step_plan()
        recorded_sha = self._step(parent, "a").output["artifact_sha256"]

        # The failure the parent could not get past is fixed in the world, not
        # in the plan: the workspace now exists.
        self._register_workspace("demo")
        continuation = self.service.resume_failed_plan(parent.task_id)

        self.assertIs(continuation.status, TaskStatus.SUCCEEDED)
        self.assertEqual(
            self._statuses(continuation),
            [("b", "succeeded"), ("c", "succeeded")],
        )
        c_step = next(
            step for step in continuation.plan.steps if step.step_id == "c"
        )
        # The reference was resolved to a concrete value before the plan was
        # created, so what a human is asked to approve is the real content.
        self.assertEqual(c_step.parameters["content"], recorded_sha)
        self.assertNotIn("${", str(c_step.parameters["content"]))
        c_child = self._step(continuation, "c").child_task_id
        self.assertEqual(
            self._artifact(c_child, "c.txt").read_text(encoding="utf-8"),
            recorded_sha,
        )

    def test_dependencies_on_carried_steps_are_dropped(self) -> None:
        parent = self._three_step_plan()
        self._register_workspace("demo")
        continuation = self.service.resume_failed_plan(parent.task_id)
        by_id = {step.step_id: step for step in continuation.plan.steps}
        self.assertEqual(by_id["b"].depends_on, [])
        self.assertEqual(by_id["c"].depends_on, ["b"])

    def test_the_recorded_output_wins_over_what_is_on_disk_now(self) -> None:
        parent = self._three_step_plan()
        recorded_sha = self._step(parent, "a").output["artifact_sha256"]
        a_child = self._step(parent, "a").child_task_id
        # Something rewrites the parent's artifact after the fact.
        self._artifact(a_child, "a.txt").write_text("TAMPERED\n", encoding="utf-8")
        self._register_workspace("demo")

        continuation = self.service.resume_failed_plan(parent.task_id)

        c_step = next(
            step for step in continuation.plan.steps if step.step_id == "c"
        )
        # The continuation binds to the recorded evidence, not to a file that
        # may have changed since. A different value would silently approve
        # content nobody saw.
        self.assertEqual(c_step.parameters["content"], recorded_sha)
        c_child = self._step(continuation, "c").child_task_id
        self.assertEqual(
            self._artifact(c_child, "c.txt").read_text(encoding="utf-8"),
            recorded_sha,
        )


class SideEffectDecisionTests(_ResumeCase):
    """The rule itself, exercised branch by branch.

    These call the decision function directly: the point is to pin what each
    recorded shape is *classified* as, which end-to-end tests can only reach
    for the shapes the tools happen to produce.
    """

    def _plan_step(self, step_id: str = "b") -> PlanStep:
        return PlanStep(
            step_id=step_id,
            tool_name="inspect_workspace",
            expected_result="inspect",
            parameters={"workspace": "demo"},
        )

    def _result(self, *, status: str, child_task_id: str | None) -> StepResult:
        return StepResult(
            step_id="b",
            tool_name="inspect_workspace",
            status=status,
            attempt_count=1,
            child_task_id=child_task_id,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
        )

    def _decide(self, result: StepResult, parent) -> bool:
        return self.service._step_attempt_left_no_side_effect(
            result,
            plan_step=self._plan_step(result.step_id),
            parent_task_id=parent.task_id,
        )

    def test_a_skipped_step_is_clean(self) -> None:
        parent = self._three_step_plan()
        self.assertTrue(
            self._decide(self._result(status="skipped", child_task_id=None), parent)
        )

    def test_a_step_that_never_announced_a_child_is_clean(self) -> None:
        parent = self._three_step_plan()
        self.assertTrue(
            self._decide(self._result(status="failed", child_task_id=None), parent)
        )

    def test_a_step_that_died_before_creating_its_child_is_clean(self) -> None:
        # Real shape: a step whose parameters could not be resolved. The child
        # id was announced, but no child task was ever written.
        parent = self.service.submit_plan(
            "unresolvable reference",
            [
                write_step("a", "a.txt", "A\n"),
                write_step(
                    "b", "b.txt", "${a.no_such_field}", depends_on=["a"]
                ),
            ],
        )
        self.assertIs(parent.status, TaskStatus.PARTIAL)
        recorded = self._step(parent, "b")
        self.assertIsNone(recorded.child_task_id)
        self.assertTrue(self._decide(recorded, parent))

    def test_a_child_that_stopped_before_the_tool_is_clean(self) -> None:
        parent = self._three_step_plan()
        recorded = self._step(parent, "b")
        self.assertEqual(recorded.status, "blocked")
        self.assertTrue(self._decide(recorded, parent))

    def test_a_child_that_reached_the_tool_is_not_clean(self) -> None:
        parent = self._three_step_plan()
        a_child = self._step(parent, "a").child_task_id
        # Step a's child ran for real and produced an artifact.
        fabricated = self._result(status="failed", child_task_id=a_child)
        self.assertFalse(self._decide(fabricated, parent))

    def test_a_recorded_child_that_vanished_is_not_retried(self) -> None:
        # Not reachable through the product: needs a store whose child record
        # is gone while the step still names it. Fail-closed on purpose --
        # "it is gone now" is not proof that it never wrote anything.
        parent = self._three_step_plan()
        fabricated = self._result(
            status="failed", child_task_id="00000000-0000-4000-8000-000000000000"
        )
        self.assertFalse(self._decide(fabricated, parent))


class SideEffectRefusalEndToEndTests(_ResumeCase):
    def _fail_after_the_tool_started(self, parent_path: Path):
        """Run a plan whose second step really starts a tool and really fails."""
        plan = self.service.submit_plan(
            "write twice",
            [
                write_step("a", "a.txt", "A\n"),
                write_step(
                    "b", "b.txt", "B\n", depends_on=["a"], requires_approval=True
                ),
            ],
        )
        self.assertIs(plan.status, TaskStatus.AWAITING_APPROVAL)
        self.assertTrue((parent_path / "artifacts").is_dir())
        original = (parent_path / "artifacts").stat().st_mode
        os.chmod(parent_path / "artifacts", stat.S_IRUSR | stat.S_IXUSR)
        try:
            return self.service.approve_step(plan.task_id, "b", approved=True)
        finally:
            os.chmod(parent_path / "artifacts", original)

    def test_a_failed_step_whose_tool_ran_is_refused_not_retried(self) -> None:
        parent = self._fail_after_the_tool_started(self.root)
        self.assertIs(parent.status, TaskStatus.PARTIAL)
        recorded = self._step(parent, "b")
        self.assertEqual(recorded.status, "failed")
        child = self.service.store.get_task(recorded.child_task_id)
        events = [
            event.event_type
            for event in self.service.store.list_events(child.task_id)
        ]
        # Precondition: the tool genuinely started, so "nothing happened" is
        # not something the record can prove.
        self.assertIn(EventType.TOOL_STARTED.value, events)
        before = self._task_count()

        with self.assertRaises(PlanResumeRefused) as caught:
            self.service.resume_failed_plan(parent.task_id)

        self.assertEqual(caught.exception.code, "step_side_effect_uncertain")
        self.assertEqual(caught.exception.step_id, "b")
        # Refusal creates nothing and runs nothing.
        self.assertEqual(self._task_count(), before)
        self.assertEqual(child.attempt_id, self.service.store.get_task(child.task_id).attempt_id)
        self.assertEqual(
            events,
            [
                event.event_type
                for event in self.service.store.list_events(child.task_id)
            ],
        )

    def test_a_failed_step_that_did_not_start_is_attempted_again(self) -> None:
        parent = self._three_step_plan()
        recorded = self._step(parent, "b")
        self.assertEqual(recorded.error.code, "workspace_missing")

        continuation = self.service.resume_failed_plan(parent.task_id)

        # Nothing succeeded in the continuation, so it finalises FAILED; the
        # point is that the step was *attempted again* with a fresh child
        # rather than refused as a possible duplicate.
        self.assertIs(continuation.status, TaskStatus.FAILED)
        retried = self._step(continuation, "b")
        self.assertIsNotNone(retried)
        self.assertEqual(retried.status, "blocked")
        self.assertEqual(retried.error.code, "workspace_missing")
        self.assertNotEqual(retried.child_task_id, recorded.child_task_id)


class ApprovalAcrossContinuationTests(_ResumeCase):
    def test_the_downstream_step_asks_for_approval_again(self) -> None:
        parent = self._three_step_plan(c_requires_approval=True)
        self._register_workspace("demo")

        continuation = self.service.resume_failed_plan(parent.task_id)

        self.assertIs(continuation.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(continuation.pending_approval_step_id, "c")
        reasons = [
            event.payload.get("reason")
            for event in self._events_of(
                continuation.task_id, EventType.STEP_APPROVAL_REQUIRED
            )
        ]
        self.assertEqual(reasons, ["approval_required"])
        c_child = self._step(continuation, "c")
        self.assertIsNone(c_child)
        self.assertFalse(
            (self.root / "artifacts" / continuation.task_id / "c.txt").exists()
        )

    def test_an_approval_written_for_the_parent_plan_does_not_satisfy_the_step(
        self,
    ) -> None:
        from zevorinth.multi_step import step_approval_digest

        parent = self._three_step_plan(c_requires_approval=True)
        self._register_workspace("demo")
        continuation = self.service.resume_failed_plan(parent.task_id)
        self.assertIs(continuation.status, TaskStatus.AWAITING_APPROVAL)

        # A record that says "c was approved" for exactly the content the
        # continuation would run -- but under the *parent's* plan identity.
        # Plan binding is then the only thing standing between it and
        # execution, which is what this test isolates.
        c_plan_step = next(
            step for step in continuation.plan.steps if step.step_id == "c"
        )
        self.service.store.record_event_and_update(
            continuation.task_id,
            EventType.TASK_NOTE_ADDED,
            self.service.clock(),
            payload={"message": "fixture: approval carried over from parent plan"},
            updates={
                "approved_step_ids": ["c"],
                "approved_step_digests": {
                    "c": step_approval_digest(c_plan_step, parent.plan.plan_id)
                },
            },
            require_no_lease=True,
            expected_updated_at=continuation.updated_at,
            expected_plan_version=continuation.plan_version,
            expected_status=TaskStatus.AWAITING_APPROVAL,
        )
        self.service.request_pause(
            continuation.task_id, requested_by="test", reason="re-check"
        )
        rechecked = self.service.resume(continuation.task_id)

        self.assertIs(rechecked.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(rechecked.pending_approval_step_id, "c")
        self.assertIsNone(self._step(rechecked, "c"))
        reasons = [
            event.payload.get("reason")
            for event in self._events_of(
                continuation.task_id, EventType.STEP_APPROVAL_REQUIRED
            )
        ]
        self.assertEqual(reasons[-1], "approval_content_changed")
        self.assertFalse(
            (self.root / "artifacts" / continuation.task_id / "c.txt").exists()
        )

    def test_approving_in_the_continuation_runs_the_step(self) -> None:
        parent = self._three_step_plan(c_requires_approval=True)
        self._register_workspace("demo")
        continuation = self.service.resume_failed_plan(parent.task_id)
        done = self.service.approve_step(continuation.task_id, "c", approved=True)
        self.assertIs(done.status, TaskStatus.SUCCEEDED)
        c_child = self._step(done, "c").child_task_id
        self.assertTrue(self._artifact(c_child, "c.txt").exists())


class DownstreamDoesNotStartTests(_ResumeCase):
    def test_a_dependent_step_is_not_attempted_after_its_dependency_fails(
        self,
    ) -> None:
        parent = self._three_step_plan()
        continuation = self.service.resume_failed_plan(parent.task_id)

        # Nothing succeeded in the continuation, so it finalises FAILED.
        self.assertIs(continuation.status, TaskStatus.FAILED)
        self.assertEqual(
            self._statuses(continuation), [("b", "blocked")]
        )
        # c is not merely "not succeeded": it was never started.
        self.assertIsNone(self._step(continuation, "c"))
        self.assertEqual(
            self._tool_starts(
                "write_text_artifact", parent, continuation
            ),
            1,
        )

    def test_a_step_whose_dependency_was_skipped_is_skipped_with_a_reason(
        self,
    ) -> None:
        # `continue` policy keeps going after a failure, so the dependent step
        # reaches the graph and is recorded as skipped rather than silently
        # dropped.
        parent = self.service.submit_plan(
            "inspect fails, dependent follows",
            [
                inspect_step("b", "ghost"),
                write_step("d", "d.txt", "D\n", depends_on=["b"]),
            ],
            failure_policy="continue",
        )
        self.assertIs(parent.status, TaskStatus.FAILED)
        self.assertEqual(
            self._statuses(parent), [("b", "blocked"), ("d", "skipped")]
        )
        self.assertEqual(self._tool_starts("write_text_artifact", parent), 0)


class RepeatedFailureTests(_ResumeCase):
    def test_a_continuation_that_fails_again_can_be_continued_again(self) -> None:
        parent = self._three_step_plan()
        first = self.service.resume_failed_plan(parent.task_id)
        self.assertIs(first.status, TaskStatus.FAILED)

        second = self.service.resume_failed_plan(first.task_id)

        self.assertEqual(second.metadata.get("resume_of"), first.task_id)
        self.assertEqual(self._plan_step_ids(second), ["b", "c"])
        # FAILED is resumable too, not just PARTIAL.
        self.assertIs(second.status, TaskStatus.FAILED)
        # Two continuations, still exactly one write.
        self.assertEqual(
            self._tool_starts("write_text_artifact", parent, first, second), 1
        )

        self._register_workspace("demo")
        third = self.service.resume_failed_plan(second.task_id)
        self.assertIs(third.status, TaskStatus.SUCCEEDED)


class RefusalTests(_ResumeCase):
    def test_a_successful_plan_cannot_be_continued(self) -> None:
        done = self.service.submit_plan(
            "just write",
            [write_step("a", "a.txt", "A\n")],
        )
        self.assertIs(done.status, TaskStatus.SUCCEEDED)
        with self.assertRaises(PlanResumeRefused) as caught:
            self.service.resume_failed_plan(done.task_id)
        self.assertEqual(caught.exception.code, "task_not_resumable")

    def test_a_paused_plan_is_not_continued_through_this_path(self) -> None:
        queued = self.service.enqueue(
            TaskRequest(
                goal="paused plan",
                operation=MULTI_STEP_OPERATION,
                parameters={
                    "steps": [write_step("a", "a.txt", "A\n")],
                    "failure_policy": "fail_fast",
                },
            )
        )
        self.assertIs(queued.status, TaskStatus.QUEUED)
        paused = self.service.request_pause(
            queued.task_id, requested_by="test", reason="hold this"
        )
        self.assertIs(paused.status, TaskStatus.PAUSED)
        # A paused plan is continued by resuming it (which re-checks
        # approvals), never by the failed-plan path.
        with self.assertRaises(PlanResumeRefused) as caught:
            self.service.resume_failed_plan(queued.task_id)
        self.assertEqual(caught.exception.code, "task_not_resumable")

    def test_a_cancel_request_blocks_the_continuation(self) -> None:
        parent = self._three_step_plan()
        # Cancelling a terminal task is a no-op in the store, so this state is
        # written directly. It is the shape left behind when a cancel request
        # lands between the last step checkpoint and the final transition.
        self.service.store.record_event_and_update(
            parent.task_id,
            EventType.CANCEL_REQUESTED.value,
            self.service.clock(),
            payload={"requested_by": "test", "reason": "stop"},
            updates={
                "cancel_requested_at": self.service.clock(),
                "cancel_requested_by": "test",
                "cancel_reason": "stop",
            },
            require_no_lease=True,
            expected_updated_at=parent.updated_at,
            expected_plan_version=parent.plan_version,
            expected_status=TaskStatus.PARTIAL,
        )
        before = self._task_count()
        with self.assertRaises(PlanResumeRefused) as caught:
            self.service.resume_failed_plan(parent.task_id)
        self.assertEqual(caught.exception.code, "task_cancel_requested")
        self.assertEqual(self._task_count(), before)

    def test_a_stale_plan_version_is_refused(self) -> None:
        parent = self._three_step_plan()
        with self.assertRaises(PlanResumeRefused) as caught:
            self.service.resume_failed_plan(
                parent.task_id,
                expected_plan_version=parent.plan_version + 1,
            )
        self.assertEqual(
            caught.exception.code, "task_plan_version_changed"
        )

    def test_the_matching_plan_version_is_accepted(self) -> None:
        parent = self._three_step_plan()
        continuation = self.service.resume_failed_plan(
            parent.task_id, expected_plan_version=parent.plan_version
        )
        self.assertEqual(
            self._creation_preview(continuation.task_id)["resume_plan_version"],
            parent.plan_version,
        )


class BoundaryTests(_ResumeCase):
    def test_continuing_cannot_introduce_work_that_was_never_planned(
        self,
    ) -> None:
        parent = self._three_step_plan()
        continuation = self.service.resume_failed_plan(parent.task_id)
        parent_tools = {
            step.step_id: step.tool_name for step in parent.plan.steps
        }
        for step in continuation.plan.steps:
            self.assertEqual(step.tool_name, parent_tools[step.step_id])

    def test_the_entry_point_takes_no_step_definitions(self) -> None:
        parameters = inspect.signature(
            TaskService.resume_failed_plan
        ).parameters
        self.assertEqual(
            set(parameters),
            {
                "self",
                "task_id",
                "expected_plan_version",
                "conversation_id",
                "turn_id",
            },
        )

    def test_the_operation_whitelists_are_unchanged(self) -> None:
        self.assertEqual(
            SUPPORTED_TASK_OPERATIONS,
            frozenset(
                {
                    "controlled_shell",
                    "edit_text_file",
                    "inspect_workspace",
                    "modify_text_artifact",
                    "read_text_artifact",
                    "research_report",
                    "run_tests",
                    "write_text_artifact",
                }
            ),
        )
        self.assertNotIn("run_tests", CONTROL_STARTABLE_OPERATIONS)
        self.assertNotIn("execute_plan", CONTROL_STARTABLE_OPERATIONS)
        self.assertEqual(
            CONTROL_STARTABLE_OPERATIONS,
            SUPPORTED_TASK_OPERATIONS - {"run_tests"},
        )

    def test_a_continuation_plan_cannot_name_a_tool_outside_the_plan_set(
        self,
    ) -> None:
        # The continuation copies tool names from the parent plan, which was
        # validated against SUPPORTED_STEP_OPERATIONS when it was submitted.
        parent = self._three_step_plan()
        continuation = self.service.resume_failed_plan(parent.task_id)
        for step in continuation.plan.steps:
            self.assertIn(step.tool_name, SUPPORTED_STEP_OPERATIONS)


class FinalArtifactTests(_ResumeCase):
    def test_the_continuation_reverifies_what_it_delivered(self) -> None:
        parent = self._three_step_plan()
        recorded_sha = self._step(parent, "a").output["artifact_sha256"]
        self._register_workspace("demo")

        continuation = self.service.resume_failed_plan(parent.task_id)

        self.assertIs(continuation.status, TaskStatus.SUCCEEDED)
        self.assertIsNotNone(continuation.verification)
        self.assertTrue(continuation.verification.passed)
        self.assertIsNotNone(continuation.artifact)
        # The delivered copy is re-read from disk, not trusted from the plan.
        delivered = self._artifact(
            continuation.task_id, continuation.artifact.relative_path
        )
        self.assertTrue(delivered.exists())
        self.assertEqual(
            delivered.read_text(encoding="utf-8"), recorded_sha
        )
        self.assertEqual(
            continuation.artifact.sha256,
            __import__("hashlib").sha256(delivered.read_bytes()).hexdigest(),
        )

    def test_the_parent_verdict_is_not_upgraded_by_the_continuation(self) -> None:
        parent = self._three_step_plan()
        self._register_workspace("demo")
        self.service.resume_failed_plan(parent.task_id)
        reloaded = self.service.store.require_task(parent.task_id)
        self.assertIs(reloaded.status, TaskStatus.PARTIAL)
        self.assertIsNotNone(reloaded.verification)
        self.assertFalse(reloaded.verification.passed)


if __name__ == "__main__":
    unittest.main()
