from __future__ import annotations

import unittest


from tests.e2e_acceptance_runner import run_acceptance


class AcceptanceMetricTests(unittest.TestCase):
    def test_acceptance_metrics_have_auditable_denominators(self) -> None:
        report = run_acceptance()
        self.assertEqual(report["metrics"]["passed"], 11)
        self.assertEqual(report["metrics"]["scenario_count"], 11)
        audit = report["metric_audit"]
        for name in (
            "success_rate",
            "executable_success_rate",
            "recovery_success_rate",
            "duplicate_side_effects",
            "cancellation_handled",
            "evidence_complete_rate",
        ):
            detail = audit[name]
            self.assertGreater(detail["denominator"], 0)
            self.assertGreaterEqual(detail["numerator"], 0)
            self.assertIn("excluded_count", detail)
            self.assertIn("excluded_reason_counts", detail)
            self.assertTrue(detail["contributing_case_ids"])
            self.assertEqual(
                detail["metric_definition_version"],
                report["metric_definition_version"],
            )
        self.assertEqual(audit["duplicate_side_effects"]["value"], 0)
        self.assertEqual(audit["cancellation_handled"]["value"], 1)


if __name__ == "__main__":
    unittest.main()
