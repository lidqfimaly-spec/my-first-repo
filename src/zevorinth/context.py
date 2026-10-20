"""Deterministic conversation context reconstructed from persisted records."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .event_store import EventStore
from .models import (
    Message,
    Task,
    TaskStatus,
    TurnStatus,
)
from .state_machine import is_terminal


class ContextItemKind(str, Enum):
    FACT = "fact"
    USER_REQUIREMENT = "user_requirement"
    CONFIRMED_DECISION = "confirmed_decision"
    INFERENCE = "inference"
    UNCONFIRMED_ASSUMPTION = "unconfirmed_assumption"


@dataclass(frozen=True, slots=True)
class ContextItem:
    kind: ContextItemKind
    text: str
    source_id: str
    confirmed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "source_id": self.source_id,
            "confirmed": self.confirmed,
        }


@dataclass(slots=True)
class ConversationContext:
    conversation_id: str
    version: str
    updated_at: str
    original_messages: list[Message]
    current_task: Task | None
    goal: str | None
    constraints: list[ContextItem] = field(default_factory=list)
    confirmed_decisions: list[ContextItem] = field(default_factory=list)
    completed_work: list[ContextItem] = field(default_factory=list)
    pending_steps: list[ContextItem] = field(default_factory=list)
    unresolved_questions: list[ContextItem] = field(default_factory=list)
    unconfirmed_assumptions: list[ContextItem] = field(
        default_factory=list
    )
    sources: list[str] = field(default_factory=list)
    summary: str | None = None
    summary_authoritative: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "version": self.version,
            "updated_at": self.updated_at,
            "original_messages": [
                message.to_dict() for message in self.original_messages
            ],
            "current_task": (
                self.current_task.to_dict() if self.current_task else None
            ),
            "goal": self.goal,
            "constraints": [item.to_dict() for item in self.constraints],
            "confirmed_decisions": [
                item.to_dict() for item in self.confirmed_decisions
            ],
            "completed_work": [
                item.to_dict() for item in self.completed_work
            ],
            "pending_steps": [
                item.to_dict() for item in self.pending_steps
            ],
            "unresolved_questions": [
                item.to_dict() for item in self.unresolved_questions
            ],
            "unconfirmed_assumptions": [
                item.to_dict() for item in self.unconfirmed_assumptions
            ],
            "sources": list(self.sources),
            "summary": self.summary,
            "summary_authoritative": self.summary_authoritative,
        }


class ConversationContextBuilder:
    """Build a context snapshot without treating summaries as authority."""

    CONSTRAINT_MARKERS = (
        "不要",
        "不能",
        "必须",
        "只能",
        "禁止",
        "改成",
        "改为",
        "文件名",
        "csv",
        "do not",
        "must",
        "only",
    )

    def __init__(self, store: EventStore) -> None:
        self.store = store

    def build(
        self,
        conversation_id: str,
        *,
        summary: str | None = None,
    ) -> ConversationContext:
        conversation = self.store.get_conversation(conversation_id)
        if conversation is None:
            from .event_store import ConversationNotFound

            raise ConversationNotFound(conversation_id)
        messages = self.store.list_conversation_messages(
            conversation_id,
            limit=EventStore.MAX_CONVERSATION_HISTORY,
        )
        turns = self.store.list_conversation_turns(conversation_id)
        tasks = self.store.list_tasks_for_conversation(conversation_id)
        current_task = self._current_task(tasks)
        sources = [
            f"conversation:{conversation_id}",
            *[f"message:{message.message_id}" for message in messages],
            *[f"turn:{turn.turn_id}" for turn in turns],
            *[f"task:{task.task_id}" for task in tasks],
        ]
        constraints = [
            ContextItem(
                ContextItemKind.USER_REQUIREMENT,
                message.content,
                message.message_id,
                True,
            )
            for message in messages
            if self._is_constraint(message.content)
        ]
        confirmed: list[ContextItem] = []
        unresolved: list[ContextItem] = []
        for task in tasks:
            for question in self.store.list_clarification_requests(
                task.task_id
            ):
                if question.status.value == "open":
                    unresolved.append(
                        ContextItem(
                            ContextItemKind.FACT,
                            question.question_text,
                            question.question_id,
                            False,
                        )
                    )
                for answer in self.store.list_clarification_answers(
                    question.question_id
                ):
                    value = (
                        answer.normalized_value
                        if isinstance(answer.normalized_value, str)
                        else json.dumps(
                            answer.normalized_value,
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
                    confirmed.append(
                        ContextItem(
                            ContextItemKind.CONFIRMED_DECISION,
                            f"{question.field_name}: {value}",
                            answer.message_id,
                            answer.validation_status.value == "applied",
                        )
                    )
            if task.error is not None:
                unresolved.append(
                    ContextItem(
                        ContextItemKind.FACT,
                        task.error.message,
                        task.task_id,
                        True,
                    )
                )
            if task.cancel_requested_at is not None and not is_terminal(
                task.status
            ):
                unresolved.append(
                    ContextItem(
                        ContextItemKind.FACT,
                        "cancellation requested",
                        task.task_id,
                        True,
                    )
                )

        completed = self._completed_items(current_task)
        pending = self._pending_items(current_task)
        assumptions = [
            ContextItem(
                ContextItemKind.UNCONFIRMED_ASSUMPTION,
                f"turn requires attention: {turn.turn_id}",
                turn.turn_id,
                False,
            )
            for turn in turns
            if turn.status
            in {TurnStatus.NEEDS_ATTENTION, TurnStatus.FAILED}
        ]
        updated_candidates = [
            conversation.updated_at,
            *[message.created_at for message in messages],
            *[task.updated_at for task in tasks],
        ]
        plan_version = current_task.plan_version if current_task else 0
        generation = current_task.claim_generation if current_task else 0
        return ConversationContext(
            conversation_id=conversation_id,
            version=(
                f"conv-{conversation.version}-plan-{plan_version}"
                f"-claim-{generation}"
            ),
            updated_at=max(updated_candidates),
            original_messages=messages,
            current_task=current_task,
            goal=(
                current_task.request.goal if current_task is not None else None
            ),
            constraints=constraints,
            confirmed_decisions=confirmed,
            completed_work=completed,
            pending_steps=pending,
            unresolved_questions=unresolved,
            unconfirmed_assumptions=assumptions,
            sources=sources,
            summary=summary,
            summary_authoritative=False,
        )

    @staticmethod
    def _current_task(tasks: list[Task]) -> Task | None:
        if not tasks:
            return None
        active = [task for task in tasks if not is_terminal(task.status)]
        return active[-1] if active else tasks[-1]

    def _is_constraint(self, content: str) -> bool:
        lowered = content.lower()
        return any(marker in lowered for marker in self.CONSTRAINT_MARKERS)

    @staticmethod
    def _completed_items(task: Task | None) -> list[ContextItem]:
        if task is None:
            return []
        items: list[ContextItem] = []
        if task.tool_result is not None:
            items.append(
                ContextItem(
                    ContextItemKind.FACT,
                    f"tool outcome recorded: {task.tool_result.success}",
                    task.task_id,
                    True,
                )
            )
        if task.artifact is not None:
            items.append(
                ContextItem(
                    ContextItemKind.FACT,
                    f"artifact {task.artifact.relative_path} "
                    f"sha256={task.artifact.sha256}",
                    task.task_id,
                    True,
                )
            )
        if task.verification is not None:
            items.append(
                ContextItem(
                    ContextItemKind.FACT,
                    f"verification {task.verification.passed}: "
                    f"{task.verification.reason}",
                    task.task_id,
                    True,
                )
            )
        return items

    @staticmethod
    def _pending_items(task: Task | None) -> list[ContextItem]:
        if task is None or task.tool_result is not None:
            return []
        if task.plan is not None:
            return [
                ContextItem(
                    ContextItemKind.FACT,
                    step.expected_result,
                    task.task_id,
                    True,
                )
                for step in task.plan.steps
            ]
        if task.missing_fields:
            return [
                ContextItem(
                    ContextItemKind.FACT,
                    f"missing field: {field_name}",
                    task.task_id,
                    False,
                )
                for field_name in task.missing_fields
            ]
        if task.status is not TaskStatus.SUCCEEDED:
            return [
                ContextItem(
                    ContextItemKind.FACT,
                    "plan has not been produced",
                    task.task_id,
                    False,
                )
            ]
        return []
