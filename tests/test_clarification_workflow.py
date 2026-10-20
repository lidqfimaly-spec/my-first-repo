from __future__ import annotations

import json
import multiprocessing
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.conversation_service import ConversationService  # noqa: E402
from zevorinth.event_store import ClarificationConflict, EventStore  # noqa: E402
from zevorinth.models import (  # noqa: E402
    ClarificationAnswerStatus,
    IntentKind,
    TaskStatus,
)


NOW = "2026-10-20T00:00:00.000+00:00"


def _answer_worker(
    root_text: str,
    question_id: str,
    message_id: str,
    barrier: object,
    queue: object,
) -> None:
    try:
        barrier.wait(timeout=10)
        store = EventStore(Path(root_text) / "data" / "zevorinth.sqlite3")
        store.initialize()
        store.record_clarification_answer(
            question_id=question_id,
            message_id=message_id,
            normalized_value="answer",
            now=NOW,
        )
        queue.put(("ok", None))
    except BaseException as exc:
        queue.put(("error", type(exc).__name__))


class ClarificationWorkflowTests(unittest.TestCase):
    def test_new_task_to_clarification_to_verified_completion(self) -> None:
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
            self.assertEqual(created.outcome, "awaiting_clarification")
            self.assertIsNotNone(created.task)
            self.assertGreaterEqual(len(created.clarifications), 1)

            operation = next(
                item
                for item in created.clarifications
                if item.field_name == "operation"
            )
            after_operation = service.process_message(
                "conversation-1",
                "write_text_artifact",
                client_message_id="m2",
                answer_to=operation.question_id,
                now=NOW,
            )
            self.assertEqual(after_operation.outcome, "awaiting_clarification")

            path_question = next(
                item
                for item in after_operation.clarifications
                if item.field_name == "artifact_path"
            )
            after_path = service.process_message(
                "conversation-1",
                "result.txt",
                client_message_id="m3",
                answer_to=path_question.question_id,
                now=NOW,
            )
            self.assertEqual(after_path.outcome, "awaiting_clarification")

            content_question = next(
                item
                for item in after_path.clarifications
                if item.field_name == "content"
            )
            completed = service.process_message(
                "conversation-1",
                "verified content",
                client_message_id="m4",
                answer_to=content_question.question_id,
                now=NOW,
            )
            self.assertEqual(completed.outcome, "completed")
            assert completed.task is not None
            self.assertEqual(completed.task.status, TaskStatus.SUCCEEDED)
            self.assertEqual(
                len(service.history("conversation-1")),
                4,
            )

            repeated = service.process_message(
                "conversation-1",
                "verified content",
                client_message_id="m4",
                answer_to=content_question.question_id,
                now=NOW,
            )
            self.assertEqual(repeated.outcome, "already_processed")
            self.assertEqual(repeated.message.message_id, completed.message.message_id)

            connection = sqlite3.connect(
                Path(temp_dir) / "data" / "zevorinth.sqlite3"
            )
            try:
                task_count = connection.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0]
                answer_count = connection.execute(
                    "SELECT COUNT(*) FROM clarification_answers"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(task_count, 1)
            self.assertEqual(answer_count, 3)

    def test_question_must_match_active_conversation_and_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ConversationService(Path(temp_dir))
            service.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            service.create_conversation(
                conversation_id="conversation-2",
                now=NOW,
            )
            created = service.process_message(
                "conversation-1",
                "创建一个文件",
                client_message_id="m1",
                now=NOW,
            )
            question = created.clarifications[0]

            result = service.process_message(
                "conversation-2",
                "write_text_artifact",
                client_message_id="m2",
                answer_to=question.question_id,
                now=NOW,
            )
            self.assertEqual(result.outcome, "ambiguous")
            self.assertEqual(
                len(service.history("conversation-1")),
                1,
            )
            self.assertEqual(
                len(service.history("conversation-2")),
                1,
            )

    def test_answers_do_not_duplicate_remaining_open_questions(self) -> None:
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
                item
                for item in created.clarifications
                if item.field_name == "operation"
            )
            after_operation = service.process_message(
                "conversation-1",
                "write_text_artifact",
                client_message_id="m2",
                answer_to=operation.question_id,
                now=NOW,
            )
            replayed_operation = service.process_message(
                "conversation-1",
                "write_text_artifact",
                client_message_id="m2",
                answer_to=operation.question_id,
                now=NOW,
            )
            self.assertEqual(
                replayed_operation.outcome,
                "already_processed",
            )
            assert created.task is not None
            self.assertEqual(
                len(
                    service.store.list_clarification_requests(
                        created.task.task_id
                    )
                ),
                3,
            )
            path_question = next(
                item
                for item in after_operation.clarifications
                if item.field_name == "artifact_path"
            )
            after_path = service.process_message(
                "conversation-1",
                "result.txt",
                client_message_id="m3",
                answer_to=path_question.question_id,
                now=NOW,
            )
            content_question = next(
                item
                for item in after_path.clarifications
                if item.field_name == "content"
            )
            completed = service.process_message(
                "conversation-1",
                "verified",
                client_message_id="m4",
                answer_to=content_question.question_id,
                now=NOW,
            )

            self.assertEqual(completed.outcome, "completed")
            assert created.task is not None
            questions = service.store.list_clarification_requests(
                created.task.task_id
            )
            self.assertEqual(
                [question.field_name for question in questions],
                ["operation", "artifact_path", "content"],
            )
            self.assertEqual(
                [question.status.value for question in questions],
                ["answered", "answered", "answered"],
            )

    def test_status_query_does_not_execute_or_create_task(self) -> None:
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
            self.assertEqual(created.outcome, "awaiting_clarification")

            status = service.process_message(
                "conversation-1",
                "状态",
                client_message_id="m2",
                now=NOW,
            )
            self.assertEqual(status.outcome, "status")
            self.assertEqual(status.intent.kind, IntentKind.STATUS_QUERY)
            self.assertIsNotNone(status.task)

    def test_duplicate_answer_conflict_is_explicit(self) -> None:
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
            question = created.clarifications[0]

            with self.assertRaises(ClarificationConflict):
                service.store.record_clarification_answer(
                    question_id=question.question_id,
                    message_id=service.history("conversation-1")[0].message_id,
                    normalized_value="first",
                )
                service.store.record_clarification_answer(
                    question_id=question.question_id,
                    message_id=service.history("conversation-1")[0].message_id,
                    normalized_value="second",
                )

    def test_answer_from_another_conversation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ConversationService(Path(temp_dir))
            service.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            service.create_conversation(
                conversation_id="conversation-2",
                now=NOW,
            )
            created = service.process_message(
                "conversation-1",
                "创建一个文件",
                client_message_id="m1",
                now=NOW,
            )
            _, other_message = service.append_user_message(
                "conversation-2",
                "wrong",
                client_message_id="other-1",
                now=NOW,
            )

            with self.assertRaises(ClarificationConflict):
                service.store.record_clarification_answer(
                    question_id=created.clarifications[0].question_id,
                    message_id=other_message.message_id,
                    normalized_value="wrong",
                    now=NOW,
                )

    def test_clarification_events_carry_question_association(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ConversationService(root)

            for index, (status, terminal_event) in enumerate(
                (
                    (
                        ClarificationAnswerStatus.APPLIED,
                        "clarification_applied",
                    ),
                    (
                        ClarificationAnswerStatus.REJECTED,
                        "clarification_rejected",
                    ),
                ),
                start=1,
            ):
                with self.subTest(status=status.value):
                    conversation_id = f"conversation-{index}"
                    service.create_conversation(
                        conversation_id=conversation_id,
                        now=NOW,
                    )
                    created = service.process_message(
                        conversation_id,
                        "创建一个文件",
                        client_message_id=f"m{index}",
                        now=NOW,
                    )
                    question = created.clarifications[0]
                    message = service.history(conversation_id)[0]
                    answer = service.store.record_clarification_answer(
                        question_id=question.question_id,
                        message_id=message.message_id,
                        normalized_value="answer",
                        now=NOW,
                    )
                    service.store.mark_clarification_answer(
                        answer.answer_id,
                        status,
                        now=NOW,
                    )

                    connection = sqlite3.connect(
                        root / "data" / "zevorinth.sqlite3"
                    )
                    connection.row_factory = sqlite3.Row
                    try:
                        rows = connection.execute(
                            """
                            SELECT event_type, turn_id, task_id, payload_json
                            FROM conversation_events
                            WHERE conversation_id = ?
                              AND event_type IN (
                                  'clarification_answered', ?
                              )
                            ORDER BY sequence
                            """,
                            (conversation_id, terminal_event),
                        ).fetchall()
                    finally:
                        connection.close()

                    self.assertEqual(
                        [row["event_type"] for row in rows],
                        ["clarification_answered", terminal_event],
                    )
                    for row in rows:
                        self.assertEqual(row["turn_id"], question.turn_id)
                        self.assertEqual(row["task_id"], question.task_id)
                        payload = json.loads(row["payload_json"])
                        self.assertEqual(
                            payload["question_id"],
                            question.question_id,
                        )
                        self.assertEqual(
                            payload["answer_id"],
                            answer.answer_id,
                        )

    def test_concurrent_answers_accept_only_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ConversationService(root)
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
            question_id = created.clarifications[0].question_id
            _, first = service.append_user_message(
                "conversation-1",
                "first",
                client_message_id="answer-1",
                now=NOW,
            )
            _, second = service.append_user_message(
                "conversation-1",
                "second",
                client_message_id="answer-2",
                now=NOW,
            )
            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            queue = context.Queue()
            processes = [
                context.Process(
                    target=_answer_worker,
                    args=(
                        str(root),
                        question_id,
                        message.message_id,
                        barrier,
                        queue,
                    ),
                )
                for message in (first, second)
            ]
            try:
                for process in processes:
                    process.start()
                results = [queue.get(timeout=15), queue.get(timeout=15)]
                for process in processes:
                    process.join(timeout=10)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.kill()
                    process.join(timeout=5)
                queue.close()
                queue.join_thread()

            self.assertEqual(
                sorted(result[0] for result in results),
                ["error", "ok"],
            )
            connection = sqlite3.connect(root / "data" / "zevorinth.sqlite3")
            try:
                answer_count = connection.execute(
                    "SELECT COUNT(*) FROM clarification_answers"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(answer_count, 1)

    def test_recovery_marks_pending_answer_applied_after_task_success(self) -> None:
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
            question = next(
                item
                for item in created.clarifications
                if item.field_name == "operation"
            )
            message = service.history("conversation-1")[0]
            answer = service.store.record_clarification_answer(
                question_id=question.question_id,
                message_id=message.message_id,
                normalized_value="write_text_artifact",
                now=NOW,
            )
            assert created.task is not None
            service.task_service.resume(
                created.task.task_id,
                {"operation": "write_text_artifact"},
            )
            service.task_service.resume(
                created.task.task_id,
                {"parameters": {"artifact_path": "recovered.txt"}},
            )
            completed = service.task_service.resume(
                created.task.task_id,
                {"parameters": {"content": "recovered"}},
            )
            self.assertEqual(completed.status, TaskStatus.SUCCEEDED)

            recovered = service.recover_task(created.task.task_id, now=NOW)
            self.assertEqual(recovered.status, TaskStatus.SUCCEEDED)
            answers = service.store.list_clarification_answers(
                question.question_id
            )
            self.assertEqual(len(answers), 1)
            self.assertEqual(
                answers[0].validation_status.value,
                "applied",
            )


if __name__ == "__main__":
    unittest.main()
