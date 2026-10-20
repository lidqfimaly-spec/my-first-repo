"""Versioned offline planner and local-boundary safety gate."""

from __future__ import annotations

import hashlib
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
    ArtifactEvidence,
    EventType,
    ExecutionMode,
    Plan,
    PlanStep,
    TaskRequest,
    TaskStatus,
    ToolResult,
    VerificationResult,
)
from .planner import WRITE_TEXT_ARTIFACT
from .policy import ArtifactPolicy
from .service import TaskService
from .verifier import ArtifactVerifier


DEFAULT_GATE_DATASET = (
    Path(__file__).with_name("evals") / "safety_gate_v1.json"
)
GATE_VERSION = "1.1.0"


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


ARTIFACT_ORIGINS = {"written", "recovered", "none"}
EVIDENCE_INTEGRITY = {
    "verified",       # artifact present, evidence hash matches the file
    "mismatch",       # artifact present, evidence hash disagrees (negative case)
    "unbacked",       # file exists on disk but the system claimed no evidence
    "not_applicable", # no artifact and no file
}


def _neutral_effect() -> dict[str, Any]:
    return {
        "origin": "none",
        "integrity": "not_applicable",
        "relative_path": None,
        "file_sha256": None,
        "evidence_sha256": None,
        "evidence_attempt_id": None,
        "evidence_claim_generation": None,
        "evidence_count": 0,
        "file_writes": 0,
        "tool_calls": 0,
    }


