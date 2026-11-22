"""S-B: understanding a sentence, asking like a human, and replying at all.

The acceptance targets come from the P0 audit (R-01 / R-18 / R-05):

* a goal that states what to do, on what, and with what content must run
  without asking the user for any internal field name
* when something is genuinely missing, the question must be phrased in the
  user's language and offer examples
* an answer that routes nowhere must be refused *at answer time* with a
  readable reason, not stored and rejected later by the policy layer
* the conversation must contain what the system said
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.conversation_service import ConversationService  # noqa: E402
from zevorinth.intent_extraction import (  # noqa: E402
    extract_goal,
    has_user_question,
)
from zevorinth.models import TaskStatus  # noqa: E402

NOW = "2026-11-22T00:00:00.000+00:00"

# The ten goals named in the F-01 acceptance criterion.
ACCEPTANCE_GOALS = (
    "把这段内容保存成 note.txt：今天下午三点在 3 楼会议室开会",
    "把内容存成 x.txt",
    "改 calc.py 的 bug",
    "总结 docs/a.md",
    "读取 config.json",
    "检查工作区",
    "运行测试",
    "在 note.txt 末尾追加一行：结论已验证",
    "生成研究报告 docs/a.md docs/b.md",
    "看看项目结构",
)


class ExtractionTests(unittest.TestCase):
    def test_no_goal_ever_yields_an_internal_field_name(self) -> None:
        # Whatever the extractor cannot determine must become a field the
        # question layer can phrase in user language.
        known = {
            "operation",
            "artifact_path",
            "content",
            "old_text",
            "new_text",
            "source_files",
            "goal",
        }
        for goal in ACCEPTANCE_GOALS:
            extraction = extract_goal(goal)
            self.assertTrue(
                set(extraction.missing) <= known,
                f"{goal} produced unknown missing fields {extraction.missing}",
            )

    def test_ten_of_ten_goals_avoid_internal_field_names(self) -> None:
        # The F-01 criterion read literally: no goal may leave the user facing
        # an internal field name. Every gap must map to a real user question.
        for goal in ACCEPTANCE_GOALS:
            extraction = extract_goal(goal)
            for missing in extraction.missing:
                self.assertTrue(
                    has_user_question(missing),
                    f"{goal} would ask about '{missing}' with no user wording",
                )

    def test_seven_of_ten_goals_need_no_follow_up_at_all(self) -> None:
        # Honest ceiling: three goals genuinely lack information the user never
        # gave — "把内容存成 x.txt" states no content, "改 calc.py 的 bug" states
        # no replacement, "末尾追加" has no append verb in the engine. Making
        # this 8 or 10 would mean guessing a filename or inventing content.
        complete = [
            goal for goal in ACCEPTANCE_GOALS if extract_goal(goal).complete
        ]
        self.assertEqual(
            len(complete),
            7,
            f"actionable goals changed: {complete}",
        )
        partial = set(ACCEPTANCE_GOALS) - set(complete)
        self.assertEqual(
            partial,
            {
                "把内容存成 x.txt",
                "改 calc.py 的 bug",
                "在 note.txt 末尾追加一行：结论已验证",
            },
        )

    def test_a_goal_with_content_and_filename_carries_both(self) -> None:
        extraction = extract_goal(
            "把这段内容保存成 note.txt：今天下午三点在 3 楼会议室开会"
        )
        self.assertEqual(extraction.operation, "write_text_artifact")
        self.assertEqual(extraction.parameters["artifact_path"], "note.txt")
        self.assertEqual(
            extraction.parameters["content"], "今天下午三点在 3 楼会议室开会\n"
        )
        self.assertEqual(extraction.missing, ())

    def test_a_verb_without_a_filename_asks_only_for_the_filename(self) -> None:
        extraction = extract_goal("读取一下")
        self.assertEqual(extraction.operation, "read_text_artifact")
        self.assertEqual(extraction.missing, ("artifact_path",))

    def test_an_under_specified_goal_asks_for_the_action_first(self) -> None:
        extraction = extract_goal("帮我弄一下")
        self.assertIsNone(extraction.operation)
        self.assertEqual(extraction.missing, ("operation",))


class ConversationUnderstandingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.service = ConversationService(self.root)
        self.service.create_conversation(
            conversation_id="c1", now=NOW
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _say(self, content: str, message_id: str = "m1", **kwargs):
        return self.service.process_message(
            "c1",
            content,
            client_message_id=message_id,
            now=NOW,
            **kwargs,
        )

    def test_a_plain_sentence_writes_the_file_without_any_question(self) -> None:
        result = self._say(
            "把这段内容保存成 note.txt：今天下午三点在 3 楼会议室开会"
        )
        self.assertNotEqual(result.outcome, "awaiting_clarification")
        self.assertEqual(result.clarifications, [])
        self.assertIsNotNone(result.task)
        self.assertIs(result.task.status, TaskStatus.SUCCEEDED)
        artifact = (
            self.root / "artifacts" / result.task.task_id / "note.txt"
        )
        self.assertEqual(
            artifact.read_text(encoding="utf-8"),
            "今天下午三点在 3 楼会议室开会\n",
        )

    def test_a_recognised_action_does_not_ask_for_the_operation_field(self) -> None:
        result = self._say("创建一个文件")
        self.assertEqual(result.outcome, "awaiting_clarification")
        fields = {item.field_name for item in result.clarifications}
        self.assertNotIn("operation", fields)
        self.assertEqual(fields, {"artifact_path", "content"})
        for item in result.clarifications:
            # No internal identifiers in what the user reads.
            self.assertNotIn("artifact_path", item.question_text)
            self.assertNotIn("content", item.question_text)
            self.assertNotIn("operation", item.question_text)

    def test_an_unknown_action_asks_exactly_one_question(self) -> None:
        result = self._say("帮我弄一下")
        self.assertEqual(result.outcome, "awaiting_clarification")
        self.assertEqual(len(result.clarifications), 1)
        self.assertEqual(result.clarifications[0].field_name, "operation")
        text = result.clarifications[0].question_text
        self.assertIn("你想让我做什么", text)

    def test_a_chinese_answer_to_the_action_question_is_normalised(self) -> None:
        first = self._say("帮我弄一下")
        question = first.clarifications[0]
        second = self._say(
            "写文件",
            message_id="m2",
            answer_to=question.question_id,
        )
        # "写文件" used to be stored verbatim and blow up in the policy layer.
        assert second.task is not None
        self.assertEqual(second.task.request.operation, "write_text_artifact")
        self.assertNotEqual(second.task.request.operation, "写文件")

    def test_an_uninterpretable_answer_is_refused_with_the_options(self) -> None:
        first = self._say("帮我弄一下")
        question = first.clarifications[0]
        second = self._say(
            "？？？",
            message_id="m2",
            answer_to=question.question_id,
        )
        self.assertEqual(second.outcome, "invalid_answer")
        assert second.task is not None
        self.assertIsNone(second.task.request.operation)
        self.assertIn("写一个新文件", second.error or "")

    def test_the_conversation_contains_what_the_system_said(self) -> None:
        self._say("把这段内容保存成 note.txt：结论已验证")
        history = self.service.history("c1")
        roles = [message.role.value for message in history]
        self.assertIn("assistant", roles)
        assistant = [
            message for message in history if message.role.value == "assistant"
        ]
        self.assertEqual(len(assistant), 1)
        self.assertIn("已完成", assistant[0].content)

    def test_a_status_question_is_answered_in_words(self) -> None:
        self._say("把这段内容保存成 note.txt：结论已验证")
        answer = self._say("进度", message_id="m2")
        self.assertEqual(answer.outcome, "status")
        history = self.service.history("c1")
        assistant = [
            message for message in history if message.role.value == "assistant"
        ]
        self.assertTrue(assistant)
        self.assertIn("已完成", assistant[-1].content)
        self.assertNotIn("%", assistant[-1].content)


if __name__ == "__main__":
    unittest.main()
