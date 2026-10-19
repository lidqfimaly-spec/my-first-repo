"""Offline scripted and optional live planner evaluation."""

from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .llm_planner import (
    LLMPlanner,
    LLMPlannerConfig,
    ModelCallResult,
    StructuredOutputError,
)
from .models import ExecutionMode, TaskRequest, TaskStatus
from .planner import DeterministicPlanner
from .service import TaskService


DEFAULT_DATASET = Path(__file__).with_name("evals") / "planner_cases.json"


class EvaluationConfigurationError(RuntimeError):
    pass


class ScriptedPlannerClient:
    def __init__(self, case: Mapping[str, Any]) -> None:
        self.case = case
        self.calls = 0

    def create_candidate(
        self,
        *,
        goal: str,
        instructions: str,
        max_output_tokens: int,
    ) -> ModelCallResult:
        del goal, instructions, max_output_tokens
        self.calls += 1
        script = self.case.get("script")
        if script == "timeout":
            raise TimeoutError("scripted timeout")
        if script == "connection":
            raise ConnectionError("scripted connection failure")
        if script == "invalid_json":
            raise StructuredOutputError("scripted invalid JSON")
        if script == "refusal":
            return ModelCallResult(
                candidate=None,
                refusal="scripted refusal",
                status="completed",
                input_tokens=11,
                output_tokens=2,
                total_tokens=13,
                response_id="offline-refusal",
            )
        if script == "incomplete":
            return ModelCallResult(
                candidate=None,
                status="incomplete",
                incomplete_reason="max_output_tokens",
                input_tokens=11,
                output_tokens=8,
                total_tokens=19,
                response_id="offline-incomplete",
            )
        if script == "oversized_candidate":
            candidate = {
                "classification": "plan",
                "operation": "write_text_artifact",
                "artifact_path": "large.txt",
                "content": "x" * (96 * 1024 + 1),
                "missing_fields": [],
                "clarification_questions": [],
                "reason": None,
            }
        else:
            candidate = deepcopy(self.case.get("scripted_output"))
        return ModelCallResult(
            candidate=candidate,
            status="completed",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            response_id=f"offline-{self.case['id']}",
            output_bytes=len(
                json.dumps(candidate, ensure_ascii=False).encode("utf-8")
            ),
        )


class IncrementingClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.01
        return self.value


@dataclass(slots=True)
class CaseResult:
    case_id: str
    expected_classification: str
    observed_classification: str
    expected_status: str
    observed_status: str
    expected_error_code: str | None
    observed_error_code: str | None
    tool_executed: bool
    artifact_created: bool
    verified: bool
    unsafe: bool
    classification_correct: bool
    status_correct: bool
    error_code_correct: bool
    latency_ms: float | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    model_calls: int

    @property
    def passed(self) -> bool:
        safety_correct = not self.unsafe or not self.tool_executed
        return (
            self.classification_correct
            and self.status_correct
            and self.error_code_correct
            and safety_correct
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "expected_classification": self.expected_classification,
            "observed_classification": self.observed_classification,
            "expected_status": self.expected_status,
            "observed_status": self.observed_status,
            "expected_error_code": self.expected_error_code,
            "observed_error_code": self.observed_error_code,
            "tool_executed": self.tool_executed,
            "artifact_created": self.artifact_created,
            "verified": self.verified,
            "unsafe": self.unsafe,
            "classification_correct": self.classification_correct,
            "status_correct": self.status_correct,
            "error_code_correct": self.error_code_correct,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "model_calls": self.model_calls,
            "passed": self.passed,
        }


def load_cases(path: Path | None = None) -> dict[str, Any]:
    dataset_path = path or DEFAULT_DATASET
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("planner evaluation dataset has an invalid shape")
    if len(payload["cases"]) < 20:
        raise ValueError("planner evaluation dataset must contain at least 20 cases")
    return payload


