from __future__ import annotations

import json
import os
import sys
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
    EvaluationConfigurationError,
    load_cases,
    run_evaluation,
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
        self.assertEqual(report["metrics"]["contract_validity_rate"], 1.0)
        self.assertEqual(report["metrics"]["unsafe_block_rate"], 1.0)
        self.assertEqual(report["metrics"]["end_to_end_success_rate"], 1.0)
        self.assertEqual(report["metrics"]["model_calls"], 26)
        baseline = report["deterministic_baseline"]
        self.assertEqual(baseline["metrics"]["model_calls"], 0)
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
                run_evaluation(online=True, limit=1)

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
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "",
                "ZEVORINTH_MODEL": "",
            },
            clear=False,
        ), redirect_stderr(stderr):
            exit_code = main(["eval-planner", "--online", "--limit", "1"])

        self.assertEqual(exit_code, 7)
        self.assertIn("configuration error", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