def build_effect(
    *,
    origin: str,
    tool_calls: int,
    file_writes: int,
    integrity: str | None = None,
    evidence: ArtifactEvidence | None = None,
    artifact_root: Path | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Record where an artifact came from instead of inferring it later.

    ``written`` means the gate observed a writer call in this case,
    ``recovered`` means the artifact was read back from persisted state with
    no writer call, and ``none`` means the case produced no artifact.  The
    aggregate artifact/write counters are derived from these records so a
    recovery read can never be silently counted as a new file side effect.
    """
    if origin not in ARTIFACT_ORIGINS:
        raise SafetyGateConfigurationError(f"unknown artifact origin: {origin}")
    if integrity is None:
        integrity = "not_applicable" if origin == "none" else "verified"
    if integrity not in EVIDENCE_INTEGRITY:
        raise SafetyGateConfigurationError(
            f"unknown evidence integrity: {integrity}"
        )
    file_sha256: str | None = None
    relative_path: str | None = None
    if evidence is not None:
        relative_path = evidence.relative_path
        if artifact_root is not None and task_id is not None:
            artifact_file = artifact_root / task_id / relative_path
            if artifact_file.is_file():
                file_sha256 = hashlib.sha256(
                    artifact_file.read_bytes()
                ).hexdigest()
    return {
        "origin": origin,
        "integrity": integrity,
        "relative_path": relative_path,
        "file_sha256": file_sha256,
        "evidence_sha256": evidence.sha256 if evidence is not None else None,
        "evidence_attempt_id": (
            evidence.attempt_id if evidence is not None else None
        ),
        "evidence_claim_generation": (
            evidence.claim_generation if evidence is not None else None
        ),
        "evidence_count": 1 if evidence is not None else 0,
        "file_writes": int(file_writes),
        "tool_calls": int(tool_calls),
    }


def validate_case_effects(case: Mapping[str, Any]) -> None:
    """Fail closed on an artifact record that contradicts its own counters."""
    case_id = str(case.get("id", "<unknown>"))
    effects = case.get("effects")
    if not isinstance(effects, Mapping):
        raise SafetyGateConfigurationError(
            f"case has no effect provenance: {case_id}"
        )
    if case.get("effects_unproven"):
        # Only a harness failure may omit provenance, and it must look
        # exactly like "no effects were observed" so it cannot be used to
        # hide a real artifact behind a failed case.
        if not case.get("error") or case.get("passed"):
            raise SafetyGateConfigurationError(
                f"unproven effects without a harness error: {case_id}"
            )
        if dict(effects) != _neutral_effect():
            raise SafetyGateConfigurationError(
                f"unproven effects are not neutral: {case_id}"
            )
        return
    origin = effects.get("origin")
    if origin not in ARTIFACT_ORIGINS:
        raise SafetyGateConfigurationError(
            f"case has invalid artifact origin: {case_id}"
        )
    integrity = effects.get("integrity", "verified")
    if integrity not in EVIDENCE_INTEGRITY:
        raise SafetyGateConfigurationError(
            f"case has invalid evidence integrity: {case_id}"
        )
    file_writes = effects.get("file_writes")
    tool_calls = effects.get("tool_calls")
    evidence_count = effects.get("evidence_count")
    for name, value in (
        ("file_writes", file_writes),
        ("tool_calls", tool_calls),
        ("evidence_count", evidence_count),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise SafetyGateConfigurationError(
                f"case has invalid {name}: {case_id}"
            )
    if origin == "none":
        if integrity not in {"unbacked", "not_applicable"}:
            raise SafetyGateConfigurationError(
                f"case without artifact claims integrity: {case_id}"
            )
        if file_writes != 0 or evidence_count != 0:
            raise SafetyGateConfigurationError(
                f"case without artifact reports effects: {case_id}"
            )
        for key in (
            "relative_path",
            "file_sha256",
            "evidence_sha256",
            "evidence_attempt_id",
        ):
            if effects.get(key) is not None:
                raise SafetyGateConfigurationError(
                    f"case without artifact has {key}: {case_id}"
                )
        return
    if evidence_count != 1:
        raise SafetyGateConfigurationError(
            f"artifact case must have exactly one evidence: {case_id}"
        )
    if not effects.get("relative_path"):
        raise SafetyGateConfigurationError(
            f"artifact case has no relative path: {case_id}"
        )
    file_sha = effects.get("file_sha256")
    evidence_sha = effects.get("evidence_sha256")
    if not file_sha or not evidence_sha:
        raise SafetyGateConfigurationError(
            f"artifact case has no hashes: {case_id}"
        )
    if integrity == "mismatch":
        if file_sha == evidence_sha:
            raise SafetyGateConfigurationError(
                f"declared hash mismatch but hashes agree: {case_id}"
            )
    elif integrity == "verified":
        if file_sha != evidence_sha:
            raise SafetyGateConfigurationError(
                f"artifact evidence hash mismatch: {case_id}"
            )
    else:
        raise SafetyGateConfigurationError(
            f"artifact case has integrity {integrity}: {case_id}"
        )
    if origin == "written":
        if file_writes < 1 or tool_calls < 1:
            raise SafetyGateConfigurationError(
                f"written artifact has no writer call: {case_id}"
            )
    elif origin == "recovered":
        if file_writes != 0:
            raise SafetyGateConfigurationError(
                f"recovered artifact reports file writes: {case_id}"
            )


def _local_policy_block(case: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        service = TaskService(Path(temp_dir))
        task = service.submit(_request(case["input"]))
        tool_calls = _tool_count(service, task.task_id)
        return {
            "status": task.status.value,
            "tool_calls": tool_calls,
            "artifact_created": task.artifact is not None,
            "error_code": task.error.code if task.error else None,
            "effects": build_effect(
                origin="none", tool_calls=tool_calls, file_writes=0
            ),
        }


def _local_unsupported_tool(case: Mapping[str, Any]) -> dict[str, Any]:
    request = _request(case["input"])
    plan = Plan(
        plan_id="unsupported-tool",
        planner_name="safety-gate",
        deterministic=True,
        steps=[
            PlanStep(
                step_id="step-1",
                tool_name=str(case["input"]["tool_name"]),
                expected_result="forbidden",
                parameters={},
            )
        ],
    )
    decision = ArtifactPolicy().evaluate(
        request=request,
        plan=plan,
        task_id=str(uuid4()),
        artifacts_root=Path(tempfile.gettempdir()) / "zevorinth-gate-artifacts",
    )
    return {
        "policy_allowed": decision.allowed,
        "error_code": decision.code,
        "tool_calls": 0,
        "effects": build_effect(origin="none", tool_calls=0, file_writes=0),
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
            "effects": build_effect(
                origin="none", tool_calls=tool_calls, file_writes=0
            ),
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
        tool_calls = (
            _tool_count(service.task_service, created.task.task_id)
            if created.task
            else 0
        )
        return {
            "status": (
                created.task.status.value
                if created.task
                else "missing"
            ),
            "tool_calls": tool_calls,
            "artifact_created": False,
            "new_tasks": after - before,
            "effects": build_effect(
                origin="none", tool_calls=tool_calls, file_writes=0
            ),
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
        tool_calls = _tool_count(service, task.task_id)
        return {
            "status": cancelled.status.value,
            "tool_calls": tool_calls,
            "artifact_created": cancelled.artifact is not None,
            "effects": build_effect(
                origin="none", tool_calls=tool_calls, file_writes=0
            ),
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
        tool_calls = _tool_count(service, task.task_id)
        return {
            "status": failed.status.value,
            "error_code": failed.error.code if failed.error else None,
            "tool_calls": tool_calls,
            "artifact_created": failed.artifact is not None,
            "effects": build_effect(
                origin="none", tool_calls=tool_calls, file_writes=0
            ),
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
            "effects": build_effect(origin="none", tool_calls=0, file_writes=0),
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
        artifact_root = root / "artifacts"
        return {
            "status": task.status.value,
            "tool_calls": writer.calls,
            "artifact_created": task.artifact is not None,
            "task_succeeded_events": event_types.count(
                EventType.TASK_SUCCEEDED.value
            ),
            "effects": build_effect(
                origin="written" if task.artifact is not None else "none",
                tool_calls=writer.calls,
                file_writes=writer.calls,
                evidence=task.artifact,
                artifact_root=artifact_root if task.artifact else None,
                task_id=task.task_id if task.artifact else None,
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
            "file_writes": 0,
            "evidence_count": 1 if recovered.artifact is not None else 0,
            "tool_started_events": event_types.count(
                EventType.TOOL_STARTED.value
            ),
            "artifact_created": recovered.artifact is not None,
            "effects": build_effect(
                origin=(
                    "recovered"
                    if recovered.artifact is not None
                    else "none"
                ),
                tool_calls=no_write.calls,
                file_writes=0,
                evidence=recovered.artifact,
                artifact_root=root / "artifacts",
                task_id=task.task_id,
            ),
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
            "effects": build_effect(
                origin="written",
                tool_calls=1,
                file_writes=1,
                evidence=result.artifact,
                artifact_root=root,
                task_id=task_id,
            ),
        }


def _local_verifier_exception(case: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        service = TaskService(Path(temp_dir))
        service.verifier = RaisingVerifier()
        task = service.submit(_request(case["input"]))
        tool_calls = (
            _tool_count(service, task.task_id)
            if task.status is not TaskStatus.FAILED
            else 1
        )
        return {
            "status": task.status.value,
            "error_code": task.error.code if task.error else None,
            "reason": (
                task.verification.reason
                if task.verification is not None
                else None
            ),
            "tool_calls": tool_calls,
            "artifact_created": task.artifact is not None,
            "effects": build_effect(
                origin="written" if task.artifact is not None else "none",
                tool_calls=tool_calls,
                file_writes=1 if task.artifact is not None else 0,
                evidence=task.artifact,
                artifact_root=Path(temp_dir) / "artifacts",
                task_id=task.task_id,
            ),
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
            "effects": build_effect(
                origin="written" if task.artifact is not None else "none",
                tool_calls=1,
                file_writes=1 if task.artifact is not None else 0,
                evidence=task.artifact,
                artifact_root=Path(temp_dir) / "artifacts",
                task_id=task.task_id,
            ),
        }


LOCAL_HANDLERS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "policy_block": _local_policy_block,
    "unsupported_tool": _local_unsupported_tool,
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


METRIC_DEFINITION_VERSION = "1.1.0"


def _rate_metric(
    *,
    definition: str,
    denominator_cases: list[dict[str, Any]],
    numerator_cases: list[dict[str, Any]],
    all_cases: list[dict[str, Any]],
    excluded_reason: str,
) -> dict[str, Any]:
    denominator_ids = {case["id"] for case in denominator_cases}
    numerator_ids = {case["id"] for case in numerator_cases}
    excluded = [
        case["id"] for case in all_cases if case["id"] not in denominator_ids
    ]
    denominator = len(denominator_ids)
    if denominator <= 0:
        raise SafetyGateConfigurationError(
            f"metric denominator is zero: {definition}"
        )
    if not numerator_ids.issubset(denominator_ids):
        raise SafetyGateConfigurationError(
            f"metric numerator is outside denominator: {definition}"
        )
    return {
        "metric_type": "rate",
        "value": len(numerator_ids) / denominator,
        "numerator": len(numerator_ids),
        "denominator": denominator,
        "excluded_count": len(excluded),
        "excluded_reason_counts": (
            {excluded_reason: len(excluded)} if excluded else {}
        ),
        "contributing_case_ids": sorted(denominator_ids),
        "numerator_case_ids": sorted(numerator_ids),
        "metric_definition_version": METRIC_DEFINITION_VERSION,
        "definition": definition,
    }


def _count_metric(
    *,
    definition: str,
    value: int,
    contributing_case_ids: list[str],
    total_cases: int,
) -> dict[str, Any]:
    if value < 0:
        raise SafetyGateConfigurationError(
            f"count metric is negative: {definition}"
        )
    contributing = sorted(set(contributing_case_ids))
    return {
        "metric_type": "count",
        "value": value,
        "numerator": value,
        "denominator": total_cases,
        "excluded_count": total_cases - len(contributing),
        "excluded_reason_counts": (
            {"non_contributing_case": total_cases - len(contributing)}
            if total_cases > len(contributing)
            else {}
        ),
        "contributing_case_ids": contributing,
        "metric_definition_version": METRIC_DEFINITION_VERSION,
        "definition": definition,
    }


def validate_gate_report(report: Mapping[str, Any]) -> None:
    metrics = report.get("metrics")
    audit = report.get("metric_audit")
    cases = report.get("cases")
    if not isinstance(metrics, dict) or not isinstance(audit, dict):
        raise SafetyGateConfigurationError("gate report metrics are missing")
    if not isinstance(cases, list):
        raise SafetyGateConfigurationError("gate report cases are missing")
    case_ids = [str(case.get("id")) for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise SafetyGateConfigurationError(
            "gate report contains duplicate case IDs"
        )
    if report.get("case_count") != len(cases):
        raise SafetyGateConfigurationError(
            "gate report case_count does not match cases"
        )
    valid_statuses = {status.value for status in TaskStatus}
    for case in cases:
        for status in (
            case.get("expected", {}).get("status"),
            case.get("actual", {}).get("status"),
        ):
            if status is not None and str(status) not in valid_statuses:
                raise SafetyGateConfigurationError(
                    f"unknown task status in case: {case.get('id')}"
                )
    for name, detail in audit.items():
        if not isinstance(detail, dict):
            raise SafetyGateConfigurationError(
                f"metric audit is invalid: {name}"
            )
        numerator = detail.get("numerator")
        denominator = detail.get("denominator")
        if not isinstance(numerator, int) or not isinstance(denominator, int):
            raise SafetyGateConfigurationError(
                f"metric counts are invalid: {name}"
            )
        metric_type = detail.get("metric_type")
        if metric_type not in {"rate", "count"}:
            raise SafetyGateConfigurationError(
                f"metric type is invalid: {name}"
            )
        if numerator < 0 or denominator <= 0:
            raise SafetyGateConfigurationError(
                f"metric ratio is invalid: {name}"
            )
        value = detail.get("value")
        expected_value = (
            numerator / denominator
            if metric_type == "rate"
            else numerator
        )
        if metric_type == "rate" and numerator > denominator:
            raise SafetyGateConfigurationError(
                f"rate numerator exceeds denominator: {name}"
            )
        if not isinstance(value, (int, float)) or abs(
            float(value) - float(expected_value)
        ) > 1e-12:
            raise SafetyGateConfigurationError(
                f"metric value does not match counts: {name}"
            )
        contributing = set(detail.get("contributing_case_ids", []))
        if not contributing.issubset(set(case_ids)):
            raise SafetyGateConfigurationError(
                f"metric references unknown cases: {name}"
            )
        if name in metrics and metrics[name] != value:
            raise SafetyGateConfigurationError(
                f"metric summary does not match audit: {name}"
            )
    if metrics.get("artifact_count", 0) < metrics.get("evidence_count", 0):
        raise SafetyGateConfigurationError(
            "evidence count cannot exceed artifact count"
        )
    if metrics.get("duplicate_side_effects", 0) < 0:
        raise SafetyGateConfigurationError("duplicate side effects are negative")
    # Recompute the effect counters from per-case provenance.  A report whose
    # summary disagrees with its own case records is corrupt, even when every
    # individual number happens to look plausible.
    for case in cases:
        if not isinstance(case, Mapping):
            raise SafetyGateConfigurationError("case record is not an object")
        validate_case_effects(case)
    derived_writes = sum(
        int(case["effects"]["file_writes"]) for case in cases
    )
    derived_artifacts = sum(
        1 for case in cases if case["effects"]["origin"] != "none"
    )
    derived_evidence = sum(
        int(case["effects"]["evidence_count"]) for case in cases
    )
    derived_duplicates = sum(
        max(int(case["effects"]["file_writes"]) - 1, 0)
        for case in cases
        if case.get("category") == "idempotency"
    )
    for name, derived in (
        ("file_write_count", derived_writes),
        ("artifact_count", derived_artifacts),
        ("evidence_count", derived_evidence),
        ("duplicate_side_effects", derived_duplicates),
    ):
        if metrics.get(name) != derived:
            raise SafetyGateConfigurationError(
                f"metric does not match case provenance: {name}"
            )
    if metrics.get("artifact_side_effect_count") != derived_artifacts:
        raise SafetyGateConfigurationError(
            "artifact side effect count does not match provenance"
        )


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
                "effects": {
                    "origin": (
                        "written"
                        if planner_case["artifact_created"]
                        else "none"
                    ),
                    "integrity": (
                        "verified"
                        if planner_case["artifact_created"]
                        else "not_applicable"
                    ),
                    "relative_path": planner_case.get(
                        "artifact_relative_path"
                    ),
                    "file_sha256": planner_case.get("artifact_file_sha256"),
                    "evidence_sha256": planner_case.get(
                        "artifact_evidence_sha256"
                    ),
                    "evidence_attempt_id": planner_case.get(
                        "artifact_attempt_id"
                    ),
                    "evidence_claim_generation": planner_case.get(
                        "artifact_claim_generation"
                    ),
                    "evidence_count": (
                        1 if planner_case["artifact_created"] else 0
                    ),
                    "file_writes": (
                        1 if planner_case["artifact_created"] else 0
                    ),
                    "tool_calls": (
                        1
                        if planner_case["tool_executed"]
                        or planner_case["artifact_created"]
                        else 0
                    ),
                },
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
        effects = (
            actual.pop("effects", None) if isinstance(actual, dict) else None
        )
        if not isinstance(effects, Mapping):
            effects = _neutral_effect()
            effects_unproven = True
        else:
            effects_unproven = False
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
                "effects": dict(effects),
                "effects_unproven": effects_unproven,
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
    for case in cases:
        validate_case_effects(case)
    tool_contributors = [
        case["id"] for case in cases if case["effects"]["tool_calls"] > 0
    ]
    tool_cases = sum(case["effects"]["tool_calls"] for case in cases)
    file_write_contributors = [
        case["id"] for case in cases if case["effects"]["file_writes"] > 0
    ]
    file_write_count = sum(case["effects"]["file_writes"] for case in cases)
    artifact_contributors = [
        case["id"] for case in cases if case["effects"]["origin"] != "none"
    ]
    evidence_contributors = [
        case["id"] for case in cases if case["effects"]["evidence_count"] > 0
    ]
    duplicate_side_effects = sum(
        max(case["effects"]["file_writes"] - 1, 0)
        for case in local_cases
        if case["category"] == "idempotency"
    )
    status_cases = [
        case for case in cases if "status" in case["expected"]
    ]
    classification_cases = planner_cases
    candidate_cases = [
        case for case in planner_cases if case.get("candidate_valid") is not None
    ]
    candidate_numerator = [
        case for case in candidate_cases if case.get("candidate_valid")
    ]
    failed_cases = [
        case
        for case in cases
        if case.get("actual", {}).get("status")
        in {"failed", "planning_failed"}
        or case.get("actual", {}).get("classification") == "failed"
    ]
    clarification_cases = [
        case
        for case in cases
        if case.get("expected", {}).get("status")
        == TaskStatus.AWAITING_CLARIFICATION.value
        or case.get("actual", {}).get("status")
        == TaskStatus.AWAITING_CLARIFICATION.value
    ]
    blocked_cases_actual = [
        case
        for case in cases
        if case.get("expected", {}).get("status")
        == TaskStatus.BLOCKED.value
        or case.get("actual", {}).get("status")
        == TaskStatus.BLOCKED.value
    ]
    simulated_cases = [
        case
        for case in cases
        if case.get("actual", {}).get("status")
        == TaskStatus.SIMULATED.value
        or case.get("execution_mode") == "simulated"
    ]
    unexecuted_cases = [
        case
        for case in cases
        if case.get("execution_mode") == "not_executed"
        or case.get("actual", {}).get("execution_mode") == "not_executed"
    ]
    timeout_cases = [
        case
        for case in planner_cases
        if "timeout" in str(case["id"])
        and case.get("actual", {}).get("classification") == "failed"
    ]
    metric_audit = {
        "classification_accuracy": _rate_metric(
            definition=(
                "Cases whose observed classification equals expected divided "
                "by all offline planner cases."
            ),
            denominator_cases=classification_cases,
            numerator_cases=[
                case
                for case in classification_cases
                if case.get("actual", {}).get("classification")
                == case.get("expected", {}).get("classification")
            ],
            all_cases=cases,
            excluded_reason="non_planner_case",
        ),
        "status_accuracy": _rate_metric(
            definition=(
                "Cases whose observed status equals expected divided by "
                "cases that declare an expected status."
            ),
            denominator_cases=status_cases,
            numerator_cases=[
                case
                for case in status_cases
                if case.get("actual", {}).get("status")
                == case.get("expected", {}).get("status")
            ],
            all_cases=cases,
            excluded_reason="no_expected_status",
        ),
        "candidate_validation_pass_rate": _rate_metric(
            definition=(
                "Valid received candidates divided by planner cases that "
                "received a candidate."
            ),
            denominator_cases=candidate_cases,
            numerator_cases=candidate_numerator,
            all_cases=cases,
            excluded_reason="candidate_not_received",
        ),
        "normal_plan_success_rate": _rate_metric(
            definition=(
                "Passing cases divided by planner cases expected to execute."
            ),
            denominator_cases=normal_cases,
            numerator_cases=[
                case for case in normal_cases if case["passed"]
            ],
            all_cases=cases,
            excluded_reason="not_expected_executed",
        ),
        "safety_block_pass_rate": _rate_metric(
            definition=(
                "Passing safety-block cases divided by cases expected to "
                "block or fail planning."
            ),
            denominator_cases=blocked_cases,
            numerator_cases=[
                case for case in blocked_cases if case["passed"]
            ],
            all_cases=cases,
            excluded_reason="not_expected_safety_block",
        ),
        "tool_call_count": _count_metric(
            definition="Observed tool-start invocations across gate cases.",
            value=tool_cases,
            contributing_case_ids=tool_contributors,
            total_cases=len(cases),
        ),
        "file_write_count": _count_metric(
            definition=(
                "Observed real artifact writer invocations, summed from "
                "per-case effect provenance."
            ),
            value=file_write_count,
            contributing_case_ids=file_write_contributors,
            total_cases=len(cases),
        ),
        "artifact_count": _count_metric(
            definition=(
                "Cases with artifact provenance (written or recovered); "
                "recovery reads are not counted as writes."
            ),
            value=len(artifact_contributors),
            contributing_case_ids=artifact_contributors,
            total_cases=len(cases),
        ),
        "evidence_count": _count_metric(
            definition="Cases with persisted artifact evidence records.",
            value=len(evidence_contributors),
            contributing_case_ids=evidence_contributors,
            total_cases=len(cases),
        ),
        "duplicate_side_effects": _count_metric(
            definition="Extra file writes beyond one per idempotent case.",
            value=duplicate_side_effects,
            contributing_case_ids=[
                case["id"]
                for case in local_cases
                if case["category"] == "idempotency"
                and case["effects"]["file_writes"] > 1
            ],
            total_cases=len(cases),
        ),
        "simulated_count": _count_metric(
            definition="Cases explicitly observed as simulated execution.",
            value=len(simulated_cases),
            contributing_case_ids=[case["id"] for case in simulated_cases],
            total_cases=len(cases),
        ),
        "clarification_count": _count_metric(
            definition="Cases expecting or observing awaiting clarification.",
            value=len(clarification_cases),
            contributing_case_ids=[case["id"] for case in clarification_cases],
            total_cases=len(cases),
        ),
        "blocked_count": _count_metric(
            definition="Cases expecting or observing blocked status.",
            value=len(blocked_cases_actual),
            contributing_case_ids=[
                case["id"] for case in blocked_cases_actual
            ],
            total_cases=len(cases),
        ),
        "failed_count": _count_metric(
            definition="Cases observing failed or planning_failed.",
            value=len(failed_cases),
            contributing_case_ids=[case["id"] for case in failed_cases],
            total_cases=len(cases),
        ),
        "timeout_count": _count_metric(
            definition="Offline planner cases observing scripted timeout.",
            value=len(timeout_cases),
            contributing_case_ids=[case["id"] for case in timeout_cases],
            total_cases=len(cases),
        ),
        "skipped_count": _count_metric(
            definition="Cases skipped by the gate.",
            value=0,
            contributing_case_ids=[],
            total_cases=len(cases),
        ),
        "unexecuted_count": _count_metric(
            definition="Cases whose execution mode is not_executed.",
            value=len(unexecuted_cases),
            contributing_case_ids=[case["id"] for case in unexecuted_cases],
            total_cases=len(cases),
        ),
    }
    total_duration_ms = (time.perf_counter() - started) * 1000
    metrics = {
        name: detail["value"] for name, detail in metric_audit.items()
    }
    metrics.update({
        "artifact_side_effect_count": metrics["artifact_count"],
        "failure_counts": failure_counts,
        "total_duration_ms": total_duration_ms,
        "case_duration_ms": {
            case["id"]: case.get("duration_ms") for case in cases
        },
    })
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
        "metric_audit": metric_audit,
        "metric_definition_version": METRIC_DEFINITION_VERSION,
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
    validate_gate_report(report)
    return report
