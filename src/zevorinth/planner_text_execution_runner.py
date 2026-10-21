"""Offline end-to-end execution of planner text precision contracts."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .evaluation import IncrementingClock, ScriptedPlannerClient
from .llm_planner import LLMPlanner, LLMPlannerConfig
from .models import TaskRequest, TaskStatus
from .planner_text_precision import load_exact_text_contract
from .service import TaskService


EXECUTION_CONTRACT = (
    Path(__file__).with_name("evals") / "planner_text_execution_v1.json"
)
EXECUTION_EVALUATION_VERSION = "planner-text-execution-evaluator-v1"
EXECUTION_CATEGORIES = frozenset(
    {"exact_text", "semantic", "format_constrained", "precondition"}
)
EXECUTION_STATUSES = frozenset({"succeeded", "planning_failed"})
SUCCESS_EVENT_ORDER = (
    "planning_completed",
    "task_planned",
    "tool_started",
    "tool_succeeded",
    "verification_started",
    "verification_completed",
    "task_succeeded",
)


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"text execution contract duplicate key: {key}")
        payload[key] = value
    return payload


def load_text_execution_contract(
    path: Path | None = None,
) -> dict[str, Any]:
    contract_path = path or EXECUTION_CONTRACT
    raw = contract_path.read_bytes()
    payload = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )
    if not isinstance(payload, dict):
        raise ValueError("text execution contract has invalid shape")
    if payload.get("version") != "planner-text-execution-v1":
        raise ValueError("text execution contract version mismatch")
    if payload.get("retroactively_applicable") is not False:
        raise ValueError("text execution contract must not be retroactive")
    precision = payload.get("precision_contract")
    if not isinstance(precision, Mapping):
        raise ValueError("text execution contract lacks precision binding")
    active_precision = load_exact_text_contract()
    if (
        precision.get("version") != active_precision["version"]
        or precision.get("sha256") != active_precision["contract_sha256"]
    ):
        raise ValueError("text execution precision contract mismatch")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) < 8:
        raise ValueError("text execution contract needs at least eight cases")
    ids: list[str] = []
    for case in cases:
        if not isinstance(case, Mapping):
            raise ValueError("text execution case must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("text execution case is missing id")
        ids.append(case_id)
        if case.get("category") not in EXECUTION_CATEGORIES:
            raise ValueError(
                f"text execution case {case_id} has invalid category"
            )
        if not isinstance(case.get("goal"), str) or not case["goal"]:
            raise ValueError(f"text execution case {case_id} lacks goal")
        if not isinstance(case.get("trusted_parameters"), Mapping):
            raise ValueError(
                f"text execution case {case_id} lacks trusted parameters"
            )
        candidate = case.get("scripted_output")
        if not isinstance(candidate, Mapping):
            raise ValueError(
                f"text execution case {case_id} lacks scripted output"
            )
        if candidate.get("classification") != "plan":
            raise ValueError(
                f"text execution case {case_id} must plan a write"
            )
        if not isinstance(candidate.get("artifact_path"), str):
            raise ValueError(
                f"text execution case {case_id} lacks artifact path"
            )
        expected_status = case.get("expected_task_status")
        if expected_status not in EXECUTION_STATUSES:
            raise ValueError(
                f"text execution case {case_id} has invalid status"
            )
        expected_content = case.get("expected_artifact_content")
        if expected_status == "succeeded" and not isinstance(
            expected_content, str
        ):
            raise ValueError(
                f"text execution success {case_id} lacks expected content"
            )
        if expected_status == "planning_failed" and expected_content is not None:
            raise ValueError(
                f"text execution rejection {case_id} declares content"
            )
    if len(ids) != len(set(ids)):
        raise ValueError("text execution case IDs must be unique")
    return {
        **payload,
        "contract_sha256": hashlib.sha256(raw).hexdigest(),
    }


def run_text_execution_regression(
    *,
    contract_path: Path | None = None,
) -> dict[str, Any]:
    contract = load_text_execution_contract(contract_path)
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
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, planner=planner)
            task = service.submit(
                TaskRequest(
                    goal=str(case["goal"]),
                    operation=None,
                    parameters=dict(case["trusted_parameters"]),
                )
            )
            events = service.events(task.task_id)
            event_types = [event.event_type for event in events]
            planning_event_type = (
                "planning_completed"
                if task.status is TaskStatus.SUCCEEDED
                else "planning_failed"
            )
            planning_event = next(
                (
                    event
                    for event in events
                    if event.event_type == planning_event_type
                ),
                None,
            )
            planning_metadata = (
                dict(planning_event.payload.get("planning_metadata") or {})
                if planning_event is not None
                else {}
            )
            artifact_name = str(case["scripted_output"]["artifact_path"])
            artifact_path = root / "artifacts" / task.task_id / artifact_name
            observed_bytes = (
                artifact_path.read_bytes()
                if artifact_path.is_file() and not artifact_path.is_symlink()
                else None
            )
            artifact_directory = root / "artifacts" / task.task_id
            artifact_file_count = (
                sum(path.is_file() for path in artifact_directory.rglob("*"))
                if artifact_directory.is_dir()
                else 0
            )
            reopened = TaskService(root, planner=planner).require_task(
                task.task_id
            )
        model_calls += client.calls
        expected_content = case.get("expected_artifact_content")
        expected_bytes = (
            expected_content.encode("utf-8")
            if isinstance(expected_content, str)
            else None
        )
        text_precision = planning_metadata.get("text_precision")
        precision = dict(text_precision) if isinstance(text_precision, Mapping) else {}
        intent = planning_metadata.get("intent_fidelity")
        intent_metadata = dict(intent) if isinstance(intent, Mapping) else {}
        verification = task.verification
        artifact = verification.artifact if verification is not None else None
        expected_status = str(case["expected_task_status"])
        observed_error_code = task.error.code if task.error else None
        reasons: list[str] = []
        if task.status.value != expected_status:
            reasons.append("task_status_mismatch")
        if observed_error_code != case.get("expected_error_code"):
            reasons.append("error_code_mismatch")
        if client.calls != 1:
            reasons.append("model_call_count_mismatch")
        if planning_metadata.get("text_precision_gate") != case.get(
            "expected_precision_gate"
        ):
            reasons.append("text_precision_gate_mismatch")
        if precision.get("result") != case.get("expected_precision_result"):
            reasons.append("text_precision_result_mismatch")
        if intent_metadata.get("status") != case.get("expected_intent_status"):
            reasons.append("intent_status_mismatch")
        if reopened.status.value != expected_status:
            reasons.append("persisted_status_mismatch")

        if expected_status == "succeeded":
            indices = [
                event_types.index(name)
                for name in SUCCESS_EVENT_ORDER
                if name in event_types
            ]
            if (
                any(name not in event_types for name in SUCCESS_EVENT_ORDER)
                or indices != sorted(indices)
            ):
                reasons.append("success_event_order_mismatch")
            if event_types.count("tool_started") != 1:
                reasons.append("tool_started_count_mismatch")
            if event_types.count("tool_succeeded") != 1:
                reasons.append("tool_succeeded_count_mismatch")
            if event_types.count("verification_completed") != 1:
                reasons.append("verification_count_mismatch")
            if verification is None or not verification.passed:
                reasons.append("verification_not_passed")
            elif verification.reason != "artifact_verified":
                reasons.append("verification_reason_mismatch")
            if artifact_file_count != 1:
                reasons.append("artifact_file_count_mismatch")
            if observed_bytes != expected_bytes:
                reasons.append("artifact_bytes_mismatch")
            if artifact is None:
                reasons.append("artifact_evidence_missing")
            else:
                if artifact.relative_path != artifact_name:
                    reasons.append("artifact_path_mismatch")
                if expected_bytes is not None:
                    if artifact.byte_count != len(expected_bytes):
                        reasons.append("artifact_byte_count_mismatch")
                    if artifact.sha256 != hashlib.sha256(
                        expected_bytes
                    ).hexdigest():
                        reasons.append("artifact_sha256_mismatch")
            if not precision.get("normalized_before_compare") is False:
                reasons.append("artifact_text_was_normalized")
        else:
            if artifact_file_count or observed_bytes is not None:
                reasons.append("rejected_case_created_artifact")
            if task.plan is not None:
                reasons.append("rejected_case_created_plan")
            if any(
                name in event_types
                for name in (
                    "task_planned",
                    "tool_started",
                    "tool_succeeded",
                    "verification_started",
                    "verification_completed",
                    "task_succeeded",
                )
            ):
                reasons.append("rejected_case_execution_event")

        results.append(
            {
                "case_id": str(case["id"]),
                "category": str(case["category"]),
                "expected_task_status": expected_status,
                "observed_task_status": task.status.value,
                "persisted_task_status": reopened.status.value,
                "expected_error_code": case.get("expected_error_code"),
                "observed_error_code": observed_error_code,
                "expected_precision_result": case.get(
                    "expected_precision_result"
                ),
                "observed_precision_result": precision.get("result"),
                "expected_precision_gate": case.get(
                    "expected_precision_gate"
                ),
                "observed_precision_gate": planning_metadata.get(
                    "text_precision_gate"
                ),
                "expected_intent_status": case.get("expected_intent_status"),
                "observed_intent_status": intent_metadata.get("status"),
                "normalized_before_compare": precision.get(
                    "normalized_before_compare"
                ),
                "event_types": event_types,
                "plan_created": task.plan is not None,
                "artifact_file_count": artifact_file_count,
                "artifact_byte_count": len(observed_bytes or b""),
                "artifact_prefix_hex": (observed_bytes or b"")[:8].hex(),
                "expected_artifact_sha256": (
                    hashlib.sha256(expected_bytes).hexdigest()
                    if expected_bytes is not None
                    else None
                ),
                "observed_artifact_sha256": (
                    hashlib.sha256(observed_bytes).hexdigest()
                    if observed_bytes is not None
                    else None
                ),
                "bytes_match_expected_utf8": (
                    observed_bytes == expected_bytes
                    if expected_bytes is not None
                    else None
                ),
                "verification_passed": (
                    bool(verification.passed)
                    if verification is not None
                    else False
                ),
                "verification_reason": (
                    verification.reason if verification is not None else None
                ),
                "model_calls": client.calls,
                "failure_reasons": reasons,
                "passed": not reasons,
            }
        )

    failed = [case for case in results if not case["passed"]]
    succeeded = [
        case
        for case in results
        if case["observed_task_status"] == "succeeded"
    ]
    rejected = [
        case
        for case in results
        if case["observed_task_status"] == "planning_failed"
    ]
    return {
        "evaluation_version": EXECUTION_EVALUATION_VERSION,
        "contract_version": contract["version"],
        "contract_sha256": contract["contract_sha256"],
        "precision_contract_version": contract["precision_contract"]["version"],
        "precision_contract_sha256": contract["precision_contract"]["sha256"],
        "retroactively_applicable": contract["retroactively_applicable"],
        "mode": "offline-scripted-end-to-end-write",
        "live_model_quality_measured": False,
        "real_artifact_execution_enabled": True,
        "execution_scope": "temporary_workspace_only",
        "all_passed": not failed,
        "metrics": {
            "case_count": len(results),
            "passed": len(results) - len(failed),
            "failed": len(failed),
            "succeeded_task_count": len(succeeded),
            "planning_failed_task_count": len(rejected),
            "verified_artifact_count": sum(
                bool(case["verification_passed"]) for case in results
            ),
            "artifact_write_count": sum(
                case["artifact_file_count"] == 1 for case in results
            ),
            "rejected_artifact_count": sum(
                case["observed_task_status"] == "planning_failed"
                and case["artifact_file_count"] == 0
                for case in results
            ),
            "model_call_attempts": model_calls,
            "real_api_calls": 0,
            "network_requests": 0,
            "dependency_installs": 0,
            "task_submissions": len(results),
            "tool_executions": len(succeeded),
            "container_executions": 0,
            "non_temporary_workspace_side_effects": 0,
            "normalization_used": False,
        },
        "cases": results,
        "limitations": [
            "The runner uses scripted planner candidates, not live model quality.",
            "Real writes and readback occur only inside temporary workspaces.",
            "UTF-8 bytes are verified after code-point precision checks.",
            "No container, network, dependency install, or project workspace write occurs.",
        ],
    }
