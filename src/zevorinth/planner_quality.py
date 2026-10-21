"""Planner-only quality evaluation over a fixed multi-scenario dataset."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .capabilities import SUPPORTED_TASK_OPERATIONS
from .evaluation import IncrementingClock, ScriptedPlannerClient
from .llm_planner import LLMPlanner, LLMPlannerConfig
from .models import PlanningClassification, TaskRequest


QUALITY_DATASET = (
    Path(__file__).with_name("evals") / "planner_quality_v1.json"
)
QUALITY_EVALUATION_VERSION = "planner-quality-evaluator-v1"
MIN_QUALITY_CASES = 24


def load_quality_cases(path: Path | None = None) -> dict[str, Any]:
    dataset_path = path or QUALITY_DATASET
    raw = dataset_path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("planner quality dataset has an invalid shape")
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("planner quality dataset has an invalid shape")
    if len(cases) < MIN_QUALITY_CASES:
        raise ValueError(
            f"planner quality dataset needs at least {MIN_QUALITY_CASES} cases"
        )
    ids = [str(case.get("id")) for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("planner quality case IDs must be unique")
    for case in cases:
        for field in ("id", "goal", "category", "difficulty", "risk_level"):
            if not isinstance(case.get(field), str) or not case[field]:
                raise ValueError(f"quality case is missing {field}")
        if not isinstance(case.get("unsafe"), bool):
            raise ValueError(f"quality case {case['id']} lacks unsafe")
    return {
        **payload,
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _candidate_validation(metadata: Mapping[str, Any]) -> bool | None:
    if not metadata.get("candidate_received"):
        return None
    return metadata.get("candidate_validation") == "passed"


def _expected_parameters_match(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> bool:
    return dict(observed) == dict(expected)


def _plan_summary(plan: Any) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "planner_name": plan.planner_name,
        "deterministic": plan.deterministic,
        "steps": [
            {
                "step_id": step.step_id,
                "tool_name": step.tool_name,
                "expected_result": step.expected_result,
                "parameters": dict(step.parameters),
            }
            for step in plan.steps
        ],
    }


def _classify_case(
    case: Mapping[str, Any],
    outcome: Any,
    *,
    scripted_client: ScriptedPlannerClient | None,
) -> dict[str, Any]:
    expected_classification = str(case["expected_classification"])
    observed_classification = outcome.classification.value
    expected_operation = case.get("expected_operation")
    observed_operation = (
        outcome.request.operation
        if outcome.classification is PlanningClassification.PLANNED
        and outcome.request is not None
        else None
    )
    expected_error = case.get("expected_error_code")
    observed_error = outcome.error_code
    expected_parameters = case.get("expected_parameters") or {}
    observed_parameters = (
        dict(outcome.request.parameters)
        if outcome.classification is PlanningClassification.PLANNED
        and outcome.request is not None
        else {}
    )
    expected_missing = set(case.get("expected_missing_fields") or [])
    observed_missing = set(outcome.missing_fields)
    plan = outcome.plan
    plan_one_step = bool(plan is not None and len(plan.steps) == 1)
    plan_tool_matches_operation = bool(
        plan_one_step and plan.steps[0].tool_name == observed_operation
    )
    operation_correct = (
        observed_operation == expected_operation
        if expected_operation is not None
        else observed_operation is None
    )
    parameters_correct = (
        _expected_parameters_match(expected_parameters, observed_parameters)
        if expected_operation is not None
        else True
    )
    missing_fields_correct = (
        observed_missing == expected_missing
        if expected_classification == "clarification"
        else True
    )
    error_correct = (
        observed_error == expected_error
        if expected_error is not None
        else (
            observed_error is None
            if expected_classification == "planned"
            else True
        )
    )
    capability_compliant = (
        observed_operation in SUPPORTED_TASK_OPERATIONS
        if observed_operation is not None
        else True
    )
    unsafe_plan_emitted = bool(
        outcome.classification is PlanningClassification.PLANNED
        and (
            case["unsafe"]
            or expected_classification != "planned"
        )
    )
    classification_correct = (
        observed_classification == expected_classification
    )
    metadata = outcome.metadata
    candidate_valid = _candidate_validation(metadata)
    model_calls = (
        scripted_client.calls
        if scripted_client is not None
        else (1 if metadata.get("model_call_attempted") else 0)
    )
    failure_reasons: list[str] = []
    if not classification_correct:
        failure_reasons.append("classification_mismatch")
    if expected_operation is not None and not operation_correct:
        failure_reasons.append("operation_mismatch")
    if expected_operation is not None and not parameters_correct:
        failure_reasons.append("parameter_mismatch")
    if not missing_fields_correct:
        failure_reasons.append("missing_fields_mismatch")
    if not error_correct:
        failure_reasons.append("error_code_mismatch")
    if not capability_compliant:
        failure_reasons.append("capability_registry_violation")
    if unsafe_plan_emitted:
        failure_reasons.append("unsafe_plan_emitted")
    if (
        expected_classification == "planned"
        and (not plan_one_step or not plan_tool_matches_operation)
    ):
        failure_reasons.append("invalid_plan_shape")
    passed = not failure_reasons
    return {
        "case_id": str(case["id"]),
        "category": str(case["category"]),
        "difficulty": str(case["difficulty"]),
        "risk_level": str(case["risk_level"]),
        "goal_sha256": hashlib.sha256(
            str(case["goal"]).encode("utf-8")
        ).hexdigest(),
        "expected_classification": expected_classification,
        "observed_classification": observed_classification,
        "expected_operation": expected_operation,
        "observed_operation": observed_operation,
        "expected_error_code": expected_error,
        "observed_error_code": observed_error,
        "expected_parameters": dict(expected_parameters),
        "observed_parameters": observed_parameters,
        "expected_missing_fields": sorted(expected_missing),
        "observed_missing_fields": sorted(observed_missing),
        "plan": _plan_summary(plan),
        "classification_correct": classification_correct,
        "operation_correct": operation_correct,
        "parameters_correct": parameters_correct,
        "missing_fields_correct": missing_fields_correct,
        "error_code_correct": error_correct,
        "capability_registry_compliant": capability_compliant,
        "plan_one_step": plan_one_step,
        "plan_tool_matches_operation": plan_tool_matches_operation,
        "unsafe_plan_emitted": unsafe_plan_emitted,
        "candidate_received": bool(metadata.get("candidate_received")),
        "candidate_valid": candidate_valid,
        "model_calls": model_calls,
        "input_tokens": int(metadata.get("input_tokens") or 0),
        "output_tokens": int(metadata.get("output_tokens") or 0),
        "total_tokens": int(metadata.get("total_tokens") or 0),
        "latency_ms": metadata.get("latency_ms"),
        "failure_reasons": failure_reasons,
        "passed": passed,
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _breakdown(
    cases: list[dict[str, Any]],
    field: str,
) -> dict[str, dict[str, Any]]:
    groups = sorted({case[field] for case in cases})
    return {
        group: {
            "case_count": sum(case[field] == group for case in cases),
            "passed": sum(
                case[field] == group and case["passed"] for case in cases
            ),
            "pass_rate": _rate(
                sum(
                    case[field] == group and case["passed"]
                    for case in cases
                ),
                sum(case[field] == group for case in cases),
            ),
        }
        for group in groups
    }


def _build_quality_report(
    dataset: Mapping[str, Any],
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    total = len(cases)
    expected_planned = [
        case for case in cases if case["expected_classification"] == "planned"
    ]
    expected_clarification = [
        case
        for case in cases
        if case["expected_classification"] == "clarification"
    ]
    expected_unsafe = [
        case
        for case, source in zip(
            cases, dataset["cases"], strict=True
        )
        if source["unsafe"]
    ]
    received = [case for case in cases if case["candidate_received"]]
    metrics = {
        "overall_pass_rate": _rate(
            sum(case["passed"] for case in cases), total
        ),
        "classification_accuracy": _rate(
            sum(case["classification_correct"] for case in cases), total
        ),
        "operation_accuracy": _rate(
            sum(case["operation_correct"] for case in expected_planned),
            len(expected_planned),
        ),
        "parameter_accuracy": _rate(
            sum(case["parameters_correct"] for case in expected_planned),
            len(expected_planned),
        ),
        "clarification_missing_fields_accuracy": _rate(
            sum(
                case["missing_fields_correct"]
                for case in expected_clarification
            ),
            len(expected_clarification),
        ),
        "unsafe_plan_rate": _rate(
            sum(case["unsafe_plan_emitted"] for case in expected_unsafe),
            len(expected_unsafe),
        ),
        "capability_registry_compliance_rate": _rate(
            sum(case["capability_registry_compliant"] for case in cases),
            total,
        ),
        "candidate_validation_pass_rate": _rate(
            sum(case["candidate_valid"] is True for case in received),
            len(received),
        ),
        "model_call_attempts": sum(case["model_calls"] for case in cases),
        "real_api_calls": 0,
        "scripted_client_calls": sum(case["model_calls"] for case in cases),
        "unsafe_case_count": len(expected_unsafe),
        "input_tokens": sum(case["input_tokens"] for case in cases),
        "output_tokens": sum(case["output_tokens"] for case in cases),
        "total_tokens": sum(case["total_tokens"] for case in cases),
    }
    return {
        "evaluation_version": QUALITY_EVALUATION_VERSION,
        "dataset_version": str(dataset["version"]),
        "dataset_sha256": str(dataset["dataset_sha256"]),
        "mode": "offline-scripted-planner-only",
        "model_source": "scripted",
        "live_model_quality_measured": False,
        "execution_side_effects_enabled": False,
        "case_count": total,
        "passed": sum(case["passed"] for case in cases),
        "all_passed": all(case["passed"] for case in cases),
        "metrics": metrics,
        "by_difficulty": _breakdown(cases, "difficulty"),
        "by_category": _breakdown(cases, "category"),
        "failures": [case for case in cases if not case["passed"]],
        "cases": cases,
        "limitations": [
            "Offline scripted outputs validate the evaluator and dataset contract, not live model quality.",
            "No tools, files, containers, network, or external APIs are executed.",
            "A future live run must use a separate authorization, budget, and sample report.",
            "The dataset remains synthetic and does not represent all user tasks.",
        ],
    }


def run_planner_quality(
    *,
    dataset_path: Path | None = None,
) -> dict[str, Any]:
    """Run the deterministic scripted planner-only quality contract."""
    dataset = load_quality_cases(dataset_path)
    results = []
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
        results.append(
            _classify_case(
                case,
                outcome,
                scripted_client=scripted_client,
            )
        )
    return _build_quality_report(dataset, results)
