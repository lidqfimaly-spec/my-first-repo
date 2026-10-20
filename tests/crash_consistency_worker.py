"""Subprocess worker for crash-consistency checkpoints."""

from __future__ import annotations

import json
import signal
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import RealArtifactWriter  # noqa: E402
from zevorinth.conversation_service import ConversationService  # noqa: E402
from zevorinth.event_store import EventStore  # noqa: E402
from zevorinth.models import (  # noqa: E402
    ClarificationAnswerStatus,
    TaskRequest,
    TaskStatus,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


CONVERSATION_ID = "crash-consistency"
CLIENT_MESSAGE_ID = "crash-message-1"
ANSWER_CLIENT_MESSAGE_ID = "crash-message-2"
MESSAGE_CONTENT = "create crash consistency proof"
ARTIFACT_PATH = "crash-proof.txt"
ARTIFACT_CONTENT = "durable after crash\n"


def _event_name(event_type: object) -> str:
    return str(getattr(event_type, "value", event_type))


def _checkpoint(stage: str, **payload: Any) -> None:
    print(
        json.dumps(
            {"checkpoint": stage, **payload},
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    while True:
        signal.pause()


class CheckpointStore(EventStore):
    """Test-only store that pauses at named persistence boundaries."""

    def __init__(self, path: Path, mode: str) -> None:
        super().__init__(path)
        self.mode = mode

    def _connect(self):
        connection = super()._connect()
        if self.mode == "turn_before_message_commit":
            table = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'messages'
                """
            ).fetchone()
            if table is not None:
                connection.create_function(
                    "crash_turn_checkpoint",
                    0,
                    lambda: _checkpoint("turn_before_message_commit"),
                )
                connection.execute(
                    """
                    CREATE TEMP TRIGGER crash_before_message_insert
                    BEFORE INSERT ON messages
                    BEGIN
                        SELECT crash_turn_checkpoint();
                    END
                    """
                )
        return connection

    def _insert_conversation_event(
        self,
        connection,
        conversation_id,
        *,
        turn_id,
        task_id,
        event_type,
        occurred_at,
        payload,
    ) -> None:
        name = _event_name(event_type)
        if self.mode == "message_before_commit" and name == "message_recorded":
            _checkpoint("message_before_commit")
        if (
            self.mode == "question_before_commit"
            and name == "clarification_requested"
        ):
            _checkpoint(
                "question_before_commit",
                conversation_id=conversation_id,
                turn_id=turn_id,
                task_id=task_id,
            )
        if self.mode == "task_link_before_commit" and name == "task_linked":
            _checkpoint(
                "task_link_before_commit",
                conversation_id=conversation_id,
                turn_id=turn_id,
                task_id=task_id,
            )
        super()._insert_conversation_event(
            connection,
            conversation_id,
            turn_id=turn_id,
            task_id=task_id,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=payload,
        )

    def _insert_event(self, connection, event):
        if (
            self.mode == "success_before_commit"
            and event.event_type == "task_succeeded"
        ):
            _checkpoint(
                "success_before_commit",
                task_id=event.task_id,
            )
        return super()._insert_event(connection, event)

    def transition(self, task_id, target, event_type, occurred_at, **kwargs):
        result = super().transition(
            task_id,
            target,
            event_type,
            occurred_at,
            **kwargs,
        )
        if (
            self.mode == "success_after_commit"
            and target is TaskStatus.SUCCEEDED
        ):
            _checkpoint(
                "success_after_commit",
                task_id=task_id,
                claim_generation=result[0].claim_generation,
            )
        return result

    def record_clarification_answer(
        self,
        *,
        question_id,
        message_id,
        normalized_value,
        now=None,
    ):
        answer = super().record_clarification_answer(
            question_id=question_id,
            message_id=message_id,
            normalized_value=normalized_value,
            now=now,
        )
        if self.mode == "answer_after_commit":
            question = self.get_clarification_request(question_id)
            assert question is not None
            _checkpoint(
                "answer_after_commit",
                task_id=question.task_id,
                turn_id=question.turn_id,
                question_id=question_id,
                answer_id=answer.answer_id,
            )
        return answer

    def mark_clarification_answer(
        self,
        answer_id,
        status,
        *,
        now=None,
    ):
        answer = self.list_clarification_answers_by_id(answer_id)
        if (
            self.mode == "answer_before_applied"
            and status is ClarificationAnswerStatus.APPLIED
            and answer is not None
        ):
            question = self.get_clarification_request(answer.question_id)
            assert question is not None
            _checkpoint(
                "answer_before_applied",
                task_id=question.task_id,
                turn_id=question.turn_id,
                question_id=question.question_id,
                answer_id=answer.answer_id,
            )
        return super().mark_clarification_answer(
            answer_id,
            status,
            now=now,
        )

    def list_clarification_answers_by_id(self, answer_id: str):
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT answer_id, question_id, message_id, conversation_id,
                       normalized_value_json, validation_status,
                       created_at, applied_at
                FROM clarification_answers
                WHERE answer_id = ?
                """,
                (answer_id,),
            ).fetchone()
        if row is None:
            return None
        return self._clarification_answer_from_row(row)


def _request() -> TaskRequest:
    return TaskRequest(
        goal=MESSAGE_CONTENT,
        operation=WRITE_TEXT_ARTIFACT,
        parameters={
            "artifact_path": ARTIFACT_PATH,
            "content": ARTIFACT_CONTENT,
        },
    )


def _seed_conversation(store: EventStore) -> tuple[object, object]:
    store.create_conversation(
        conversation_id=CONVERSATION_ID,
        now="2026-10-20T00:00:00.000+00:00",
    )
    return store.append_user_message(
        CONVERSATION_ID,
        MESSAGE_CONTENT,
        client_message_id=CLIENT_MESSAGE_ID,
        now="2026-10-20T00:00:00.000+00:00",
    )


def _message_checkpoint(root: Path, mode: str) -> None:
    database = root / "data" / "zevorinth.sqlite3"
    if mode in {"message_before_commit", "turn_before_message_commit"}:
        store = CheckpointStore(database, mode)
        store.initialize()
        service = ConversationService(root, store=store)
        service.create_conversation(
            conversation_id=CONVERSATION_ID,
            now="2026-10-20T00:00:00.000+00:00",
        )
        service.process_message(
            CONVERSATION_ID,
            MESSAGE_CONTENT,
            client_message_id=CLIENT_MESSAGE_ID,
            operation=WRITE_TEXT_ARTIFACT,
            artifact_path=ARTIFACT_PATH,
            task_content=ARTIFACT_CONTENT,
            now="2026-10-20T00:00:00.000+00:00",
        )
        return

    service = ConversationService(root)
    service.create_conversation(
        conversation_id=CONVERSATION_ID,
        now="2026-10-20T00:00:00.000+00:00",
    )
    turn, message = service.append_user_message(
        CONVERSATION_ID,
        MESSAGE_CONTENT,
        client_message_id=CLIENT_MESSAGE_ID,
        now="2026-10-20T00:00:00.000+00:00",
    )
    _checkpoint(
        "message_after_commit",
        conversation_id=CONVERSATION_ID,
        turn_id=turn.turn_id,
        message_id=message.message_id,
    )


def _task_link_checkpoint(root: Path) -> None:
    database = root / "data" / "zevorinth.sqlite3"
    seed = EventStore(database)
    seed.initialize()
    turn, _ = _seed_conversation(seed)
    store = CheckpointStore(database, "task_link_before_commit")
    service = TaskService(root)
    service.store = store
    service.submit(
        _request(),
        conversation_id=CONVERSATION_ID,
        turn_id=turn.turn_id,
    )


def _queued_task_checkpoint(root: Path) -> None:
    service = TaskService(root)
    service.store.create_conversation(
        conversation_id=CONVERSATION_ID,
        now="2026-10-20T00:00:00.000+00:00",
    )
    turn, _ = service.store.append_user_message(
        CONVERSATION_ID,
        MESSAGE_CONTENT,
        client_message_id=CLIENT_MESSAGE_ID,
        now="2026-10-20T00:00:00.000+00:00",
    )
    task = service.enqueue(
        _request(),
        conversation_id=CONVERSATION_ID,
        turn_id=turn.turn_id,
    )
    _checkpoint(
        "task_after_commit",
        task_id=task.task_id,
        turn_id=turn.turn_id,
    )


def _execution_checkpoint(root: Path, mode: str) -> None:
    database = root / "data" / "zevorinth.sqlite3"
    service = TaskService(root)
    store = CheckpointStore(database, mode)
    service.store = store
    if mode in {
        "writer_after_staging",
        "writer_after_authority",
        "writer_after_publish",
    }:
        checkpoint_stage = (
            "after_staging_write"
            if mode == "writer_after_staging"
            else (
                "after_publish_authority"
                if mode == "writer_after_authority"
                else "after_publish"
            )
        )

        def writer_checkpoint(
            stage: str,
            task_id: str,
            attempt_id: str,
            claim_generation: int | None,
        ) -> None:
            if stage == checkpoint_stage:
                _checkpoint(
                    mode,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    claim_generation=claim_generation,
                )

        service.writer = RealArtifactWriter(
            root / "artifacts",
            checkpoint_hook=writer_checkpoint,
        )
    service.submit(_request())


def _answer_checkpoint(root: Path, mode: str) -> None:
    database = root / "data" / "zevorinth.sqlite3"
    store = CheckpointStore(database, mode)
    service = ConversationService(root, store=store)
    service.create_conversation(
        conversation_id=CONVERSATION_ID,
        now="2026-10-20T00:00:00.000+00:00",
    )
    created = service.process_message(
        CONVERSATION_ID,
        MESSAGE_CONTENT,
        client_message_id=CLIENT_MESSAGE_ID,
        operation=WRITE_TEXT_ARTIFACT,
        artifact_path=ARTIFACT_PATH,
        task_content=None,
        now="2026-10-20T00:00:00.000+00:00",
    )
    question = next(
        item for item in created.clarifications if item.field_name == "content"
    )
    service.process_message(
        CONVERSATION_ID,
        ARTIFACT_CONTENT,
        client_message_id=ANSWER_CLIENT_MESSAGE_ID,
        answer_to=question.question_id,
        now="2026-10-20T00:00:00.000+00:00",
    )


def _question_checkpoint(root: Path) -> None:
    database = root / "data" / "zevorinth.sqlite3"
    store = CheckpointStore(database, "question_before_commit")
    service = ConversationService(root, store=store)
    service.create_conversation(
        conversation_id=CONVERSATION_ID,
        now="2026-10-20T00:00:00.000+00:00",
    )
    service.process_message(
        CONVERSATION_ID,
        MESSAGE_CONTENT,
        client_message_id=CLIENT_MESSAGE_ID,
        now="2026-10-20T00:00:00.000+00:00",
    )


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: crash_consistency_worker.py <mode> <root>")
    mode = sys.argv[1]
    root = Path(sys.argv[2])
    if mode in {
        "message_before_commit",
        "message_after_commit",
        "turn_before_message_commit",
    }:
        _message_checkpoint(root, mode)
    elif mode == "task_link_before_commit":
        _task_link_checkpoint(root)
    elif mode == "task_after_commit":
        _queued_task_checkpoint(root)
    elif mode in {
        "success_before_commit",
        "success_after_commit",
        "writer_after_staging",
        "writer_after_authority",
        "writer_after_publish",
    }:
        _execution_checkpoint(root, mode)
    elif mode in {"answer_after_commit", "answer_before_applied"}:
        _answer_checkpoint(root, mode)
    elif mode == "question_before_commit":
        _question_checkpoint(root)
    else:
        raise SystemExit(f"unknown checkpoint mode: {mode}")
    return 24


if __name__ == "__main__":
    raise SystemExit(main())
