from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.intent import IntentClassifier  # noqa: E402
from zevorinth.models import (  # noqa: E402
    ArtifactEvidence,
    ExecutionMode,
    IntentKind,
    Task,
    TaskRequest,
    TaskStatus,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


def _task(
    *,
    status: TaskStatus = TaskStatus.QUEUED,
    execution_mode: ExecutionMode = ExecutionMode.NOT_EXECUTED,
    artifact: ArtifactEvidence | None = None,
) -> Task:
    return Task(
        task_id="task-1",
        status=status,
        request=TaskRequest(
            goal="task",
            operation=WRITE_TEXT_ARTIFACT,
            parameters={"artifact_path": "task.txt", "content": "task"},
        ),
        execution_mode=execution_mode,
        attempt_id="attempt-1",
        phase="test",
        created_at=NOW,
        updated_at=NOW,
        artifact=artifact,
    )


class IntentClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = IntentClassifier()

    def test_no_active_task_is_new_task(self) -> None:
        result = self.classifier.classify("创建一个文件")
        self.assertEqual(result.kind, IntentKind.NEW_TASK)

    def test_queued_task_supplement_and_modify_are_distinct(self) -> None:
        task = _task()
        supplement = self.classifier.classify(
            "补充一个约束",
            active_task=task,
        )
        modify = self.classifier.classify(
            "修改文件名",
            active_task=task,
        )
        self.assertEqual(supplement.kind, IntentKind.SUPPLEMENT)
        self.assertEqual(supplement.target_task_id, task.task_id)
        self.assertEqual(modify.kind, IntentKind.MODIFY_PENDING)
        self.assertEqual(modify.target_task_id, task.task_id)

    def test_explicit_clarification_answer_is_bound_to_question(self) -> None:
        task = _task(status=TaskStatus.AWAITING_CLARIFICATION)
        result = self.classifier.classify(
            "内容使用 hello",
            active_task=task,
            answer_to="question-1",
        )
        self.assertEqual(result.kind, IntentKind.CLARIFICATION_ANSWER)
        self.assertEqual(result.question_id, "question-1")
        self.assertFalse(result.requires_question_binding)

    def test_unbound_clarification_answer_requires_binding(self) -> None:
        task = _task(status=TaskStatus.AWAITING_CLARIFICATION)
        result = self.classifier.classify("内容使用 hello", active_task=task)
        self.assertEqual(result.kind, IntentKind.CLARIFICATION_ANSWER)
        self.assertTrue(result.requires_question_binding)
        self.assertIsNone(result.question_id)

    def test_modify_after_side_effect_requires_new_task_confirmation(self) -> None:
        task = _task(
            status=TaskStatus.RUNNING,
            execution_mode=ExecutionMode.REAL,
            artifact=ArtifactEvidence(
                relative_path="task.txt",
                byte_count=4,
                sha256="0" * 64,
            ),
        )
        result = self.classifier.classify("修改文件内容", active_task=task)
        self.assertEqual(result.kind, IntentKind.NEW_TASK)
        self.assertTrue(result.requires_confirmation)

    def test_terminal_task_requires_new_task(self) -> None:
        result = self.classifier.classify(
            "补充说明",
            active_task=_task(status=TaskStatus.SUCCEEDED),
        )
        self.assertEqual(result.kind, IntentKind.NEW_TASK)

    def test_status_query_and_empty_message(self) -> None:
        status = self.classifier.classify("状态", active_task=_task())
        empty = self.classifier.classify("  ", active_task=_task())
        self.assertEqual(status.kind, IntentKind.STATUS_QUERY)
        self.assertEqual(status.target_task_id, "task-1")
        self.assertEqual(empty.kind, IntentKind.AMBIGUOUS)

    def test_answer_to_wrong_task_state_is_ambiguous(self) -> None:
        result = self.classifier.classify(
            "answer",
            active_task=_task(status=TaskStatus.QUEUED),
            answer_to="question-1",
        )
        self.assertEqual(result.kind, IntentKind.AMBIGUOUS)


if __name__ == "__main__":
    unittest.main()
