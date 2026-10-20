"""Phase 3.38: lock what the LLM planner can and cannot express."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.llm_planner import (  # noqa: E402
    CANDIDATE_JSON_SCHEMA,
    DEEPSEEK_CANDIDATE_JSON_SCHEMA,
)
from zevorinth.models import TaskRequest  # noqa: E402
from zevorinth.planning_validation import PlanningCandidateValidator  # noqa: E402
from zevorinth.planner import DeterministicPlanner, RUN_TESTS  # noqa: E402


def _candidate(**overrides) -> dict:
    base = {
        "classification": "plan",
        "operation": "write_text_artifact",
        "artifact_path": "a.txt",
        "content": "x",
        "missing_fields": [],
        "clarification_questions": [],
        "reason": None,
    }
    base.update(overrides)
    return base


class CandidateSchemaTests(unittest.TestCase):
    def test_schema_operation_enum_excludes_run_tests(self) -> None:
        for schema in (CANDIDATE_JSON_SCHEMA, DEEPSEEK_CANDIDATE_JSON_SCHEMA):
            operation = schema["properties"]["operation"]
            self.assertEqual(
                operation["anyOf"][0]["enum"], ["write_text_artifact"]
            )
            self.assertNotIn("run_tests", str(operation))

    def test_schema_has_no_test_execution_fields(self) -> None:
        keys = set(CANDIDATE_JSON_SCHEMA["properties"])
        for forbidden in (
            "target",
            "workspace",
            "evidence_id",
            "execution_backend",
            "image",
            "command_digest",
            "host_authorization",
        ):
            self.assertNotIn(forbidden, keys)

    def test_schema_is_closed(self) -> None:
        self.assertFalse(CANDIDATE_JSON_SCHEMA["additionalProperties"])


class ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = PlanningCandidateValidator()
        self.request = TaskRequest(goal="do it", operation=None, parameters={})

    def test_write_candidate_is_planned(self) -> None:
        result = self.validator.validate(
            _candidate(), base_request=self.request
        )
        self.assertEqual(result.classification.value, "planned")

    def test_run_tests_candidate_is_unsupported(self) -> None:
        result = self.validator.validate(
            _candidate(operation="run_tests"), base_request=self.request
        )
        self.assertEqual(result.classification.value, "unsupported")
        self.assertEqual(result.error_code, "unsupported_operation")
        self.assertIsNone(result.plan)

    def test_model_cannot_supply_evidence_or_identity(self) -> None:
        for field in ("evidence_id", "task_id", "execution_id"):
            result = self.validator.validate(
                _candidate(**{field: "forged"}), base_request=self.request
            )
            self.assertEqual(result.classification.value, "failed", field)
            self.assertEqual(result.error_code, "unknown_candidate_fields")
            self.assertIsNone(result.plan)


class DeterministicParityTests(unittest.TestCase):
    def test_deterministic_planner_supports_run_tests(self) -> None:
        outcome = DeterministicPlanner().plan_request(
            TaskRequest(
                goal="run tests",
                operation=RUN_TESTS,
                parameters={
                    "runner": "unittest",
                    "target": "tests.test_x",
                    "workspace": "demo",
                },
            )
        )
        self.assertEqual(outcome.classification.value, "planned")
        self.assertEqual(outcome.plan.steps[0].tool_name, RUN_TESTS)


if __name__ == "__main__":
    unittest.main()
