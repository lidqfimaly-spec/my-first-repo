"""Central task-state transition rules."""

from __future__ import annotations

from .models import TaskStatus


class InvalidTransition(ValueError):
    """Raised when a caller attempts an undeclared task transition."""


TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.PARTIAL,
        TaskStatus.FAILED,
        TaskStatus.PLANNING_FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.SIMULATED,
    }
)


ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.PAUSED,
            TaskStatus.AWAITING_CLARIFICATION,
            TaskStatus.BLOCKED,
            TaskStatus.PLANNING_FAILED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.AWAITING_CLARIFICATION: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.BLOCKED,
            TaskStatus.PLANNING_FAILED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.VERIFYING,
            TaskStatus.PAUSED,
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.SIMULATED,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.PARTIAL,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.INTERRUPTED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.PAUSED,
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.VERIFYING,
            TaskStatus.AWAITING_CLARIFICATION,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.PLANNING_FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.BLOCKED: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.AWAITING_CLARIFICATION,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.PAUSED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.AWAITING_APPROVAL: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.PARTIAL: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.PLANNING_FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.SIMULATED: frozenset(),
}


def ensure_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"illegal transition: {current.value} -> {target.value}")


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL_STATUSES
