"""Phase 3.87: exact parameter, schema, and unsupported-intent regressions."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.planner_precision import (  # noqa: E402
    load_precision_cases,
    run_precision_regression,
)


class PlannerPrecisionTests(unittest.TestCase):
    def test_dataset_covers_all_required_failure_families(self) -> None:
        dataset = load_precision_cases()
        cases = dataset["cases"]
        self.assertGreaterEqual(len(cases), 16)
        self.assertEqual(
            {case["category"] for case in cases},
            {
                "exact_parameters",
                "schema_contract",
                "unsupported_intent",
            },
        )
        self.assertGreaterEqual(
            sum(case["category"] == "schema_contract" for case in cases), 6
        )
        self.assertGreaterEqual(
            sum(case["category"] == "unsupported_intent" for case in cases),
            4,
        )

    def test_precision_regression_passes_without_execution(self) -> None:
        with patch(
            "zevorinth.evaluation.TaskService.submit",
            side_effect=AssertionError("precision runner must not execute"),
        ):
            first = run_precision_regression()
        second = run_precision_regression()
        self.assertEqual(first, second)
        self.assertTrue(first["all_passed"], first["cases"])
        self.assertEqual(first["metrics"]["case_count"], 16)
        self.assertEqual(first["metrics"]["invalid_schema_plan_count"], 0)
        self.assertEqual(first["metrics"]["lossy_plan_bypass_count"], 0)
        self.assertEqual(first["metrics"]["unsupported_intent_plan_count"], 0)
        self.assertEqual(first["metrics"]["execution_attempts"], 0)
        self.assertEqual(first["metrics"]["real_api_calls"], 0)
        self.assertFalse(first["execution_side_effects_enabled"])

    def test_dataset_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "duplicate.json"
            path.write_text(
                '{"version":"one","version":"two","cases":[]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate key"):
                load_precision_cases(path)


if __name__ == "__main__":
    unittest.main()
