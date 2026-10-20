from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.audit_report import (  # noqa: E402
    build_metadata,
    compare_stable,
    stable_digest,
    stable_projection,
)
from tests.user_acceptance_runner import (  # noqa: E402
    RESULT_MAP,
    run_acceptance,
)


class UserAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = run_acceptance()
        cls.by_id = {item["id"]: item for item in cls.report["scenarios"]}

    def test_every_scenario_meets_its_expectation(self) -> None:
        self.assertEqual(self.report["status"], "PASS")
        self.assertEqual(self.report["failed_scenarios"], [])
        self.assertEqual(len(self.report["scenarios"]), 16)
        ids = [item["id"] for item in self.report["scenarios"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_result_vocabulary_matches_observed_status(self) -> None:
        vocabulary = {
            "passed",
            "failed",
            "blocked",
            "needs_clarification",
            "cancelled",
            "not_executed",
            "simulated",
        }
        for scenario in self.report["scenarios"]:
            self.assertIn(scenario["result"], vocabulary)
            self.assertEqual(
                scenario["result"],
                RESULT_MAP.get(scenario["observed_status"]),
            )

    def test_non_success_is_not_reported_as_passed(self) -> None:
        for scenario in self.report["scenarios"]:
            if scenario["observed_status"] != "succeeded":
                self.assertNotEqual(scenario["result"], "passed")
        self.assertEqual(self.by_id["D2-budget"]["result"], "failed")
        self.assertEqual(self.by_id["D3-cancel"]["result"], "cancelled")
        self.assertEqual(
            self.by_id["B3-clarification"]["result"], "needs_clarification"
        )
        self.assertEqual(self.by_id["C1-research"]["result"], "blocked")

    def test_artifact_hashes_come_from_disk(self) -> None:
        for scenario in self.report["scenarios"]:
            effects = scenario["effects"]
            if effects["origin"] == "none":
                self.assertIsNone(effects["file_sha256"])
                continue
            artifact = scenario["artifact"] or {}
            self.assertEqual(
                effects["file_sha256"], artifact.get("file_sha256")
            )
            if effects["integrity"] == "verified":
                self.assertEqual(
                    effects["file_sha256"], effects["evidence_sha256"]
                )

    def test_metrics_are_recomputed_from_scenarios(self) -> None:
        metrics = self.report["metrics"]
        counts = {
            name: sum(
                1
                for item in self.report["scenarios"]
                if item["result"] == name
            )
            for name in (
                "passed",
                "failed",
                "blocked",
                "needs_clarification",
                "cancelled",
                "not_executed",
                "simulated",
            )
        }
        for name, value in counts.items():
            self.assertEqual(metrics[name], value)
        self.assertEqual(
            sum(counts.values()), metrics["scenario_count"]
        )
        self.assertEqual(metrics["duplicate_side_effects"], 0)
        # Positive counters describe the same population.
        positive = [
            item
            for item in self.report["scenarios"]
            if item["effects"]["origin"] != "none"
            and not item.get("negative_control")
        ]
        self.assertEqual(metrics["artifact_count"], len(positive))
        self.assertEqual(
            metrics["file_write_count"],
            sum(item["effects"]["file_writes"] for item in positive),
        )
        self.assertEqual(
            metrics["evidence_count"],
            sum(item["effects"]["evidence_count"] for item in positive),
        )
        self.assertEqual(metrics["negative_control_write_count"], 1)

    def test_negative_control_is_excluded_from_rates(self) -> None:
        audit = self.report["metric_audit"]["verifier_pass_rate"]
        self.assertNotIn("A4-tamper", audit["contributing_case_ids"])
        self.assertEqual(
            audit["excluded_reason_counts"].get(
                "no_positive_artifact_case"
            ),
            self.report["metrics"]["scenario_count"] - audit["denominator"],
        )
        self.assertEqual(self.report["metrics"]["negative_control_count"], 1)

    def test_stable_report_repeats(self) -> None:
        first = stable_projection(self.report, "user_acceptance")
        second = stable_projection(run_acceptance(), "user_acceptance")
        result = compare_stable(first, second)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["differing_top_level_keys"], [])

    def test_metadata_records_gaps_and_digest(self) -> None:
        metadata = build_metadata(
            self.report,
            kind="user_acceptance",
            command=["python3", "-m", "tests.user_acceptance_runner"],
            exit_code=0,
            started_at="2026-10-20T00:00:00+00:00",
            duration_ms=1.0,
            not_run=["network research (capability gap)"],
        )
        self.assertEqual(
            metadata["stable_sha256"], stable_digest(self.report)
        )
        self.assertEqual(
            metadata["not_run"], ["network research (capability gap)"]
        )
        self.assertNotIn("duration_ms", self.report)

    def test_capability_gaps_are_explicit(self) -> None:
        self.assertIn(
            "gap", self.by_id["B2b-shell"]["details"]
        )
        self.assertIn("gap", self.by_id["C1-research"]["details"])
        self.assertIn(
            "gap", self.by_id["B1-code-file"]["details"]
        )


if __name__ == "__main__":
    unittest.main()
