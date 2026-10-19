from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.conversation_service import ConversationService  # noqa: E402
from zevorinth.event_store import (  # noqa: E402
    ActiveTaskConflict,
    ConversationNotFound,
    EventStoreError,
    TaskLinkConflict,
)
from zevorinth.models import (  # noqa: E402
    ExecutionMode,
    TaskRequest,
    TaskStatus,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


def _request() -> TaskRequest:
    return TaskRequest(
        goal="conversation task",
        operation=WRITE_TEXT_ARTIFACT,
        parameters={"artifact_path": "conversation.txt", "content": "linked"},
    )


class TaskConversationTests(unittest.TestCase):
    def test_task_enqueue_persists_conversation_id_and_old_path_stays_null(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            conversations = ConversationService(root)
            conversation = conversations.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            tasks = TaskService(root, clock=lambda: NOW)

            linked = tasks.enqueue(
                _request(),
                conversation_id=conversation.conversation_id,
            )
            unlinked = tasks.enqueue(_request())

            self.assertEqual(linked.conversation_id, "conversation-1")
            self.assertIsNone(unlinked.conversation_id)
            self.assertEqual(
                tasks.require_task(linked.task_id).conversation_id,
                "conversation-1",
            )
            self.assertEqual(
                tasks.require_task(unlinked.task_id).conversation_id,
                None,
            )

    def test_task_and_turn_are_linked_atomically_and_task_executes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            conversations = ConversationService(root)
            conversation = conversations.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            turn, message = conversations.append_user_message(
                conversation.conversation_id,
                "create it",
                client_message_id="client-1",
                now=NOW,
            )
            tasks = TaskService(root, clock=lambda: NOW)

            task = tasks.submit(
                _request(),
                conversation_id=conversation.conversation_id,
                turn_id=turn.turn_id,
            )

            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            self.assertEqual(task.execution_mode, ExecutionMode.REAL)
            connection = sqlite3.connect(
                root / "data" / "zevorinth.sqlite3"
            )
            try:
                linked_task_id = connection.execute(
                    "SELECT task_id FROM conversation_turns WHERE turn_id = ?",
                    (turn.turn_id,),
                ).fetchone()[0]
                event_types = [
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT event_type FROM conversation_events
                        WHERE conversation_id = ?
                        ORDER BY sequence
                        """,
                        (conversation.conversation_id,),
                    )
                ]
            finally:
                connection.close()
            self.assertEqual(linked_task_id, task.task_id)
            self.assertIn("task_linked", event_types)
            self.assertEqual(
                conversations.history(conversation.conversation_id)[0].message_id,
                message.message_id,
            )

    def test_invalid_conversation_and_active_task_conflict_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            conversations = ConversationService(root)
            conversation = conversations.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            tasks = TaskService(root, clock=lambda: NOW)
            before = len(tasks.store.find_tasks_by_statuses(
                {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.SUCCEEDED}
            ))

            with self.assertRaises(ConversationNotFound):
                tasks.enqueue(_request(), conversation_id="missing")

            tasks.enqueue(
                _request(),
                conversation_id=conversation.conversation_id,
            )
            with self.assertRaises(ActiveTaskConflict):
                tasks.enqueue(
                    _request(),
                    conversation_id=conversation.conversation_id,
                )

            after = len(tasks.store.find_tasks_by_statuses(
                {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.SUCCEEDED}
            ))
            self.assertEqual(after, before + 1)

    def test_cross_conversation_turn_link_is_rejected_without_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            conversations = ConversationService(root)
            first = conversations.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            second = conversations.create_conversation(
                conversation_id="conversation-2",
                now=NOW,
            )
            turn, _ = conversations.append_user_message(
                first.conversation_id,
                "first",
                client_message_id="client-1",
                now=NOW,
            )
            tasks = TaskService(root, clock=lambda: NOW)

            with self.assertRaises(TaskLinkConflict):
                tasks.enqueue(
                    _request(),
                    conversation_id=second.conversation_id,
                    turn_id=turn.turn_id,
                )

            self.assertEqual(
                len(tasks.store.find_tasks_by_statuses(
                    {TaskStatus.QUEUED, TaskStatus.RUNNING}
                )),
                0,
            )
            connection = sqlite3.connect(
                root / "data" / "zevorinth.sqlite3"
            )
            try:
                linked_task_id = connection.execute(
                    "SELECT task_id FROM conversation_turns WHERE turn_id = ?",
                    (turn.turn_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertIsNone(linked_task_id)

    def test_turn_link_failure_after_task_insert_rolls_back_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            conversations = ConversationService(root)
            conversation = conversations.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            turn, _ = conversations.append_user_message(
                conversation.conversation_id,
                "link me",
                client_message_id="client-1",
                now=NOW,
            )
            connection = sqlite3.connect(
                root / "data" / "zevorinth.sqlite3"
            )
            try:
                connection.execute(
                    """
                    CREATE TRIGGER fail_turn_link
                    BEFORE UPDATE OF task_id ON conversation_turns
                    BEGIN
                        SELECT RAISE(ABORT, 'injected turn link failure');
                    END
                    """
                )
                connection.commit()
            finally:
                connection.close()
            tasks = TaskService(root, clock=lambda: NOW)

            with self.assertRaises(EventStoreError):
                tasks.enqueue(
                    _request(),
                    conversation_id=conversation.conversation_id,
                    turn_id=turn.turn_id,
                )

            connection = sqlite3.connect(
                root / "data" / "zevorinth.sqlite3"
            )
            try:
                task_count = connection.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0]
                linked_task_id = connection.execute(
                    "SELECT task_id FROM conversation_turns WHERE turn_id = ?",
                    (turn.turn_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(task_count, 0)
            self.assertIsNone(linked_task_id)


if __name__ == "__main__":
    unittest.main()
