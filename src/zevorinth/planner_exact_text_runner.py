"""Offline regression runner for exact text and declared-format fidelity."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .evaluation import IncrementingClock, ScriptedPlannerClient
from .llm_planner import LLMPlanner, LLMPlannerConfig
from .models import TaskRequest
from .planner_text_precision import load_exact_text_contract


EXACT_TEXT_EVALUATION_VERSION = "planner-exact-text-evaluator-v1"


def run_exact_text_regression(
    *,
    contract_path: Path | None = None,
) -> dict[str, Any]:
    contract = load_exact_text_contract(contract_path)
    results: list[dict[str, Any]] = []
    model_calls = 0
    for case in contract["cases"]:
        client = ScriptedPlannerClient(case)
        planner = LLMPlanner(
            config=LLMPlannerConfig(
                model="offline-scripted",
                api_key_present=True,
            ),
            client=client,
            clock=IncrementingClock(),
        )
        request = TaskRequest(
            goal=str(case["goal"]),
            operation=None,
            parameters=dict(case["trusted_parameters"]),
        )
        outcome = planner.plan_request(request)
        model_calls += client.calls
        precision = outcome.metadata.get("text_precision")
        precision_metadata = (
            dict(precision) if isinstance(precision, Mapping) else {}
        )
        intent = outcome.metadata.get("intent_fidelity")
        intent_metadata = (
            dict(intent) if isinstance(intent, Mapping) else {}
        )
        reasons: list[str] = []
        if outcome.classification.value != case["expected_classification"]:
            reasons.append("classification_mismatch")
        if outcome.error_code != case.get("expected_error_code"):
            reasons.append("error_code_mismatch")
        if case["must_not_plan"] and outcome.plan is not None:
            reasons.append("plan_not_allowed")
        expected_gate = (
            "passed"
            if case["expected_classification"] == "planned"
            else "blocked"
        )
        if outcome.metadata.get("text_precision_gate") != expected_gate:
            reasons.append("text_precision_gate_mismatch")
        if expected_gate == "passed":
            if outcome.metadata.get("text_precision_preflight") is None:
                reasons.append("text_precision_preflight_missing")
            if not precision_metadata:
                reasons.append("text_precision_metadata_missing")
            elif precision_metadata.get("normalized_before_compare") is not False:
                reasons.append("text_was_normalized")
        elif outcome.metadata.get("text_precision_gate") != "blocked":
            reasons.append("blocked_gate_not_recorded")
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
                "expected_text_precision_gate": expected_gate,
                "observed_text_precision_gate": outcome.metadata.get(
                    "text_precision_gate"
                ),
                "precision_mode": precision_metadata.get("mode"),
                "mode_source": precision_metadata.get("mode_source"),
                "normalized_before_compare": precision_metadata.get(
                    "normalized_before_compare"
                ),
                "plan_created": outcome.plan is not None,
                "must_not_plan": bool(case["must_not_plan"]),
                "intent_status": intent_metadata.get("status"),
                "model_calls": client.calls,
                "execution_attempted": False,
                "failure_reasons": reasons,
                "passed": not reasons,
            }
        )
    failed_cases = [case for case in results if not case["passed"]]
    metrics = {
        "case_count": len(results),
        "passed": len(results) - len(failed_cases),
        "failed": len(failed_cases),
        "exact_case_count": sum(
            case["category"] == "exact_text" for case in results
        ),
        "format_case_count": sum(
            case["category"] == "format_constrained"
            for case in results
        ),
        "semantic_case_count": sum(
            case["category"] == "semantic" for case in results
        ),
        "contract_error_case_count": sum(
            case["category"] == "contract_error" for case in results
        ),
        "exact_mismatch_plan_count": sum(
            case["observed_error_code"] == "text_precision_mismatch"
            and case["plan_created"]
            for case in results
        ),
        "format_mismatch_plan_count": sum(
            case["observed_error_code"] == "text_format_mismatch"
            and case["plan_created"]
            for case in results
        ),
        "model_call_attempts": model_calls,
        "execution_attempts": 0,
        "real_api_calls": 0,
        "task_submissions": 0,
        "tool_executions": 0,
        "container_executions": 0,
        "workspace_side_effects": 0,
    }
    return {
        "evaluation_version": EXACT_TEXT_EVALUATION_VERSION,
        "contract_version": contract["version"],
        "contract_sha256": contract["contract_sha256"],
        "retroactively_applicable": contract["retroactively_applicable"],
        "mode": "offline-scripted-exact-text",
        "live_model_quality_measured": False,
        "execution_side_effects_enabled": False,
        "all_passed": not failed_cases,
        "metrics": metrics,
        "cases": results,
        "limitations": [
            "Scripted candidates validate deterministic precision gates.",
            "The runner does not measure live model quality.",
            "Empty write content remains invalid before text precision.",
            "No task, tool, container, network, or workspace execution occurs.",
        ],
    }
