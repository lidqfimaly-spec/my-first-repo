from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.product_evaluation import (  # noqa: E402
    ProductAcceptanceError,
    main,
    product_acceptance_markdown,
    run_product_acceptance,
    write_product_acceptance,
)
from zevorinth.capabilities import CAPABILITY_REGISTRY  # noqa: E402


class ProductAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = run_product_acceptance()

    def test_all_positive_and_negative_scenarios_pass(self) -> None:
        self.assertTrue(self.report["all_passed"])
        self.assertEqual(self.report["metrics"]["failed"], 0)
        self.assertEqual(self.report["metrics"]["false_success_count"], 0)
        self.assertEqual(
            self.report["metrics"]["negative_controls_passed"],
            self.report["metrics"]["negative_controls"],
        )

    def test_metrics_cover_every_task_class(self) -> None:
        classes = self.report["metrics"]["by_task_class"]
        self.assertEqual(set(classes), {"file", "code", "research", "complex"})
        for details in classes.values():
            self.assertGreaterEqual(details["passed"], 1)
            self.assertEqual(details["failed"], 0)

    def test_runner_capability_is_registered_truthfully(self) -> None:
        details = CAPABILITY_REGISTRY["capabilities"][
            "product_acceptance_runner"
        ]
        self.assertEqual(details["status"], "available")
        self.assertTrue(details["verified"])
        self.assertIn("cli:eval-product", details["access"])
        self.assertIn(
            "not a production trust or certification claim",
            details["limitations"],
        )

    def test_report_records_real_execution_and_boundaries(self) -> None:
        self.assertEqual(
            self.report["mode"],
            "REAL_LOCAL_TEMP_ISOLATED",
        )
        self.assertEqual(self.report["acceptance_scope"], "ENGINE_SLICES")
        self.assertFalse(self.report["v1_user_journey_complete"])
        isolation = self.report["isolation"]
        self.assertTrue(isolation["temporary_home_and_workspaces"])
        self.assertEqual(isolation["network_requests"], 0)
        self.assertEqual(isolation["live_model_requests"], 0)
        self.assertEqual(isolation["container_requests"], 0)
        self.assertEqual(isolation["production_workspace_writes"], 0)
        self.assertTrue(
            any(
                "not production trust" in item
                for item in self.report["limitations"]
            )
        )
        self.assertTrue(
            any(
                "control-center user journey is incomplete" in item
                for item in self.report["limitations"]
            )
        )
        self.assertTrue(
            any(
                "caller-authored" in item
                for item in self.report["limitations"]
            )
        )

    def test_markdown_is_derived_from_route_results(self) -> None:
        markdown = product_acceptance_markdown(self.report)
        self.assertIn("# Zevorinth V1 Engine-Slice Acceptance", markdown)
        self.assertIn("- Engine slice status: `PASS`", markdown)
        self.assertIn("- Acceptance scope: `ENGINE_SLICES`", markdown)
        self.assertIn("- V1 user journey complete: `false`", markdown)
        for scenario in self.report["scenarios"]:
            self.assertIn(scenario["id"], markdown)

    def test_runner_writes_json_and_markdown_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "acceptance.json"
            with redirect_stdout(io.StringIO()):
                code = main(["--output", str(output), "--json"])
            self.assertEqual(code, 0)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(data["all_passed"])
            self.assertTrue(output.with_suffix(".md").is_file())
            with self.assertRaisesRegex(
                ProductAcceptanceError,
                "refusing to overwrite",
            ):
                write_product_acceptance(output, report=self.report)

    def test_false_success_cannot_be_hidden_by_summary(self) -> None:
        forged = json.loads(json.dumps(self.report))
        first = forged["scenarios"][0]
        first["false_success"] = True
        forged["metrics"] = {
            **forged["metrics"],
            "false_success_count": 1,
        }
        forged["all_passed"] = False
        markdown = product_acceptance_markdown(forged)
        self.assertIn("- Engine slice status: `FAIL`", markdown)
        self.assertIn("- False successes: `1`", markdown)

    def test_runner_is_stable_across_repetition(self) -> None:
        second = run_product_acceptance()
        self.assertEqual(
            [item["id"] for item in second["scenarios"]],
            [item["id"] for item in self.report["scenarios"]],
        )
        self.assertTrue(second["all_passed"])


if __name__ == "__main__":
    unittest.main()
