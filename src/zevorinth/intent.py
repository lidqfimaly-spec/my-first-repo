"""Deterministic, side-effect-free classification of conversation messages."""

from __future__ import annotations

from .models import (
    ExecutionMode,
    IntentClassification,
    IntentKind,
    Task,
    TaskStatus,
)
from .state_machine import is_terminal


_STATUS_QUERIES = frozenset(
    {"status", "状态", "进度", "现在怎么样", "完成了吗"}
)
_NEW_TASK_MARKERS = (
    "新任务",
    "新的任务",
    "另一个",
    "另外",
    "重新开始",
    "新建",
    "new task",
    "new:",
)
_MODIFY_MARKERS = (
    "修改",
    "改成",
    "改为",
    "换成",
    "调整",
    "替换",
    "更新",
    "change",
    "modify",
    "update",
)
_CANCEL_MARKERS = ("取消", "cancel")


class IntentClassifier:
    """Classify a user message without calling a model or mutating a task."""

    def classify(
        self,
        message: str,
        *,
        active_task: Task | None = None,
        answer_to: str | None = None,
    ) -> IntentClassification:
        text = message.strip().lower()
        if not text:
            return IntentClassification(
                kind=IntentKind.AMBIGUOUS,
                reason="empty message",
            )
        if text in _STATUS_QUERIES:
            return IntentClassification(
                kind=IntentKind.STATUS_QUERY,
                reason="explicit status query",
                target_task_id=active_task.task_id if active_task else None,
            )
        if answer_to is not None:
            if (
                active_task is None
                or active_task.status is not TaskStatus.AWAITING_CLARIFICATION
            ):
                return IntentClassification(
                    kind=IntentKind.AMBIGUOUS,
                    reason="answer refers to a task that is not awaiting clarification",
                    target_task_id=active_task.task_id if active_task else None,
                )
            return IntentClassification(
                kind=IntentKind.CLARIFICATION_ANSWER,
                reason="explicit answer binding",
                target_task_id=active_task.task_id,
                question_id=answer_to,
            )
        if active_task is None:
            return IntentClassification(
                kind=IntentKind.NEW_TASK,
                reason="no active task in this conversation",
            )

        if any(marker in text for marker in _CANCEL_MARKERS):
            return IntentClassification(
                kind=IntentKind.CANCEL_REQUEST,
                reason="message requests cancellation of the active task",
                target_task_id=active_task.task_id,
                requires_confirmation=True,
            )

        has_new_marker = any(marker in text for marker in _NEW_TASK_MARKERS)
        if has_new_marker:
            return IntentClassification(
                kind=IntentKind.NEW_TASK,
                reason="message explicitly starts a new task",
            )

        if active_task.status is TaskStatus.AWAITING_CLARIFICATION:
            if any(marker in text for marker in _MODIFY_MARKERS):
                return IntentClassification(
                    kind=IntentKind.MODIFY_PENDING,
                    reason="pending task changed while clarification is open",
                    target_task_id=active_task.task_id,
                    requires_confirmation=True,
                )
            return IntentClassification(
                kind=IntentKind.CLARIFICATION_ANSWER,
                reason="message is treated as an answer to open clarification",
                target_task_id=active_task.task_id,
                requires_question_binding=True,
            )

        if is_terminal(active_task.status):
            return IntentClassification(
                kind=IntentKind.NEW_TASK,
                reason="active task is terminal; a new task is required",
            )

        has_modification = any(marker in text for marker in _MODIFY_MARKERS)
        has_side_effect = self._has_side_effect(active_task)
        if has_modification:
            if has_side_effect or active_task.status in {
                TaskStatus.RUNNING,
                TaskStatus.VERIFYING,
            }:
                return IntentClassification(
                    kind=IntentKind.NEW_TASK,
                    reason="change after execution must be a new task",
                    requires_confirmation=True,
                )
            if active_task.status in {
                TaskStatus.QUEUED,
                TaskStatus.BLOCKED,
                TaskStatus.INTERRUPTED,
            }:
                return IntentClassification(
                    kind=IntentKind.MODIFY_PENDING,
                    reason="unexecuted task can be modified safely",
                    target_task_id=active_task.task_id,
                )

        return IntentClassification(
            kind=IntentKind.SUPPLEMENT,
            reason="message adds information to the active task",
            target_task_id=active_task.task_id,
        )

    @staticmethod
    def _has_side_effect(task: Task) -> bool:
        return (
            task.tool_result is not None
            or task.artifact is not None
            or (
                task.execution_mode is ExecutionMode.REAL
                and task.status in {TaskStatus.RUNNING, TaskStatus.VERIFYING}
            )
        )
