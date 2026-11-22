from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import (  # noqa: E402
    EventType,
    ExecutionMode,
    TaskRequest,
    TaskStatus,
    ToolResult,
)
from zevorinth.artifact_tool import RealArtifactWriter  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.capabilities import CAPABILITY_REGISTRY  # noqa: E402
from zevorinth.event_store import PlanRevisionConflict  # noqa: E402


def write_step(
    step_id: str,
    artifact_path: str,
    content: object,
    *,
    depends_on: list[str] | None = None,
    requires_approval: bool = False,
) -> dict[str, object]:
    return {
        "step_id": step_id,
        "tool_name": "write_text_artifact",
        "expected_result": f"create verified artifact {artifact_path}",
        "parameters": {
            "artifact_path": artifact_path,
            "content": content,
        },
        "depends_on": list(depends_on or []),
        "requires_approval": requires_approval,
        "max_attempts": 1,
    }


class FailIfCalledWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self) -> None:
        self.calls = 0

    def write(self, **_: object) -> ToolResult:
        self.calls += 1
        raise AssertionError("writer must not run after the step side effect")


class ChildRewriteGuardWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self, artifacts_root: Path, forbidden_task_id: str) -> None:
        self.inner = RealArtifactWriter(artifacts_root)
        self.forbidden_task_id = forbidden_task_id
        self.task_ids: list[str] = []

    def write(self, *, task_id: str, **kwargs: object) -> ToolResult:
        if task_id == self.forbidden_task_id:
            raise AssertionError("child side effect must not be repeated")
        self.task_ids.append(task_id)
        return self.inner.write(task_id=task_id, **kwargs)


