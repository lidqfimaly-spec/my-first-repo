from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import RealArtifactWriter  # noqa: E402
from zevorinth.models import (  # noqa: E402
    EventType,
    ExecutionMode,
    TaskRequest,
    TaskStatus,
)
from zevorinth.planner import (  # noqa: E402
    WRITE_TEXT_ARTIFACT,
    DeterministicPlanner,
)
from zevorinth.policy import ArtifactPolicy  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.verifier import ArtifactVerifier  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


def _request() -> TaskRequest:
    return TaskRequest(
        goal="cooperative cancellation",
        operation=WRITE_TEXT_ARTIFACT,
        parameters={
            "artifact_path": "cancel.txt",
            "content": "cancel content\n",
        },
    )


class CancellingPlanner:
    mode = DeterministicPlanner.mode
    name = "cancelling-planner"

    def __init__(self, service: TaskService, task_id: str) -> None:
        self.service = service
        self.task_id = task_id
        self.delegate = DeterministicPlanner()

    def plan_request(self, request: TaskRequest):
        self.service.request_cancel(
            self.task_id,
            requested_by="planner-test",
            reason="cancel after planning",
        )
        return self.delegate.plan_request(request)


class CancellingPolicy(ArtifactPolicy):
    def __init__(self, service: TaskService, task_id: str) -> None:
        self.service = service
        self.task_id = task_id

    def evaluate(self, **kwargs: Any):
        self.service.request_cancel(
            self.task_id,
            requested_by="policy-test",
            reason="cancel before tool",
        )
        return super().evaluate(**kwargs)


class CancellingVerifier:
    name = "cancelling-verifier"

    def __init__(self, service: TaskService, task_id: str, root: Path) -> None:
        self.service = service
        self.task_id = task_id
        self.delegate = ArtifactVerifier(root)

    def verify(self, **kwargs: Any):
        self.service.request_cancel(
            self.task_id,
            requested_by="verifier-test",
            reason="cancel before success",
        )
        return self.delegate.verify(**kwargs)


class CancelAfterWriteWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self, service: TaskService, task_id: str, root: Path) -> None:
        self.service = service
        self.task_id = task_id
        self.delegate = RealArtifactWriter(root)

    def write(self, **kwargs: Any):
        result = self.delegate.write(**kwargs)
        self.service.request_cancel(
            self.task_id,
            requested_by="writer-test",
            reason="cancel after tool",
        )
        return result


