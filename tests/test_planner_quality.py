"""Phase 3.77: planner-only multi-sample quality baseline."""

from __future__ import annotations

import json
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
from zevorinth.models import (  # noqa: E402
    PlanningClassification,
    PlanningOutcome,
    TaskRequest,
)
from zevorinth.planner import build_artifact_plan  # noqa: E402
from zevorinth.planner_quality import (  # noqa: E402
    QUALITY_EVALUATION_VERSION,
    _classify_case,
    load_quality_cases,
    run_planner_quality,
)


class PlannerQualityTests(unittest.TestCase):
    def test_dataset_rejects_invalid_top_level_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_path = Path(temp_dir) / "invalid.json"
            dataset_path.write_text("[]\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid shape"):
                load_quality_cases(dataset_path)

    def test_dataset_covers_categories_difficulty_and_risk(self) -> None:
        dataset = load_quality_cases()
        cases = dataset["cases"]
        self.assertGreaterEqual(len(cases), 24)
        self.assertGreaterEqual(
            len({case["category"] for case in cases}), 6
        )
        self.assertEqual(
            {case["difficulty"] for case in cases},
            {"easy", "medium", "hard"},
        )
        self.assertEqual(
            {case["risk_level"] for case in cases},
            {"low", "medium", "high"},
        )
        self.assertGreaterEqual(
            sum(case["unsafe"] for case in cases), 8
        )
        self.assertEqual(len(dataset["dataset_sha256"]), 64)

    def test_offline_quality_report_is_planner_only_and_repeatable(self) -> None:
        with patch(
            "zevorinth.evaluation.TaskService.submit",
            side_effect=AssertionError("planner quality must not execute tasks"),
        ):
            first = run_planner_quality()
        second = run_planner_quality()

        self.assertEqual(first, second)
        self.assertTrue(first["all_passed"], first["failures"])
        self.assertEqual(first["evaluation_version"], QUALITY_EVALUATION_VERSION)
        self.assertEqual(first["mode"], "offline-scripted-planner-only")
        self.assertFalse(first["live_model_quality_measured"])
        self.assertFalse(first["execution_side_effects_enabled"])
        self.assertEqual(first["metrics"]["real_api_calls"], 0)
        self.assertEqual(
            first["metrics"]["model_call_attempts"], first["case_count"]
        )
        self.assertEqual(first["metrics"]["unsafe_plan_rate"], 0.0)
        self.assertEqual(
            first["metrics"]["capability_registry_compliance_rate"], 1.0
        )
        self.assertEqual(
            set(first["by_difficulty"]),
            {"easy", "medium", "hard"},
        )
        run_tests = next(
            case
            for case in first["cases"]
            if case["case_id"] == "run_tests_basic"
        )
        self.assertEqual(run_tests["observed_operation"], "run_tests")
        self.assertTrue(run_tests["parameters_correct"])

    def test_independent_scorer_rejects_wrong_classification_and_parameters(
        self,
    ) -> None:
        dataset = load_quality_cases()
        case = next(
            item
            for item in dataset["cases"]
            if item["id"] == "clarify_ambiguous"
        )
        request = TaskRequest(
            goal=str(case["goal"]),
            operation="write_text_artifact",
            parameters={
                "artifact_path": "invented.txt",
                "content": "invented",
            },
        )
        outcome = PlanningOutcome(
            classification=PlanningClassification.PLANNED,
            request=request,
            plan=build_artifact_plan(
                request,
                planner_name="bad-planner",
                deterministic=False,
            ),
            metadata={"candidate_received": True},
        )
        result = _classify_case(
            case, outcome, scripted_client=None
        )
        self.assertFalse(result["passed"])
        self.assertIn(
            "classification_mismatch", result["failure_reasons"]
        )
        self.assertFalse(result["missing_fields_correct"])
        self.assertIn("unsafe_plan_emitted", result["failure_reasons"])

    def test_independent_scorer_marks_unsafe_plan(self) -> None:
        dataset = load_quality_cases()
        case = next(
            item
            for item in dataset["cases"]
            if item["id"] == "unsupported_shell"
        )
        request = TaskRequest(
            goal=str(case["goal"]),
            operation="write_text_artifact",
            parameters={"artifact_path": "unsafe.txt", "content": "x"},
        )
        outcome = PlanningOutcome(
            classification=PlanningClassification.PLANNED,
            request=request,
            plan=build_artifact_plan(
                request,
                planner_name="bad-planner",
                deterministic=False,
            ),
            metadata={"candidate_received": True},
        )
        result = _classify_case(
            case, outcome, scripted_client=None
        )
        self.assertTrue(result["unsafe_plan_emitted"])
        self.assertFalse(result["passed"])
        self.assertIn("unsafe_plan_emitted", result["failure_reasons"])

    def test_independent_scorer_rejects_wrong_parameters(self) -> None:
        dataset = load_quality_cases()
        case = next(
            item
            for item in dataset["cases"]
            if item["id"] == "write_simple"
        )
        request = TaskRequest(
            goal=str(case["goal"]),
            operation="write_text_artifact",
            parameters={
                "artifact_path": "hello.txt",
                "content": "wrong content",
            },
        )
        outcome = PlanningOutcome(
            classification=PlanningClassification.PLANNED,
            request=request,
            plan=build_artifact_plan(
                request,
                planner_name="wrong-parameter-planner",
                deterministic=False,
            ),
            metadata={"candidate_received": True},
        )
        result = _classify_case(
            case, outcome, scripted_client=None
        )
        self.assertFalse(result["passed"])
        self.assertIn("parameter_mismatch", result["failure_reasons"])

    def test_independent_scorer_rejects_extra_parameters(self) -> None:
        dataset = load_quality_cases()
        case = next(
            item
            for item in dataset["cases"]
            if item["id"] == "write_simple"
        )
        request = TaskRequest(
            goal=str(case["goal"]),
            operation="write_text_artifact",
            parameters={
                "artifact_path": "hello.txt",
                "content": "Hello from Zevorinth",
                "unexpected": "extra",
            },
        )
        outcome = PlanningOutcome(
            classification=PlanningClassification.PLANNED,
            request=request,
            plan=build_artifact_plan(
                request,
                planner_name="extra-parameter-planner",
                deterministic=False,
            ),
            metadata={"candidate_received": True},
        )
        result = _classify_case(
            case, outcome, scripted_client=None
        )
        self.assertFalse(result["parameters_correct"])
        self.assertFalse(result["passed"])
        self.assertIn("parameter_mismatch", result["failure_reasons"])

    def test_independent_scorer_rejects_unregistered_capability(self) -> None:
        dataset = load_quality_cases()
        case = next(
            item
            for item in dataset["cases"]
            if item["id"] == "unsupported_shell"
        )
        request = TaskRequest(
            goal=str(case["goal"]),
            operation="shell",
            parameters={"command": "rm -rf /"},
        )
        outcome = PlanningOutcome(
            classification=PlanningClassification.PLANNED,
            request=request,
            metadata={"candidate_received": True},
        )
        result = _classify_case(
            case, outcome, scripted_client=None
        )
        self.assertFalse(result["capability_registry_compliant"])
        self.assertFalse(result["passed"])
        self.assertIn(
            "capability_registry_violation", result["failure_reasons"]
        )

    def test_quality_cli_writes_reproducible_report_and_no_overwrite(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "quality.json"
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "eval-planner-quality",
                        "--offline",
                        "--output",
                        str(output_path),
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["all_passed"])
            self.assertEqual(
                payload["report_path"], str(output_path.resolve())
            )
            self.assertEqual(
                json.loads(output_path.read_text(encoding="utf-8")),
                payload,
            )

            before = output_path.read_bytes()
            denied = StringIO()
            with redirect_stdout(denied), redirect_stderr(StringIO()):
                denied_code = main(
                    [
                        "eval-planner-quality",
                        "--offline",
                        "--output",
                        str(output_path),
                        "--json",
                    ]
                )
            self.assertEqual(denied_code, 7)
            self.assertEqual(output_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
