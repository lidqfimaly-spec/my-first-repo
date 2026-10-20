from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.eval_compare import compare_reports  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
