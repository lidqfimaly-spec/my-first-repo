"""Phase 3.41: lock the planner instructions to the run_tests contract."""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.llm_planner import (  # noqa: E402
    CANDIDATE_JSON_SCHEMA,
    DEEPSEEK_CANDIDATE_JSON_SCHEMA,
    LLMPlanner,
    LLMPlannerConfig,
    LLMProvider,
    PLANNER_INSTRUCTIONS,
)
from zevorinth.models import TaskRequest  # noqa: E402
from zevorinth.planning_validation import (  # noqa: E402
    ALLOWED_MISSING_FIELDS,
    PlanningCandidateValidator,
)
from zevorinth.response_normalization import ModelCallResult  # noqa: E402


class RecordingClient:
    def __init__(self, candidate: dict | None = None) -> None:
        self.instructions: str | None = None
        self.goal: str | None = None
        self.trusted_context: dict | None = None
        self.candidate = candidate

    def create_candidate(
        self,
        *,
        goal: str,
        instructions: str,
        max_output_tokens: int,
        trusted_context: dict | None = None,
    ) -> ModelCallResult:
        del max_output_tokens
        self.goal = goal
        self.trusted_context = trusted_context
        self.instructions = instructions
        return ModelCallResult(
            candidate=self.candidate,
            status="completed",
            output_bytes=64,
        )


def _candidate(**overrides) -> dict:
    base = {
        "classification": "plan",
        "operation": "run_tests",
        "artifact_path": None,
        "content": None,
        "test_target": "tests.test_example",
        "missing_fields": [],
        "clarification_questions": [],
        "reason": None,
    }
    base.update(overrides)
    return base


