from __future__ import annotations

import json
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import RealArtifactWriter  # noqa: E402
from zevorinth.conversation_service import ConversationService  # noqa: E402
from zevorinth.event_store import (  # noqa: E402
    ClaimLost,
    EventStore,
    MessageConflict,
)
from zevorinth.models import (  # noqa: E402
    ClarificationAnswerStatus,
    EventType,
    ExecutionMode,
    TaskRequest,
    TaskStatus,
    ToolResult,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


CONVERSATION_ID = "crash-consistency"
CLIENT_MESSAGE_ID = "crash-message-1"
ANSWER_CLIENT_MESSAGE_ID = "crash-message-2"
MESSAGE_CONTENT = "create crash consistency proof"
ARTIFACT_PATH = "crash-proof.txt"
ARTIFACT_CONTENT = "durable after crash\n"
WORKER = PROJECT_ROOT / "tests" / "crash_consistency_worker.py"


class NoWriteWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self) -> None:
        self.calls = 0

    def write(self, **_: Any) -> ToolResult:
        self.calls += 1
        raise AssertionError("recovery must not invoke the artifact writer")


class CountingWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self, artifacts_root: Path) -> None:
        self.delegate = RealArtifactWriter(artifacts_root)
        self.calls = 0

    def write(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        return self.delegate.write(**kwargs)


class RaisingVerifier:
    name = "raising-test-verifier"

    def verify(self, **_: Any):
        raise RuntimeError("controlled verifier failure")


class InvalidResultVerifier:
    name = "invalid-result-test-verifier"

    def verify(self, **_: Any):
        return object()


class CrashConsistencyTests(unittest.TestCase):
    def _run_until_checkpoint(
        self,
        mode: str,
        root: Path,
    ) -> dict[str, Any]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.Popen(
            [sys.executable, str(WORKER), mode, str(root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        selector = selectors.DefaultSelector()
        try:
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=15):
                process.kill()
                _, stderr = process.communicate(timeout=5)
                self.fail(f"worker {mode} did not reach its checkpoint: {stderr}")
            line = process.stdout.readline()
            if not line:
                _, stderr = process.communicate(timeout=5)
                self.fail(f"worker {mode} exited before checkpoint: {stderr}")
            checkpoint = json.loads(line)

            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(stderr, "")
            return checkpoint
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def _db(self, root: Path) -> Path:
        return root / "data" / "zevorinth.sqlite3"

    def _count(
        self,
        root: Path,
        table: str,
        where: str = "",
        params: tuple[object, ...] = (),
    ) -> int:
        connection = sqlite3.connect(self._db(root))
        try:
            query = f"SELECT COUNT(*) FROM {table}"
            if where:
                query += f" WHERE {where}"
            return int(connection.execute(query, params).fetchone()[0])
        finally:
            connection.close()

    def _assert_database_consistent(self, root: Path) -> None:
        connection = sqlite3.connect(self._db(root))
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(integrity, [("ok",)])
        self.assertEqual(foreign_keys, [])

    def _conversation_event_types(self, root: Path) -> list[str]:
        connection = sqlite3.connect(self._db(root))
        try:
            return [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT event_type FROM conversation_events
                    WHERE conversation_id = ?
                    ORDER BY sequence
                    """,
                    (CONVERSATION_ID,),
                )
            ]
        finally:
            connection.close()

    def _task_event_types(self, root: Path, task_id: str) -> list[str]:
        connection = sqlite3.connect(self._db(root))
        try:
            return [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT event_type FROM events
                    WHERE task_id = ?
                    ORDER BY sequence
                    """,
                    (task_id,),
                )
            ]
        finally:
            connection.close()

    def _process_request(
        self,
        service: ConversationService,
        *,
        content: str = MESSAGE_CONTENT,
        client_message_id: str = CLIENT_MESSAGE_ID,
    ):
        return service.process_message(
            CONVERSATION_ID,
            content,
            client_message_id=client_message_id,
            operation=WRITE_TEXT_ARTIFACT,
            artifact_path=ARTIFACT_PATH,
            task_content=ARTIFACT_CONTENT,
            now="2026-10-20T00:00:00.000+00:00",
        )

    def test_message_insert_is_atomic_and_replay_creates_one_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._run_until_checkpoint("message_before_commit", root)

            self._assert_database_consistent(root)
            self.assertEqual(self._count(root, "conversations"), 1)
            self.assertEqual(self._count(root, "conversation_turns"), 0)
            self.assertEqual(self._count(root, "messages"), 0)
            self.assertEqual(self._count(root, "tasks"), 0)
            self.assertNotIn("message_recorded", self._conversation_event_types(root))

            writer = CountingWriter(root / "artifacts")
            service = ConversationService(
                root,
                task_service=TaskService(root, writer=writer),
            )
            completed = self._process_request(service)
            replayed = self._process_request(service)

            self.assertEqual(completed.outcome, "completed")
            self.assertEqual(completed.task.status, TaskStatus.SUCCEEDED)
            self.assertEqual(replayed.outcome, "already_processed")
            self.assertEqual(writer.calls, 1)
            self.assertEqual(self._count(root, "conversation_turns"), 1)
            self.assertEqual(self._count(root, "messages"), 1)
            self.assertEqual(self._count(root, "tasks"), 1)
            self.assertEqual(
                self._count(
                    root,
                    "events",
                    "task_id = ? AND event_type = 'task_succeeded'",
                    (completed.task.task_id,),
                ),
                1,
            )
            with self.assertRaises(MessageConflict):
                self._process_request(service, content="different content")
            self.assertEqual(self._count(root, "messages"), 1)
            self.assertEqual(writer.calls, 1)

    def test_turn_insert_rolls_back_when_message_insert_is_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._run_until_checkpoint("turn_before_message_commit", root)

            self._assert_database_consistent(root)
            self.assertEqual(self._count(root, "conversations"), 1)
            self.assertEqual(self._count(root, "conversation_turns"), 0)
            self.assertEqual(self._count(root, "messages"), 0)
            self.assertEqual(self._count(root, "tasks"), 0)
            self.assertNotIn("message_recorded", self._conversation_event_types(root))

            writer = CountingWriter(root / "artifacts")
            service = ConversationService(
                root,
                task_service=TaskService(root, writer=writer),
            )
            completed = self._process_request(service)

            self.assertEqual(completed.outcome, "completed")
            self.assertEqual(writer.calls, 1)
            self.assertEqual(self._count(root, "conversation_turns"), 1)
            self.assertEqual(self._count(root, "messages"), 1)
            self.assertEqual(self._count(root, "tasks"), 1)

    def test_committed_message_without_processing_recovers_on_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = self._run_until_checkpoint(
                "message_after_commit",
                root,
            )

            self._assert_database_consistent(root)
            self.assertEqual(self._count(root, "conversation_turns"), 1)
            self.assertEqual(self._count(root, "messages"), 1)
            self.assertEqual(self._count(root, "tasks"), 0)
            connection = sqlite3.connect(self._db(root))
            try:
                status = connection.execute(
                    "SELECT status FROM conversation_turns WHERE turn_id = ?",
                    (checkpoint["turn_id"],),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(status, "received")

            writer = CountingWriter(root / "artifacts")
            service = ConversationService(
                root,
                task_service=TaskService(root, writer=writer),
            )
            completed = self._process_request(service)
            replayed = self._process_request(service)

            self.assertEqual(completed.outcome, "completed")
            self.assertEqual(
                completed.message.message_id,
                checkpoint["message_id"],
            )
            self.assertEqual(replayed.outcome, "already_processed")
            self.assertEqual(writer.calls, 1)
            self.assertEqual(self._count(root, "conversation_turns"), 1)
            self.assertEqual(self._count(root, "tasks"), 1)
            self.assertEqual(
                self._conversation_event_types(root).count("task_linked"),
                1,
            )

    def test_task_and_turn_link_roll_back_together_on_sigkill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._run_until_checkpoint("task_link_before_commit", root)

            self._assert_database_consistent(root)
            self.assertEqual(self._count(root, "tasks"), 0)
            self.assertEqual(
                self._count(root, "events", "event_type = 'task_created'"),
                0,
            )
            connection = sqlite3.connect(self._db(root))
            try:
                turn = connection.execute(
                    """
                    SELECT status, task_id
                    FROM conversation_turns
                    WHERE conversation_id = ?
                    """,
                    (CONVERSATION_ID,),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(turn, ("received", None))
            self.assertNotIn("task_linked", self._conversation_event_types(root))

            writer = CountingWriter(root / "artifacts")
            service = ConversationService(
                root,
                task_service=TaskService(root, writer=writer),
            )
            completed = self._process_request(service)

            self.assertEqual(completed.outcome, "completed")
            self.assertEqual(completed.task.status, TaskStatus.SUCCEEDED)
            self.assertEqual(writer.calls, 1)
            self.assertEqual(self._count(root, "tasks"), 1)
            self.assertEqual(
                self._count(root, "events", "event_type = 'task_created'"),
                1,
            )
            self.assertEqual(
                self._conversation_event_types(root).count("task_linked"),
                1,
            )

    def test_committed_queued_task_is_recovered_without_duplicate_side_effect(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = self._run_until_checkpoint(
                "task_after_commit",
                root,
            )
            task_id = checkpoint["task_id"]

            self._assert_database_consistent(root)
            queued = EventStore(self._db(root)).require_task(task_id)
            self.assertEqual(queued.status, TaskStatus.QUEUED)
            self.assertIsNone(queued.lease_token)
            self.assertNotIn("tool_started", self._task_event_types(root, task_id))
            self.assertFalse((root / "artifacts" / task_id / ARTIFACT_PATH).exists())

            writer = CountingWriter(root / "artifacts")
            service = ConversationService(
                root,
                task_service=TaskService(root, writer=writer),
            )
            recovered = service.recover_task(task_id)
            repeated = service.recover_task(task_id)

            self.assertEqual(recovered.status, TaskStatus.SUCCEEDED)
            self.assertEqual(repeated.status, TaskStatus.SUCCEEDED)
            self.assertEqual(writer.calls, 1)
            self.assertEqual(
                self._task_event_types(root, task_id).count("tool_started"),
                1,
            )
            self.assertEqual(
                (root / "artifacts" / task_id / ARTIFACT_PATH).read_text(),
                ARTIFACT_CONTENT,
            )

    def test_clarification_request_insert_rolls_back_and_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = self._run_until_checkpoint(
                "question_before_commit",
                root,
            )
            task_id = checkpoint["task_id"]

            self._assert_database_consistent(root)
            task = EventStore(self._db(root)).require_task(task_id)
            self.assertEqual(task.status, TaskStatus.AWAITING_CLARIFICATION)
            self.assertEqual(
                self._count(root, "clarification_requests"),
                0,
            )
            self.assertNotIn(
                "clarification_requested",
                self._conversation_event_types(root),
            )

            writer = CountingWriter(root / "artifacts")
            service = ConversationService(
                root,
                task_service=TaskService(root, writer=writer),
            )
            replayed = self._process_request(service)

            self.assertEqual(replayed.outcome, "awaiting_clarification")
            self.assertEqual(len(replayed.clarifications), 3)
            self.assertEqual(
                [
                    question.field_name
                    for question in replayed.clarifications
                ],
                ["operation", "artifact_path", "content"],
            )
            self.assertEqual(
                self._count(root, "clarification_requests"),
                3,
            )
            self.assertEqual(
                self._conversation_event_types(root).count(
                    "clarification_requested"
                ),
                3,
            )
            self.assertEqual(writer.calls, 0)
            self.assertEqual(self._count(root, "tasks"), 1)

    def test_sigkill_after_staging_leaves_no_published_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = self._run_until_checkpoint(
                "writer_after_staging",
                root,
            )
            task_id = checkpoint["task_id"]
            task_dir = root / "artifacts" / task_id
            artifact = task_dir / ARTIFACT_PATH

            self._assert_database_consistent(root)
            task = EventStore(self._db(root)).require_task(task_id)
            self.assertEqual(task.status, TaskStatus.RUNNING)
            self.assertFalse(artifact.exists())
            staging_files = list((task_dir / ".staging").glob("*.part"))
            self.assertEqual(len(staging_files), 1)
            event_types = self._task_event_types(root, task_id)
            self.assertNotIn("tool_succeeded", event_types)

            recovery_time = (
                datetime.fromisoformat(str(task.lease_expires_at))
                + timedelta(seconds=1)
            ).isoformat(timespec="milliseconds")
            writer = CountingWriter(root / "artifacts")
            restarted = TaskService(
                root,
                writer=writer,
                clock=lambda: recovery_time,
            )
            recovered = restarted.resume(task_id)

            self.assertEqual(recovered.status, TaskStatus.SUCCEEDED)
            self.assertEqual(writer.calls, 1)
            self.assertEqual(artifact.read_text(), ARTIFACT_CONTENT)
            self.assertEqual(recovered.artifact.claim_generation, 2)
            self.assertEqual(
                self._task_event_types(root, task_id).count("tool_started"),
                2,
            )
            self.assertTrue(staging_files[0].exists())
            self.assertFalse(
                (root / "artifacts" / ".locks" / f"{task_id}.lock").is_symlink()
            )

    def test_sigkill_while_holding_publish_lock_releases_coordination(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = self._run_until_checkpoint(
                "writer_after_authority",
                root,
            )
            task_id = checkpoint["task_id"]
            task_dir = root / "artifacts" / task_id
            artifact = task_dir / ARTIFACT_PATH

            self._assert_database_consistent(root)
            task = EventStore(self._db(root)).require_task(task_id)
            self.assertEqual(task.status, TaskStatus.RUNNING)
            self.assertFalse(artifact.exists())
            self.assertEqual(
                len(list((task_dir / ".staging").glob("*.part"))),
                1,
            )

            recovery_time = (
                datetime.fromisoformat(str(task.lease_expires_at))
                + timedelta(seconds=1)
            ).isoformat(timespec="milliseconds")
            writer = CountingWriter(root / "artifacts")
            restarted = TaskService(
                root,
                writer=writer,
                clock=lambda: recovery_time,
            )
            recovered = restarted.resume(task_id)

            self.assertEqual(recovered.status, TaskStatus.SUCCEEDED)
            self.assertEqual(writer.calls, 1)
            self.assertEqual(recovered.claim_generation, 2)
            self.assertEqual(artifact.read_text(), ARTIFACT_CONTENT)
            self.assertEqual(
                self._task_event_types(root, task_id).count("task_succeeded"),
                1,
            )

    def test_sigkill_after_publish_blocks_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = self._run_until_checkpoint(
                "writer_after_publish",
                root,
            )
            task_id = checkpoint["task_id"]
            artifact = root / "artifacts" / task_id / ARTIFACT_PATH

            self._assert_database_consistent(root)
            task = EventStore(self._db(root)).require_task(task_id)
            self.assertEqual(task.status, TaskStatus.RUNNING)
            self.assertIsNone(task.tool_result)
            self.assertEqual(artifact.read_text(), ARTIFACT_CONTENT)
            before_inode = artifact.stat().st_ino
            self.assertFalse(
                any(
                    (root / "artifacts" / task_id / ".staging").glob("*.part")
                )
            )

            recovery_time = (
                datetime.fromisoformat(str(task.lease_expires_at))
                + timedelta(seconds=1)
            ).isoformat(timespec="milliseconds")
            no_write = NoWriteWriter()
            restarted = TaskService(
                root,
                writer=no_write,
                clock=lambda: recovery_time,
            )
            recovered = restarted.resume(task_id)

            self.assertEqual(recovered.status, TaskStatus.BLOCKED)
            self.assertEqual(recovered.error.code, "artifact_exists")
            self.assertEqual(no_write.calls, 0)
            self.assertEqual(artifact.stat().st_ino, before_inode)
            self.assertEqual(artifact.read_text(), ARTIFACT_CONTENT)
            self.assertNotIn(
                "task_succeeded",
                self._task_event_types(root, task_id),
            )

    def test_sigkill_inside_final_success_transaction_rolls_back_together(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = self._run_until_checkpoint(
                "success_before_commit",
                root,
            )
            task_id = checkpoint["task_id"]

            self._assert_database_consistent(root)
            store = EventStore(self._db(root))
            before = store.require_task(task_id)
            self.assertEqual(before.status, TaskStatus.VERIFYING)
            self.assertTrue(before.verification.passed)
            self.assertIsNotNone(before.lease_expires_at)
            event_types = self._task_event_types(root, task_id)
            self.assertIn("verification_completed", event_types)
            self.assertNotIn("task_succeeded", event_types)
            artifact = root / "artifacts" / task_id / ARTIFACT_PATH
            self.assertEqual(artifact.read_text(), ARTIFACT_CONTENT)

            recovery_time = (
                datetime.fromisoformat(str(before.lease_expires_at))
                + timedelta(seconds=1)
            ).isoformat(timespec="milliseconds")
            no_write = NoWriteWriter()
            restarted = TaskService(
                root,
                writer=no_write,
                clock=lambda: recovery_time,
            )
            self.assertEqual(
                restarted.require_task(task_id).status,
                TaskStatus.INTERRUPTED,
            )
            recovered = restarted.resume(task_id)
            event_count = len(restarted.events(task_id))
            repeated = restarted.resume(task_id)

            self.assertEqual(recovered.status, TaskStatus.SUCCEEDED)
            self.assertEqual(repeated.status, TaskStatus.SUCCEEDED)
            self.assertEqual(no_write.calls, 0)
            self.assertEqual(len(restarted.events(task_id)), event_count)
            final_events = self._task_event_types(root, task_id)
            self.assertEqual(final_events.count("tool_started"), 1)
            self.assertEqual(final_events.count("task_succeeded"), 1)
            self.assertEqual(artifact.read_text(), ARTIFACT_CONTENT)

    def test_committed_success_survives_caller_not_receiving_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = self._run_until_checkpoint(
                "success_after_commit",
                root,
            )
            task_id = checkpoint["task_id"]

            self._assert_database_consistent(root)
            task = EventStore(self._db(root)).require_task(task_id)
            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            self.assertEqual(task.claim_generation, checkpoint["claim_generation"])
            self.assertIsNone(task.lease_token)
            artifacts = list((root / "artifacts" / task_id).iterdir())
            visible = [
                item for item in artifacts if not item.name.startswith(".")
            ]
            self.assertEqual([item.name for item in visible], [ARTIFACT_PATH])
            self.assertEqual(visible[0].read_text(), ARTIFACT_CONTENT)

            no_write = NoWriteWriter()
            restarted = TaskService(root, writer=no_write)
            event_count = len(restarted.events(task_id))
            replayed = restarted.resume(task_id)

            self.assertEqual(replayed.status, TaskStatus.SUCCEEDED)
            self.assertEqual(no_write.calls, 0)
            self.assertEqual(len(restarted.events(task_id)), event_count)
            self.assertEqual(
                self._task_event_types(root, task_id).count("task_succeeded"),
                1,
            )

    def test_pending_answer_replay_finishes_without_duplicate_side_effect(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = self._run_until_checkpoint(
                "answer_after_commit",
                root,
            )
            task_id = checkpoint["task_id"]

            self._assert_database_consistent(root)
            task = EventStore(self._db(root)).require_task(task_id)
            self.assertEqual(task.status, TaskStatus.AWAITING_CLARIFICATION)
            answer = EventStore(self._db(root)).list_clarification_answers(
                checkpoint["question_id"]
            )[0]
            self.assertEqual(
                answer.validation_status,
                ClarificationAnswerStatus.PENDING_APPLY,
            )
            self.assertFalse((root / "artifacts" / task_id / ARTIFACT_PATH).exists())

            writer = CountingWriter(root / "artifacts")
            service = ConversationService(
                root,
                task_service=TaskService(root, writer=writer),
            )
            replayed = service.process_message(
                CONVERSATION_ID,
                ARTIFACT_CONTENT,
                client_message_id=ANSWER_CLIENT_MESSAGE_ID,
                answer_to=checkpoint["question_id"],
                now="2026-10-20T00:00:00.000+00:00",
            )
            repeated = service.process_message(
                CONVERSATION_ID,
                ARTIFACT_CONTENT,
                client_message_id=ANSWER_CLIENT_MESSAGE_ID,
                answer_to=checkpoint["question_id"],
                now="2026-10-20T00:00:00.000+00:00",
            )

            self.assertEqual(replayed.outcome, "completed")
            self.assertEqual(replayed.task.status, TaskStatus.SUCCEEDED)
            self.assertEqual(repeated.outcome, "already_processed")
            self.assertEqual(writer.calls, 1)
            applied = EventStore(self._db(root)).list_clarification_answers(
                checkpoint["question_id"]
            )
            self.assertEqual(len(applied), 1)
            self.assertEqual(
                applied[0].validation_status,
                ClarificationAnswerStatus.APPLIED,
            )
            self.assertEqual(
                self._conversation_event_types(root).count(
                    "clarification_applied"
                ),
                1,
            )
            self.assertEqual(
                self._task_event_types(root, task_id).count("tool_started"),
                1,
            )

    def test_success_before_answer_mark_is_reconciled_without_tool_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checkpoint = self._run_until_checkpoint(
                "answer_before_applied",
                root,
            )
            task_id = checkpoint["task_id"]

            self._assert_database_consistent(root)
            task = EventStore(self._db(root)).require_task(task_id)
            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            answer = EventStore(self._db(root)).list_clarification_answers(
                checkpoint["question_id"]
            )[0]
            self.assertEqual(
                answer.validation_status,
                ClarificationAnswerStatus.PENDING_APPLY,
            )
            self.assertNotIn(
                "clarification_applied",
                self._conversation_event_types(root),
            )

            no_write = NoWriteWriter()
            service = ConversationService(
                root,
                task_service=TaskService(root, writer=no_write),
            )
            recovered = service.recover_task(task_id)
            event_count = len(service.store.list_events(task_id))
            repeated = service.recover_task(task_id)

            self.assertEqual(recovered.status, TaskStatus.SUCCEEDED)
            self.assertEqual(repeated.status, TaskStatus.SUCCEEDED)
            self.assertEqual(no_write.calls, 0)
            self.assertEqual(len(service.store.list_events(task_id)), event_count)
            applied = EventStore(self._db(root)).list_clarification_answers(
                checkpoint["question_id"]
            )
            self.assertEqual(
                applied[0].validation_status,
                ClarificationAnswerStatus.APPLIED,
            )
            self.assertEqual(
                self._conversation_event_types(root).count(
                    "clarification_applied"
                ),
                1,
            )

    def test_verifier_exception_fails_closed_without_retrying_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            writer = CountingWriter(root / "artifacts")
            service = TaskService(root, writer=writer)
            service.verifier = RaisingVerifier()

            task = service.submit(
                TaskRequest(
                    goal="verifier failure",
                    operation=WRITE_TEXT_ARTIFACT,
                    parameters={
                        "artifact_path": ARTIFACT_PATH,
                        "content": ARTIFACT_CONTENT,
                    },
                )
            )

            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "verification_failed")
            self.assertEqual(
                task.verification.reason,
                "verifier_exception_RuntimeError",
            )
            self.assertEqual(writer.calls, 1)
            self.assertEqual(
                (root / "artifacts" / task.task_id / ARTIFACT_PATH).read_text(),
                ARTIFACT_CONTENT,
            )
            event_types = self._task_event_types(root, task.task_id)
            self.assertIn("verification_completed", event_types)
            self.assertIn("task_failed", event_types)
            self.assertNotIn("task_succeeded", event_types)

            replayed = service.resume(task.task_id)
            self.assertEqual(replayed.status, TaskStatus.FAILED)
            self.assertEqual(writer.calls, 1)

    def test_verifier_invalid_result_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            writer = CountingWriter(root / "artifacts")
            service = TaskService(root, writer=writer)
            service.verifier = InvalidResultVerifier()

            task = service.submit(
                TaskRequest(
                    goal="verifier invalid result",
                    operation=WRITE_TEXT_ARTIFACT,
                    parameters={
                        "artifact_path": ARTIFACT_PATH,
                        "content": ARTIFACT_CONTENT,
                    },
                )
            )

            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "verification_failed")
            self.assertEqual(
                task.verification.reason,
                "verifier_invalid_result_object",
            )
            self.assertEqual(writer.calls, 1)
            self.assertNotIn(
                "task_succeeded",
                self._task_event_types(root, task.task_id),
            )

    def test_stale_executor_cannot_commit_after_lease_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            t0 = "2026-10-20T00:00:00.000+00:00"
            expires = "2026-10-20T00:00:01.000+00:00"
            takeover = "2026-10-20T00:00:02.000+00:00"
            first_attempt = str(uuid4())
            second_attempt = str(uuid4())
            service = TaskService(root, clock=lambda: t0, lease_seconds=30)
            task = service.enqueue(
                TaskRequest(
                    goal="fencing proof",
                    operation=WRITE_TEXT_ARTIFACT,
                    parameters={
                        "artifact_path": ARTIFACT_PATH,
                        "content": ARTIFACT_CONTENT,
                    },
                )
            )
            claim = service.store.claim_task(
                task.task_id,
                owner_id="owner-a",
                lease_token="lease-a",
                attempt_id=first_attempt,
                now=t0,
                expires_at=expires,
                allowed_statuses=TaskService.RUNNABLE_STATUSES,
            )
            self.assertTrue(claim.acquired)
            service.store.transition(
                task.task_id,
                TaskStatus.RUNNING,
                EventType.TASK_PLANNED,
                t0,
                payload={"owner_id": "owner-a"},
                updates={"phase": "planned"},
                lease_token="lease-a",
            )
            service.store.record_event_and_update(
                task.task_id,
                EventType.TOOL_STARTED,
                t0,
                payload={"owner_id": "owner-a"},
                updates={
                    "execution_mode": ExecutionMode.REAL,
                    "phase": "tool_started",
                },
                lease_token="lease-a",
            )
            writer = RealArtifactWriter(root / "artifacts")
            writer.write(
                task_id=task.task_id,
                attempt_id=claim.task.attempt_id,
                artifact_path=ARTIFACT_PATH,
                content=ARTIFACT_CONTENT,
                started_at=t0,
                finished_at=t0,
            )

            interrupted = service.store.expire_claim(
                task.task_id,
                now=takeover,
            )
            self.assertEqual(interrupted.status, TaskStatus.INTERRUPTED)
            takeover_claim = service.store.claim_task(
                task.task_id,
                owner_id="owner-b",
                lease_token="lease-b",
                attempt_id=second_attempt,
                now=takeover,
                expires_at="2026-10-20T00:01:00.000+00:00",
                allowed_statuses=TaskService.RESUMABLE_STATUSES,
            )
            self.assertTrue(takeover_claim.acquired)

            with self.assertRaises(ClaimLost):
                service.store.record_event_and_update(
                    task.task_id,
                    EventType.TOOL_SUCCEEDED,
                    takeover,
                    payload={"owner_id": "owner-a"},
                    updates={"phase": "stale_success"},
                    lease_token="lease-a",
                )

            self._assert_database_consistent(root)
            final = service.require_task(task.task_id)
            self.assertEqual(final.claim_generation, 2)
            self.assertEqual(final.status, TaskStatus.INTERRUPTED)
            self.assertNotIn(
                "tool_succeeded",
                self._task_event_types(root, task.task_id),
            )
            self.assertEqual(
                (root / "artifacts" / task.task_id / ARTIFACT_PATH).read_text(),
                ARTIFACT_CONTENT,
            )


if __name__ == "__main__":
    unittest.main()
