from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.event_store import (  # noqa: E402
    ConversationNotFound,
    EventStore,
    MessageConflict,
)


NOW = "2026-10-20T00:00:00.000+00:00"


def _new_store(path: Path) -> EventStore:
    store = EventStore(path)
    store.initialize()
    return store


class ConversationStoreTests(unittest.TestCase):
    def test_create_conversation_survives_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            store = _new_store(path)
            created = store.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
                metadata={"source": "test"},
            )

            reopened = EventStore(path)
            loaded = reopened.get_conversation(created.conversation_id)

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.status.value, "active")
            self.assertEqual(loaded.version, 1)
            self.assertEqual(loaded.created_at, NOW)
            self.assertEqual(loaded.metadata, {"source": "test"})

    def test_append_message_creates_turn_message_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            store = _new_store(path)
            store.create_conversation(conversation_id="conversation-1", now=NOW)

            turn, message = store.append_user_message(
                "conversation-1",
                "hello",
                client_message_id="client-1",
                now=NOW,
                metadata={"source": "cli"},
            )

            self.assertEqual(turn.turn_index, 0)
            self.assertEqual(turn.status.value, "received")
            self.assertEqual(message.content, "hello")
            self.assertEqual(message.role.value, "user")
            self.assertEqual(
                message.content_sha256,
                hashlib.sha256(b"hello").hexdigest(),
            )
            self.assertEqual(message.byte_count, 5)
            self.assertEqual(message.metadata, {"source": "cli"})

            history = store.list_conversation_messages("conversation-1")
            self.assertEqual([item.content for item in history], ["hello"])
            self.assertEqual(store.get_conversation("conversation-1").version, 2)

            connection = sqlite3.connect(path)
            try:
                event_types = [
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT event_type FROM conversation_events
                        WHERE conversation_id = ?
                        ORDER BY sequence
                        """,
                        ("conversation-1",),
                    )
                ]
            finally:
                connection.close()
            self.assertEqual(event_types, ["conversation_created", "message_recorded"])

    def test_history_is_ordered_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            store = _new_store(path)
            store.create_conversation(conversation_id="conversation-1", now=NOW)
            store.append_user_message(
                "conversation-1", "first", client_message_id="c1", now=NOW
            )
            store.append_user_message(
                "conversation-1", "second", client_message_id="c2", now=NOW
            )

            history = store.list_conversation_messages("conversation-1", limit=2)
            limited = store.list_conversation_messages("conversation-1", limit=1)

            self.assertEqual([item.content for item in history], ["first", "second"])
            self.assertEqual([item.content for item in limited], ["first"])
            with self.assertRaises(ValueError):
                store.list_conversation_messages(
                    "conversation-1",
                    limit=EventStore.MAX_CONVERSATION_HISTORY + 1,
                )

    def test_duplicate_client_message_is_rejected_without_extra_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            store = _new_store(path)
            store.create_conversation(conversation_id="conversation-1", now=NOW)
            store.append_user_message(
                "conversation-1", "first", client_message_id="same", now=NOW
            )

            with self.assertRaises(MessageConflict):
                store.append_user_message(
                    "conversation-1",
                    "second",
                    client_message_id="same",
                    now=NOW,
                )

            self.assertEqual(
                [item.content for item in store.list_conversation_messages("conversation-1")],
                ["first"],
            )
            connection = sqlite3.connect(path)
            try:
                turn_count = connection.execute(
                    "SELECT COUNT(*) FROM conversation_turns WHERE conversation_id = ?",
                    ("conversation-1",),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(turn_count, 1)

    def test_append_requires_existing_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            store = _new_store(path)

            with self.assertRaises(ConversationNotFound):
                store.append_user_message(
                    "missing", "hello", client_message_id="c1", now=NOW
                )

    def test_append_failure_rolls_back_turn_and_message(self) -> None:
        class FailingEventStore(EventStore):
            def _insert_conversation_event(self, *args, **kwargs):
                raise RuntimeError("injected conversation event failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            seed = _new_store(path)
            seed.create_conversation(conversation_id="conversation-1", now=NOW)
            failing = FailingEventStore(path)
            failing.initialize()

            with self.assertRaises(RuntimeError):
                failing.append_user_message(
                    "conversation-1", "will roll back",
                    client_message_id="c-fail", now=NOW,
                )

            connection = sqlite3.connect(path)
            try:
                turn_count = connection.execute(
                    "SELECT COUNT(*) FROM conversation_turns WHERE conversation_id = ?",
                    ("conversation-1",),
                ).fetchone()[0]
                message_count = connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                    ("conversation-1",),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(turn_count, 0)
            self.assertEqual(message_count, 0)


if __name__ == "__main__":
    unittest.main()
