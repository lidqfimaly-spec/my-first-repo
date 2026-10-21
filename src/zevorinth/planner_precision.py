"""Offline regression runner for precision, schema, and unsupported intents."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .evaluation import IncrementingClock, ScriptedPlannerClient
from .llm_planner import LLMPlanner, LLMPlannerConfig
from .models import PlanningClassification, TaskRequest


PRECISION_DATASET = (
    Path(__file__).with_name("evals") / "planner_precision_v1.json"
)
PRECISION_EVALUATION_VERSION = "planner-precision-evaluator-v1"


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"planner precision dataset duplicate key: {key}")
        payload[key] = value
    return payload


def load_precision_cases(path: Path | None = None) -> dict[str, Any]:
    dataset_path = path or PRECISION_DATASET
    raw = dataset_path.read_bytes()
    payload = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("planner precision dataset has invalid shape")
    cases = payload["cases"]
    if len(cases) < 16:
        raise ValueError("planner precision dataset needs at least 16 cases")
    ids = [str(case.get("id")) for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("planner precision case IDs must be unique")
    for case in cases:
        for field in ("id", "category", "goal", "expected_classification"):
            if not isinstance(case.get(field), str) or not case[field]:
                raise ValueError(f"planner precision case is missing {field}")
        if not isinstance(case.get("must_not_plan"), bool):
            raise ValueError(
                f"planner precision case {case['id']} lacks must_not_plan"
            )
        if "script" not in case and not isinstance(
            case.get("scripted_output"), dict
        ):
            raise ValueError(
                f"planner precision case {case['id']} lacks candidate"
            )
    return {
        **payload,
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
    }


def run_precision_regression(
    *,
    dataset_path: Path | None = None,
) -> dict[str, Any]:
    dataset = load_precision_cases(dataset_path)
    results: list[dict[str, Any]] = []
    for case in dataset["cases"]:
        scripted_client = ScriptedPlannerClient(case)
        planner = LLMPlanner(
            config=LLMPlannerConfig(
                model="offline-scripted",
                api_key_present=True,
            ),
            client=scripted_client,
            clock=IncrementingClock(),
        )
        request = TaskRequest(
            goal=str(case["goal"]),
            operation=None,
            parameters=dict(case.get("trusted_parameters") or {}),
        )
        outcome = planner.plan_request(request)
        observed_parameters = (
            dict(outcome.request.parameters)
            if outcome.classification is PlanningClassification.PLANNED
            and outcome.request is not None
            else {}
        )
        intent = outcome.metadata.get("intent_fidelity")
        intent_status = (
            str(intent.get("status"))
            if isinstance(intent, dict)
            else None
        )
        expected_parameters = case.get("expected_plan_parameters")
        reasons: list[str] = []
        if outcome.classification.value != case["expected_classification"]:
            reasons.append("classification_mismatch")
        if outcome.error_code != case.get("expected_error_code"):
            reasons.append("error_code_mismatch")
        if case["must_not_plan"] and outcome.plan is not None:
            reasons.append("plan_not_allowed")
        if expected_parameters is not None and observed_parameters != expected_parameters:
            reasons.append("parameter_mismatch")
        expected_intent_status = case.get("expected_intent_status")
        if (
            expected_intent_status is not None
            and intent_status != expected_intent_status
        ):
            reasons.append("intent_status_mismatch")
        results.append(
            {
                "case_id": str(case["id"]),
                "category": str(case["category"]),
                "expected_classification": str(
                    case["expected_classification"]
                ),
                "observed_classification": outcome.classification.value,
                "expected_error_code": case.get("expected_error_code"),
                "observed_error_code": outcome.error_code,
                "expected_plan_parameters": expected_parameters,
                "observed_plan_parameters": observed_parameters,
                "expected_intent_status": expected_intent_status,
                "observed_intent_status": intent_status,
                "plan_created": outcome.plan is not None,
                "must_not_plan": case["must_not_plan"],
                "failure_reasons": reasons,
                "passed": not reasons,
            }
        )
    schema_results = [
        case for case in results if case["category"] == "schema_contract"
    ]
    unsupported_results = [
        case for case in results if case["category"] == "unsupported_intent"
    ]
    lossy_results = [
        case
        for case in results
        if case["expected_intent_status"] == "lossy_plan"
    ]
    metrics = {
        "case_count": len(results),
        "passed": sum(case["passed"] for case in results),
        "failed": sum(not case["passed"] for case in results),
        "schema_case_count": len(schema_results),
        "invalid_schema_plan_count": sum(
            case["plan_created"] and case["must_not_plan"]
            for case in schema_results
        ),
        "lossy_plan_bypass_count": sum(
            case["plan_created"] for case in lossy_results
        ),
        "unsupported_intent_plan_count": sum(
            case["plan_created"]
            for case in unsupported_results
            if case["must_not_plan"]
        ),
        "execution_attempts": 0,
        "real_api_calls": 0,
    }
    return {
        "evaluation_version": PRECISION_EVALUATION_VERSION,
        "dataset_version": str(dataset["version"]),
        "dataset_sha256": str(dataset["dataset_sha256"]),
        "mode": "offline-scripted-precision",
        "live_model_quality_measured": False,
        "execution_side_effects_enabled": False,
        "all_passed": all(case["passed"] for case in results),
        "metrics": metrics,
        "cases": results,
        "limitations": [
            "Scripted cases validate deterministic precision and rejection boundaries.",
            "No live model quality is measured.",
            "No task, tool, container, network, or workspace execution occurs.",
        ],
    }
