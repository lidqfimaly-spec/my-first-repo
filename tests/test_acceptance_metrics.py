from __future__ import annotations

import unittest


from tests.e2e_acceptance_runner import run_acceptance


class AcceptanceMetricTests(unittest.TestCase):
    def test_acceptance_metrics_have_auditable_denominators(self) -> None:
        report = run_acceptance()
        self.assertEqual(report["metrics"]["passed"], 12)
        self.assertEqual(report["metrics"]["scenario_count"], 12)
        self.assertEqual(report["metric_definition_version"], "1.1.0")
        audit = report["metric_audit"]
        for name in (
            "success_rate",
            "executable_success_rate",
            "recovery_success_rate",
            "duplicate_side_effects",
            "cancellation_handled",
            "evidence_integrity_rate",
            "file_write_count",
            "artifact_count",
            "evidence_count",
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
        self.assertEqual(audit["artifact_count"]["value"], 8)
        self.assertEqual(audit["file_write_count"]["value"], 6)
        self.assertEqual(audit["evidence_integrity_rate"]["numerator"], 7)
        self.assertEqual(audit["evidence_integrity_rate"]["denominator"], 8)


if __name__ == "__main__":
    unittest.main()
