from __future__ import annotations

import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.conversation_service import ConversationService  # noqa: E402
from zevorinth.event_store import MessageConflict  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


def _append_worker(
    root_text: str,
    conversation_id: str,
    content: str,
    client_message_id: str,
    barrier: object,
    queue: object,
) -> None:
    try:
        barrier.wait(timeout=10)
        service = ConversationService(Path(root_text))
        turn, message = service.append_user_message(
            conversation_id,
            content,
            client_message_id=client_message_id,
            now=NOW,
        )
        queue.put(
            ("ok", message.message_id, turn.turn_id, turn.turn_index)
        )
    except BaseException as exc:
        queue.put(("error", type(exc).__name__, str(exc)))


class ConversationServiceTests(unittest.TestCase):
    def test_service_create_append_history_and_same_content_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ConversationService(Path(temp_dir))
            service.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )

            first_turn, first_message = service.append_user_message(
                "conversation-1",
                "same",
                client_message_id="client-1",
                now=NOW,
            )
            second_turn, second_message = service.append_user_message(
                "conversation-1",
                "same",
                client_message_id="client-1",
                now=NOW,
            )

            self.assertEqual(first_message.message_id, second_message.message_id)
            self.assertEqual(first_turn.turn_id, second_turn.turn_id)
            self.assertEqual(first_turn.turn_index, 0)
            self.assertEqual(
                [item.content for item in service.history("conversation-1")],
                ["same"],
            )

    def test_service_same_key_different_content_is_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ConversationService(Path(temp_dir))
            service.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            service.append_user_message(
                "conversation-1",
                "original",
                client_message_id="client-1",
                now=NOW,
            )

            with self.assertRaises(MessageConflict):
                service.append_user_message(
                    "conversation-1",
                    "changed",
                    client_message_id="client-1",
                    now=NOW,
                )

            self.assertEqual(
                [item.content for item in service.history("conversation-1")],
                ["original"],
            )

    def test_concurrent_same_content_message_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ConversationService(root)
            service.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            results = self._run_workers(
                root,
                [
                    ("same", "client-same"),
                    ("same", "client-same"),
                ],
            )

            self.assertEqual(len(results), 2)
            self.assertTrue(all(result[0] == "ok" for result in results), results)
            self.assertEqual(len({result[1] for result in results}), 1)
            self.assertEqual(len({result[2] for result in results}), 1)
            self.assertEqual(
                len(service.history("conversation-1")),
                1,
            )

    def test_concurrent_different_keys_allocate_unique_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ConversationService(root)
            service.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            results = self._run_workers(
                root,
                [
                    ("first", "client-1"),
                    ("second", "client-2"),
                ],
            )

            self.assertTrue(all(result[0] == "ok" for result in results), results)
            self.assertEqual(len({result[1] for result in results}), 2)
            self.assertEqual(
                {result[3] for result in results},
                {0, 1},
            )
            self.assertEqual(
                {item.content for item in service.history("conversation-1")},
                {"first", "second"},
            )

    def test_concurrent_same_key_different_content_has_one_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = ConversationService(root)
            service.create_conversation(
                conversation_id="conversation-1",
                now=NOW,
            )
            results = self._run_workers(
                root,
                [
                    ("first", "client-same"),
                    ("second", "client-same"),
                ],
            )

            successes = [result for result in results if result[0] == "ok"]
            errors = [result for result in results if result[0] == "error"]
            self.assertEqual(len(successes), 1, results)
            self.assertEqual(len(errors), 1, results)
            self.assertEqual(errors[0][1], "MessageConflict")
            self.assertEqual(len(service.history("conversation-1")), 1)

    def _run_workers(
        self,
        root: Path,
        cases: list[tuple[str, str]],
    ) -> list[tuple[object, ...]]:
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(len(cases))
        queue = context.Queue()
        processes = [
            context.Process(
                target=_append_worker,
                args=(
                    str(root),
                    "conversation-1",
                    content,
                    client_message_id,
                    barrier,
                    queue,
                ),
            )
            for content, client_message_id in cases
        ]
        try:
            for process in processes:
                process.start()
            return [queue.get(timeout=15) for _ in processes]
        finally:
            for process in processes:
                if process.is_alive():
                    process.kill()
                process.join(timeout=5)
            queue.close()
            queue.join_thread()


if __name__ == "__main__":
    unittest.main()
