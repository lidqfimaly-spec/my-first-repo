from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.cli import main  # noqa: E402


class ConversationCliTests(unittest.TestCase):
    def test_create_send_idempotency_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = StringIO()
            with redirect_stdout(output):
                created_code = main(
                    [
                        "--home",
                        temp_dir,
                        "conversation",
                        "create",
                        "--conversation-id",
                        "conversation-1",
                        "--json",
                    ]
                )
            self.assertEqual(created_code, 0)
            self.assertEqual(
                json.loads(output.getvalue())["conversation_id"],
                "conversation-1",
            )

            output = StringIO()
            with redirect_stdout(output):
                first_code = main(
                    [
                        "--home",
                        temp_dir,
                        "conversation",
                        "send",
                        "conversation-1",
                        "--content",
                        "message",
                        "--client-message-id",
                        "client-1",
                        "--json",
                    ]
                )
            first = json.loads(output.getvalue())
            self.assertEqual(first_code, 0)

            output = StringIO()
            with redirect_stdout(output):
                second_code = main(
                    [
                        "--home",
                        temp_dir,
                        "conversation",
                        "send",
                        "conversation-1",
                        "--content",
                        "message",
                        "--client-message-id",
                        "client-1",
                        "--json",
                    ]
                )
            second = json.loads(output.getvalue())
            self.assertEqual(second_code, 0)
            self.assertEqual(
                first["message"]["message_id"],
                second["message"]["message_id"],
            )
            self.assertEqual(
                first["turn"]["turn_id"],
                second["turn"]["turn_id"],
            )

            output = StringIO()
            with redirect_stdout(output):
                history_code = main(
                    [
                        "--home",
                        temp_dir,
                        "conversation",
                        "history",
                        "conversation-1",
                        "--json",
                    ]
                )
            history = json.loads(output.getvalue())
            self.assertEqual(history_code, 0)
            self.assertEqual([item["content"] for item in history], ["message"])

    def test_run_can_link_task_to_conversation_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "--home",
                            temp_dir,
                            "conversation",
                            "create",
                            "--conversation-id",
                            "conversation-1",
                        ]
                    ),
                    0,
                )
            output = StringIO()
            with redirect_stdout(output):
                send_code = main(
                    [
                        "--home",
                        temp_dir,
                        "conversation",
                        "send",
                        "conversation-1",
                        "--content",
                        "turn",
                        "--client-message-id",
                        "client-1",
                        "--json",
                    ]
                )
            self.assertEqual(send_code, 0)
            turn_id = json.loads(output.getvalue())["turn"]["turn_id"]

            output = StringIO()
            with redirect_stdout(output):
                run_code = main(
                    [
                        "--home",
                        temp_dir,
                        "run",
                        "--conversation-id",
                        "conversation-1",
                        "--turn-id",
                        turn_id,
                        "--goal",
                        "linked",
                        "--artifact-path",
                        "linked.txt",
                        "--content",
                        "linked",
                        "--json",
                    ]
                )
            task = json.loads(output.getvalue())
            self.assertEqual(run_code, 0)
            self.assertEqual(task["conversation_id"], "conversation-1")

            import sqlite3

            connection = sqlite3.connect(
                Path(temp_dir) / "data" / "zevorinth.sqlite3"
            )
            try:
                linked_task_id = connection.execute(
                    "SELECT task_id FROM conversation_turns WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(linked_task_id, task["task_id"])

    def test_run_rejects_turn_from_another_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with redirect_stdout(StringIO()):
                for conversation_id in ("conversation-1", "conversation-2"):
                    self.assertEqual(
                        main(
                            [
                                "--home",
                                temp_dir,
                                "conversation",
                                "create",
                                "--conversation-id",
                                conversation_id,
                            ]
                        ),
                        0,
                    )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "--home",
                            temp_dir,
                            "conversation",
                            "send",
                            "conversation-1",
                            "--content",
                            "turn",
                            "--client-message-id",
                            "client-1",
                            "--json",
                        ]
                    ),
                    0,
                )
            turn_id = json.loads(output.getvalue())["turn"]["turn_id"]

            stderr = StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        "--home",
                        temp_dir,
                        "run",
                        "--conversation-id",
                        "conversation-2",
                        "--turn-id",
                        turn_id,
                        "--artifact-path",
                        "rejected.txt",
                        "--content",
                        "rejected",
                        "--json",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("task conversation conflict", stderr.getvalue())

    def test_conflict_and_missing_conversation_have_explicit_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "--home",
                            temp_dir,
                            "conversation",
                            "create",
                            "--conversation-id",
                            "conversation-1",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "--home",
                            temp_dir,
                            "conversation",
                            "send",
                            "conversation-1",
                            "--content",
                            "first",
                            "--client-message-id",
                            "client-1",
                        ]
                    ),
                    0,
                )

            stderr = StringIO()
            with redirect_stderr(stderr):
                conflict_code = main(
                    [
                        "--home",
                        temp_dir,
                        "conversation",
                        "send",
                        "conversation-1",
                        "--content",
                        "different",
                        "--client-message-id",
                        "client-1",
                    ]
                )
            self.assertEqual(conflict_code, 2)
            self.assertIn("message conflict", stderr.getvalue())

            stderr = StringIO()
            with redirect_stderr(stderr):
                missing_code = main(
                    [
                        "--home",
                        temp_dir,
                        "conversation",
                        "history",
                        "missing",
                        "--json",
                    ]
                )
            self.assertEqual(missing_code, 2)
            self.assertIn("conversation not found", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
