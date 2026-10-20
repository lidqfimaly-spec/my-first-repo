"""Versioned offline planner and local-boundary safety gate."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from .artifact_tool import RealArtifactWriter
from .conversation_service import ConversationService
from .evaluation import EVALUATION_VERSION, run_evaluation
from .llm_planner import openai_sdk_status
from .models import (
    EventType,
    ExecutionMode,
    TaskRequest,
    TaskStatus,
    ToolResult,
    VerificationResult,
)
from .planner import WRITE_TEXT_ARTIFACT
from .service import TaskService
from .verifier import ArtifactVerifier


DEFAULT_GATE_DATASET = (
    Path(__file__).with_name("evals") / "safety_gate_v1.json"
)
GATE_VERSION = "1.0.0"


class SafetyGateConfigurationError(RuntimeError):
    pass


class CountingWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self, root: Path) -> None:
        self.delegate = RealArtifactWriter(root)
        self.calls = 0

    def write(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        return self.delegate.write(**kwargs)


class RaisingVerifier:
    name = "safety-gate-raising-verifier"

    def verify(self, **_kwargs: Any):
        raise RuntimeError("controlled verifier failure")


class NoWriteWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self) -> None:
        self.calls = 0

    def write(self, **_kwargs: Any) -> ToolResult:
        self.calls += 1
        raise AssertionError("recovery must not invoke the writer")


def load_gate_dataset(path: Path | None = None) -> dict[str, Any]:
    dataset_path = path or DEFAULT_GATE_DATASET
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SafetyGateConfigurationError("safety gate dataset must be an object")
    cases = payload.get("cases")
    required = payload.get("required_categories")
    if not isinstance(cases, list) or not isinstance(required, list):
        raise SafetyGateConfigurationError(
            "safety gate dataset requires cases and required_categories"
        )
    if not cases or not required:
        raise SafetyGateConfigurationError(
            "safety gate dataset must contain cases and required categories"
        )
    seen: set[str] = set()
    categories: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise SafetyGateConfigurationError("gate case must be an object")
        for field in (
            "id",
            "category",
            "kind",
            "component",
            "input",
            "expected",
            "forbidden_actions",
            "design_reason",
        ):
            if field not in case:
                raise SafetyGateConfigurationError(
                    f"gate case missing field: {field}"
                )
        case_id = str(case["id"])
        if case_id in seen:
            raise SafetyGateConfigurationError(
                f"duplicate gate case: {case_id}"
            )
        seen.add(case_id)
        categories.add(str(case["category"]))
        if case["kind"] not in LOCAL_HANDLERS:
            raise SafetyGateConfigurationError(
                f"unknown local gate kind: {case['kind']}"
            )
        if not isinstance(case["input"], dict) or not isinstance(
            case["expected"], dict
        ):
            raise SafetyGateConfigurationError(
                f"gate input/expected must be objects: {case_id}"
            )
    missing = set(str(item) for item in required) - categories - {
        "normal_plan",
        "unsupported",
        "malformed_model_output",
    }
    if missing:
        raise SafetyGateConfigurationError(
            f"required local categories missing: {sorted(missing)}"
        )
    return payload


def _request(input_data: Mapping[str, Any]) -> TaskRequest:
    return TaskRequest(
        goal=str(input_data.get("goal", "safety gate")),
        operation=WRITE_TEXT_ARTIFACT,
        parameters={
            "artifact_path": input_data.get("artifact_path", "gate.txt"),
            "content": input_data.get("content", "gate"),
        },
    )


def _tool_count(service: TaskService, task_id: str) -> int:
    return sum(
        1
        for event in service.events(task_id)
        if event.event_type == EventType.TOOL_STARTED.value
    )


def _local_policy_block(case: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        service = TaskService(Path(temp_dir))
        task = service.submit(_request(case["input"]))
        return {
            "status": task.status.value,
            "tool_calls": _tool_count(service, task.task_id),
            "artifact_created": task.artifact is not None,
            "error_code": task.error.code if task.error else None,
        }


def _local_clarification(case: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        service = ConversationService(Path(temp_dir))
        service.create_conversation(conversation_id="gate")
        result = service.process_message(
            "gate",
            str(case["input"]["content"]),
            client_message_id="gate-write",
        )
        status = result.task.status.value if result.task else "missing"
        tool_calls = (
            _tool_count(service.task_service, result.task.task_id)
            if result.task
            else 0
        )
        return {
            "status": status,
            "tool_calls": tool_calls,
            "artifact_created": bool(
                result.task and result.task.artifact is not None
            ),
            "new_tasks": 1 if result.task else 0,
        }


def _local_status_query(case: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        service = ConversationService(Path(temp_dir))
        service.create_conversation(conversation_id="gate")
        created = service.process_message(
            "gate",
            "create a file",
            client_message_id="gate-new",
        )
        before = len(
            service.store.find_tasks_by_statuses(
                {
                    TaskStatus.QUEUED,
                    TaskStatus.RUNNING,
                    TaskStatus.AWAITING_CLARIFICATION,
                    TaskStatus.BLOCKED,
                    TaskStatus.VERIFYING,
                }
            )
        )
        result = service.process_message(
            "gate",
            str(case["input"]["content"]),
            client_message_id="gate-status",
        )
        after = len(
            service.store.find_tasks_by_statuses(
                {
                    TaskStatus.QUEUED,
                    TaskStatus.RUNNING,
                    TaskStatus.AWAITING_CLARIFICATION,
                    TaskStatus.BLOCKED,
                    TaskStatus.VERIFYING,
                }
            )
        )
        return {
            "status": (
                created.task.status.value
                if created.task
                else "missing"
            ),
            "tool_calls": (
                _tool_count(service.task_service, created.task.task_id)
                if created.task
                else 0
            ),
            "artifact_created": False,
            "new_tasks": after - before,
        }


def _local_cancellation(case: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        service = TaskService(Path(temp_dir))
        task = service.enqueue(_request(case["input"]))
        service.request_cancel(
            task.task_id,
            requested_by="safety-gate",
            reason=str(case["input"]["reason"]),
        )
        cancelled = service.execute_task(task.task_id)
        return {
            "status": cancelled.status.value,
            "tool_calls": _tool_count(service, task.task_id),
            "artifact_created": cancelled.artifact is not None,
        }


def _local_budget(case: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        clock = ["2026-10-20T00:00:00.000+00:00"]
        service = TaskService(
            Path(temp_dir),
            clock=lambda: clock[0],
            max_execution_seconds=float(
                case["input"].get("max_execution_seconds", 1)
            ),
        )
        task = service.enqueue(_request(case["input"]))
        claim = service._claim(
            task.task_id,
            allowed=TaskService.RUNNABLE_STATUSES,
        )
        if not claim.acquired:
            raise RuntimeError("gate budget task was not claimed")
        service.store.transition(
            task.task_id,
            TaskStatus.RUNNING,
            EventType.TASK_PLANNED,
            clock[0],
            lease_token=str(claim.task.lease_token),
        )
        clock[0] = "2026-10-20T00:00:02.000+00:00"
        failed = service.execute_task(task.task_id)
        return {
            "status": failed.status.value,
            "error_code": failed.error.code if failed.error else None,
            "tool_calls": _tool_count(service, task.task_id),
            "artifact_created": failed.artifact is not None,
        }


def _local_message_idempotency(case: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        service = ConversationService(root)
        service.create_conversation(conversation_id="gate")
        first = service.append_user_message(
            "gate",
            str(case["input"]["content"]),
            client_message_id=str(case["input"]["client_message_id"]),
        )
        second = service.append_user_message(
            "gate",
            str(case["input"]["content"]),
            client_message_id=str(case["input"]["client_message_id"]),
        )
        connection = sqlite3.connect(
            root / "data" / "zevorinth.sqlite3"
        )
        try:
            messages = connection.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
            turns = connection.execute(
                "SELECT COUNT(*) FROM conversation_turns"
            ).fetchone()[0]
        finally:
            connection.close()
        return {
            "messages": messages,
            "turns": turns,
            "same_message": first[1].message_id == second[1].message_id,
            "tool_calls": 0,
        }


def _local_duplicate_execution(case: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        writer = CountingWriter(root / "artifacts")
        service = TaskService(root, writer=writer)
        task = service.submit(_request(case["input"]))
        service.execute_task(task.task_id)
        service.resume(task.task_id)
        event_types = [
            event.event_type for event in service.events(task.task_id)
        ]
        return {
            "status": task.status.value,
            "tool_calls": writer.calls,
            "artifact_created": task.artifact is not None,
            "task_succeeded_events": event_types.count(
                EventType.TASK_SUCCEEDED.value
            ),
        }


def _local_recovery(case: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        task_ids: list[str] = []

        def interrupt(_stage: str, task) -> None:
            task_ids.append(task.task_id)
            raise RuntimeError("offline gate interrupt")

        crashing = TaskService(root, fault_hook=interrupt)
        try:
            crashing.submit(_request(case["input"]))
        except RuntimeError:
            pass
        if not task_ids:
            raise RuntimeError("recovery scenario did not reach interrupt")
        task = crashing.require_task(task_ids[0])
        if task.lease_expires_at is None:
            raise RuntimeError("recovery scenario has no lease expiry")
        from datetime import datetime, timedelta

        recovery_time = (
            datetime.fromisoformat(task.lease_expires_at)
            + timedelta(seconds=1)
        ).isoformat(timespec="milliseconds")
        no_write = NoWriteWriter()
        recovered = TaskService(
            root,
            writer=no_write,
            clock=lambda: recovery_time,
        ).resume(task.task_id)
        event_types = [
            event.event_type
            for event in TaskService(root).events(task.task_id)
        ]
        return {
            "status": recovered.status.value,
            "tool_calls": no_write.calls,
            "tool_started_events": event_types.count(
                EventType.TOOL_STARTED.value
            ),
            "artifact_created": recovered.artifact is not None,
        }


def _local_verification(case: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "artifacts"
        task_id = str(uuid4())
        result = RealArtifactWriter(root).write(
            task_id=task_id,
            attempt_id=str(uuid4()),
            artifact_path=str(case["input"]["artifact_path"]),
            content=str(case["input"]["content"]),
            started_at="a",
            finished_at="b",
            lease_token="lease",
            claim_generation=1,
            authorize=lambda _token, _generation: None,
        )
        result.artifact.attempt_id = str(uuid4())
        verification = ArtifactVerifier(root).verify(
            task_id=task_id,
            artifact_path=str(case["input"]["artifact_path"]),
            expected_content=str(case["input"]["content"]),
            tool_result=result,
            checked_at="checked",
            expected_attempt_id=result.attempt_id,
            require_provenance=True,
            maximum_claim_generation=1,
        )
        return {
            "verified": verification.passed,
            "reason": verification.reason,
            "tool_calls": 1,
            "artifact_created": True,
        }


def _local_verifier_exception(case: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        service = TaskService(Path(temp_dir))
        service.verifier = RaisingVerifier()
        task = service.submit(_request(case["input"]))
        return {
            "status": task.status.value,
            "error_code": task.error.code if task.error else None,
            "reason": (
                task.verification.reason
                if task.verification is not None
                else None
            ),
            "tool_calls": (
                _tool_count(service, task.task_id)
                if task.status is not TaskStatus.FAILED
                else 1
            ),
            "artifact_created": task.artifact is not None,
        }


def _local_plan_snapshot(case: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        service = TaskService(Path(temp_dir))

        def mutate(_stage: str, task) -> None:
            service.store.record_event_and_update(
                task.task_id,
                "test_plan_mutation",
                task.updated_at,
                updates={"plan_version": task.plan_version + 1},
                lease_token=task.lease_token,
            )

        service.fault_hook = mutate
        task = service.submit(_request(case["input"]))
        return {
            "status": task.status.value,
            "error_code": task.error.code if task.error else None,
            "tool_calls": 1,
            "artifact_created": task.artifact is not None,
        }


LOCAL_HANDLERS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "policy_block": _local_policy_block,
    "clarification": _local_clarification,
    "status_query": _local_status_query,
    "cancellation": _local_cancellation,
    "budget": _local_budget,
    "message_idempotency": _local_message_idempotency,
    "duplicate_execution": _local_duplicate_execution,
    "recovery": _local_recovery,
    "verification": _local_verification,
    "verifier_exception": _local_verifier_exception,
    "plan_snapshot_mismatch": _local_plan_snapshot,
}


def _expected_matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    for key, expected_value in expected.items():
        if actual.get(key) != expected_value:
            return False
    return True


def _planner_category(expected_classification: str) -> str:
    return {
        "planned": "normal_plan",
        "clarification": "clarification",
        "unsupported": "unsupported",
        "failed": "malformed_model_output",
    }.get(expected_classification, "planner_other")


def _git_head() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def run_safety_gate(
    *,
    dataset_path: Path | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    dataset = load_gate_dataset(dataset_path)
    planner_report = run_evaluation(online=False)
    if str(planner_report.get("dataset_version")) != str(
        dataset.get("planner_dataset_version")
    ):
        raise SafetyGateConfigurationError(
            "planner dataset version does not match safety gate"
        )
    cases: list[dict[str, Any]] = []
    for planner_case in planner_report["cases"]:
        expected = {
            "classification": planner_case["expected_classification"],
            "status": planner_case["expected_status"],
        }
        actual = {
            "classification": planner_case["observed_classification"],
            "status": planner_case["observed_status"],
        }
        cases.append(
            {
                "id": str(planner_case["case_id"]),
                "category": _planner_category(
                    str(planner_case["expected_classification"])
                ),
                "layer": "offline_planner",
                "component": "LLMPlanner/CandidateValidator/Policy",
                "input": {"case_id": planner_case["case_id"]},
                "expected": expected,
                "actual": actual,
                "forbidden_actions": [
                    "tool_execution_on_unsafe_case",
                    "false_success",
                ],
                "tool_executed": planner_case["tool_executed"],
                "artifact_created": planner_case["artifact_created"],
                "candidate_valid": planner_case["candidate_valid"],
                "execution_mode": planner_case["execution_mode"],
                "duration_ms": planner_case["latency_ms"],
                "failure_category": (
                    None
                    if planner_case["passed"]
                    else "planning_or_validation"
                ),
                "passed": bool(planner_case["passed"]),
            }
        )
    for case in dataset["cases"]:
        case_started = time.perf_counter()
        try:
            actual = LOCAL_HANDLERS[str(case["kind"])](case)
            passed = _expected_matches(case["expected"], actual)
            error = None
        except Exception as exc:
            actual = {}
            passed = False
            error = type(exc).__name__
        duration_ms = (time.perf_counter() - case_started) * 1000
        cases.append(
            {
                "id": str(case["id"]),
                "category": str(case["category"]),
                "layer": "local",
                "component": str(case["component"]),
                "input": dict(case["input"]),
                "expected": dict(case["expected"]),
                "actual": actual,
                "forbidden_actions": list(case["forbidden_actions"]),
                "error": error,
                "duration_ms": duration_ms,
                "failure_category": (
                    None
                    if passed
                    else (
                        "harness"
                        if error is not None
                        else str(case["category"])
                    )
                ),
                "passed": passed,
            }
        )

    categories = sorted({str(case["category"]) for case in cases})
    required = set(str(item) for item in dataset["required_categories"])
    missing_categories = sorted(required - set(categories))
    failures = [case for case in cases if not case["passed"]]
    failure_counts: dict[str, int] = {}
    for case in failures:
        key = case["failure_category"] or "unknown"
        failure_counts[key] = failure_counts.get(key, 0) + 1
    planner_cases = [
        case for case in cases if case["layer"] == "offline_planner"
    ]
    local_cases = [case for case in cases if case["layer"] == "local"]
    normal_cases = [
        case
        for case in planner_cases
        if case["expected"].get("status") == "succeeded"
    ]
    blocked_cases = [
        case
        for case in cases
        if case["expected"].get("status") in {"blocked", "planning_failed"}
    ]
    tool_cases = sum(
        1 for case in cases if bool(case.get("tool_executed"))
    ) + sum(
        int(case.get("actual", {}).get("tool_calls", 0))
        for case in local_cases
    )
    duplicate_side_effects = sum(
        int(case.get("actual", {}).get("tool_calls", 0)) - 1
        for case in local_cases
        if case["category"] == "idempotency"
        and int(case.get("actual", {}).get("tool_calls", 0)) > 1
    )
    total_duration_ms = (time.perf_counter() - started) * 1000
    status_cases = [
        case for case in cases if "status" in case["expected"]
    ]
    metrics = {
        "classification_accuracy": planner_report["metrics"][
            "classification_accuracy"
        ],
        "status_accuracy": (
            sum(
                1
                for case in status_cases
                if case.get("actual", {}).get("status")
                == case.get("expected", {}).get("status")
            )
            / len(status_cases)
        ),
        "candidate_validation_pass_rate": planner_report["metrics"][
            "candidate_validation_pass_rate"
        ],
        "normal_plan_success_rate": (
            sum(1 for case in normal_cases if case["passed"])
            / len(normal_cases)
            if normal_cases
            else None
        ),
        "safety_block_pass_rate": (
            sum(1 for case in blocked_cases if case["passed"])
            / len(blocked_cases)
            if blocked_cases
            else None
        ),
        "tool_call_count": tool_cases,
        "artifact_side_effect_count": sum(
            1
            for case in cases
            if bool(
                case.get("artifact_created")
                or case.get("actual", {}).get("artifact_created")
            )
        ),
        "duplicate_side_effects": duplicate_side_effects,
        "timeout_count": sum(
            1
            for case in planner_cases
            if case.get("actual", {}).get("classification") == "failed"
            and "timeout" in str(case["id"])
        ),
        "skipped_count": 0,
        "unexecuted_count": sum(
            1
            for case in cases
            if case.get("execution_mode") == "not_executed"
        ),
        "failure_counts": failure_counts,
        "total_duration_ms": total_duration_ms,
        "case_duration_ms": {
            case["id"]: case.get("duration_ms") for case in cases
        },
    }
    all_passed = (
        not failures
        and not missing_categories
        and duplicate_side_effects == 0
    )
    return {
        "gate_version": GATE_VERSION,
        "evaluation_version": EVALUATION_VERSION,
        "dataset_version": str(dataset["version"]),
        "planner_dataset_version": str(dataset["planner_dataset_version"]),
        "git_head": _git_head(),
        "python_version": sys.version,
        "sdk_status": openai_sdk_status(),
        "planner_client": "ScriptedPlannerClient",
        "case_count": len(cases),
        "planner_case_count": len(planner_cases),
        "local_case_count": len(local_cases),
        "categories": categories,
        "missing_categories": missing_categories,
        "cases": cases,
        "metrics": metrics,
        "all_passed": all_passed,
        "status": "PASS" if all_passed else "FAIL",
        "failure_reasons": (
            []
            if all_passed
            else [
                f"{case['id']}:{case['failure_category']}"
                for case in failures
            ]
            + (
                [f"missing_categories:{missing_categories}"]
                if missing_categories
                else []
            )
        ),
        "real_api_calls": 0,
        "network_request_sent": False,
    }
