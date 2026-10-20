from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.audit_report import compare_stable, stable_projection  # noqa: E402
from tests.side_effect_audit_runner import run_audit  # noqa: E402


class SideEffectAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = run_audit()
        cls.by_id = {item["id"]: item for item in cls.report["scenarios"]}

    def test_matrix_passes(self) -> None:
        self.assertEqual(self.report["status"], "PASS")
        self.assertEqual(self.report["failed_scenarios"], [])
        self.assertEqual(self.report["metrics"]["scenario_count"], 8)

    def test_counts_are_recomputed_from_scenarios(self) -> None:
        metrics = self.report["metrics"]
        scenarios = self.report["scenarios"]
        self.assertEqual(
            metrics["started"],
            sum(1 for item in scenarios if item["started"] > 0),
        )
        self.assertEqual(
            metrics["unexpected_changes"],
            sum(
                item["diff_counts"].get("unexpected", 0)
                for item in scenarios
            ),
        )
        self.assertEqual(
            metrics["expected_changes"],
            sum(
                item["diff_counts"].get("expected", 0)
                for item in scenarios
            ),
        )

    def test_success_requires_complete_audit(self) -> None:
        for item in self.report["scenarios"]:
            if item["observed_status"] == "succeeded":
                self.assertTrue(item["complete_audit"], item["id"])
                self.assertEqual(
                    item["diff_counts"].get("unexpected", 0), 0, item["id"]
                )

    def test_unexpected_changes_fail_the_task(self) -> None:
        for scenario_id in ("S-3-unexpected-source", "S-4-protected-report"):
            item = self.by_id[scenario_id]
            self.assertEqual(item["observed_status"], "failed")
            self.assertGreater(item["diff_counts"]["unexpected"], 0)
            self.assertEqual(
                item["verifier_reason"], "unexpected_workspace_change"
            )

    def test_allowed_write_is_expected(self) -> None:
        item = self.by_id["S-2-allowed-report"]
        self.assertEqual(item["observed_status"], "succeeded")
        self.assertEqual(item["diff_counts"]["expected"], 2)
        self.assertEqual(item["diff_counts"]["unexpected"], 0)

    def test_incomplete_snapshot_never_starts(self) -> None:
        item = self.by_id["S-8-incomplete-before"]
        self.assertEqual(item["observed_status"], "failed")
        self.assertEqual(item["started"], 0)
        self.assertFalse(item["complete_audit"])

    def test_recovery_does_not_rerun(self) -> None:
        item = self.by_id["S-7-recovery-unknown"]
        self.assertEqual(item["observed_status"], "blocked")
        self.assertEqual(item["recovery_reruns"], 0)
        self.assertEqual(self.report["metrics"]["recovery_reruns"], 0)

    def test_reported_diff_matches_recomputation(self) -> None:
        self.assertEqual(
            self.report["metrics"]["diff_consistency_rate"], 1.0
        )
        for item in self.report["scenarios"]:
            if item["diff_present"]:
                self.assertTrue(
                    item["diff_recomputed_consistent"], item["id"]
                )

    def test_stable_report_repeats(self) -> None:
        first = stable_projection(self.report, "execution_audit")
        second = stable_projection(run_audit(), "execution_audit")
        result = compare_stable(first, second)
        self.assertEqual(result["status"], "PASS", result)


if __name__ == "__main__":
    unittest.main()