class MultiStepTaskTests(unittest.TestCase):
    def test_multi_step_capability_is_registered_with_bounds(self) -> None:
        details = CAPABILITY_REGISTRY["capabilities"]["multi_step_task_engine"]
        self.assertEqual(details["status"], "available")
        self.assertTrue(details["verified"])
        self.assertIn("service:submit_plan", details["access"])
        self.assertIn(
            "not model-selectable through the LLM planner",
            details["limitations"],
        )

    def test_dependency_data_flow_executes_every_step_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)

            task = service.submit_plan(
                "create a source then a downstream copy",
                [
                    write_step("source", "source.txt", "hello"),
                    write_step(
                        "copy",
                        "copy.txt",
                        "${source.content}",
                        depends_on=["source"],
                    ),
                ],
            )

            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            self.assertEqual(len(task.step_results), 2)
            self.assertTrue(
                all(result.status == "succeeded" for result in task.step_results)
            )
            downstream = task.step_results[1]
            self.assertIsNotNone(downstream.child_task_id)
            self.assertEqual(downstream.output["content"], "hello")
            child_path = (
                root
                / "artifacts"
                / str(downstream.child_task_id)
                / "copy.txt"
            )
            self.assertEqual(child_path.read_text(encoding="utf-8"), "hello")
            self.assertIsNotNone(task.artifact)
            parent_path = (
                root
                / "artifacts"
                / task.task_id
                / task.artifact.relative_path
            )
            self.assertEqual(
                parent_path.read_text(encoding="utf-8"),
                child_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                task.artifact.sha256,
                downstream.artifact.sha256,
            )
            self.assertTrue(task.verification.passed)
            self.assertEqual(
                task.verification.reason,
                "multi_step_all_steps_verified",
            )
            self.assertTrue(service.audit_lifecycle(task.task_id)["ok"])
            child = service.require_task(str(downstream.child_task_id))
            self.assertEqual(child.parent_task_id, task.task_id)

    def test_cycle_and_unknown_dependency_fail_closed_at_planning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            for steps in (
                [
                    write_step("a", "a.txt", "a", depends_on=["b"]),
                    write_step("b", "b.txt", "b", depends_on=["a"]),
                ],
                [write_step("a", "a.txt", "a", depends_on=["missing"])],
            ):
                with self.subTest(steps=steps):
                    task = service.submit_plan("invalid graph", steps)
                    self.assertEqual(task.status, TaskStatus.PLANNING_FAILED)
                    self.assertEqual(task.execution_mode.value, "not_executed")

    def test_undeclared_data_reference_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            task = service.submit_plan(
                "missing dependency declaration",
                [
                    write_step("source", "source.txt", "source"),
                    write_step("copy", "copy.txt", "${source.content}"),
                ],
            )
            self.assertEqual(task.status, TaskStatus.PLANNING_FAILED)
            self.assertEqual(task.execution_mode.value, "not_executed")

    def test_step_limit_and_duplicate_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            duplicate = service.submit_plan(
                "duplicate ids",
                [
                    write_step("same", "one.txt", "one"),
                    write_step("same", "two.txt", "two"),
                ],
            )
            self.assertEqual(duplicate.status, TaskStatus.PLANNING_FAILED)
            too_many = service.submit_plan(
                "too many steps",
                [
                    write_step(f"s{index}", f"{index}.txt", str(index))
                    for index in range(9)
                ],
            )
            self.assertEqual(too_many.status, TaskStatus.PLANNING_FAILED)

    def test_approval_pauses_and_resumes_exact_pending_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            task = service.submit_plan(
                "approve before writing",
                [
                    write_step(
                        "approval",
                        "approved.txt",
                        "approved",
                        requires_approval=True,
                    )
                ],
            )
            self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)
            self.assertEqual(task.pending_approval_step_id, "approval")
            self.assertEqual(task.step_results, [])

            finished = service.approve_step(
                task.task_id,
                "approval",
                approved=True,
            )
            self.assertEqual(finished.status, TaskStatus.SUCCEEDED)
            self.assertEqual(finished.pending_approval_step_id, None)
            self.assertEqual(finished.approved_step_ids, ["approval"])

    def test_rejected_approval_cancels_without_child_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            task = service.submit_plan(
                "reject before writing",
                [
                    write_step(
                        "approval",
                        "rejected.txt",
                        "rejected",
                        requires_approval=True,
                    )
                ],
            )
            rejected = service.approve_step(
                task.task_id,
                "approval",
                approved=False,
            )
            self.assertEqual(rejected.status, TaskStatus.CANCELLED)
            self.assertFalse(
                (root / "artifacts" / task.task_id / "rejected.txt").exists()
            )

    def test_approval_must_match_exact_pending_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            task = service.submit_plan(
                "approval identity",
                [
                    write_step(
                        "expected",
                        "approved.txt",
                        "approved",
                        requires_approval=True,
                    )
                ],
            )
            with self.assertRaisesRegex(
                Exception,
                "approval step does not match",
            ):
                service.approve_step(
                    task.task_id,
                    "other",
                    approved=True,
                )

    def test_approval_identity_is_revalidated_after_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            task = service.submit_plan(
                "approval identity race",
                [
                    write_step(
                        "expected",
                        "approved.txt",
                        "approved",
                        requires_approval=True,
                    )
                ],
            )
            original_claim = service._claim

            def claim_after_pending_step_change(task_id, **kwargs):
                service.store.record_event_and_update(
                    task_id,
                    EventType.STEP_APPROVAL_REQUIRED,
                    service.clock(),
                    payload={"step_id": "other"},
                    updates={"pending_approval_step_id": "other"},
                )
                return original_claim(task_id, **kwargs)

            service._claim = claim_after_pending_step_change

            with self.assertRaisesRegex(
                PlanRevisionConflict,
                "approval step changed",
            ):
                service.approve_step(
                    task.task_id,
                    "expected",
                    approved=True,
                    expected_plan_version=task.plan_version,
                )

            current = service.require_task(task.task_id)
            self.assertIs(current.status, TaskStatus.AWAITING_APPROVAL)
            self.assertIsNone(current.lease_token)
            self.assertEqual(current.pending_approval_step_id, "other")

    def test_cancel_request_wins_before_pending_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            waiting = service.submit_plan(
                "cancel before approval",
                [
                    write_step(
                        "approval",
                        "not-written.txt",
                        "not-written",
                        requires_approval=True,
                    )
                ],
            )
            service.request_cancel(
                waiting.task_id,
                requested_by="test",
                reason="stop",
            )
            cancelled = service.approve_step(
                waiting.task_id,
                "approval",
                approved=True,
            )
            self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
            self.assertEqual(cancelled.step_results, [])

    def test_pause_before_execution_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            queued = service.enqueue(
                TaskRequest(
                    goal="pause before execution",
                    operation="execute_plan",
                    parameters={
                        "steps": [write_step("one", "one.txt", "one")],
                        "failure_policy": "fail_fast",
                    },
                )
            )
            paused = service.request_pause(
                queued.task_id,
                requested_by="test",
                reason="inspect first",
            )
            self.assertEqual(paused.status, TaskStatus.PAUSED)
            resumed = service.resume(queued.task_id)
            self.assertEqual(resumed.status, TaskStatus.SUCCEEDED)

    def test_continue_policy_reports_partial_success_truthfully(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            task = service.submit_plan(
                "continue independent work",
                [
                    write_step("ok", "ok.txt", "ok"),
                    write_step("bad", "../escape.txt", "bad"),
                    write_step("also-ok", "also.txt", "also"),
                ],
                failure_policy="continue",
            )
            self.assertEqual(task.status, TaskStatus.PARTIAL)
            statuses = {
                result.step_id: result.status for result in task.step_results
            }
            self.assertEqual(statuses["ok"], "succeeded")
            self.assertEqual(statuses["bad"], "blocked")
            self.assertEqual(statuses["also-ok"], "succeeded")
            self.assertFalse(task.verification.passed)
            self.assertEqual(task.error.code, "multi_step_partial")
            self.assertTrue(service.audit_lifecycle(task.task_id)["ok"])

    def test_fail_fast_preserves_completed_work_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            task = service.submit_plan(
                "stop after failure",
                [
                    write_step("ok", "ok.txt", "ok"),
                    write_step("bad", "../escape.txt", "bad"),
                    write_step("not-run", "not-run.txt", "not-run"),
                ],
                failure_policy="fail_fast",
            )
            self.assertEqual(task.status, TaskStatus.PARTIAL)
            self.assertEqual(
                [result.step_id for result in task.step_results],
                ["ok", "bad"],
            )
            self.assertFalse(task.verification.passed)

    def test_continue_policy_skips_steps_with_unsatisfied_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            task = service.submit_plan(
                "continue independent branch",
                [
                    write_step("bad", "../escape.txt", "bad"),
                    write_step(
                        "dependent",
                        "dependent.txt",
                        "${bad.content}",
                        depends_on=["bad"],
                    ),
                    write_step("independent", "independent.txt", "ok"),
                ],
                failure_policy="continue",
            )
            self.assertEqual(task.status, TaskStatus.PARTIAL)
            statuses = {
                result.step_id: result.status for result in task.step_results
            }
            self.assertEqual(statuses["bad"], "blocked")
            self.assertEqual(statuses["dependent"], "skipped")
            self.assertEqual(statuses["independent"], "succeeded")

    def test_replan_replaces_only_pending_steps_and_preserves_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            waiting = service.submit_plan(
                "replace pending plan",
                [
                    write_step(
                        "approval",
                        "old.txt",
                        "old",
                        requires_approval=True,
                    )
                ],
            )
            self.assertEqual(waiting.status, TaskStatus.AWAITING_APPROVAL)
            revised = service.replan_pending_steps(
                waiting.task_id,
                [write_step("new", "new.txt", "new")],
                expected_plan_version=waiting.plan_version,
            )
            self.assertEqual(revised.status, TaskStatus.PAUSED)
            self.assertEqual(revised.plan_version, waiting.plan_version + 1)
            self.assertIsNone(revised.pending_approval_step_id)
            finished = service.resume(revised.task_id)
            self.assertEqual(finished.status, TaskStatus.SUCCEEDED)
            self.assertEqual(finished.step_results[0].step_id, "new")

    def test_replan_after_completed_step_preserves_completed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            waiting = service.submit_plan(
                "keep completed output",
                [
                    write_step("completed", "completed.txt", "kept"),
                    write_step(
                        "approval",
                        "old.txt",
                        "old",
                        depends_on=["completed"],
                        requires_approval=True,
                    ),
                ],
            )
            self.assertEqual(waiting.status, TaskStatus.AWAITING_APPROVAL)
            self.assertEqual(
                [item.step_id for item in waiting.step_results],
                ["completed"],
            )
            revised = service.replan_pending_steps(
                waiting.task_id,
                [
                    write_step(
                        "replacement",
                        "replacement.txt",
                        "${completed.content}",
                        depends_on=["completed"],
                    )
                ],
                expected_plan_version=waiting.plan_version,
            )
            finished = service.resume(revised.task_id)
            self.assertEqual(finished.status, TaskStatus.SUCCEEDED)
            self.assertEqual(
                [item.step_id for item in finished.step_results],
                ["completed", "replacement"],
            )
            self.assertEqual(
                finished.step_results[0].child_task_id,
                waiting.step_results[0].child_task_id,
            )

    def test_interrupted_child_is_resumed_without_duplicate_step_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_time = "2026-10-19T00:00:00.000+00:00"
            future_time = "2026-10-19T00:02:01.000+00:00"
            service = TaskService(root, clock=lambda: base_time)
            request = TaskRequest(
                goal="resume a completed child write",
                operation="execute_plan",
                parameters={
                    "steps": [write_step("write", "value.txt", "value")],
                    "failure_policy": "fail_fast",
                },
            )
            parent = service.enqueue(request)

            def interrupt(stage: str, task: object) -> None:
                if stage == "after_write_before_verification":
                    raise RuntimeError("simulated parent observation crash")

            service.fault_hook = interrupt
            with self.assertRaisesRegex(RuntimeError, "observation crash"):
                service.execute_task(parent.task_id)

            recovered_parent = service.require_task(parent.task_id)
            self.assertEqual(recovered_parent.status, TaskStatus.RUNNING)
            self.assertEqual(len(recovered_parent.step_results), 1)
            running_step = recovered_parent.step_results[0]
            self.assertEqual(running_step.status, "running")
            self.assertIsNotNone(running_step.child_task_id)
            output_path = (
                root
                / "artifacts"
                / str(running_step.child_task_id)
                / "value.txt"
            )
            self.assertEqual(output_path.read_text(encoding="utf-8"), "value")

            writer = ChildRewriteGuardWriter(
                root / "artifacts",
                str(running_step.child_task_id),
            )
            restarted = TaskService(
                root,
                writer=writer,
                clock=lambda: future_time,
            )
            finished = restarted.resume(parent.task_id)
            self.assertEqual(finished.status, TaskStatus.SUCCEEDED)
            self.assertEqual(writer.task_ids, [parent.task_id])
            self.assertEqual(finished.step_results[0].status, "succeeded")
            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "value",
            )

    def test_queued_pause_uses_transactional_state_and_lease_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            queued = service.enqueue(
                TaskRequest(
                    goal="pause against a stale queued snapshot",
                    operation="execute_plan",
                    parameters={
                        "steps": [write_step("one", "one.txt", "one")],
                        "failure_policy": "fail_fast",
                    },
                )
            )
            claim = service._claim(
                queued.task_id,
                allowed=frozenset({TaskStatus.QUEUED}),
            )
            self.assertTrue(claim.acquired)

            with self.assertRaisesRegex(
                PlanRevisionConflict,
                "task has an active execution lease",
            ):
                service.request_pause(
                    queued.task_id,
                    requested_by="test",
                )
            current = service.require_task(queued.task_id)
            self.assertIs(current.status, TaskStatus.QUEUED)
            self.assertIsNotNone(current.lease_token)

    def test_replan_rejects_a_task_claimed_after_initial_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            waiting = service.submit_plan(
                "claim before replan",
                [
                    write_step(
                        "approval",
                        "approved.txt",
                        "approved",
                        requires_approval=True,
                    )
                ],
            )
            claim = service._claim(
                waiting.task_id,
                allowed=frozenset({TaskStatus.AWAITING_APPROVAL}),
            )
            self.assertTrue(claim.acquired)

            with self.assertRaisesRegex(
                PlanRevisionConflict,
                "task still owns an execution lease",
            ):
                service.replan_pending_steps(
                    waiting.task_id,
                    [write_step("new", "new.txt", "new")],
                    expected_plan_version=waiting.plan_version,
                )
            current = service.require_task(waiting.task_id)
            self.assertIs(current.status, TaskStatus.AWAITING_APPROVAL)
            self.assertEqual(
                service.events(waiting.task_id)[-1].event_type,
                "task_claimed",
            )

    def test_waiting_state_expired_lease_is_recovered_without_state_loss(
        self,
    ) -> None:
        for status in (
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.PAUSED,
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                base_time = "2026-10-19T00:00:00.000+00:00"
                future_time = "2026-10-19T00:02:01.000+00:00"
                service = TaskService(root, clock=lambda: base_time)
                if status is TaskStatus.AWAITING_APPROVAL:
                    task = service.submit_plan(
                        "approval lease recovery",
                        [
                            write_step(
                                "approval",
                                "approved.txt",
                                "approved",
                                requires_approval=True,
                            )
                        ],
                    )
                else:
                    task = service.enqueue(
                        TaskRequest(
                            goal="paused lease recovery",
                            operation="execute_plan",
                            parameters={
                                "steps": [
                                    write_step("one", "one.txt", "one")
                                ],
                                "failure_policy": "fail_fast",
                            },
                        )
                    )
                    task = service.request_pause(
                        task.task_id,
                        requested_by="test",
                    )
                claim = service._claim(
                    task.task_id,
                    allowed=frozenset({status}),
                )
                self.assertTrue(claim.acquired)
                self.assertIsNotNone(claim.task.lease_token)

                restarted = TaskService(
                    root,
                    clock=lambda: future_time,
                    recover_leases=False,
                )
                recovered = restarted.recover_expired_leases()
                self.assertEqual(
                    [item.task_id for item in recovered],
                    [task.task_id],
                )
                current = restarted.require_task(task.task_id)
                self.assertIs(current.status, status)
                self.assertIsNone(current.lease_token)
                self.assertTrue(current.phase.endswith("_lease_expiry"))


if __name__ == "__main__":
    unittest.main()
