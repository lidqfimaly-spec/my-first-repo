"""Phase 3.89: semantic, format, and exact code-point text contracts."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.llm_planner import (  # noqa: E402
    LLMPlanner,
    LLMPlannerConfig,
)
from zevorinth.models import (  # noqa: E402
    PlanningClassification,
    TaskRequest,
    TaskStatus,
)
from zevorinth.planner_exact_text_runner import (  # noqa: E402
    run_exact_text_regression,
)
from zevorinth.planner_text_precision import (  # noqa: E402
    load_exact_text_contract,
)
from zevorinth.response_normalization import ModelCallResult  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


class RecordingClient:
    def __init__(self, candidate: dict[str, Any]) -> None:
        self.candidate = candidate
        self.calls = 0
        self.trusted_context: dict[str, Any] | None = None

    def create_candidate(
        self,
        *,
        goal: str,
        instructions: str,
        max_output_tokens: int,
        trusted_context: dict[str, Any] | None = None,
    ) -> ModelCallResult:
        del goal, instructions, max_output_tokens
        self.calls += 1
        self.trusted_context = dict(trusted_context or {})
        return ModelCallResult(
            candidate=self.candidate,
            status="completed",
            text_present=True,
            output_bytes=128,
        )


def _write_candidate(content: str, *, path: str = "exact.txt") -> dict[str, Any]:
    return {
        "classification": "plan",
        "operation": "write_text_artifact",
        "artifact_path": path,
        "content": content,
        "missing_fields": [],
        "clarification_questions": [],
        "reason": None,
    }


def _planner(candidate: dict[str, Any]) -> tuple[LLMPlanner, RecordingClient]:
    client = RecordingClient(candidate)
    return (
        LLMPlanner(
            config=LLMPlannerConfig(
                model="offline-exact-text",
                api_key_present=True,
            ),
            client=client,
        ),
        client,
    )


class ExactTextContractTests(unittest.TestCase):
    def test_contract_freezes_three_non_normalizing_modes(self) -> None:
        contract = load_exact_text_contract()
        self.assertEqual(contract["version"], "planner-exact-text-v1")
        self.assertFalse(contract["retroactively_applicable"])
        self.assertEqual(
            set(contract["modes"]),
            {"semantic", "format_constrained", "exact_text"},
        )
        self.assertTrue(
            all(
                mode["normalizes_text"] is False
                for mode in contract["modes"].values()
            )
        )
        self.assertEqual(len(contract["contract_sha256"]), 64)

    def test_contract_rejects_version_duplicate_and_normalization_drift(
        self,
    ) -> None:
        original = load_exact_text_contract()
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            version = directory / "version.json"
            payload = dict(original)
            payload.pop("contract_sha256")
            payload["version"] = "planner-exact-text-v0"
            version.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "version mismatch"):
                load_exact_text_contract(version)

            duplicate = directory / "duplicate.json"
            duplicate.write_text(
                '{"version":"one","version":"two"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate key"):
                load_exact_text_contract(duplicate)

            normalized = directory / "normalized.json"
            payload = dict(original)
            payload.pop("contract_sha256")
            payload["modes"] = json.loads(
                json.dumps(payload["modes"])
            )
            payload["modes"]["exact_text"]["normalizes_text"] = True
            normalized.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "normalizes text"):
                load_exact_text_contract(normalized)

    def test_runner_is_repeatable_and_has_no_execution_side_effects(
        self,
    ) -> None:
        with patch(
            "zevorinth.evaluation.TaskService.submit",
            side_effect=AssertionError("exact text runner must not submit"),
        ):
            first = run_exact_text_regression()
        second = run_exact_text_regression()
        self.assertEqual(first, second)
        self.assertTrue(first["all_passed"], first["cases"])
        self.assertFalse(first["live_model_quality_measured"])
        self.assertFalse(first["execution_side_effects_enabled"])
        self.assertEqual(first["metrics"]["case_count"], 14)
        self.assertEqual(first["metrics"]["passed"], 14)
        self.assertEqual(first["metrics"]["exact_mismatch_plan_count"], 0)
        self.assertEqual(first["metrics"]["format_mismatch_plan_count"], 0)
        self.assertEqual(first["metrics"]["execution_attempts"], 0)
        self.assertEqual(first["metrics"]["real_api_calls"], 0)
        self.assertEqual(first["metrics"]["task_submissions"], 0)

    def test_runner_rejects_a_planner_that_claims_normalization(self) -> None:
        from zevorinth import llm_planner

        original = llm_planner.apply_text_precision

        def claim_normalization(*args: Any, **kwargs: Any):
            outcome = original(*args, **kwargs)
            text_precision = outcome.metadata.get("text_precision")
            if isinstance(text_precision, dict):
                text_precision["normalized_before_compare"] = True
            return outcome

        with patch(
            "zevorinth.llm_planner.apply_text_precision",
            side_effect=claim_normalization,
        ):
            report = run_exact_text_regression()
        self.assertFalse(report["all_passed"])
        self.assertTrue(
            any(
                "text_was_normalized" in case["failure_reasons"]
                for case in report["cases"]
            )
        )


class ExactTextEnforcementTests(unittest.TestCase):
    def test_write_simple_semantic_punctuation_is_not_a_system_failure(
        self,
    ) -> None:
        planner, client = _planner(
            _write_candidate(
                "Hello from Zevorinth。",
                path="hello.txt",
            )
        )
        outcome = planner.plan_request(
            TaskRequest(
                goal="创建 hello.txt，内容写 Hello from Zevorinth。",
                operation=None,
                parameters={},
            )
        )
        self.assertEqual(
            outcome.classification,
            PlanningClassification.PLANNED,
        )
        self.assertEqual(client.calls, 1)
        precision = outcome.metadata["text_precision"]
        self.assertEqual(precision["mode"], "semantic")
        self.assertEqual(precision["mode_source"], "default_semantic")
        self.assertEqual(precision["result"], "semantic")
        self.assertFalse(precision["normalized_before_compare"])

    def test_exact_match_uses_visible_trusted_text_without_normalization(
        self,
    ) -> None:
        expected = "line1\nline2"
        planner, client = _planner(_write_candidate(expected))
        outcome = planner.plan_request(
            TaskRequest(
                goal="创建 exact.txt，内容按可信规范提供。",
                operation=None,
                parameters={
                    "precision_mode": "exact_text",
                    "expected_text": expected,
                },
            )
        )
        self.assertEqual(
            outcome.classification,
            PlanningClassification.PLANNED,
        )
        self.assertEqual(client.trusted_context["precision_mode"], "exact_text")
        self.assertEqual(client.trusted_context["expected_text"], expected)
        precision = outcome.metadata["text_precision"]
        self.assertEqual(precision["result"], "exact_match")
        self.assertFalse(precision["normalized_before_compare"])
        self.assertEqual(precision["expected_length"], len(expected))
        self.assertEqual(
            precision["expected_sha256"],
            precision["observed_sha256"],
        )

    def test_unicode_whitespace_and_newline_differences_fail_closed(
        self,
    ) -> None:
        cases = [
            ("é", "e\u0301"),
            ("abc", " abc "),
            ("line1\nline2", "line1\nline2\n"),
            ("任务完成", "任务完成。"),
        ]
        for expected, observed in cases:
            with self.subTest(expected=expected, observed=observed):
                planner, client = _planner(_write_candidate(observed))
                outcome = planner.plan_request(
                    TaskRequest(
                        goal="创建 exact.txt，内容按可信规范提供。",
                        operation=None,
                        parameters={
                            "precision_mode": "exact_text",
                            "expected_text": expected,
                        },
                    )
                )
                self.assertEqual(client.calls, 1)
                self.assertEqual(
                    outcome.classification,
                    PlanningClassification.FAILED,
                )
                self.assertEqual(
                    outcome.error_code,
                    "text_precision_mismatch",
                )
                self.assertIsNone(outcome.plan)
                precision = outcome.metadata["text_precision"]
                self.assertEqual(precision["result"], "exact_mismatch")
                self.assertNotEqual(
                    precision["expected_sha256"],
                    precision["observed_sha256"],
                )
                self.assertFalse(precision["normalized_before_compare"])

    def test_exact_mismatch_keeps_intent_assessment_independent(self) -> None:
        planner, _ = _planner(_write_candidate("wrong"))
        outcome = planner.plan_request(
            TaskRequest(
                goal="创建 exact.txt，内容按可信规范提供。",
                operation=None,
                parameters={
                    "precision_mode": "exact_text",
                    "expected_text": "right",
                },
            )
        )
        self.assertEqual(outcome.error_code, "text_precision_mismatch")
        self.assertEqual(outcome.metadata["text_precision_gate"], "blocked")
        intent = outcome.metadata["intent_fidelity"]
        self.assertEqual(intent["status"], "planned_faithful")
        self.assertTrue(intent["fidelity_pass"])
        self.assertFalse(intent["execution_attempted"])
        self.assertEqual(
            outcome.metadata["intent_fidelity_gate"],
            "terminal_non_execution",
        )

    def test_contract_errors_fail_before_model_call(self) -> None:
        requests = [
            TaskRequest(
                goal="创建 exact.txt，内容按可信规范提供。",
                operation=None,
                parameters={"precision_mode": "exact_text"},
            ),
            TaskRequest(
                goal="创建 exact.txt，内容写 ok。",
                operation=None,
                parameters={"precision_mode": "unknown"},
            ),
            TaskRequest(
                goal="创建 data.json，内容为格式数据。",
                operation=None,
                parameters={"precision_mode": "format_constrained"},
            ),
        ]
        for request in requests:
            with self.subTest(parameters=request.parameters):
                planner, client = _planner(_write_candidate("ok"))
                outcome = planner.plan_request(request)
                self.assertEqual(client.calls, 0)
                self.assertEqual(
                    outcome.classification,
                    PlanningClassification.FAILED,
                )
                self.assertEqual(
                    outcome.error_code,
                    "text_precision_contract_error",
                )
                self.assertEqual(
                    outcome.metadata["text_precision_gate"],
                    "blocked",
                )
                self.assertIsNone(outcome.plan)

    def test_contract_loader_error_fails_closed_before_client(self) -> None:
        planner, client = _planner(_write_candidate("ok"))
        with patch(
            "zevorinth.llm_planner.load_exact_text_contract",
            side_effect=ValueError("contract unavailable"),
        ):
            outcome = planner.plan_request(
                TaskRequest(
                    goal="创建 ok.txt，内容写 ok。",
                    operation=None,
                    parameters={},
                )
            )
        self.assertEqual(client.calls, 0)
        self.assertEqual(
            outcome.error_code,
            "text_precision_contract_error",
        )
        self.assertIsNone(outcome.plan)
        self.assertEqual(outcome.metadata["text_precision_gate"], "blocked")

    def test_format_constrained_accepts_and_rejects_declared_format(
        self,
    ) -> None:
        accepted, _ = _planner(
            _write_candidate('{"ok": true}', path="data.json")
        )
        accepted_outcome = accepted.plan_request(
            TaskRequest(
                goal="创建 data.json，内容按可信规范提供。",
                operation=None,
                parameters={
                    "precision_mode": "format_constrained",
                    "content_format": "json_object",
                },
            )
        )
        self.assertEqual(
            accepted_outcome.classification,
            PlanningClassification.PLANNED,
        )
        self.assertEqual(
            accepted_outcome.metadata["text_precision"]["result"],
            "format_match",
        )

        rejected, _ = _planner(
            _write_candidate("not-json", path="data.json")
        )
        rejected_outcome = rejected.plan_request(
            TaskRequest(
                goal="创建 data.json，内容按可信规范提供。",
                operation=None,
                parameters={
                    "precision_mode": "format_constrained",
                    "content_format": "json_object",
                },
            )
        )
        self.assertEqual(
            rejected_outcome.classification,
            PlanningClassification.FAILED,
        )
        self.assertEqual(
            rejected_outcome.error_code,
            "text_format_mismatch",
        )
        self.assertIsNone(rejected_outcome.plan)
        self.assertEqual(
            rejected_outcome.metadata["text_precision_gate"],
            "blocked",
        )

    def test_service_blocks_exact_mismatch_without_task_or_tool_events(
        self,
    ) -> None:
        planner, client = _planner(_write_candidate("wrong"))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, planner=planner)
            task = service.submit(
                TaskRequest(
                    goal="创建 exact.txt，内容按可信规范提供。",
                    operation=None,
                    parameters={
                        "precision_mode": "exact_text",
                        "expected_text": "right",
                    },
                )
            )
            events = service.events(task.task_id)
            artifact_exists = root.joinpath("exact.txt").exists()

        self.assertEqual(client.calls, 1)
        self.assertEqual(task.status, TaskStatus.PLANNING_FAILED)
        self.assertEqual(task.error.code, "text_precision_mismatch")
        self.assertIsNone(task.plan)
        self.assertFalse(artifact_exists)
        event_types = [event.event_type for event in events]
        self.assertNotIn("task_planned", event_types)
        self.assertNotIn("tool_started", event_types)
        self.assertNotIn("tool_succeeded", event_types)
        failed_event = next(
            event
            for event in events
            if event.event_type == "planning_failed"
        )
        metadata = failed_event.payload["planning_metadata"]
        self.assertEqual(metadata["text_precision_gate"], "blocked")
        self.assertEqual(
            metadata["text_precision"]["result"],
            "exact_mismatch",
        )
        self.assertEqual(
            metadata["intent_fidelity"]["status"],
            "planned_faithful",
        )


if __name__ == "__main__":
    unittest.main()