class InstructionContractTests(unittest.TestCase):
    def test_instructions_cover_both_supported_operations(self) -> None:
        self.assertIn("write_text_artifact", PLANNER_INSTRUCTIONS)
        self.assertIn("run_tests", PLANNER_INSTRUCTIONS)

    def test_instructions_use_the_exact_test_target_field(self) -> None:
        self.assertIn("test_target", PLANNER_INSTRUCTIONS)
        self.assertIsNone(re.search(r"\btarget\s*[:=]", PLANNER_INSTRUCTIONS))

    def test_instructions_define_when_run_tests_applies(self) -> None:
        text = " ".join(PLANNER_INSTRUCTIONS.split())
        self.assertIn("explicitly asks to run or execute existing tests", text)
        self.assertIn("dotted identifier", text)
        self.assertIn("never guess a target", text)

    def test_instructions_separate_writing_tests_from_running_them(self) -> None:
        self.assertIn("write, design, or modify", PLANNER_INSTRUCTIONS)
        self.assertIn(
            "Do not treat every mention of tests as a request to execute a command.",
            PLANNER_INSTRUCTIONS,
        )

    def test_instructions_forbid_trusted_fields(self) -> None:
        text = PLANNER_INSTRUCTIONS
        for field in (
            "workspace roots",
            "evidence identifiers",
            "execution identifiers",
            "commands",
            "images",
            "execution backends",
            "authorization tokens",
        ):
            self.assertIn(field, text)

    def test_instructions_do_not_promise_success(self) -> None:
        self.assertIn("Never claim that a file was created or that tests passed", PLANNER_INSTRUCTIONS)
        self.assertIn("trusted execution", PLANNER_INSTRUCTIONS)

    def test_write_artifact_guidance_is_retained(self) -> None:
        self.assertIn("create one text file", PLANNER_INSTRUCTIONS)
        self.assertIn("artifact_path", PLANNER_INSTRUCTIONS)

    def test_trusted_context_is_data_and_missing_values_are_not_invented(
        self,
    ) -> None:
        self.assertIn("trusted_parameters", PLANNER_INSTRUCTIONS)
        self.assertIn("never invent", PLANNER_INSTRUCTIONS)

    def test_deepseek_planner_receives_the_same_instructions(self) -> None:
        client = RecordingClient(_candidate())
        planner = LLMPlanner(
            config=LLMPlannerConfig(
                provider=LLMProvider.DEEPSEEK,
                model="deepseek-flash",
                api_key_present=True,
                api_key_env="DEEPSEEK_API_KEY",
            ),
            client=client,
        )
        outcome = planner.plan_request(
            TaskRequest(
                goal="run tests.test_example from the demo workspace",
                operation=None,
                parameters={"workspace": "demo"},
            )
        )
        self.assertEqual(client.instructions, PLANNER_INSTRUCTIONS)
        self.assertEqual(client.trusted_context, {"workspace": "demo"})
        self.assertEqual(outcome.classification.value, "planned")
        self.assertEqual(outcome.plan.steps[0].tool_name, "run_tests")

    def test_only_model_visible_trusted_parameters_reach_client(self) -> None:
        client = RecordingClient(
            {
                "classification": "clarification",
                "missing_fields": ["test_target"],
                "clarification_questions": ["Which target?"],
            }
        )
        planner = LLMPlanner(
            config=LLMPlannerConfig(
                provider=LLMProvider.DEEPSEEK,
                model="deepseek-flash",
                api_key_present=True,
                api_key_env="DEEPSEEK_API_KEY",
            ),
            client=client,
        )
        outcome = planner.plan_request(
            TaskRequest(
                goal="run tests in the configured workspace",
                operation=None,
                parameters={
                    "workspace": "demo",
                    "runner": "unittest",
                    "evidence_id": "must-not-enter-model-input",
                    "command": "rm -rf /",
                    "image": "untrusted",
                },
            )
        )
        self.assertEqual(
            client.trusted_context,
            {"workspace": "demo", "runner": "unittest"},
        )
        self.assertEqual(
            outcome.metadata["trusted_context_keys"],
            ["runner", "workspace"],
        )
        self.assertNotIn(
            "rm -rf",
            json.dumps(client.trusted_context, ensure_ascii=False),
        )

    def test_unsupported_stays_unsupported(self) -> None:
        client = RecordingClient(
            {
                "classification": "unsupported",
                "reason": "no supported operation matches",
            }
        )
        planner = LLMPlanner(
            config=LLMPlannerConfig(
                provider=LLMProvider.DEEPSEEK,
                model="deepseek-flash",
                api_key_present=True,
                api_key_env="DEEPSEEK_API_KEY",
            ),
            client=client,
        )
        outcome = planner.plan_request(
            TaskRequest(goal="deploy to production", operation=None, parameters={})
        )
        self.assertEqual(outcome.classification.value, "unsupported")
        self.assertIsNone(outcome.plan)


class SchemaConsistencyTests(unittest.TestCase):
    def test_missing_fields_schema_matches_runtime_validator(self) -> None:
        for schema in (CANDIDATE_JSON_SCHEMA, DEEPSEEK_CANDIDATE_JSON_SCHEMA):
            enum = set(
                schema["properties"]["missing_fields"]["items"]["enum"]
            )
            self.assertEqual(enum, set(ALLOWED_MISSING_FIELDS))

    def test_schema_still_rejects_forged_trusted_fields(self) -> None:
        validator = PlanningCandidateValidator()
        request = TaskRequest(
            goal="run tests", operation=None, parameters={"workspace": "demo"}
        )
        for field in ("evidence_id", "task_id", "execution_id", "execution_backend"):
            result = validator.validate(
                _candidate(**{field: "forged"}), base_request=request
            )
            self.assertEqual(result.classification.value, "failed", field)
            self.assertEqual(result.error_code, "unknown_candidate_fields", field)

    def test_clarification_can_request_workspace(self) -> None:
        validator = PlanningCandidateValidator()
        result = validator.validate(
            {
                "classification": "clarification",
                "missing_fields": ["workspace"],
                "clarification_questions": ["Which registered workspace should be used?"],
            },
            base_request=TaskRequest(goal="run tests", operation=None, parameters={}),
        )
        self.assertEqual(result.classification.value, "clarification")
        self.assertIn("workspace", result.missing_fields)


if __name__ == "__main__":
    unittest.main()
