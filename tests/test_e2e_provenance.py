from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.e2e_acceptance_runner import (  # noqa: E402
    AcceptanceContractError,
    _scenario,
    run_acceptance,
    validate_acceptance_report,
)
from zevorinth.artifact_tool import RealArtifactWriter  # noqa: E402
from zevorinth.models import TaskStatus  # noqa: E402
from zevorinth.safety_gate import (  # noqa: E402
    SafetyGateConfigurationError,
    build_effect,
    validate_case_effects,
)
from zevorinth.verifier import ArtifactVerifier  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


class E2EProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = run_acceptance()

    def _scenario(self, report: dict, scenario_id: str) -> dict:
        return next(
            item for item in report["scenarios"] if item["id"] == scenario_id
        )

    def test_every_scenario_records_effect_provenance(self) -> None:
        self.assertEqual(len(self.report["scenarios"]), 12)
        self.assertTrue(all(item["passed"] for item in self.report["scenarios"]))
        for scenario in self.report["scenarios"]:
            validate_case_effects(scenario)
        self.assertEqual(self.report["metric_definition_version"], "1.1.0")

    def test_written_recovered_and_none_are_distinguished(self) -> None:
        written = self._scenario(self.report, "E2E-1")["effects"]
        recovered = self._scenario(self.report, "E2E-4")["effects"]
        absent = self._scenario(self.report, "E2E-2")["effects"]
        self.assertEqual(written["origin"], "written")
        self.assertGreaterEqual(written["file_writes"], 1)
        self.assertEqual(recovered["origin"], "recovered")
        self.assertEqual(recovered["file_writes"], 0)
        self.assertEqual(recovered["evidence_count"], 1)
        self.assertEqual(absent["origin"], "none")
        self.assertEqual(absent["file_writes"], 0)

    def test_artifact_excess_over_writes_equals_recovery_reads(self) -> None:
        metrics = self.report["metrics"]
        recovered = [
            item
            for item in self.report["scenarios"]
            if item["effects"]["origin"] == "recovered"
        ]
        self.assertEqual(
            metrics["artifact_count"] - metrics["file_write_count"],
            len(recovered),
        )

    def test_planted_file_without_evidence_is_unbacked(self) -> None:
        scenario = self._scenario(self.report, "E2E-5")
        self.assertEqual(scenario["effects"]["origin"], "none")
        self.assertEqual(scenario["effects"]["integrity"], "unbacked")
        self.assertEqual(scenario["effects"]["evidence_count"], 0)
        self.assertFalse(scenario["evidence_complete"])
        self.assertFalse(scenario["artifact"]["claimed"])

    def test_declared_hash_mismatch_is_checked_not_asserted(self) -> None:
        scenario = self._scenario(self.report, "E2E-12")
        effects = scenario["effects"]
        self.assertEqual(effects["integrity"], "mismatch")
        self.assertNotEqual(effects["file_sha256"], effects["evidence_sha256"])
        self.assertTrue(scenario["checks"]["evidence_mismatch_detected"])
        self.assertFalse(scenario["evidence_complete"])

    def test_hashes_are_recomputed_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            task_id = str(uuid4())
            result = RealArtifactWriter(root).write(
                task_id=task_id,
                attempt_id=str(uuid4()),
                artifact_path="disk.txt",
                content="original\n",
                started_at=NOW,
                finished_at=NOW,
                lease_token="lease",
                claim_generation=1,
                authorize=lambda _token, _generation: None,
            )
            first = build_effect(
                origin="written",
                tool_calls=1,
                file_writes=1,
                evidence=result.artifact,
                artifact_root=root,
                task_id=task_id,
            )
            (root / task_id / "disk.txt").write_text("changed\n")
            second = build_effect(
                origin="written",
                tool_calls=1,
                file_writes=1,
                evidence=result.artifact,
                artifact_root=root,
                task_id=task_id,
            )
            self.assertNotEqual(first["file_sha256"], second["file_sha256"])
            self.assertNotEqual(
                second["file_sha256"], second["evidence_sha256"]
            )

    def test_missing_artifact_on_disk_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            task_id = str(uuid4())
            result = RealArtifactWriter(root).write(
                task_id=task_id,
                attempt_id=str(uuid4()),
                artifact_path="gone.txt",
                content="content\n",
                started_at=NOW,
                finished_at=NOW,
                lease_token="lease",
                claim_generation=1,
                authorize=lambda _token, _generation: None,
            )
            (root / task_id / "gone.txt").unlink()
            effects = build_effect(
                origin="written",
                tool_calls=1,
                file_writes=1,
                evidence=result.artifact,
                artifact_root=root,
                task_id=task_id,
            )
            self.assertIsNone(effects["file_sha256"])
            with self.assertRaises(SafetyGateConfigurationError):
                validate_case_effects({"id": "missing", "effects": effects})

    def test_attempt_mismatch_is_rejected_by_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            task_id = str(uuid4())
            attempt = str(uuid4())
            result = RealArtifactWriter(root).write(
                task_id=task_id,
                attempt_id=attempt,
                artifact_path="attempt.txt",
                content="attempt\n",
                started_at=NOW,
                finished_at=NOW,
                lease_token="lease",
                claim_generation=1,
                authorize=lambda _token, _generation: None,
            )
            verification = ArtifactVerifier(root).verify(
                task_id=task_id,
                artifact_path="attempt.txt",
                expected_content="attempt\n",
                tool_result=result,
                checked_at=NOW,
                expected_attempt_id=str(uuid4()),
                require_provenance=True,
                maximum_claim_generation=1,
            )
            self.assertFalse(verification.passed)
            self.assertEqual(verification.reason, "tool_evidence_mismatch")

    def test_written_origin_without_writer_call_fails_closed(self) -> None:
        report = copy.deepcopy(self.report)
        scenario = self._scenario(report, "E2E-1")
        scenario["effects"]["file_writes"] = 0
        with self.assertRaises(SafetyGateConfigurationError):
            validate_acceptance_report(report)

    def test_duplicate_side_effect_count_must_match_provenance(self) -> None:
        report = copy.deepcopy(self.report)
        report["metrics"]["file_write_count"] = 99
        with self.assertRaises(AcceptanceContractError):
            validate_acceptance_report(report)

    def test_summary_must_match_case_records(self) -> None:
        report = copy.deepcopy(self.report)
        report["metric_audit"]["evidence_integrity_rate"]["numerator"] = 99
        with self.assertRaises(AcceptanceContractError):
            validate_acceptance_report(report)

    def test_rejected_case_cannot_be_reported_as_success(self) -> None:
        scenario = _scenario(
            "probe",
            "verifier accepted a mismatched artifact",
            "evidence_hash_mismatch",
            TaskStatus.FAILED,
            TaskStatus.FAILED,
            effects=build_effect(
                origin="none", tool_calls=0, file_writes=0
            ),
            artifact=None,
            exit_code=1,
            failure_class="verification",
            details={},
            checks={"evidence_mismatch_detected": False},
        )
        self.assertFalse(scenario["passed"])


if __name__ == "__main__":
    unittest.main()