class CooperativeCancellationTests(unittest.TestCase):
    def test_request_is_idempotent_and_executor_cancels_at_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, clock=lambda: NOW)
            task = service.enqueue(_request())
            first = service.request_cancel(
                task.task_id,
                requested_by="user",
                reason="stop",
            )
            second = service.request_cancel(
                task.task_id,
                requested_by="other",
                reason="duplicate",
            )
            self.assertEqual(first.cancel_requested_at, NOW)
            self.assertEqual(second.cancel_requested_at, NOW)
            self.assertEqual(second.cancel_requested_by, "user")
            events = service.events(task.task_id)
            self.assertEqual(
                [
                    event.event_type
                    for event in events
                    if event.event_type == EventType.CANCEL_REQUESTED.value
                ],
                [EventType.CANCEL_REQUESTED.value],
            )

            cancelled = service.execute_task(task.task_id)
            self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
            self.assertEqual(
                [
                    event.event_type
                    for event in service.events(task.task_id)
                    if event.event_type == EventType.TOOL_STARTED.value
                ],
                [],
            )

    def test_cancel_after_planning_and_before_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir), clock=lambda: NOW)
            planned = service.enqueue(_request())
            service.planner = CancellingPlanner(service, planned.task_id)
            after_planning = service.execute_task(planned.task_id)
            self.assertEqual(after_planning.status, TaskStatus.CANCELLED)
            self.assertEqual(after_planning.phase, "cancelled_after_planning")
            self.assertNotIn(
                EventType.TOOL_STARTED.value,
                [event.event_type for event in service.events(planned.task_id)],
            )

            before_tool = service.enqueue(_request())
            service.planner = DeterministicPlanner()
            service.policy = CancellingPolicy(service, before_tool.task_id)
            cancelled = service.execute_task(before_tool.task_id)
            self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
            self.assertEqual(cancelled.phase, "cancelled_before_tool")
            self.assertNotIn(
                EventType.TOOL_STARTED.value,
                [event.event_type for event in service.events(before_tool.task_id)],
            )

    def test_cancel_before_publish_leaves_no_final_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, clock=lambda: NOW)
            task = service.enqueue(_request())

            def checkpoint(
                stage: str,
                _task_id: str,
                _attempt_id: str,
                _generation: int | None,
            ) -> None:
                if stage == "after_staging_write":
                    service.request_cancel(
                        task.task_id,
                        requested_by="publish-test",
                        reason="cancel before publish",
                    )

            service.writer = RealArtifactWriter(
                root / "artifacts",
                checkpoint_hook=checkpoint,
            )
            cancelled = service.execute_task(task.task_id)

            self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
            self.assertEqual(cancelled.phase, "cancelled_before_publish")
            self.assertFalse(
                (root / "artifacts" / task.task_id / "cancel.txt").exists()
            )
            event_types = [
                event.event_type for event in service.events(task.task_id)
            ]
            self.assertNotIn(EventType.TOOL_SUCCEEDED.value, event_types)
            self.assertIn(EventType.TASK_CANCELLED.value, event_types)

    def test_cancel_after_tool_records_side_effect_without_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, clock=lambda: NOW)
            task = service.enqueue(_request())
            service.writer = CancelAfterWriteWriter(
                service,
                task.task_id,
                root / "artifacts",
            )
            cancelled = service.execute_task(task.task_id)

            self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
            self.assertEqual(cancelled.phase, "cancelled_after_tool")
            self.assertIsNotNone(cancelled.tool_result)
            self.assertIsNotNone(cancelled.artifact)
            self.assertEqual(
                (root / "artifacts" / task.task_id / "cancel.txt").read_text(),
                "cancel content\n",
            )
            event_types = [
                event.event_type for event in service.events(task.task_id)
            ]
            self.assertIn(EventType.TOOL_SUCCEEDED.value, event_types)
            self.assertNotIn(EventType.TASK_SUCCEEDED.value, event_types)

    def test_cancel_before_verification_and_before_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, clock=lambda: NOW)
            before_verify = service.enqueue(_request())

            def interrupt(stage: str, task) -> None:
                if stage == "after_write_before_verification":
                    service.request_cancel(
                        task.task_id,
                        requested_by="verify-boundary",
                        reason="cancel before verification",
                    )

            service.fault_hook = interrupt
            verified = service.execute_task(before_verify.task_id)
            self.assertEqual(verified.status, TaskStatus.CANCELLED)
            self.assertEqual(
                verified.phase,
                "cancelled_before_verification",
            )
            self.assertNotIn(
                EventType.VERIFICATION_COMPLETED.value,
                [event.event_type for event in service.events(before_verify.task_id)],
            )

            service.fault_hook = None
            before_success = service.enqueue(_request())
            service.verifier = CancellingVerifier(
                service,
                before_success.task_id,
                root / "artifacts",
            )
            final = service.execute_task(before_success.task_id)
            self.assertEqual(final.status, TaskStatus.CANCELLED)
            self.assertEqual(final.phase, "cancelled_before_success")
            event_types = [
                event.event_type
                for event in service.events(before_success.task_id)
            ]
            self.assertIn(EventType.VERIFICATION_COMPLETED.value, event_types)
            self.assertNotIn(EventType.TASK_SUCCEEDED.value, event_types)


if __name__ == "__main__":
    unittest.main()
