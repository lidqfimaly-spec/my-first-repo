"""Durable conversation and message operations over EventStore."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .event_store import (
    ClarificationConflict,
    ClarificationNotFound,
    EventStore,
    EventStoreError,
)
from .intent import IntentClassifier
from .models import (
    ClarificationAnswerStatus,
    ClarificationRequest,
    Conversation,
    ConversationProcessResult,
    ConversationTurn,
    IntentClassification,
    IntentKind,
    Message,
    Task,
    TaskRequest,
    TaskStatus,
    TurnStatus,
)
from .service import TaskService


class ConversationService:
    """A thin service boundary around the conversation persistence methods."""

    def __init__(
        self,
        project_root: Path,
        *,
        store: EventStore | None = None,
        task_service: TaskService | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        if store is None:
            store = EventStore(
                self.project_root / "data" / "zevorinth.sqlite3"
            )
            store.initialize()
        self.store = store
        self.task_service = task_service or TaskService(self.project_root)
        self.intent_classifier = IntentClassifier()

    def create_conversation(
        self,
        *,
        conversation_id: str | None = None,
        now: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Conversation:
        return self.store.create_conversation(
            conversation_id=conversation_id,
            now=now,
            metadata=metadata,
        )

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.store.get_conversation(conversation_id)

    def append_user_message(
        self,
        conversation_id: str,
        content: str,
        *,
        client_message_id: str | None = None,
        now: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[ConversationTurn, Message]:
        return self.store.append_user_message(
            conversation_id,
            content,
            client_message_id=client_message_id,
            now=now,
            metadata=metadata,
        )

    def history(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
    ) -> list[Message]:
        return self.store.list_conversation_messages(
            conversation_id,
            limit=limit,
        )

    def process_message(
        self,
        conversation_id: str,
        content: str,
        *,
        client_message_id: str,
        answer_to: str | None = None,
        operation: str | None = None,
        artifact_path: str | None = None,
        task_content: str | None = None,
        now: str | None = None,
    ) -> ConversationProcessResult:
        """Persist one message and route it through bounded deterministic rules."""
        turn, message = self.append_user_message(
            conversation_id,
            content,
            client_message_id=client_message_id,
            now=now,
        )
        existing_task = self.store.get_task_for_turn(turn.turn_id)
        if existing_task is not None:
            return self._existing_task_result(
                turn=turn,
                message=message,
                task=existing_task,
                now=now,
            )
        if turn.status is not TurnStatus.RECEIVED:
            intent_kind = (
                IntentKind(turn.intent_kind)
                if turn.intent_kind
                else IntentKind.AMBIGUOUS
            )
            return ConversationProcessResult(
                outcome="already_processed",
                turn=turn,
                message=message,
                intent=IntentClassification(
                    kind=intent_kind,
                    reason="turn already processed",
                    target_task_id=(
                        existing_task.task_id if existing_task else None
                    ),
                ),
                task=existing_task,
                clarifications=[],
            )

        active_task = self.store.find_active_task_for_conversation(
            conversation_id
        )
        classification = self.intent_classifier.classify(
            content,
            active_task=active_task,
            answer_to=answer_to,
        )
        turn = self.store.update_conversation_turn(
            turn.turn_id,
            # Keep the turn retryable until the branch has committed its
            # durable outcome. CLASSIFIED would lose a message if the process
            # dies before a task is created.
            status=TurnStatus.RECEIVED,
            intent_kind=classification.kind.value,
            now=now,
        )

        if classification.kind is IntentKind.STATUS_QUERY:
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.COMPLETED,
                intent_kind=classification.kind.value,
                now=now,
            )
            return self._result(
                "status",
                turn,
                message,
                classification,
                active_task,
            )

        if classification.kind is IntentKind.AMBIGUOUS:
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.NEEDS_ATTENTION,
                intent_kind=classification.kind.value,
                now=now,
            )
            return self._result(
                "ambiguous",
                turn,
                message,
                classification,
                active_task,
            )

        if classification.kind is IntentKind.CLARIFICATION_ANSWER:
            if (
                answer_to is None
                or classification.requires_question_binding
            ):
                turn = self.store.update_conversation_turn(
                    turn.turn_id,
                    status=TurnStatus.NEEDS_ATTENTION,
                    intent_kind=classification.kind.value,
                    now=now,
                )
                return self._result(
                    "needs_question_binding",
                    turn,
                    message,
                    classification,
                    active_task,
                )
            return self._apply_clarification_answer(
                conversation_id=conversation_id,
                answer_to=answer_to,
                content=content,
                turn=turn,
                message=message,
                classification=classification,
                active_task=active_task,
                now=now,
            )

        if classification.kind is IntentKind.NEW_TASK:
            if active_task is not None:
                turn = self.store.update_conversation_turn(
                    turn.turn_id,
                    status=TurnStatus.NEEDS_ATTENTION,
                    intent_kind=classification.kind.value,
                    now=now,
                )
                return self._result(
                    "active_task_conflict",
                    turn,
                    message,
                    classification,
                    active_task,
                )
            request = TaskRequest(
                goal=content,
                operation=operation,
                parameters={
                    "artifact_path": artifact_path,
                    "content": task_content,
                },
            )
            task = self.task_service.submit(
                request,
                conversation_id=conversation_id,
                turn_id=turn.turn_id,
            )
            return self._record_task_outcome(
                conversation_id=conversation_id,
                turn=turn,
                message=message,
                classification=classification,
                task=task,
                now=now,
            )

        # Supplement and modify_pending are deliberately not applied from
        # unstructured text. The turn is recorded and the caller gets a safe
        # next action instead of a guessed database mutation.
        turn = self.store.update_conversation_turn(
            turn.turn_id,
            status=TurnStatus.NEEDS_ATTENTION,
            intent_kind=classification.kind.value,
            now=now,
        )
        outcome = (
            "requires_confirmation"
            if classification.requires_confirmation
            else "requires_structured_update"
        )
        return self._result(
            outcome,
            turn,
            message,
            classification,
            active_task,
        )

    def _apply_clarification_answer(
        self,
        *,
        conversation_id: str,
        answer_to: str,
        content: str,
        turn: ConversationTurn,
        message: Message,
        classification: IntentClassification,
        active_task: Task | None,
        now: str | None,
    ) -> ConversationProcessResult:
        question = self.store.get_clarification_request(answer_to)
        if question is None:
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.NEEDS_ATTENTION,
                intent_kind=classification.kind.value,
                now=now,
            )
            return self._result(
                "question_not_found",
                turn,
                message,
                classification,
                active_task,
            )
        if (
            active_task is None
            or question.task_id != active_task.task_id
            or question.conversation_id != conversation_id
        ):
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.NEEDS_ATTENTION,
                intent_kind=classification.kind.value,
                now=now,
            )
            return self._result(
                "question_mismatch",
                turn,
                message,
                classification,
                active_task,
            )

        answers = self.store.list_clarification_answers(question.question_id)
        answer = next(
            (
                item
                for item in answers
                if item.message_id == message.message_id
            ),
            None,
        )
        if answer is not None and question.status.value != "answered":
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.NEEDS_ATTENTION,
                intent_kind=classification.kind.value,
                now=now,
            )
            return self._result(
                "question_mismatch",
                turn,
                message,
                classification,
                active_task,
            )
        if answer is None:
            if question.status.value != "open":
                turn = self.store.update_conversation_turn(
                    turn.turn_id,
                    status=TurnStatus.NEEDS_ATTENTION,
                    intent_kind=classification.kind.value,
                    now=now,
                )
                return self._result(
                    "question_mismatch",
                    turn,
                    message,
                    classification,
                    active_task,
                )
            try:
                answer = self.store.record_clarification_answer(
                    question_id=question.question_id,
                    message_id=message.message_id,
                    normalized_value=content,
                    now=now,
                )
            except (ClarificationConflict, ClarificationNotFound) as exc:
                turn = self.store.update_conversation_turn(
                    turn.turn_id,
                    status=TurnStatus.NEEDS_ATTENTION,
                    intent_kind=classification.kind.value,
                    now=now,
                )
                return self._result(
                    "answer_conflict",
                    turn,
                    message,
                    classification,
                    active_task,
                    error=str(exc),
                )
        elif answer.validation_status is ClarificationAnswerStatus.APPLIED:
            if active_task.status is TaskStatus.SUCCEEDED:
                turn = self.store.update_conversation_turn(
                    turn.turn_id,
                    status=TurnStatus.COMPLETED,
                    intent_kind=classification.kind.value,
                    now=now,
                )
                return self._result(
                    "already_processed",
                    turn,
                    message,
                    classification,
                    active_task,
                )
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.NEEDS_ATTENTION,
                intent_kind=classification.kind.value,
                now=now,
            )
            return self._result(
                "requires_recovery",
                turn,
                message,
                classification,
                active_task,
            )
        elif answer.validation_status is ClarificationAnswerStatus.REJECTED:
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.NEEDS_ATTENTION,
                intent_kind=classification.kind.value,
                now=now,
            )
            return self._result(
                "answer_rejected",
                turn,
                message,
                classification,
                active_task,
            )

        patch = self._patch_for_question(question.field_name, content)
        try:
            task = self.task_service.resume(question.task_id, patch)
        except EventStoreError as exc:
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.NEEDS_ATTENTION,
                intent_kind=classification.kind.value,
                now=now,
            )
            return self._result(
                "answer_pending",
                turn,
                message,
                classification,
                active_task,
                error=str(exc),
            )

        if task.status is TaskStatus.SUCCEEDED:
            self.store.mark_clarification_answer(
                answer.answer_id,
                ClarificationAnswerStatus.APPLIED,
                now=now,
            )
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.COMPLETED,
                intent_kind=classification.kind.value,
                now=now,
            )
            return self._result(
                "completed",
                turn,
                message,
                classification,
                task,
            )
        if task.status is TaskStatus.AWAITING_CLARIFICATION:
            new_questions = self._create_questions(
                task,
                turn.turn_id,
                now=now,
            )
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.AWAITING_CLARIFICATION,
                intent_kind=classification.kind.value,
                now=now,
            )
            return ConversationProcessResult(
                outcome="awaiting_clarification",
                turn=turn,
                message=message,
                intent=classification,
                task=task,
                clarifications=new_questions,
            )
        self.store.mark_clarification_answer(
            answer.answer_id,
            ClarificationAnswerStatus.REJECTED,
            now=now,
        )
        turn = self.store.update_conversation_turn(
            turn.turn_id,
            status=TurnStatus.NEEDS_ATTENTION,
            intent_kind=classification.kind.value,
            now=now,
        )
        return self._result(
            "answer_rejected",
            turn,
            message,
            classification,
            task,
        )

    def _record_task_outcome(
        self,
        *,
        conversation_id: str,
        turn: ConversationTurn,
        message: Message,
        classification: IntentClassification,
        task: Task,
        now: str | None,
    ) -> ConversationProcessResult:
        if task.status is TaskStatus.AWAITING_CLARIFICATION:
            questions = self._create_questions(
                task,
                turn.turn_id,
                now=now,
            )
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.AWAITING_CLARIFICATION,
                intent_kind=classification.kind.value,
                now=now,
            )
            return ConversationProcessResult(
                outcome="awaiting_clarification",
                turn=turn,
                message=message,
                intent=classification,
                task=task,
                clarifications=questions,
            )
        turn = self.store.update_conversation_turn(
            turn.turn_id,
            status=(
                TurnStatus.COMPLETED
                if task.status is TaskStatus.SUCCEEDED
                else TurnStatus.NEEDS_ATTENTION
            ),
            intent_kind=classification.kind.value,
            now=now,
        )
        return self._result(
            (
                "completed"
                if task.status is TaskStatus.SUCCEEDED
                else task.status.value
            ),
            turn,
            message,
            classification,
            task,
        )

    def _existing_task_result(
        self,
        *,
        turn: ConversationTurn,
        message: Message,
        task: Task,
        now: str | None,
    ) -> ConversationProcessResult:
        intent_kind = (
            IntentKind(turn.intent_kind)
            if turn.intent_kind
            else IntentKind.AMBIGUOUS
        )
        classification = IntentClassification(
            kind=intent_kind,
            reason="turn already linked to a task",
            target_task_id=task.task_id,
        )
        pending = self.store.list_pending_clarification_answers(task.task_id)
        if task.status is TaskStatus.SUCCEEDED:
            for answer in pending:
                self.store.mark_clarification_answer(
                    answer.answer_id,
                    ClarificationAnswerStatus.APPLIED,
                    now=now,
                )
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.COMPLETED,
                intent_kind=classification.kind.value,
                now=now,
            )
            return self._result(
                "already_processed",
                turn,
                message,
                classification,
                task,
            )
        if task.status in {
            TaskStatus.FAILED,
            TaskStatus.PLANNING_FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
            TaskStatus.SIMULATED,
        }:
            for answer in pending:
                self.store.mark_clarification_answer(
                    answer.answer_id,
                    ClarificationAnswerStatus.REJECTED,
                    now=now,
                )
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.NEEDS_ATTENTION,
                intent_kind=classification.kind.value,
                now=now,
            )
            return self._result(
                task.status.value,
                turn,
                message,
                classification,
                task,
            )
        if task.status is TaskStatus.AWAITING_CLARIFICATION:
            if pending:
                turn = self.store.update_conversation_turn(
                    turn.turn_id,
                    status=TurnStatus.AWAITING_CLARIFICATION,
                    intent_kind=classification.kind.value,
                    now=now,
                )
                return self._result(
                    "answer_pending",
                    turn,
                    message,
                    classification,
                    task,
                )
            questions = self.store.list_clarification_requests(
                task.task_id,
                open_only=True,
            )
            if not questions:
                questions = self._create_questions(task, turn.turn_id, now=now)
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.AWAITING_CLARIFICATION,
                intent_kind=classification.kind.value,
                now=now,
            )
            return ConversationProcessResult(
                outcome="awaiting_clarification",
                turn=turn,
                message=message,
                intent=classification,
                task=task,
                clarifications=questions,
            )
        turn = self.store.update_conversation_turn(
            turn.turn_id,
            status=TurnStatus.NEEDS_ATTENTION,
            intent_kind=classification.kind.value,
            now=now,
        )
        return self._result(
            "requires_recovery",
            turn,
            message,
            classification,
            task,
        )

    def _create_questions(
        self,
        task: Task,
        turn_id: str,
        *,
        now: str | None,
    ) -> list[ClarificationRequest]:
        if not task.missing_fields:
            return []
        current_fields = set(task.missing_fields)
        existing = [
            question
            for question in self.store.list_clarification_requests(
                task.task_id,
                open_only=True,
            )
            if question.field_name in current_fields
        ]
        existing_fields = {question.field_name for question in existing}
        questions_by_field = {
            field: question
            for field, question in zip(
                task.missing_fields,
                task.clarification_questions,
                strict=False,
            )
        }
        fields_to_create = [
            field
            for field in task.missing_fields
            if field not in existing_fields
        ]
        created = self.store.create_clarification_requests(
            task_id=task.task_id,
            conversation_id=task.conversation_id or "",
            turn_id=turn_id,
            fields=fields_to_create,
            questions=[
                questions_by_field.get(field, f"请提供 {field}")
                for field in fields_to_create
            ],
            now=now,
        )
        return sorted(existing + created, key=lambda item: item.ordinal)

    @staticmethod
    def _patch_for_question(field_name: str, value: str) -> dict[str, object]:
        if field_name in {"goal", "operation"}:
            return {field_name: value}
        return {"parameters": {field_name: value}}

    def _result(
        self,
        outcome: str,
        turn: ConversationTurn,
        message: Message,
        classification: IntentClassification,
        task: Task | None,
        *,
        error: str | None = None,
    ) -> ConversationProcessResult:
        return ConversationProcessResult(
            outcome=outcome,
            turn=turn,
            message=message,
            intent=classification,
            task=task,
            clarifications=(
                self.store.list_clarification_requests(
                    task.task_id,
                    open_only=True,
                )
                if task
                else []
            ),
            error=error,
        )

    def recover_task(self, task_id: str, *, now: str | None = None) -> Task:
        """Reconcile pending clarification answers without blind execution."""
        task = self.task_service.require_task(task_id)
        pending = self.store.list_pending_clarification_answers(task_id)
        if not pending:
            if task.status in {TaskStatus.QUEUED, TaskStatus.INTERRUPTED}:
                return self.task_service.resume(task_id)
            return task
        if task.status is TaskStatus.SUCCEEDED:
            for answer in pending:
                self.store.mark_clarification_answer(
                    answer.answer_id,
                    ClarificationAnswerStatus.APPLIED,
                    now=now,
                )
            return task
        if task.status in {
            TaskStatus.FAILED,
            TaskStatus.PLANNING_FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
            TaskStatus.SIMULATED,
        }:
            for answer in pending:
                self.store.mark_clarification_answer(
                    answer.answer_id,
                    ClarificationAnswerStatus.REJECTED,
                    now=now,
                )
            return task
        if task.status not in {
            TaskStatus.AWAITING_CLARIFICATION,
            TaskStatus.BLOCKED,
        }:
            # Running/verifying/interrupted tasks may already have side effects.
            # Their pending answers require explicit review, not replay.
            return task
        for answer in pending:
            question = self.store.get_clarification_request(answer.question_id)
            if question is None:
                continue
            patch = self._patch_for_question(
                question.field_name,
                str(answer.normalized_value),
            )
            task = self.task_service.resume(task.task_id, patch)
            if task.status is TaskStatus.SUCCEEDED:
                self.store.mark_clarification_answer(
                    answer.answer_id,
                    ClarificationAnswerStatus.APPLIED,
                    now=now,
                )
            elif task.status in {
                TaskStatus.FAILED,
                TaskStatus.PLANNING_FAILED,
                TaskStatus.BLOCKED,
            }:
                self.store.mark_clarification_answer(
                    answer.answer_id,
                    ClarificationAnswerStatus.REJECTED,
                    now=now,
                )
            if task.status is not TaskStatus.AWAITING_CLARIFICATION:
                break
        return task
