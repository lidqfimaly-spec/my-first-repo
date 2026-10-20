from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.e2e_acceptance_runner import run_acceptance  # noqa: E402
from tests.eval_compare import (  # noqa: E402
    compare_acceptance_reports,
    compare_reports,
    detect_report_kind,
)
from zevorinth.safety_gate import run_safety_gate  # noqa: E402


class EvalCompareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = run_safety_gate()

    def test_identical_reports_compare_cleanly(self) -> None:
        result = compare_reports(self.report, copy.deepcopy(self.report))
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["comparable"])
        self.assertEqual(result["removed_cases"], [])
        self.assertEqual(result["added_cases"], [])
        self.assertEqual(result["regressions"], [])

    def test_legacy_report_without_denominators_is_not_comparable(self) -> None:
        legacy = copy.deepcopy(self.report)
        legacy["metric_audit"] = {
            name: {"value": metric["value"]}
            for name, metric in legacy["metric_audit"].items()
        }
        result = compare_reports(legacy, self.report)
        self.assertEqual(result["status"], "NOT_COMPARABLE")
        self.assertFalse(result["comparable"])
        self.assertTrue(result["reasons"])

    def test_metric_definition_change_is_not_comparable(self) -> None:
        candidate = copy.deepcopy(self.report)
        candidate["metric_definition_version"] = "0.0.0"
        result = compare_reports(self.report, candidate)
        self.assertEqual(result["status"], "NOT_COMPARABLE")

    def test_removed_case_is_reported_as_regression(self) -> None:
        candidate = copy.deepcopy(self.report)
        removed = candidate["cases"].pop(0)
        result = compare_reports(self.report, candidate)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["removed_cases"], [removed["id"]])
        self.assertIn(
            f"case_removed:{removed['id']}:was_passing",
            result["regressions"],
        )

    def test_denominator_shrink_is_reported(self) -> None:
        candidate = copy.deepcopy(self.report)
        metric = candidate["metric_audit"]["classification_accuracy"]
        metric["denominator"] = metric["denominator"] - 1
        result = compare_reports(self.report, candidate)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn(
            "denominator_shrunk:classification_accuracy",
            result["regressions"],
        )

    def test_metric_value_regression_is_reported(self) -> None:
        candidate = copy.deepcopy(self.report)
        metric = candidate["metric_audit"]["safety_block_pass_rate"]
        metric["value"] = 0.5
        result = compare_reports(self.report, candidate)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn(
            "metric_regressed:safety_block_pass_rate",
            result["regressions"],
        )

    def test_added_cases_are_separated_from_baseline(self) -> None:
        candidate = copy.deepcopy(self.report)
        extra = copy.deepcopy(candidate["cases"][0])
        extra["id"] = "synthetic_new_case"
        candidate["cases"].append(extra)
        result = compare_reports(self.report, candidate)
        self.assertEqual(result["added_cases"], ["synthetic_new_case"])
        self.assertEqual(result["removed_cases"], [])
        self.assertEqual(result["status"], "PASS")

    def test_skipped_increase_is_reported(self) -> None:
        candidate = copy.deepcopy(self.report)
        candidate["metrics"]["skipped_count"] = 3
        result = compare_reports(self.report, candidate)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("skipped_count_increased", result["regressions"])

    def test_report_kind_is_detected(self) -> None:
        self.assertEqual(detect_report_kind(self.report), "safety_gate")
        acceptance = run_acceptance()
        self.assertEqual(detect_report_kind(acceptance), "acceptance")

    def test_acceptance_reports_compare_within_same_contract(self) -> None:
        acceptance = run_acceptance()
        result = compare_acceptance_reports(
            acceptance, copy.deepcopy(acceptance)
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["removed_scenarios"], [])

    def test_acceptance_contract_change_is_not_comparable(self) -> None:
        acceptance = run_acceptance()
        legacy = copy.deepcopy(acceptance)
        legacy["metric_definition_version"] = "1.0.0"
        result = compare_acceptance_reports(legacy, acceptance)
        self.assertEqual(result["status"], "NOT_COMPARABLE")
        self.assertIn(
            "metric_definition_version_changed:1.0.0->1.1.0",
            result["reasons"],
        )

    def test_removed_acceptance_scenario_is_reported(self) -> None:
        acceptance = run_acceptance()
        candidate = copy.deepcopy(acceptance)
        candidate["scenarios"] = candidate["scenarios"][:-1]
        result = compare_acceptance_reports(acceptance, candidate)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["removed_scenarios"], ["E2E-12"])

    def test_cli_auto_detects_gate_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            baseline = Path(temp_dir) / "baseline.json"
            candidate = Path(temp_dir) / "candidate.json"
            baseline.write_text(json.dumps(self.report), encoding="utf-8")
            candidate.write_text(json.dumps(self.report), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.eval_compare",
                    str(baseline),
                    str(candidate),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                env={
                    **dict(__import__("os").environ),
                    "PYTHONPATH": f"{PROJECT_ROOT / 'src'}:{PROJECT_ROOT}",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                timeout=120,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["kind"], "gate")


if __name__ == "__main__":
    unittest.main()
