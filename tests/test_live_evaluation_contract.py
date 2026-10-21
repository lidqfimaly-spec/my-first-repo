"""Phase 3.85: freeze live evaluation contracts and metric semantics."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.live_evaluation_contract import (  # noqa: E402
    LIVE_EVALUATION_METRIC_CONTRACT,
    build_live_evaluation_integration_report,
    load_live_evaluation_metric_contract,
    validate_live_report_against_metric_contract,
    write_live_evaluation_integration_report,
)


class LiveEvaluationContractTests(unittest.TestCase):
    def test_contract_binds_dataset_and_both_contracts(self) -> None:
        contract = load_live_evaluation_metric_contract()
        self.assertEqual(
            contract["version"], "live-evaluation-metric-contract-v2"
        )
        self.assertFalse(contract["retroactively_applicable"])
        self.assertFalse(contract["reporting_rules"]["composite_score_allowed"])
        self.assertEqual(
            contract["group_counts"],
            {
                "total": 25,
                "live_applicable": 19,
                "live_exploratory": 3,
                "scripted_only": 3,
                "intent_fidelity_eligible": 22,
            },
        )
        self.assertEqual(len(contract["metric_contract_sha256"]), 64)

    def test_integration_validation_makes_no_requests_and_passes(self) -> None:
        report = build_live_evaluation_integration_report()
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["ready_for_third_live_run"])
        self.assertFalse(report["network_request_sent"])
        self.assertEqual(report["real_model_requests"], 0)
        self.assertTrue(all(check["passed"] for check in report["checks"]))

    def test_contract_rejects_retroactive_or_composite_changes(self) -> None:
        original = json.loads(
            LIVE_EVALUATION_METRIC_CONTRACT.read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            retroactive = json.loads(json.dumps(original))
            retroactive["retroactively_applicable"] = True
            path = root / "retroactive.json"
            path.write_text(json.dumps(retroactive), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must not be retroactive"):
                load_live_evaluation_metric_contract(path)

            composite = json.loads(json.dumps(original))
            composite["reporting_rules"]["composite_score_allowed"] = True
            path = root / "composite.json"
            path.write_text(json.dumps(composite), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "composite"):
                load_live_evaluation_metric_contract(path)

    def test_contract_rejects_version_or_hash_drift(self) -> None:
        original = json.loads(
            LIVE_EVALUATION_METRIC_CONTRACT.read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            changed = json.loads(json.dumps(original))
            changed["contracts"]["intent_fidelity"]["version"] = (
                "planner-intent-fidelity-v1"
            )
            path = root / "changed.json"
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "intent contract mismatch"):
                load_live_evaluation_metric_contract(path)

    def test_contract_rejects_group_count_change(self) -> None:
        original = json.loads(
            LIVE_EVALUATION_METRIC_CONTRACT.read_text(encoding="utf-8")
        )
        original["group_counts"]["live_applicable"] = 18
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "groups.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "group counts changed"):
                load_live_evaluation_metric_contract(path)

    def test_live_report_validation_rejects_missing_metrics(self) -> None:
        contract = load_live_evaluation_metric_contract()
        with self.assertRaisesRegex(ValueError, "missing required field"):
            validate_live_report_against_metric_contract(
                {
                    "dataset_sha256": contract["dataset"]["sha256"],
                    "live_model_quality_measured": True,
                    "execution_side_effects_enabled": False,
                },
                contract=contract,
            )

    def test_live_report_validation_rejects_request_boundary_drift(self) -> None:
        contract = load_live_evaluation_metric_contract()
        report = {
            "dataset_sha256": contract["dataset"]["sha256"],
            "live_applicability_contract": {
                "contract_sha256": contract["contracts"]["applicability"][
                    "sha256"
                ]
            },
            "intent_fidelity_contract": {
                "contract_sha256": contract["contracts"]["intent_fidelity"][
                    "sha256"
                ]
            },
            "live_applicability": {"metrics": {}},
            "intent_fidelity_summary": {},
            "request_accounting": {
                "retries": 1,
                "maximum_requests": 25,
            },
            "authorization": {"authorized": True},
            "live_model_quality_measured": True,
            "execution_side_effects_enabled": False,
        }
        with self.assertRaisesRegex(ValueError, "request boundaries changed"):
            validate_live_report_against_metric_contract(
                report,
                contract=contract,
            )

    def test_readiness_report_writer_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "readiness.json"
            report = write_live_evaluation_integration_report(path)
            first_bytes = path.read_bytes()
            self.assertTrue(report["ready_for_third_live_run"])
            with self.assertRaises(FileExistsError):
                write_live_evaluation_integration_report(path)
            self.assertEqual(path.read_bytes(), first_bytes)


if __name__ == "__main__":
    unittest.main()
