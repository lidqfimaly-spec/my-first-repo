"""Phase 3.42: safe candidate diagnostics without weakening validation."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.llm_planner import (  # noqa: E402
    LLMPlanner,
    LLMPlannerConfig,
    LLMProvider,
    ModelCallResult,
)
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planning_validation import PlanningCandidateValidator  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


def _candidate(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "classification": "plan",
        "operation": "run_tests",
        "artifact_path": None,
        "content": None,
        "test_target": "test_ok",
        "missing_fields": [],
        "clarification_questions": [],
        "reason": None,
    }
    base.update(overrides)
    return base


class CandidateDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = PlanningCandidateValidator()
        self.request = TaskRequest(
            goal="run test_ok",
            operation=None,
            parameters={"workspace": "demo"},
        )

    def _validate(self, candidate: object):
        return self.validator.validate(candidate, base_request=self.request)

    def test_type_field_is_rejected_and_shape_is_recorded(self) -> None:
        result = self._validate(_candidate(type="provider-type-secret"))

        self.assertEqual(result.classification.value, "failed")
        self.assertEqual(result.error_code, "unknown_candidate_fields")
        metadata = result.metadata
        self.assertIn("type", metadata["candidate_fields"])
        self.assertIn("type", metadata["candidate_unknown_fields"])
        self.assertEqual(metadata["candidate_operation"], "run_tests")
        self.assertEqual(metadata["candidate_classification"], "plan")
        self.assertNotIn(
            "provider-type-secret",
            json.dumps(metadata, ensure_ascii=False),
        )
        self.assertIsNone(result.plan)

    def test_unknown_field_value_is_not_logged(self) -> None:
        result = self._validate(
            _candidate(hidden_instruction="sensitive-value")
        )

        rendered = json.dumps(result.metadata, ensure_ascii=False)
        self.assertNotIn("sensitive-value", rendered)
        self.assertIn("hidden_instruction", result.metadata["candidate_fields"])

    def test_content_value_is_not_logged(self) -> None:
        result = self._validate(
            _candidate(content="TOP-SECRET-CONTENT", type="x")
        )

        rendered = json.dumps(result.metadata, ensure_ascii=False)
        self.assertNotIn("TOP-SECRET-CONTENT", rendered)

    def test_valid_write_candidate_has_no_diagnostic_shape(self) -> None:
        result = self._validate(
            _candidate(
                operation="write_text_artifact",
                artifact_path="result.txt",
                content="hello\n",
                test_target=None,
            )
        )

        self.assertEqual(result.classification.value, "planned")
        self.assertNotIn("candidate_fields", result.metadata)

    def test_valid_run_tests_candidate_is_unchanged(self) -> None:
        result = self._validate(_candidate())

        self.assertEqual(result.classification.value, "planned")
        self.assertEqual(result.plan.steps[0].tool_name, "run_tests")
        self.assertEqual(result.plan.steps[0].parameters["target"], "test_ok")
        self.assertNotIn("candidate_fields", result.metadata)

    def test_missing_operation_failure_has_shape(self) -> None:
        candidate = _candidate()
        del candidate["operation"]

        result = self._validate(candidate)

        self.assertEqual(result.error_code, "missing_candidate_fields")
        self.assertIn("candidate_fields", result.metadata)
        self.assertNotIn("candidate_operation", result.metadata)

    def test_invalid_operation_type_is_not_logged_as_a_value(self) -> None:
        result = self._validate(_candidate(operation=123))

        self.assertEqual(result.classification.value, "unsupported")
        self.assertIn("candidate_fields", result.metadata)
        self.assertNotIn("candidate_operation", result.metadata)

    def test_non_object_candidate_is_safe(self) -> None:
        result = self._validate(["not", "an", "object"])

        self.assertEqual(result.error_code, "invalid_candidate")
        self.assertTrue(result.metadata["candidate_received"])
        self.assertNotIn("candidate_fields", result.metadata)

    def test_field_names_are_bounded_and_sanitized(self) -> None:
        extras = {f"extra_{index:02d}": "x" for index in range(25)}
        extras["bad key!"] = "x"

        result = self._validate(_candidate(**extras))

        fields = result.metadata["candidate_fields"]
        self.assertLessEqual(len(fields), 20)
        self.assertIn("<redacted>", fields)

    def test_field_count_reflects_the_full_candidate(self) -> None:
        extras = {f"extra_{index:02d}": "x" for index in range(30)}
        candidate = _candidate(**extras)

        result = self._validate(candidate)

        self.assertEqual(
            result.metadata["candidate_field_count"],
            len(candidate),
        )
        self.assertEqual(len(result.metadata["candidate_fields"]), 20)

    def test_malicious_field_names_are_sanitized(self) -> None:
        extras = {
            "bad\nname": "x",
            "x" * 100: "x",
            "__proto__": "x",
            "ok.field-1": "x",
        }

        result = self._validate(_candidate(**extras))

        fields = result.metadata["candidate_fields"]
        self.assertNotIn("bad\nname", fields)
        self.assertNotIn("x" * 100, fields)
        self.assertNotIn("__proto__", fields)
        self.assertIn("ok.field-1", fields)
        self.assertIn("<redacted>", fields)
        rendered = json.dumps(result.metadata, ensure_ascii=False)
        self.assertNotIn("\n", rendered)

    def test_unknown_operation_records_only_the_sentinel(self) -> None:
        result = self._validate(_candidate(operation="shell"))

        self.assertEqual(result.classification.value, "unsupported")
        self.assertEqual(result.metadata["candidate_operation"], "invalid")
        self.assertNotIn(
            "shell",
            json.dumps(result.metadata, ensure_ascii=False),
        )

    def test_invalid_test_target_failure_has_shape(self) -> None:
        result = self._validate(_candidate(test_target="../escape"))

        self.assertEqual(result.error_code, "invalid_test_target")
        self.assertEqual(result.metadata["candidate_operation"], "run_tests")
        self.assertIn("test_target", result.metadata["candidate_fields"])

    def test_invalid_clarification_failure_has_shape(self) -> None:
        result = self._validate(
            {
                "classification": "clarification",
                "missing_fields": ["workspace"],
                "clarification_questions": "not-a-list",
            }
        )

        self.assertEqual(result.error_code, "invalid_clarification")
        self.assertIn("classification", result.metadata["candidate_fields"])

    def test_missing_workspace_is_a_clarification_without_shape(self) -> None:
        request = TaskRequest(goal="run test_ok", operation=None, parameters={})
        result = self.validator.validate(_candidate(), base_request=request)

        self.assertEqual(result.classification.value, "clarification")
        self.assertIn("workspace", result.missing_fields)
        self.assertNotIn("candidate_fields", result.metadata)

    def test_unreadable_mapping_fails_closed(self) -> None:
        class ExplodingMapping(Mapping):
            def __getitem__(self, key):
                raise KeyError(key)

            def __iter__(self):
                raise RuntimeError("boom")

            def __len__(self) -> int:
                return 1

        result = self._validate(ExplodingMapping())

        self.assertEqual(result.error_code, "invalid_candidate")
        self.assertEqual(result.metadata["candidate_fields"], ["<unavailable>"])
        self.assertEqual(result.metadata["candidate_field_count"], -1)

    def test_forged_trusted_field_is_rejected_and_named(self) -> None:
        result = self._validate(_candidate(evidence_id="forged"))

        self.assertEqual(result.error_code, "unknown_candidate_fields")
        self.assertIn("evidence_id", result.metadata["candidate_fields"])
        self.assertIsNone(result.plan)

    def test_unsupported_stays_unsupported(self) -> None:
        result = self._validate(
            {
                "classification": "unsupported",
                "reason": "no supported operation matches",
            }
        )

        self.assertEqual(result.classification.value, "unsupported")
        self.assertIsNone(result.plan)


class FakeCandidateClient:
    def __init__(self, candidate: dict[str, Any]) -> None:
        self.candidate = candidate
        self.calls = 0

    def create_candidate(
        self,
        *,
        goal: str,
        instructions: str,
        max_output_tokens: int,
    ) -> ModelCallResult:
        del goal, instructions, max_output_tokens
        self.calls += 1
        return ModelCallResult(
            candidate=self.candidate,
            status="completed",
            text_present=True,
            output_bytes=128,
        )


class PlanningEventDiagnosticsTests(unittest.TestCase):
    def test_planning_failed_event_contains_safe_shape(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        client = FakeCandidateClient(_candidate(type="provider-type-secret"))
        planner = LLMPlanner(
            config=LLMPlannerConfig(
                provider=LLMProvider.DEEPSEEK,
                model="deepseek-flash",
                api_key_present=True,
                api_key_env="DEEPSEEK_API_KEY",
            ),
            client=client,
        )
        service = TaskService(root, planner=planner)

        task = service.submit(
            TaskRequest(
                goal="run test_ok from demo",
                operation=None,
                parameters={"workspace": "demo"},
            )
        )

        self.assertEqual(task.status, TaskStatus.PLANNING_FAILED)
        self.assertEqual(client.calls, 1)
        events = [
            event
            for event in service.events(task.task_id)
            if event.event_type == "planning_failed"
        ]
        self.assertEqual(len(events), 1)
        metadata = events[0].payload["planning_metadata"]
        self.assertIn("type", metadata["candidate_fields"])
        self.assertEqual(metadata["candidate_operation"], "run_tests")
        self.assertNotIn(
            "provider-type-secret",
            json.dumps(events[0].payload, ensure_ascii=False),
        )


if __name__ == "__main__":
    unittest.main()
