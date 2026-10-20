from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.audit_report import compare_stable, stable_projection  # noqa: E402
from tests.execution_audit_runner import run_audit  # noqa: E402


class ExecutionAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = run_audit()
        cls.by_id = {item["id"]: item for item in cls.report["scenarios"]}

    def test_matrix_passes(self) -> None:
        self.assertEqual(self.report["status"], "PASS")
        self.assertEqual(self.report["failed_scenarios"], [])
        self.assertEqual(self.report["metrics"]["scenario_count"], 9)

    def test_counts_are_recomputed_from_scenarios(self) -> None:
        scenarios = self.report["scenarios"]
        metrics = self.report["metrics"]
        self.assertEqual(
            metrics["started"],
            sum(1 for item in scenarios if item["runner_invoked"] > 0),
        )
        self.assertEqual(
            metrics["blocked"],
            sum(
                1
                for item in scenarios
                if item["observed_status"] == "blocked"
            ),
        )
        self.assertEqual(
            metrics["cancelled_before_start"],
            sum(
                1
                for item in scenarios
                if item["observed_status"] == "cancelled"
                and item["runner_invoked"] == 0
            ),
        )
        self.assertEqual(metrics["duplicate_side_effects"], 0)

    def test_success_requires_verifier_approval(self) -> None:
        for item in self.report["scenarios"]:
            if item["observed_status"] == "succeeded":
                self.assertTrue(item["verifier_passed"], item["id"])
                self.assertEqual(item["exit_code"], 0, item["id"])

    def test_blocked_cases_never_started(self) -> None:
        for item in self.report["scenarios"]:
            if item["observed_status"] == "blocked":
                self.assertEqual(item["runner_invoked"], 0, item["id"])

    def test_timeout_and_cancel_confirmations(self) -> None:
        metrics = self.report["metrics"]
        self.assertEqual(metrics["timeout_confirmation_rate"], 1.0)
        self.assertEqual(metrics["cancel_confirmation_rate"], 1.0)
        timeout = self.by_id["E-3-timeout"]
        self.assertTrue(timeout["timed_out"])
        self.assertIsNone(timeout["exit_code"])
        self.assertTrue(timeout["terminated_confirmed"])

    def test_verifier_agreement_covers_negative_cases(self) -> None:
        metrics = self.report["metrics"]
        self.assertEqual(metrics["verifier_agreement_rate"], 1.0)
        self.assertEqual(metrics["positive_verifier_pass_rate"], 1.0)
        self.assertFalse(self.by_id["E-2-fail"]["verifier_passed"])

    def test_truncated_run_is_not_a_success(self) -> None:
        truncated = self.by_id["E-8-truncated"]
        self.assertTrue(truncated["truncated"])
        self.assertEqual(truncated["observed_status"], "failed")
        self.assertFalse(truncated["verifier_passed"])

    def test_stable_report_repeats(self) -> None:
        first = stable_projection(self.report, "execution_audit")
        second = stable_projection(run_audit(), "execution_audit")
        result = compare_stable(first, second)
        self.assertEqual(result["status"], "PASS", result)


if __name__ == "__main__":
    unittest.main()
