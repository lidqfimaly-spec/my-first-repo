from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from queue import Queue
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.context import ContextItemKind  # noqa: E402
from zevorinth.conversation_service import ConversationService  # noqa: E402
from zevorinth.event_store import (  # noqa: E402
    EventStore,
    EventStoreError,
    PlanRevisionConflict,
)
from zevorinth.models import (  # noqa: E402
    EventType,
    TaskRequest,
    TaskStatus,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


def _request(content: str | None = "content") -> TaskRequest:
    return TaskRequest(
        goal="持续任务",
        operation=WRITE_TEXT_ARTIFACT,
        parameters={
            "artifact_path": "result.txt",
            "content": content,
        },
    )


class ContextContinuityTests(unittest.TestCase):
    def test_context_survives_restart_and_summary_omission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ConversationService(root)
            service.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            turn, _ = service.append_user_message(
                "conversation-1",
                "分析数据",
                client_message_id="m1",
                now=NOW,
            )
            service.append_user_message(
                "conversation-1",
                "不要覆盖原文件，改成新文件名",
                client_message_id="m2",
                now=NOW,
            )
            service.append_user_message(
                "conversation-1",
                "再加上一个 CSV 文件",
                client_message_id="m3",
                now=NOW,
            )
            task = service.task_service.enqueue(
                _request(),
                conversation_id="conversation-1",
                turn_id=turn.turn_id,
            )

            context = service.context(
                "conversation-1",
                summary="摘要遗漏了 CSV 与文件名约束",
            )
            self.assertEqual(len(context.original_messages), 3)
            self.assertEqual(context.current_task.task_id, task.task_id)
            self.assertEqual(context.goal, "持续任务")
            self.assertEqual(len(context.constraints), 2)
            self.assertTrue(
                all(
                    item.kind is ContextItemKind.USER_REQUIREMENT
                    and item.confirmed
                    for item in context.constraints
                )
            )
            self.assertFalse(context.summary_authoritative)
            self.assertIn("CSV", context.constraints[1].text)
            self.assertTrue(
                any(source.startswith("message:") for source in context.sources)
            )
            self.assertTrue(
                any(source.startswith("task:") for source in context.sources)
            )

            reopened = ConversationService(root).context("conversation-1")
            self.assertEqual(
                reopened.to_dict(),
                service.context("conversation-1").to_dict(),
            )
            self.assertIn("CSV", context.to_dict()["original_messages"][2]["content"])

    def test_versioned_plan_revision_is_idempotent_and_stale_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir), clock=lambda: NOW)
            task = service.enqueue(_request(content=None))

            first = service.revise_plan(
                task.task_id,
                {"parameters": {"content": "v1"}},
                source_id="message-1",
                expected_plan_version=0,
                confirmed=True,
            )
            self.assertEqual(first.plan_version, 1)
            self.assertEqual(first.status, TaskStatus.QUEUED)
            self.assertEqual(first.plan_revisions[0].from_version, 0)
            self.assertEqual(first.plan_revisions[0].to_version, 1)

            second = service.revise_plan(
                task.task_id,
                {"parameters": {"artifact_path": "v2.txt"}},
                source_id="message-2",
                expected_plan_version=1,
                confirmed=True,
            )
            self.assertEqual(second.plan_version, 2)
            self.assertEqual(len(second.plan_revisions), 2)
            self.assertEqual(second.request.parameters["content"], "v1")

            duplicate = service.revise_plan(
                task.task_id,
                {"parameters": {"artifact_path": "ignored.txt"}},
                source_id="message-2",
                expected_plan_version=0,
                confirmed=True,
            )
            self.assertEqual(duplicate.plan_version, 2)
            with self.assertRaises(PlanRevisionConflict):
                service.revise_plan(
                    task.task_id,
                    {"parameters": {"content": "stale"}},
                    source_id="message-3",
                    expected_plan_version=0,
                    confirmed=True,
                )
            events = service.events(task.task_id)
            self.assertEqual(
                len(
                    [
                        event
                        for event in events
                        if event.event_type == EventType.PLAN_REVISED.value
                    ]
                ),
                2,
            )
            reopened = TaskService(Path(temp_dir)).require_task(task.task_id)
            self.assertEqual(reopened.plan_version, 2)
            self.assertEqual(len(reopened.plan_revisions), 2)
            self.assertEqual(
                reopened.plan_revisions[-1].source_id,
                "message-2",
            )

    def test_revision_rolls_back_when_event_write_fails(self) -> None:
        class FailingStore(EventStore):
            def _insert_event(self, connection, event):
                if event.event_type == EventType.PLAN_REVISED.value:
                    raise EventStoreError("injected plan revision failure")
                return super()._insert_event(connection, event)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, clock=lambda: NOW)
            task = service.enqueue(_request(content=None))
            service.store = FailingStore(
                root / "data" / "zevorinth.sqlite3"
            )

            with self.assertRaises(EventStoreError):
                service.revise_plan(
                    task.task_id,
                    {"parameters": {"content": "should roll back"}},
                    source_id="message-fail",
                    expected_plan_version=0,
                    confirmed=True,
                )

            unchanged = service.require_task(task.task_id)
            self.assertEqual(unchanged.plan_version, 0)
            self.assertIsNone(unchanged.request.parameters["content"])
            self.assertEqual(unchanged.plan_revisions, [])

    def test_execution_uses_immutable_plan_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_ids: list[str] = []

            def interrupt(_stage: str, task) -> None:
                task_ids.append(task.task_id)
                raise RuntimeError("stop before verification")

            service = TaskService(root, fault_hook=interrupt)
            with self.assertRaises(RuntimeError):
                service.submit(_request())
            task = service.require_task(task_ids[0])
            self.assertEqual(
                task.execution_plan_version,
                task.plan_version,
            )
            self.assertEqual(
                task.execution_plan_snapshot,
                task.plan.to_dict(),
            )
            with self.assertRaises(PlanRevisionConflict):
                service.revise_plan(
                    task.task_id,
                    {"parameters": {"content": "changed"}},
                    source_id="late-revision",
                    expected_plan_version=task.plan_version,
                    confirmed=True,
                )

            assert task.lease_expires_at is not None
            recovery_time = (
                datetime.fromisoformat(task.lease_expires_at)
                + timedelta(seconds=1)
            ).isoformat(timespec="milliseconds")
            recovered = TaskService(
                root,
                clock=lambda: recovery_time,
            ).resume(task.task_id)
            self.assertEqual(recovered.status, TaskStatus.SUCCEEDED)
            self.assertEqual(recovered.plan_version, 0)

    def test_plan_snapshot_mismatch_cannot_pass_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(
                Path(temp_dir),
                clock=lambda: NOW,
            )

            def mutate_plan(_stage: str, task) -> None:
                service.store.record_event_and_update(
                    task.task_id,
                    "test_plan_mutation",
                    NOW,
                    updates={"plan_version": task.plan_version + 1},
                    lease_token=task.lease_token,
                )

            service.fault_hook = mutate_plan
            task = service.submit(_request())

            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(
                task.error.code,
                "execution_plan_snapshot_changed",
            )
            event_types = [
                event.event_type for event in service.events(task.task_id)
            ]
            self.assertNotIn(EventType.TASK_SUCCEEDED.value, event_types)

    def test_concurrent_revisions_have_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed = TaskService(root, clock=lambda: NOW)
            task = seed.enqueue(_request(content=None))
            barrier = threading.Barrier(2)
            results: Queue[tuple[str, str]] = Queue()

            def revise(source: str, content: str) -> None:
                service = TaskService(root, clock=lambda: NOW)
                barrier.wait(timeout=5)
                try:
                    revised = service.revise_plan(
                        task.task_id,
                        {"parameters": {"content": content}},
                        source_id=source,
                        expected_plan_version=0,
                        confirmed=True,
                    )
                    results.put(("ok", str(revised.plan_version)))
                except PlanRevisionConflict:
                    results.put(("conflict", "0"))

            workers = [
                threading.Thread(target=revise, args=("source-a", "A")),
                threading.Thread(target=revise, args=("source-b", "B")),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

            outcomes = [results.get(timeout=2), results.get(timeout=2)]
            self.assertEqual(
                sorted(outcome[0] for outcome in outcomes),
                ["conflict", "ok"],
            )
            final = TaskService(root).require_task(task.task_id)
            self.assertEqual(final.plan_version, 1)
            self.assertEqual(len(final.plan_revisions), 1)
            self.assertIn(final.request.parameters["content"], {"A", "B"})

    def test_revision_rejects_task_with_execution_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            completed = service.submit(_request())
            self.assertEqual(completed.status, TaskStatus.SUCCEEDED)
            with self.assertRaises(PlanRevisionConflict):
                service.revise_plan(
                    completed.task_id,
                    {"parameters": {"content": "new"}},
                    source_id="message-after",
                    expected_plan_version=0,
                    confirmed=True,
                )

    def test_context_includes_applied_clarification_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ConversationService(Path(temp_dir))
            service.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            created = service.process_message(
                "conversation-1",
                "创建一个文件",
                client_message_id="m1",
                now=NOW,
            )
            operation = next(
                question
                for question in created.clarifications
                if question.field_name == "operation"
            )
            after_operation = service.process_message(
                "conversation-1",
                "write_text_artifact",
                client_message_id="m2",
                answer_to=operation.question_id,
                now=NOW,
            )
            path_question = next(
                question
                for question in after_operation.clarifications
                if question.field_name == "artifact_path"
            )
            after_path = service.process_message(
                "conversation-1",
                "context.txt",
                client_message_id="m3",
                answer_to=path_question.question_id,
                now=NOW,
            )
            content_question = next(
                question
                for question in after_path.clarifications
                if question.field_name == "content"
            )
            completed = service.process_message(
                "conversation-1",
                "context content",
                client_message_id="m4",
                answer_to=content_question.question_id,
                now=NOW,
            )
            assert completed.task is not None
            service.recover_task(completed.task.task_id, now=NOW)

            context = service.context("conversation-1")

            self.assertEqual(len(context.confirmed_decisions), 3)
            self.assertTrue(
                all(item.confirmed for item in context.confirmed_decisions)
            )
            self.assertEqual(
                context.current_task.status,
                TaskStatus.SUCCEEDED,
            )

    def test_cancel_refuses_active_lease_and_cancels_idle_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir), clock=lambda: NOW)
            idle = service.enqueue(_request())
            cancelled = service.cancel(idle.task_id)
            self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
            self.assertIn(
                EventType.TASK_CANCELLED.value,
                [event.event_type for event in service.events(idle.task_id)],
            )

            active = service.enqueue(_request())
            claim = service.store.claim_task(
                active.task_id,
                owner_id="owner",
                lease_token="lease",
                attempt_id="79a0f570-1940-4fdd-97d1-9f0b4331464f",
                now=NOW,
                expires_at="2026-10-20T00:01:00.000+00:00",
                allowed_statuses=TaskService.RUNNABLE_STATUSES,
            )
            self.assertTrue(claim.acquired)
            with self.assertRaises(PlanRevisionConflict):
                service.cancel(active.task_id)
            self.assertEqual(
                service.require_task(active.task_id).status,
                TaskStatus.QUEUED,
            )

    def test_cancel_request_is_explicit_and_does_not_cancel_automatically(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ConversationService(Path(temp_dir))
            service.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            created = service.process_message(
                "conversation-1",
                "创建一个文件",
                client_message_id="m1",
                now=NOW,
            )
            result = service.process_message(
                "conversation-1",
                "取消刚才的任务，改为分析另一份数据",
                client_message_id="m2",
                now=NOW,
            )

            self.assertEqual(result.outcome, "requires_cancel_confirmation")
            self.assertIsNotNone(created.task)
            self.assertEqual(
                service.task_service.require_task(created.task.task_id).status,
                TaskStatus.AWAITING_CLARIFICATION,
            )


if __name__ == "__main__":
    unittest.main()