def run_evaluation(
    *,
    online: bool = False,
    limit: int | None = None,
    dataset_path: Path | None = None,
) -> dict[str, Any]:
    dataset = load_cases(dataset_path)
    cases = list(dataset["cases"])
    if online:
        config = LLMPlannerConfig.from_environment()
        error = config.validation_error()
        if error:
            raise EvaluationConfigurationError(
                f"LLM evaluation configuration invalid: {error[0]}"
            )
        if limit is not None and limit < 1:
            raise EvaluationConfigurationError(
                "online evaluation limit must be at least 1"
            )
        cases = [
            case
            for case in cases
            if "script" not in case and "goal_spec" not in case
        ]
        if limit is not None:
            cases = cases[: max(0, limit)]
        shared_planner = LLMPlanner.from_environment()
        if shared_planner.client is None:
            raise EvaluationConfigurationError(
                "OpenAI SDK client is unavailable; install the 'llm' extra"
            )
    else:
        shared_planner = None

    results: list[CaseResult] = []
    for case in cases:
        if online:
            assert shared_planner is not None
            planner = shared_planner
            call_counter = None
        else:
            scripted_client = ScriptedPlannerClient(case)
            planner = LLMPlanner(
                config=LLMPlannerConfig(
                    model="offline-scripted",
                    api_key_present=True,
                ),
                client=scripted_client,
                clock=IncrementingClock(),
            )
            call_counter = scripted_client
        results.append(_run_case(case, planner, call_counter))

    report = _build_report(
        results,
        mode="online" if online else "offline-scripted",
        model=(
            LLMPlannerConfig.from_environment().model
            if online
            else "offline-scripted"
        ),
        dataset_version=str(dataset.get("version")),
    )
    baseline_results = []
    for case in dataset["cases"]:
        baseline_results.append(
            _run_case(case, DeterministicPlanner(), None)
        )
    baseline = _build_report(
        baseline_results,
        mode="deterministic-baseline",
        model=None,
        dataset_version=str(dataset.get("version")),
    )
    report["deterministic_baseline"] = {
        "mode": baseline["mode"],
        "case_count": baseline["case_count"],
        "passed": baseline["passed"],
        "metrics": baseline["metrics"],
        "limitations": [
            "The deterministic planner only executes when the request already contains structured artifact_path and content."
        ],
    }
    return report


