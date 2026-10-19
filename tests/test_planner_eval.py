from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.cli import main  # noqa: E402
from zevorinth.evaluation import (  # noqa: E402
    DEFAULT_DATASET,
    DEFAULT_PILOT,
    DEFAULT_RETEST_PILOT,
    EvaluationConfigurationError,
    load_cases,
    load_pilot,
    online_preflight,
    run_evaluation,
    _run_case,
)
from zevorinth.llm_planner import (  # noqa: E402
    LLMPlanner,
    LLMPlannerConfig,
    ModelCallResult,
)
from zevorinth.models import (  # noqa: E402
    PlannerMode,
    PlanningClassification,
    PlanningOutcome,
    TaskRequest,
)


class PlannerEvaluationTests(unittest.TestCase):
    def test_dataset_has_required_coverage(self) -> None:
        dataset = load_cases()
        cases = dataset["cases"]
        ids = [case["id"] for case in cases]
        classifications = [case["expected_classification"] for case in cases]

        self.assertGreaterEqual(len(cases), 20)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(classifications.count("planned"), 3)
        self.assertGreaterEqual(classifications.count("clarification"), 3)
        self.assertGreaterEqual(classifications.count("unsupported"), 3)
        self.assertGreaterEqual(classifications.count("failed"), 3)
        self.assertGreaterEqual(sum(bool(case.get("unsafe")) for case in cases), 5)

    def test_scripted_offline_evaluation_passes_all_cases(self) -> None:
        report = run_evaluation(online=False)

        self.assertTrue(report["all_passed"], report["failures"])
        self.assertEqual(report["mode"], "offline-scripted")
        self.assertEqual(report["case_count"], len(load_cases()["cases"]))
        self.assertEqual(report["metrics"]["classification_accuracy"], 1.0)
        self.assertEqual(report["metrics"]["unsafe_side_effect_block_rate"], 1.0)
        self.assertEqual(report["metrics"]["unsafe_classification_accuracy"], 1.0)
        self.assertEqual(
            report["metrics"]["planned_case_end_to_end_success_rate"],
            1.0,
        )
        self.assertEqual(report["metrics"]["model_call_attempts"], 26)
        self.assertEqual(report["metrics"]["real_api_calls"], 0)
        self.assertEqual(report["metrics"]["scripted_client_calls"], 26)
        self.assertLess(
            report["metrics"]["candidate_validation_pass_rate"],
            1.0,
        )
        self.assertEqual(
            report["metrics"]["expected_non_executable_case_share"],
            18 / 28,
        )
        self.assertEqual(report["legacy_metrics"]["contract_validity_rate"], 1.0)
        self.assertEqual(report["legacy_metrics"]["model_calls"], 26)
        baseline = report["deterministic_baseline"]
        self.assertEqual(baseline["metrics"]["model_call_attempts"], 0)
        self.assertLess(
            baseline["metrics"]["classification_accuracy"],
            report["metrics"]["classification_accuracy"],
        )

    def test_online_evaluation_requires_explicit_config(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "",
                "ZEVORINTH_MODEL": "",
            },
            clear=False,
        ):
            with self.assertRaises(EvaluationConfigurationError):
                run_evaluation(online=True, pilot_path=DEFAULT_PILOT)

    def test_offline_cli_evaluation_is_machine_readable(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(["eval-planner", "--offline", "--json"])

        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(report["all_passed"], report["failures"])
        self.assertEqual(report["mode"], "offline-scripted")
        self.assertTrue(DEFAULT_DATASET.is_file())

    def test_online_cli_requires_configuration_and_makes_no_request(self) -> None:
        stderr = StringIO()
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "",
                "ZEVORINTH_MODEL": "",
            },
            clear=False,
        ), redirect_stderr(stderr):
            exit_code = main(
                [
                    "eval-planner",
                    "--online",
                    "--pilot",
                    "phase-2.5",
                    "--limit",
                    "5",
                    "--output",
                    str(Path(temp_dir) / "online.json"),
                ]
            )

        self.assertEqual(exit_code, 7)
        self.assertIn("configuration error", stderr.getvalue())

    def test_phase_25_pilot_has_required_fixed_coverage(self) -> None:
        dataset = load_cases()
        pilot = load_pilot()
        case_index = {case["id"]: case for case in dataset["cases"]}

        self.assertEqual(pilot["version"], "phase-2.5-v1")
        self.assertEqual(pilot["max_cases"], 5)
        self.assertEqual(
            [item["id"] for item in pilot["cases"]],
            [
                "create_basic_text",
                "clarify_ambiguous_goal",
                "unsupported_shell",
                "malicious_path_traversal",
                "prompt_injection_is_data",
            ],
        )
        for item in pilot["cases"]:
            self.assertEqual(
                item["expected_classification"],
                case_index[item["id"]]["expected_classification"],
            )

    def test_phase_27_retest_pilot_selects_exactly_two_historical_failures(self) -> None:
        pilot = load_pilot(DEFAULT_RETEST_PILOT)
        self.assertEqual(pilot["version"], "phase-2.7-v1")
        self.assertEqual(pilot["max_cases"], 2)
        self.assertEqual(
            [item["id"] for item in pilot["cases"]],
            ["clarify_ambiguous_goal", "unsupported_shell"],
        )

    def test_preflight_with_exact_case_ids_has_two_request_bound(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ZEVORINTH_LLM_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": "test-only-key",
                "ZEVORINTH_MODEL": "deepseek-flash",
            },
            clear=False,
        ), patch(
            "zevorinth.evaluation.openai_sdk_status",
            return_value={
                "installed": True,
                "version": "3.35.1",
                "minimum_version": "3.35.1",
                "compatible": True,
                "module_path": "/fake/openai/__init__.py",
            },
        ):
            report = online_preflight(
                case_ids=["clarify_ambiguous_goal", "unsupported_shell"],
            )

        self.assertEqual(report["selection_mode"], "case_ids")
        self.assertEqual(
            report["selected_case_ids"],
            ["clarify_ambiguous_goal", "unsupported_shell"],
        )
        self.assertEqual(report["request_boundaries"]["maximum_requests"], 2)
        self.assertFalse(report["network_request_sent"])
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("帮我处理一下", serialized)
        self.assertNotIn("rm -rf", serialized)

    def test_case_id_preflight_cli_selects_no_other_cases(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "ZEVORINTH_LLM_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": "test-only-key",
                "ZEVORINTH_MODEL": "deepseek-flash",
            },
            clear=False,
        ), patch(
            "zevorinth.evaluation.openai_sdk_status",
            return_value={
                "installed": True,
                "version": "3.35.1",
                "minimum_version": "3.35.1",
                "compatible": True,
                "module_path": "/fake/openai/__init__.py",
            },
        ), redirect_stdout(output):
            exit_code = main(
                [
                    "eval-planner",
                    "--online",
                    "--case-id",
                    "clarify_ambiguous_goal",
                    "--case-id",
                    "unsupported_shell",
                    "--preflight",
                    "--output",
                    str(Path(temp_dir) / "retest.json"),
                    "--json",
                ]
            )

        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            report["selected_case_ids"],
            ["clarify_ambiguous_goal", "unsupported_shell"],
        )
        self.assertEqual(report["request_boundaries"]["maximum_requests"], 2)
        self.assertFalse(report["network_request_sent"])

    def test_online_execution_requires_explicit_output_path(self) -> None:
        stderr = StringIO()
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-only-key",
                "ZEVORINTH_MODEL": "configured-model",
            },
            clear=False,
        ), redirect_stderr(stderr):
            exit_code = main(
                [
                    "eval-planner",
                    "--online",
                    "--case-id",
                    "clarify_ambiguous_goal",
                ]
            )

        self.assertEqual(exit_code, 7)
        self.assertIn("explicit --output", stderr.getvalue())

    def test_online_execution_refuses_to_overwrite_existing_report(self) -> None:
        stderr = StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "existing-report.json"
            output_path.write_text('{"preserved": true}\n', encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-only-key",
                    "ZEVORINTH_MODEL": "configured-model",
                },
                clear=False,
            ), patch(
                "zevorinth.cli.run_evaluation",
                side_effect=AssertionError("evaluation must not start"),
            ), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "eval-planner",
                        "--online",
                        "--case-id",
                        "clarify_ambiguous_goal",
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 7)
            self.assertIn("refuses to overwrite", stderr.getvalue())
            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                '{"preserved": true}\n',
            )

    def test_preflight_rejects_limit_before_any_request(self) -> None:
        stderr = StringIO()
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "ZEVORINTH_LLM_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": "test-only-key",
                "ZEVORINTH_MODEL": "deepseek-flash",
            },
            clear=False,
        ), redirect_stderr(stderr):
            exit_code = main(
                [
                    "eval-planner",
                    "--online",
                    "--pilot",
                    "phase-2.7-retest",
                    "--preflight",
                    "--limit",
                    "1",
                    "--output",
                    str(Path(temp_dir) / "retest.json"),
                ]
            )

        self.assertEqual(exit_code, 7)
        self.assertIn("does not accept --limit", stderr.getvalue())

    def test_phase_27_pilot_rejects_limit_resize_before_client_creation(self) -> None:
        stderr = StringIO()
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "ZEVORINTH_LLM_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": "test-only-key",
                "ZEVORINTH_MODEL": "deepseek-flash",
            },
            clear=False,
        ), patch(
            "zevorinth.llm_planner.LLMPlanner.from_environment",
            side_effect=AssertionError("client must not be created"),
        ), redirect_stderr(stderr):
            exit_code = main(
                [
                    "eval-planner",
                    "--online",
                    "--pilot",
                    "phase-2.7-retest",
                    "--limit",
                    "1",
                    "--output",
                    str(Path(temp_dir) / "retest.json"),
                ]
            )

        self.assertEqual(exit_code, 7)
        self.assertIn("limit cannot change", stderr.getvalue())

    def test_online_preflight_sends_no_request_and_shows_boundaries(self) -> None:
        output_path = PROJECT_ROOT / "artifacts" / "evaluations" / "pilot.json"
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "",
                "ZEVORINTH_MODEL": "",
                "ZEVORINTH_LLM_TIMEOUT_SECONDS": "12",
                "ZEVORINTH_LLM_MAX_OUTPUT_TOKENS": "321",
            },
            clear=False,
        ):
            report = online_preflight(output_path=output_path)

        self.assertFalse(report["network_request_sent"])
        self.assertTrue(report["requires_explicit_approval"])
        self.assertFalse(report["ready_for_approval"])
        self.assertEqual(report["request_boundaries"]["maximum_requests"], 5)
        self.assertEqual(report["request_boundaries"]["retries"], 0)
        self.assertEqual(
            report["request_boundaries"]["timeout_seconds_per_case"],
            12,
        )
        self.assertEqual(
            report["configuration"]["minimum_sdk_version"],
            "3.35.1",
        )
        self.assertIn(
            report["configuration"]["sdk_compatible"],
            {True, False},
        )
        self.assertEqual(
            report["file_effects"]["persistent_result_path"],
            str(output_path),
        )

    def test_online_requires_explicit_case_selection(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-only-key",
                "ZEVORINTH_MODEL": "configured-test-model",
            },
            clear=False,
        ):
            with self.assertRaises(EvaluationConfigurationError):
                run_evaluation(online=True)

    def test_deepseek_preflight_is_ready_with_compatible_sdk_without_network(self) -> None:
        secret = "deepseek-preflight-secret"
        output_path = PROJECT_ROOT / "artifacts" / "evaluations" / "never-created.json"
        with patch.dict(
            os.environ,
            {
                "ZEVORINTH_LLM_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": secret,
                "ZEVORINTH_MODEL": "deepseek-flash",
            },
            clear=False,
        ), patch(
            "zevorinth.evaluation.openai_sdk_status",
            return_value={
                "installed": True,
                "version": "3.35.1",
                "minimum_version": "3.35.1",
                "compatible": True,
                "module_path": "/fake/openai/__init__.py",
            },
        ):
            report = online_preflight(output_path=output_path)

        self.assertTrue(report["ready_for_approval"])
        self.assertFalse(report["network_request_sent"])
        self.assertEqual(report["configuration"]["provider"], "deepseek")
        self.assertEqual(
            report["configuration"]["base_url"],
            "https://api.deepseek.com",
        )
        self.assertTrue(report["configuration"]["api_key_configured"])
        self.assertNotIn(secret, json.dumps(report))
        self.assertFalse(output_path.exists())

    def test_unexpected_planning_error_cannot_pass_clarification_case(self) -> None:
        class FailingPlanner:
            mode = PlannerMode.LLM
            name = "test-failure-planner"

            def plan_request(self, request: TaskRequest) -> PlanningOutcome:
                return PlanningOutcome(
                    classification=PlanningClassification.FAILED,
                    error_code="invalid_candidate_fields",
                    error_message="unexpected planning failure",
                )

        case = next(
            case
            for case in load_cases()["cases"]
            if case["id"] == "clarify_ambiguous_goal"
        )
        result = _run_case(case, FailingPlanner(), None)
        self.assertFalse(result.error_code_correct)
        self.assertFalse(result.passed)

    def test_case_diagnostics_are_whitelisted_and_never_include_values(self) -> None:
        class ResultClient:
            def create_candidate(self, **_: object) -> ModelCallResult:
                return ModelCallResult(
                    candidate={
                        "classification": "plan",
                        "operation": "write_text_artifact",
                        "artifact_path": "result.txt",
                        "content": "secret candidate content",
                        "missing_fields": [],
                        "clarification_questions": [],
                        "reason": None,
                        "hidden_instruction": "secret extra value",
                    },
                    status="completed",
                    text_present=True,
                    text_length=123,
                    output_item_types=["message"],
                    content_part_types=["output_text"],
                    http_status=200,
                )

        planner = LLMPlanner(
            config=LLMPlannerConfig(
                model="test-model",
                api_key_present=True,
            ),
            client=ResultClient(),
        )
        goal_secret = "secret user goal"
        case = {
            "id": "privacy_case",
            "goal": goal_secret,
            "expected_classification": "planned",
            "expected_status": "succeeded",
            "unsafe": False,
            "script": "unused",
        }
        result = _run_case(case, planner, None)
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)

        self.assertEqual(result.observed_error_code, "unknown_candidate_fields")
        self.assertEqual(result.candidate_unknown_fields, ["hidden_instruction"])
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.output_item_types, ["message"])
        self.assertEqual(result.content_part_types, ["output_text"])
        self.assertNotIn(goal_secret, serialized)
        self.assertNotIn("secret extra value", serialized)
        self.assertNotIn("secret candidate content", serialized)

    def test_online_preflight_cli_does_not_require_network(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "",
                "ZEVORINTH_MODEL": "",
            },
            clear=False,
        ), redirect_stdout(output):
            exit_code = main(
                [
                    "eval-planner",
                    "--online",
                    "--pilot",
                    "phase-2.5",
                    "--preflight",
                    "--output",
                    str(Path(temp_dir) / "pilot.json"),
                    "--json",
                ]
            )
            report_path = Path(temp_dir) / "pilot.json"

        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 7)
        self.assertFalse(report["network_request_sent"])
        self.assertFalse(report_path.exists())
        self.assertEqual(
            report["selected_case_ids"],
            [
                "create_basic_text",
                "clarify_ambiguous_goal",
                "unsupported_shell",
                "malicious_path_traversal",
                "prompt_injection_is_data",
            ],
        )

    def test_approved_online_path_writes_result_to_requested_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "online-result.json"
            fake_report = {
                "mode": "online",
                "model_source": "online_api",
                "all_passed": True,
                "passed": 5,
                "case_count": 5,
                "metrics": {},
                "failures": [],
            }
            output = StringIO()
            with patch(
                "zevorinth.cli.run_evaluation",
                return_value=fake_report,
            ) as evaluation_call, redirect_stdout(output):
                exit_code = main(
                    [
                        "eval-planner",
                        "--online",
                        "--pilot",
                        "phase-2.5",
                        "--output",
                        str(report_path),
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(report_path.is_file())
            persisted = json.loads(report_path.read_text())
            self.assertEqual(persisted["report_path"], str(report_path.resolve()))
            self.assertEqual(
                evaluation_call.call_args.kwargs["pilot_path"].name,
                "online_pilot_v1.json",
            )


if __name__ == "__main__":
    unittest.main()
