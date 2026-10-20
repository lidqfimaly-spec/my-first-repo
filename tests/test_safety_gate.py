from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.cli import main  # noqa: E402
from zevorinth.safety_gate import (  # noqa: E402
    SafetyGateConfigurationError,
    load_gate_dataset,
    run_safety_gate,
)


class SafetyGateTests(unittest.TestCase):
    def test_gate_passes_with_layered_cases_and_bounded_metrics(self) -> None:
        report = run_safety_gate()

        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["all_passed"])
        self.assertEqual(report["planner_case_count"], 28)
        self.assertEqual(report["local_case_count"], 11)
        self.assertEqual(report["case_count"], 39)
        self.assertEqual(report["real_api_calls"], 0)
        self.assertFalse(report["network_request_sent"])
        self.assertEqual(report["metrics"]["duplicate_side_effects"], 0)
        self.assertEqual(report["metrics"]["failure_counts"], {})
        for metric in (
            "classification_accuracy",
            "status_accuracy",
            "normal_plan_success_rate",
            "safety_block_pass_rate",
        ):
            self.assertGreaterEqual(report["metrics"][metric], 0.0)
            self.assertLessEqual(report["metrics"][metric], 1.0)

    def test_invalid_dataset_shapes_fail_closed(self) -> None:
        invalid_payloads = [
            {"cases": [], "required_categories": []},
            {
                "cases": [{"id": "x"}],
                "required_categories": ["policy"],
            },
            {
                "cases": [
                    {
                        "id": "duplicate",
                        "category": "policy",
                        "kind": "policy_block",
                        "component": "ArtifactPolicy",
                        "input": {},
                        "expected": {},
                        "forbidden_actions": [],
                        "design_reason": "test",
                    },
                    {
                        "id": "duplicate",
                        "category": "policy",
                        "kind": "policy_block",
                        "component": "ArtifactPolicy",
                        "input": {},
                        "expected": {},
                        "forbidden_actions": [],
                        "design_reason": "test",
                    },
                ],
                "required_categories": ["policy"],
            },
            {
                "cases": [
                    {
                        "id": "unknown",
                        "category": "policy",
                        "kind": "missing_handler",
                        "component": "x",
                        "input": {},
                        "expected": {},
                        "forbidden_actions": [],
                        "design_reason": "test",
                    }
                ],
                "required_categories": ["policy"],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            for index, payload in enumerate(invalid_payloads):
                path = Path(temp_dir) / f"invalid-{index}.json"
                path.write_text(json.dumps(payload))
                with self.subTest(index=index):
                    with self.assertRaises(SafetyGateConfigurationError):
                        load_gate_dataset(path)

    def test_inconsistent_planner_report_fails_gate(self) -> None:
        fake_report = {
            "dataset_version": "1",
            "metrics": {
                "classification_accuracy": 0.0,
                "candidate_validation_pass_rate": 0.0,
            },
            "cases": [
                {
                    "case_id": "broken",
                    "expected_classification": "planned",
                    "observed_classification": "failed",
                    "expected_status": "succeeded",
                    "observed_status": "planning_failed",
                    "tool_executed": False,
                    "artifact_created": False,
                    "candidate_valid": False,
                    "execution_mode": "not_executed",
                    "latency_ms": 1.0,
                    "passed": False,
                }
            ],
        }
        with patch(
            "zevorinth.safety_gate.run_evaluation",
            return_value=fake_report,
        ):
            report = run_safety_gate()
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["all_passed"])
        self.assertIn("missing_categories", " ".join(report["failure_reasons"]))

    def test_cli_refuses_to_overwrite_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "report.json"
            output.write_text("{}")
            code = main(
                [
                    "eval-safety-gate",
                    "--offline",
                    "--output",
                    str(output),
                    "--json",
                ]
            )
            self.assertEqual(code, 7)


if __name__ == "__main__":
    unittest.main()
