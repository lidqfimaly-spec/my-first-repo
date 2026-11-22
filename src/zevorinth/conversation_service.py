"""Durable conversation and message operations over EventStore."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from .context import ConversationContext, ConversationContextBuilder
from .event_store import (
    ClarificationConflict,
    ClarificationNotFound,
    EventStore,
    EventStoreError,
)
from .intent import IntentClassifier
from .intent_extraction import (
    extract_goal,
    has_user_question,
    merge_with_explicit,
    normalize_operation,
    operation_candidates,
    question_for,
)
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
from .progress_text import describe_progress
from .service import TaskService

LOGGER = logging.getLogger(__name__)


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
        self.context_builder = ConversationContextBuilder(self.store)

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

    def list_conversations(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List conversations with their active task status."""
        import sqlite3

        db_path = self.store.database_path
        if not db_path.exists():
            return []
        connection = sqlite3.connect(
            str(db_path), timeout=5.0, isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT c.conversation_id, c.status, c.created_at,
                       c.updated_at, c.metadata_json
                FROM conversations c
                WHERE c.status = 'active'
                ORDER BY c.updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            results: list[dict[str, Any]] = []
            for row in rows:
                conv_id = str(row["conversation_id"])
                active_task = (
                    self.store.find_active_task_for_conversation(conv_id)
                )
                results.append({
                    "conversation_id": conv_id,
                    "status": str(row["status"]),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                    "metadata": json.loads(
                        str(row["metadata_json"] or "{}")
                    ),
                    "active_task_id": (
                        active_task.task_id if active_task else None
                    ),
                    "active_task_status": (
                        active_task.status.value
                        if active_task else None
                    ),
                    "active_task_goal": (
                        active_task.request.goal
                        if active_task else None
                    ),
                })
            return results
        finally:
            connection.close()

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

    def context(
        self,
        conversation_id: str,
        *,
        summary: str | None = None,
    ) -> ConversationContext:
        return self.context_builder.build(
            conversation_id,
            summary=summary,
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
        """Persist one message, route it, then record what the system says.

        The reply is written *after* the branch has committed its durable
        outcome, so a failed processing pass never leaves a reply claiming
        something that did not happen.
        """
        result = self._process_message(
            conversation_id,
            content,
            client_message_id=client_message_id,
            answer_to=answer_to,
            operation=operation,
            artifact_path=artifact_path,
            task_content=task_content,
            now=now,
        )
        self._record_reply(result, now=now)
        return result

    def _record_reply(
        self,
        result: ConversationProcessResult,
        *,
        now: str | None,
    ) -> None:
        if result.outcome == "already_processed":
            return
        try:
            text = self._reply_text(result)
            if not text:
                return
            self.store.append_assistant_message(
                result.turn.conversation_id,
                result.turn.turn_id,
                text,
                metadata={"outcome": result.outcome},
                now=now,
            )
        except Exception:  # noqa: BLE001 - a reply must never fail a turn
            LOGGER.error(
                "could not record the assistant reply turn_id=%s",
                result.turn.turn_id,
                exc_info=True,
            )

    def _reply_text(self, result: ConversationProcessResult) -> str | None:
        """What the system says back. Derived from the outcome, never invented."""
        outcome = result.outcome
        task = result.task
        if outcome == "awaiting_clarification":
            questions = [
                question.question_text for question in result.clarifications
            ]
            if not questions:
                return "我需要再确认一些信息才能继续。"
            lines = ["我需要确认一下："]
            lines.extend(f"- {text}" for text in questions)
            return "\n".join(lines)
        if outcome == "ambiguous":
            return (
                "我没太确定你想做什么。可以说得具体一点，比如"
                "「把这段内容保存成 note.txt：…」或「检查工作区」。"
            )
        if outcome == "status":
            if task is None:
                # No *active* task does not mean nothing happened: answer about
                # the most recent one instead of claiming the conversation is
                # empty (J9).
                try:
                    task = self.store.latest_task_for_conversation(
                        result.turn.conversation_id
                    )
                except Exception:  # noqa: BLE001 - best-effort read
                    task = None
            if task is None:
                return "这个会话里现在没有正在进行的任务。"
            return self._task_sentence(task)
        if outcome == "active_task_conflict":
            return (
                "这个会话里还有一个任务没结束，先把它处理完，"
                "或者告诉我要取消它。"
            )
        if outcome == "requires_cancel_confirmation":
            return "取消是不可逆的，请确认后我再执行取消。"
        if outcome in {"question_mismatch", "needs_question_binding"}:
            return "我不确定你在回答哪个问题，请从上面列出的问题里选一个回复。"
        if outcome == "answer_conflict" or outcome == "already_processed":
            return None if outcome == "already_processed" else (
                "这个回答已经记录过了，但和之前的内容不一致，请确认一次。"
            )
        if outcome == "invalid_answer":
            return result.error or "这个回答我没法用，请换一种说法。"
        if outcome == "answer_rejected":
            return "这个回答没能用上，任务当前的状态不允许这样补充。"
        if outcome == "requires_recovery":
            return "这个任务的状态需要人工处理一下才能继续。"
        if task is not None:
            return self._task_sentence(task)
        if outcome == "requires_confirmation":
            return "这一步需要你确认后我才会执行。"
        if outcome == "requires_structured_update":
            return "这类修改需要走结构化入口，我没法只从这句话判断。"
        return None

    def _task_sentence(self, task: Task) -> str:
        try:
            events = self.task_service.store.list_events(task.task_id)
        except Exception:  # noqa: BLE001 - best-effort read
            events = []
        progress = describe_progress(task, events)
        text = progress["text"]
        if progress["steps_total"]:
            text = f"{text}（{progress['steps_done']}/{progress['steps_total']} 步）"
        return text

    def _process_message(
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
            # Understand the sentence before handing it to the planner. The
            # planner only ever saw structured fields, so every unstated field
            # became a question phrased in internal names; extraction fills
            # what the sentence actually says and leaves the rest missing.
            extraction = extract_goal(content)
            resolved_operation, resolved_parameters, _ = merge_with_explicit(
                extraction,
                {
                    "operation": operation,
                    "artifact_path": artifact_path,
                    "content": task_content,
                },
            )
            request = TaskRequest(
                goal=content,
                operation=resolved_operation,
                parameters=resolved_parameters,
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

        if classification.kind is IntentKind.CANCEL_REQUEST:
            turn = self.store.update_conversation_turn(
                turn.turn_id,
                status=TurnStatus.NEEDS_ATTENTION,
                intent_kind=classification.kind.value,
                now=now,
            )
            return self._result(
                "requires_cancel_confirmation",
                turn,
                message,
                classification,
                active_task,
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
            answer_value = content
            if question.field_name == "operation":
                # The audit measured this exact hole: answering "写文件" was
                # stored verbatim and only failed later in the policy layer,
                # where the user could not see why (R-18).
                canonical = normalize_operation(content)
                if canonical is None:
                    turn = self.store.update_conversation_turn(
                        turn.turn_id,
                        status=TurnStatus.NEEDS_ATTENTION,
                        intent_kind=classification.kind.value,
                        now=now,
                    )
                    return self._result(
                        "invalid_answer",
                        turn,
                        message,
                        classification,
                        active_task,
                        error=(
                            "我没听懂这个动作：「"
                            f"{content.strip()[:40]}」。可以选："
                            + "、".join(
                                label for _, label in operation_candidates()
                            )
                        ),
                    )
                answer_value = canonical
            elif question.field_name == "artifact_path":
                answer_value = content.strip().strip("「」“”\"'")
            try:
                answer = self.store.record_clarification_answer(
                    question_id=question.question_id,
                    message_id=message.message_id,
                    normalized_value=answer_value,
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

        patch = self._patch_for_question(
            question.field_name, str(answer.normalized_value)
        )
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
        # Dependent fields are only meaningful once the action is known: asking
        # "which file?" and "what content?" before "what do you want me to do?"
        # is what made the first-run experience feel like a form (J1).
        fields_in_play = list(task.missing_fields)
        if "operation" in fields_in_play:
            fields_in_play = ["operation"]
        current_fields = set(fields_in_play)
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
            for field in fields_in_play
            if field not in existing_fields
        ]
        created = self.store.create_clarification_requests(
            task_id=task.task_id,
            conversation_id=task.conversation_id or "",
            turn_id=turn_id,
            fields=fields_to_create,
            questions=[
                question_for(field)["question"]
                if has_user_question(field)
                else questions_by_field.get(field, f"请提供 {field}")
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
