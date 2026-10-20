from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.safety_gate import (  # noqa: E402
    SafetyGateConfigurationError,
    _neutral_effect,
    run_safety_gate,
    validate_gate_report,
)


class EffectProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = run_safety_gate()

    def _case(self, case_id: str) -> dict:
        for case in self.report["cases"]:
            if case["id"] == case_id:
                return case
        raise AssertionError(f"missing case {case_id}")

    def _corrupted(self) -> dict:
        return copy.deepcopy(self.report)

    def test_real_report_records_origin_for_every_case(self) -> None:
        for case in self.report["cases"]:
            self.assertIn(
                case["effects"]["origin"], {"written", "recovered", "none"}
            )
            self.assertFalse(case.get("effects_unproven"))

    def test_written_artifact_matches_evidence_hash(self) -> None:
        case = self._case("create_basic_text")
        effects = case["effects"]
        self.assertEqual(effects["origin"], "written")
        self.assertEqual(effects["file_writes"], 1)
        self.assertEqual(effects["evidence_count"], 1)
        self.assertIsNotNone(effects["file_sha256"])
        self.assertEqual(effects["file_sha256"], effects["evidence_sha256"])

    def test_recovery_reads_artifact_without_writing(self) -> None:
        case = self._case("local_recovery_readback_no_retry")
        effects = case["effects"]
        self.assertEqual(effects["origin"], "recovered")
        self.assertEqual(effects["file_writes"], 0)
        self.assertEqual(effects["tool_calls"], 0)
        self.assertEqual(effects["evidence_count"], 1)
        self.assertEqual(effects["file_sha256"], effects["evidence_sha256"])

    def test_artifacts_exceed_writes_only_through_recovery(self) -> None:
        artifacts = self.report["metrics"]["artifact_count"]
        writes = self.report["metrics"]["file_write_count"]
        recovered = [
            case
            for case in self.report["cases"]
            if case["effects"]["origin"] == "recovered"
        ]
        self.assertEqual(artifacts - writes, len(recovered))

    def test_written_origin_without_writer_call_fails_closed(self) -> None:
        report = self._corrupted()
        case = next(c for c in report["cases"] if c["id"] == "create_basic_text")
        case["effects"]["file_writes"] = 0
        with self.assertRaises(SafetyGateConfigurationError):
            validate_gate_report(report)

    def test_recovered_origin_with_file_write_fails_closed(self) -> None:
        report = self._corrupted()
        case = next(
            c
            for c in report["cases"]
            if c["id"] == "local_recovery_readback_no_retry"
        )
        case["effects"]["file_writes"] = 1
        with self.assertRaises(SafetyGateConfigurationError):
            validate_gate_report(report)

    def test_evidence_hash_mismatch_fails_closed(self) -> None:
        report = self._corrupted()
        case = next(c for c in report["cases"] if c["id"] == "create_basic_text")
        case["effects"]["evidence_sha256"] = "0" * 64
        with self.assertRaises(SafetyGateConfigurationError):
            validate_gate_report(report)

    def test_evidence_without_artifact_fails_closed(self) -> None:
        report = self._corrupted()
        case = next(
            c for c in report["cases"] if c["id"] == "local_policy_path_traversal"
        )
        case["effects"]["evidence_count"] = 1
        with self.assertRaises(SafetyGateConfigurationError):
            validate_gate_report(report)

    def test_artifact_without_path_fails_closed(self) -> None:
        report = self._corrupted()
        case = next(c for c in report["cases"] if c["id"] == "create_basic_text")
        case["effects"]["relative_path"] = None
        with self.assertRaises(SafetyGateConfigurationError):
            validate_gate_report(report)

    def test_summary_must_match_case_provenance(self) -> None:
        report = self._corrupted()
        report["metrics"]["artifact_count"] = 12
        report["metrics"]["artifact_side_effect_count"] = 12
        with self.assertRaises(SafetyGateConfigurationError):
            validate_gate_report(report)

    def test_write_summary_must_match_case_provenance(self) -> None:
        report = self._corrupted()
        report["metrics"]["file_write_count"] = 9
        with self.assertRaises(SafetyGateConfigurationError):
            validate_gate_report(report)

    def test_unproven_effects_require_a_harness_error(self) -> None:
        report = self._corrupted()
        case = next(c for c in report["cases"] if c["id"] == "create_basic_text")
        case["effects"] = _neutral_effect()
        case["effects_unproven"] = True
        with self.assertRaises(SafetyGateConfigurationError):
            validate_gate_report(report)

    def test_unproven_effects_must_be_neutral(self) -> None:
        report = self._corrupted()
        case = next(c for c in report["cases"] if c["id"] == "create_basic_text")
        case["effects_unproven"] = True
        case["error"] = "RuntimeError"
        case["passed"] = False
        with self.assertRaises(SafetyGateConfigurationError):
            validate_gate_report(report)


if __name__ == "__main__":
    unittest.main()