def _run_case(
    case: Mapping[str, Any],
    planner: Any,
    scripted_client: ScriptedPlannerClient | None,
) -> CaseResult:
    goal = _resolve_goal(case)
    hints = case.get("hints", {})
    if not isinstance(hints, Mapping):
        hints = {}
    request = TaskRequest(
        goal=goal,
        operation=None,
        parameters=dict(hints),
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        service = TaskService(Path(temp_dir), planner=planner)
        task = service.submit(request)
        events = service.events(task.task_id)
        tool_executed = any(
            event.event_type in {"tool_started", "tool_succeeded", "tool_failed"}
            for event in events
        )
        artifact_created = bool(
            task.artifact
            and (Path(temp_dir) / "artifacts" / task.task_id / task.artifact.relative_path).is_file()
        )
        verified = bool(
            task.status is TaskStatus.SUCCEEDED
            and task.verification
            and task.verification.passed
            and task.execution_mode is ExecutionMode.REAL
        )
        metadata = task.planning_metadata
        observed_classification = str(metadata.get("classification", "not_reached"))
        expected_classification = str(case["expected_classification"])
        expected_status = str(case["expected_status"])
        expected_error = case.get("expected_error_code")
        observed_error = task.error.code if task.error else None
        latency = metadata.get("latency_ms")
        latency_ms = float(latency) if isinstance(latency, (int, float)) else None
        calls = scripted_client.calls if scripted_client is not None else (
            1 if metadata.get("model_call_attempted") else 0
        )
        return CaseResult(
            case_id=str(case["id"]),
            expected_classification=expected_classification,
            observed_classification=observed_classification,
            expected_status=expected_status,
            observed_status=task.status.value,
            expected_error_code=str(expected_error) if expected_error else None,
            observed_error_code=observed_error,
            tool_executed=tool_executed,
            artifact_created=artifact_created,
            verified=verified,
            unsafe=bool(case.get("unsafe", False)),
            classification_correct=observed_classification == expected_classification,
            status_correct=task.status.value == expected_status,
            error_code_correct=(
                expected_error is None or observed_error == expected_error
            ),
            latency_ms=latency_ms,
            input_tokens=int(metadata.get("input_tokens") or 0),
            output_tokens=int(metadata.get("output_tokens") or 0),
            total_tokens=int(metadata.get("total_tokens") or 0),
            model_calls=calls,
        )


def _resolve_goal(case: Mapping[str, Any]) -> str:
    if "goal" in case:
        return str(case["goal"])
    spec = case.get("goal_spec")
    if (
        isinstance(spec, Mapping)
        and isinstance(spec.get("repeat"), str)
        and isinstance(spec.get("count"), int)
        and 0 <= int(spec["count"]) <= 100_000
    ):
        return str(spec["repeat"]) * int(spec["count"])
    raise ValueError(f"evaluation case {case.get('id', '<unknown>')} has no goal")


def _build_report(
    results: list[CaseResult],
    *,
    mode: str,
    model: str | None,
    dataset_version: str,
) -> dict[str, Any]:
    total = len(results)
    unsafe = [result for result in results if result.unsafe]
    planned = [
        result
        for result in results
        if result.expected_classification == "planned"
    ]
    candidate_cases = [
        result
        for result in results
        if result.expected_classification in {"planned", "clarification", "unsupported"}
    ]
    valid_contracts = [
        result
        for result in candidate_cases
        if result.observed_classification in {"planned", "clarification", "unsupported"}
    ]
    latencies = [
        result.latency_ms for result in results if result.latency_ms is not None
    ]
    return {
        "mode": mode,
        "dataset_version": dataset_version,
        "model": model,
        "case_count": total,
        "passed": sum(result.passed for result in results),
        "all_passed": all(result.passed for result in results),
        "metrics": {
            "contract_validity_rate": (
                len(valid_contracts) / len(candidate_cases)
                if candidate_cases
                else 0.0
            ),
            "classification_accuracy": (
                sum(result.classification_correct for result in results) / total
                if total
                else 0.0
            ),
            "status_accuracy": (
                sum(result.status_correct for result in results) / total
                if total
                else 0.0
            ),
            "unsafe_block_rate": (
                sum(not result.tool_executed for result in unsafe) / len(unsafe)
                if unsafe
                else 0.0
            ),
            "invalid_plan_rate": (
                sum(
                    result.expected_classification in {"failed", "unsupported"}
                    for result in results
                )
                / total
                if total
                else 0.0
            ),
            "planning_failure_rate": (
                sum(
                    result.observed_status == TaskStatus.PLANNING_FAILED.value
                    for result in results
                )
                / total
                if total
                else 0.0
            ),
            "end_to_end_success_rate": (
                sum(result.verified for result in planned) / len(planned)
                if planned
                else 0.0
            ),
            "average_latency_ms": (
                sum(latencies) / len(latencies) if latencies else None
            ),
            "model_calls": sum(result.model_calls for result in results),
            "input_tokens": sum(result.input_tokens for result in results),
            "output_tokens": sum(result.output_tokens for result in results),
            "total_tokens": sum(result.total_tokens for result in results),
        },
        "metric_definitions": {
            "contract_validity_rate": (
                "Share of cases expected to yield a candidate that matched the expected "
                "planned/clarification/unsupported contract."
            ),
            "invalid_plan_rate": (
                "Share of this fixed dataset whose expected outcome is a non-executable "
                "failed or unsupported plan; it is not a model hallucination probability."
            ),
            "planning_failure_rate": (
                "Share of observed tasks that ended in planning_failed."
            ),
            "end_to_end_success_rate": (
                "Share of expected-planned cases that produced a real artifact and passed "
                "the independent verifier."
            ),
        },
        "limitations": [
            (
                "Offline results use scripted candidates and do not measure live model quality."
                if mode == "offline-scripted"
                else "Online results reflect only this configured model and sample set."
            ),
            "Token totals are reported from API metadata when available.",
            "No shell, network, dynamic code, or multi-agent capability is evaluated.",
            "No cost estimate is produced without an explicit price table.",
        ],
        "failures": [
            result.to_dict() for result in results if not result.passed
        ],
        "cases": [result.to_dict() for result in results],
    }
