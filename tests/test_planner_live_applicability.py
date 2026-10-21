"""Phase 3.80: live-applicability contract and independent scoring."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.planner_live_applicability import (  # noqa: E402
    LIVE_APPLICABILITY_CONTRACT,
    load_live_applicability_contract,
    score_live_applicability,
)


def _case(
    case_id: str,
    *,
    observed_classification: str,
    raw_candidate: dict | None,
    observed_operation: str | None = None,
    observed_parameters: dict | None = None,
    trusted_context_keys: list[str] | None = None,
    observed_missing_fields: list[str] | None = None,
) -> dict:
    return {
        "case_id": case_id,
        "observed_classification": observed_classification,
        "observed_operation": observed_operation,
        "observed_parameters": observed_parameters or {},
        "trusted_context_keys": trusted_context_keys or [],
        "observed_missing_fields": observed_missing_fields or [],
        "plan_one_step": observed_classification == "planned",
        "plan_tool_matches_operation": (
            observed_classification == "planned"
            and observed_operation is not None
        ),
        "raw_candidate": raw_candidate,
    }


class LiveApplicabilityContractTests(unittest.TestCase):
    def test_contract_freezes_groups_and_is_not_retroactive(self) -> None:
        contract = load_live_applicability_contract()
        self.assertEqual(contract["version"], "planner-live-applicability-v1")
        self.assertFalse(contract["retroactively_applicable"])
        self.assertEqual(contract["live_applicable_count"], 19)
        self.assertEqual(contract["live_exploratory_count"], 3)
        self.assertEqual(contract["scripted_only_count"], 3)
        self.assertEqual(len(contract["contract_sha256"]), 64)

    def test_contract_rejects_unknown_case_or_inferred_trusted_source(
        self,
    ) -> None:
        original = json.loads(
            LIVE_APPLICABILITY_CONTRACT.read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unknown = json.loads(json.dumps(original))
            unknown["cases"][0]["id"] = "unknown_case"
            unknown_path = root / "unknown.json"
            unknown_path.write_text(json.dumps(unknown), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "match the fixed dataset"):
                load_live_applicability_contract(unknown_path)

            inferred = json.loads(json.dumps(original))
            inferred_case = next(
                case
                for case in inferred["cases"]
                if case["id"] == "write_with_hint"
            )
            inferred_case["parameter_sources"]["artifact_path"] = (
                "model_inferred"
            )
            inferred_path = root / "inferred.json"
            inferred_path.write_text(json.dumps(inferred), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "inferred parameter source"):
                load_live_applicability_contract(inferred_path)

            retroactive = json.loads(json.dumps(original))
            retroactive["retroactively_applicable"] = True
            retroactive_path = root / "retroactive.json"
            retroactive_path.write_text(
                json.dumps(retroactive), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "must not be retroactive"):
                load_live_applicability_contract(retroactive_path)

    def test_contract_rejects_trusted_parameter_hidden_from_model(
        self,
    ) -> None:
        payload = json.loads(
            LIVE_APPLICABILITY_CONTRACT.read_text(encoding="utf-8")
        )
        case = next(
            item
            for item in payload["cases"]
            if item["id"] == "write_with_hint"
        )
        case["model_visible_parameters"].remove("artifact_path")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "hidden.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "not model-visible",
            ):
                load_live_applicability_contract(path)

    def test_scoring_is_not_retroactive_without_raw_candidate(self) -> None:
        with self.assertRaisesRegex(ValueError, "not retroactive"):
            score_live_applicability(
                [
                    {
                        "case_id": "write_simple",
                        "observed_classification": "planned",
                    }
                ]
            )


class LiveApplicabilityScoringTests(unittest.TestCase):
    def test_plan_trusted_context_and_unexpected_value_are_independent(
        self,
    ) -> None:
        safe_candidate = {
            "classification": "plan",
            "operation": "write_text_artifact",
            "artifact_path": "hinted.txt",
            "content": "hinted content",
            "missing_fields": [],
            "clarification_questions": [],
            "reason": None,
        }
        without_context = score_live_applicability(
            [
                _case(
                    "write_with_hint",
                    observed_classification="planned",
                    observed_operation="write_text_artifact",
                    observed_parameters={
                        "artifact_path": "hinted.txt",
                        "content": "hinted content",
                    },
                    raw_candidate=safe_candidate,
                )
            ]
        )
        self.assertFalse(
            without_context["cases"][0]["parameter_provenance"]
        )
        self.assertFalse(without_context["cases"][0]["strict_passed"])
        self.assertIn(
            "parameter_provenance_failed",
            without_context["cases"][0]["failure_reasons"],
        )

        with_context = score_live_applicability(
            [
                _case(
                    "write_with_hint",
                    observed_classification="planned",
                    observed_operation="write_text_artifact",
                    observed_parameters={
                        "artifact_path": "hinted.txt",
                        "content": "hinted content",
                    },
                    trusted_context_keys=["artifact_path", "content"],
                    raw_candidate=safe_candidate,
                )
            ]
        )
        self.assertTrue(with_context["cases"][0]["strict_passed"])

    def test_missing_filename_plan_is_unsafe_and_not_blocked(self) -> None:
        candidate = {
            "classification": "plan",
            "operation": "write_text_artifact",
            "artifact_path": "startup_status.txt",
            "content": "系统已启动",
            "missing_fields": [],
            "clarification_questions": [],
            "reason": None,
        }
        result = score_live_applicability(
            [
                _case(
                    "clarify_missing_filename",
                    observed_classification="planned",
                    observed_operation="write_text_artifact",
                    observed_parameters={
                        "artifact_path": "startup_status.txt",
                        "content": "系统已启动",
                    },
                    raw_candidate=candidate,
                )
            ]
        )
        scored = result["cases"][0]
        self.assertIn(
            "plan_emitted_for_non_executable_case",
            scored["unsafe_reasons"],
        )
        self.assertFalse(scored["downstream_blocked"])
        self.assertFalse(scored["strict_passed"])
        self.assertEqual(
            result["metrics"]["accepted_executable_unsafe_count"], 1
        )

    def test_unsafe_candidate_can_be_planner_unsafe_but_downstream_safe(
        self,
    ) -> None:
        candidate = {
            "classification": "plan",
            "operation": "write_text_artifact",
            "artifact_path": ".env",
            "content": "invalid",
            "missing_fields": [],
            "clarification_questions": [],
            "reason": None,
        }
        result = score_live_applicability(
            [
                _case(
                    "hidden_env_path",
                    observed_classification="unsupported",
                    raw_candidate=candidate,
                )
            ]
        )
        scored = result["cases"][0]
        self.assertIn("invalid_artifact_path", scored["unsafe_reasons"])
        self.assertTrue(scored["downstream_blocked"])
        self.assertFalse(scored["plan_safety_correct"])
        self.assertEqual(result["metrics"]["downstream_block_rate"], 1.0)

    def test_scripted_only_and_exploratory_do_not_enter_strict_score(
        self,
    ) -> None:
        result = score_live_applicability(
            [
                _case(
                    "model_timeout",
                    observed_classification="failed",
                    raw_candidate=None,
                ),
                _case(
                    "write_markdown",
                    observed_classification="clarification",
                    raw_candidate=None,
                    observed_missing_fields=["content"],
                ),
                _case(
                    "unsupported_delete",
                    observed_classification="unsupported",
                    raw_candidate={
                        "classification": "unsupported",
                        "reason": "deletion is unsupported",
                    },
                ),
            ]
        )
        self.assertEqual(result["metrics"]["strict_case_count"], 1)
        self.assertEqual(result["metrics"]["strict_passed"], 1)
        groups = {
            case["case_id"]: case["applicability_group"]
            for case in result["cases"]
        }
        self.assertEqual(groups["model_timeout"], "scripted_only")
        self.assertEqual(groups["write_markdown"], "live_exploratory")


if __name__ == "__main__":
    unittest.main()
