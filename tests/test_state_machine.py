from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import TaskStatus  # noqa: E402
from zevorinth.state_machine import (  # noqa: E402
    InvalidTransition,
    ensure_transition,
    is_terminal,
)


class StateMachineTests(unittest.TestCase):
    def test_required_phase_one_transitions_are_allowed(self) -> None:
        transitions = [
            (TaskStatus.QUEUED, TaskStatus.RUNNING),
            (TaskStatus.QUEUED, TaskStatus.AWAITING_CLARIFICATION),
            (TaskStatus.AWAITING_CLARIFICATION, TaskStatus.QUEUED),
            (TaskStatus.RUNNING, TaskStatus.VERIFYING),
            (TaskStatus.VERIFYING, TaskStatus.SUCCEEDED),
            (TaskStatus.VERIFYING, TaskStatus.INTERRUPTED),
            (TaskStatus.INTERRUPTED, TaskStatus.VERIFYING),
        ]
        for current, target in transitions:
            with self.subTest(current=current, target=target):
                ensure_transition(current, target)

    def test_terminal_states_cannot_restart(self) -> None:
        for status in [
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.SIMULATED,
        ]:
            with self.subTest(status=status):
                self.assertTrue(is_terminal(status))
                with self.assertRaises(InvalidTransition):
                    ensure_transition(status, TaskStatus.RUNNING)

    def test_unrelated_state_jump_is_rejected(self) -> None:
        with self.assertRaises(InvalidTransition):
            ensure_transition(TaskStatus.QUEUED, TaskStatus.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
