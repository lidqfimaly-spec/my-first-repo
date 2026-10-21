"""Frozen metric semantics for integrated live Planner evaluation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .planner_intent_fidelity import (
    ACTIVE_INTENT_FIDELITY_VERSION,
    load_active_intent_fidelity_contract,
)
from .planner_live_applicability import load_live_applicability_contract
from .planner_quality import load_quality_cases


LIVE_EVALUATION_METRIC_CONTRACT = (
    Path(__file__).with_name("evals")
    / "live_evaluation_metric_contract_v1.json"
)
REQUIRED_GROUP_COUNTS = {
    "total": 25,
    "live_applicable": 19,
    "live_exploratory": 3,
    "scripted_only": 3,
    "intent_fidelity_eligible": 22,
}


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(
                f"live metric contract has duplicate key: {key}"
            )
        payload[key] = value
    return payload


def load_live_evaluation_metric_contract(
    path: Path | None = None,
) -> dict[str, Any]:
    contract_path = path or LIVE_EVALUATION_METRIC_CONTRACT
    raw = contract_path.read_bytes()
    payload = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )
    if not isinstance(payload, dict):
        raise ValueError("live metric contract has invalid shape")
    if payload.get("retroactively_applicable") is not False:
        raise ValueError("live metric contract must not be retroactive")
    rules = payload.get("reporting_rules")
    if not isinstance(rules, Mapping):
        raise ValueError("live metric contract lacks reporting rules")
    if rules.get("composite_score_allowed") is not False:
        raise ValueError("live metric contract must forbid composite scores")
    if payload.get("group_counts") != REQUIRED_GROUP_COUNTS:
        raise ValueError("live metric contract group counts changed")
    definitions = payload.get("metric_definitions")
    required_metrics = {
        "applicability_strict_pass_rate",
        "goal_conformity_pass_rate",
        "parameter_integrity_pass_rate",
        "clarification_correctness_rate",
        "rejection_correctness_rate",
        "intent_fidelity_pass_rate",
        "unsafe_candidate_rate",
        "downstream_block_rate",
        "accepted_executable_unsafe_count",
        "execution_attempts",
        "intent_status_counts",
    }
    if not isinstance(definitions, Mapping) or set(definitions) != required_metrics:
        raise ValueError("live metric definition set mismatch")
    required_fields = payload.get("required_live_report_fields")
    if (
        not isinstance(required_fields, list)
        or not required_fields
        or not all(isinstance(field, str) and field for field in required_fields)
    ):
        raise ValueError("live metric report field contract is invalid")

    dataset = load_quality_cases()
    dataset_contract = payload.get("dataset") or {}
    if (
        dataset_contract.get("version") != dataset["version"]
        or dataset_contract.get("sha256") != dataset["dataset_sha256"]
    ):
        raise ValueError("live metric dataset contract mismatch")
    applicability = load_live_applicability_contract()
    declared_applicability = (payload.get("contracts") or {}).get(
        "applicability", {}
    )
    if (
        declared_applicability.get("version") != applicability["version"]
        or declared_applicability.get("sha256")
        != applicability["contract_sha256"]
    ):
        raise ValueError("live metric applicability contract mismatch")
    intent = load_active_intent_fidelity_contract()
    declared_intent = (payload.get("contracts") or {}).get(
        "intent_fidelity", {}
    )
    if (
        declared_intent.get("version") != intent["version"]
        or declared_intent.get("sha256") != intent["contract_sha256"]
        or intent["version"] != ACTIVE_INTENT_FIDELITY_VERSION
    ):
        raise ValueError("live metric intent contract mismatch")
    return {
        **payload,
        "metric_contract_sha256": hashlib.sha256(raw).hexdigest(),
    }


def validate_live_report_against_metric_contract(
    report: Mapping[str, Any],
    *,
    contract: Mapping[str, Any] | None = None,
) -> None:
    loaded = contract or load_live_evaluation_metric_contract()
    for field in loaded["required_live_report_fields"]:
        if _lookup(report, field) is None:
            raise ValueError(f"live report is missing required field: {field}")
    if report.get("dataset_sha256") != loaded["dataset"]["sha256"]:
        raise ValueError("live report dataset SHA mismatch")
    if (
        _lookup(report, "live_applicability_contract.contract_sha256")
        != loaded["contracts"]["applicability"]["sha256"]
    ):
        raise ValueError("live report applicability contract mismatch")
    if (
        _lookup(report, "intent_fidelity_contract.contract_sha256")
        != loaded["contracts"]["intent_fidelity"]["sha256"]
    ):
        raise ValueError("live report intent contract mismatch")
    if report.get("live_model_quality_measured") is not True:
        raise ValueError("live report does not measure live model quality")
    if report.get("execution_side_effects_enabled") is not False:
        raise ValueError("live report enables execution side effects")
    accounting = report.get("request_accounting")
    if not isinstance(accounting, Mapping):
        raise ValueError("live report lacks request accounting")
    if (
        accounting.get("retries") != 0
        or accounting.get("maximum_requests") != 25
    ):
        raise ValueError("live report request boundaries changed")


def build_live_evaluation_integration_report() -> dict[str, Any]:
    contract = load_live_evaluation_metric_contract()
    applicability = load_live_applicability_contract()
    intent = load_active_intent_fidelity_contract()
    checks = [
        {
            "id": "metric_contract_loaded",
            "passed": True,
            "detail": contract["metric_contract_sha256"],
        },
        {
            "id": "applicability_contract_bound",
            "passed": (
                applicability["contract_sha256"]
                == contract["contracts"]["applicability"]["sha256"]
            ),
            "detail": applicability["version"],
        },
        {
            "id": "active_intent_contract_bound",
            "passed": (
                intent["version"] == ACTIVE_INTENT_FIDELITY_VERSION
                and intent["contract_sha256"]
                == contract["contracts"]["intent_fidelity"]["sha256"]
            ),
            "detail": intent["version"],
        },
        {
            "id": "group_denominators_frozen",
            "passed": contract["group_counts"] == REQUIRED_GROUP_COUNTS,
            "detail": contract["group_counts"],
        },
        {
            "id": "composite_score_forbidden",
            "passed": contract["reporting_rules"][
                "composite_score_allowed"
            ]
            is False,
            "detail": "separate metric dimensions",
        },
        {
            "id": "retroactive_changes_forbidden",
            "passed": contract["retroactively_applicable"] is False,
            "detail": contract["version"],
        },
    ]
    return {
        "mode": "live-evaluation-integration-validation",
        "status": "PASS" if all(check["passed"] for check in checks) else "FAIL",
        "ready_for_third_live_run": all(
            check["passed"] for check in checks
        ),
        "network_request_sent": False,
        "real_model_requests": 0,
        "checks": checks,
        "metric_contract": {
            "version": contract["version"],
            "sha256": contract["metric_contract_sha256"],
            "retroactively_applicable": contract[
                "retroactively_applicable"
            ],
            "composite_score_allowed": contract["reporting_rules"][
                "composite_score_allowed"
            ],
        },
        "contracts": contract["contracts"],
        "group_counts": contract["group_counts"],
        "entry_points": contract["entry_points"],
        "remaining_risks": contract["limitations"],
    }


def write_live_evaluation_integration_report(
    output_path: Path,
) -> dict[str, Any]:
    if output_path.exists():
        raise FileExistsError(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report = build_live_evaluation_integration_report()
    with output_path.open("x", encoding="utf-8") as handle:
        json.dump(
            report,
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")
    return report


def _lookup(payload: Mapping[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current
