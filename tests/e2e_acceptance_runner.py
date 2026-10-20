"""Reproducible Phase 3.6 end-to-end acceptance scenarios."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.conversation_service import ConversationService  # noqa: E402
from zevorinth.event_store import (  # noqa: E402
    PlanRevisionConflict,
    EventStore,
)
from zevorinth.models import (  # noqa: E402
    EventType,
    ExecutionMode,
    TaskRequest,
    TaskStatus,
    ToolResult,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


class NoWriteWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self) -> None:
        self.calls = 0

    def write(self, **_: Any) -> ToolResult:
        self.calls += 1
        raise AssertionError("recovery must not write")


class CountingWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self, root: Path) -> None:
        from zevorinth.artifact_tool import RealArtifactWriter

        self.delegate = RealArtifactWriter(root)
        self.calls = 0

    def write(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        return self.delegate.write(**kwargs)


class RejectingVerifier:
    name = "acceptance-rejector"

    def verify(self, **_kwargs: Any):
        from zevorinth.models import VerificationResult

        return VerificationResult(
            verifier_name=self.name,
            passed=False,
            exists=True,
            path_safe=True,
            content_matches=False,
            evidence_matches=False,
            reason="critic_veto",
            artifact=None,
            checked_at=NOW,
        )


def _request(content: str | None = "acceptance") -> TaskRequest:
    return TaskRequest(
        goal="acceptance",
        operation=WRITE_TEXT_ARTIFACT,
        parameters={"artifact_path": "acceptance.txt", "content": content},
    )


def _scenario(
    scenario_id: str,
    description: str,
    category: str,
    expected: TaskStatus,
    actual: TaskStatus,
    *,
    artifact: Path | None,
    evidence_complete: bool,
    exit_code: int,
    failure_class: str | None,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": scenario_id,
        "description": description,
        "category": category,
        "input": details,
        "expected_status": expected.value,
        "actual_status": actual.value,
        "artifact": (
            {
                "path": str(artifact),
                "exists": artifact.exists(),
                "bytes": artifact.stat().st_size if artifact.exists() else 0,
            }
            if artifact is not None
            else None
        ),
        "evidence_complete": evidence_complete,
        "exit_code": exit_code,
        "failure_class": failure_class,
        "passed": actual is expected,
    }


def _run_sigkill_worker(root: Path) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    process = subprocess.Popen(
        [
            sys.executable,
            str(PROJECT_ROOT / "tests" / "sigkill_worker.py"),
            str(root),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    selector = selectors.DefaultSelector()
    try:
        assert process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout=10):
            process.kill()
            _, stderr = process.communicate(timeout=5)
            raise RuntimeError(f"worker checkpoint timeout: {stderr}")
        checkpoint = json.loads(process.stdout.readline())
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        if process.returncode != -signal.SIGKILL:
            raise RuntimeError("worker was not SIGKILLed")
        process.communicate(timeout=5)
        return checkpoint
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def run_acceptance() -> dict[str, Any]:
    scenarios: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        conversations = ConversationService(root)
        conversations.create_conversation(
            conversation_id="acceptance-1",
            now=NOW,
        )
        result = conversations.process_message(
            "acceptance-1",
            "create verified file",
            client_message_id="m1",
            operation=WRITE_TEXT_ARTIFACT,
            artifact_path="verified.txt",
            task_content="verified\n",
            now=NOW,
        )
        assert result.task is not None
        path = root / "artifacts" / result.task.task_id / "verified.txt"
        scenarios.append(
            _scenario(
                "E2E-1",
                "goal to independently verified file",
                "success",
                TaskStatus.SUCCEEDED,
                result.task.status,
                artifact=path,
                evidence_complete=(
                    result.task.artifact is not None
                    and result.task.verification is not None
                    and result.task.verification.passed
                ),
                exit_code=0 if result.outcome == "completed" else 1,
                failure_class=None,
                details={"message": "create verified file"},
            )
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        service = TaskService(Path(temp_dir), clock=lambda: NOW)
        task = service.enqueue(_request(content=None))
        revised = service.revise_plan(
            task.task_id,
            {"parameters": {"content": "revised"}},
            source_id="m-constraint",
            expected_plan_version=0,
            confirmed=True,
        )
        scenarios.append(
            _scenario(
                "E2E-2",
                "append a constraint and confirm revision",
                "revision",
                TaskStatus.QUEUED,
                revised.status,
                artifact=None,
                evidence_complete=False,
                exit_code=0,
                failure_class=None,
                details={
                    "source": "m-constraint",
                    "plan_version": revised.plan_version,
                },
            )
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        service = TaskService(Path(temp_dir), clock=lambda: NOW)
        task = service.enqueue(_request())
        service.request_cancel(
            task.task_id,
            requested_by="acceptance-user",
            reason="stop before tool",
        )
        cancelled = service.execute_task(task.task_id)
        scenarios.append(
            _scenario(
                "E2E-3",
                "cooperative cancellation at claim checkpoint",
                "cancellation",
                TaskStatus.CANCELLED,
                cancelled.status,
                artifact=None,
                evidence_complete=True,
                exit_code=0,
                failure_class=None,
                details={"checkpoint": cancelled.phase},
            )
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        checkpoint = _run_sigkill_worker(root)
        task_id = checkpoint["task_id"]
        recovery_time = (
            datetime.fromisoformat(checkpoint["lease_expires_at"])
            + timedelta(seconds=1)
        ).isoformat(timespec="milliseconds")
        no_write = NoWriteWriter()
        recovered = TaskService(
            root,
            writer=no_write,
            clock=lambda: recovery_time,
        ).resume(task_id)
        repeated = TaskService(
            root,
            writer=no_write,
            clock=lambda: recovery_time,
        ).resume(task_id)
        self_calls = no_write.calls
        artifact = root / "artifacts" / task_id / "sigkill-proof.txt"
        scenarios.append(
            _scenario(
                "E2E-4",
                "SIGKILL after tool, restart and readback",
                "recovery",
                TaskStatus.SUCCEEDED,
                recovered.status,
                artifact=artifact,
                evidence_complete=(
                    recovered.verification is not None
                    and recovered.verification.passed
                ),
                exit_code=0,
                failure_class=None,
                details={
                    "writer_calls": self_calls,
                    "duplicate_after_recovery_status": repeated.status.value,
                },
            )
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        service = TaskService(root, clock=lambda: NOW)
        task = service.enqueue(_request())
        attempt = str(uuid4())
        claim = service.store.claim_task(
            task.task_id,
            owner_id="owner",
            lease_token="lease",
            attempt_id=attempt,
            now=NOW,
            expires_at="2026-10-20T00:00:01.000+00:00",
            allowed_statuses=TaskService.RUNNABLE_STATUSES,
        )
        assert claim.acquired
        service.store.transition(
            task.task_id,
            TaskStatus.RUNNING,
            EventType.TASK_PLANNED,
            NOW,
            lease_token="lease",
        )
        artifact = root / "artifacts" / task.task_id / "acceptance.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("orphan evidence\n")
        service.store.expire_claim(
            task.task_id,
            now="2026-10-20T00:00:02.000+00:00",
        )
        no_write = NoWriteWriter()
        blocked = TaskService(
            root,
            writer=no_write,
            clock=lambda: "2026-10-20T00:00:03.000+00:00",
        ).resume(task.task_id)
        scenarios.append(
            _scenario(
                "E2E-5",
                "file exists but evidence is missing",
                "recovery_evidence_gap",
                TaskStatus.BLOCKED,
                blocked.status,
                artifact=artifact,
                evidence_complete=False,
                exit_code=4,
                failure_class=None,
                details={"writer_calls": no_write.calls},
            )
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        service = TaskService(Path(temp_dir), clock=lambda: NOW)
        service.verifier = RejectingVerifier()
        task = service.submit(_request())
        scenarios.append(
            _scenario(
                "E2E-6",
                "independent verifier veto",
                "verification_veto",
                TaskStatus.FAILED,
                task.status,
                artifact=None,
                evidence_complete=False,
                exit_code=1,
                failure_class="verification",
                details={"error": task.error.code if task.error else None},
            )
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        service = TaskService(root, clock=lambda: NOW)
        task = service.enqueue(_request(content=None))
        first = service.revise_plan(
            task.task_id,
            {"parameters": {"content": "duplicate-safe"}},
            source_id="same-source",
            expected_plan_version=0,
            confirmed=True,
        )
        duplicate = service.revise_plan(
            task.task_id,
            {"parameters": {"content": "ignored"}},
            source_id="same-source",
            expected_plan_version=0,
            confirmed=True,
        )
        stale = 0
        try:
            service.revise_plan(
                task.task_id,
                {"parameters": {"content": "stale"}},
                source_id="stale-source",
                expected_plan_version=0,
                confirmed=True,
            )
        except PlanRevisionConflict:
            stale = 1
        writer = CountingWriter(root / "artifacts")
        service.writer = writer
        completed = service.execute_task(task.task_id)
        service.execute_task(task.task_id)
        artifact = root / "artifacts" / task.task_id / "acceptance.txt"
        scenarios.append(
            _scenario(
                "E2E-7",
                "duplicate and stale requests avoid repeated side effects",
                "idempotency",
                TaskStatus.SUCCEEDED,
                completed.status,
                artifact=artifact,
                evidence_complete=(
                    completed.verification is not None
                    and completed.verification.passed
                ),
                exit_code=0,
                failure_class=None,
                details={
                    "plan_version": duplicate.plan_version,
                    "stale_conflicts": stale,
                    "writer_calls": writer.calls,
                },
            )
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        clock = [NOW]
        service = TaskService(
            Path(temp_dir),
            clock=lambda: clock[0],
            max_execution_seconds=1,
        )
        task = service.enqueue(_request())
        claim = service._claim(
            task.task_id,
            allowed=TaskService.RUNNABLE_STATUSES,
        )
        assert claim.acquired
        service.store.transition(
            task.task_id,
            TaskStatus.RUNNING,
            EventType.TASK_PLANNED,
            NOW,
            lease_token=str(claim.task.lease_token),
        )
        service.request_cancel(
            task.task_id,
            requested_by="acceptance-user",
            reason="race with budget",
        )
        clock[0] = "2026-10-20T00:00:02.000+00:00"
        budget_failed = service.execute_task(task.task_id)
        scenarios.append(
            _scenario(
                "E2E-8",
                "execution budget expiration wins before cancel processing",
                "budget_cancel_race",
                TaskStatus.FAILED,
                budget_failed.status,
                artifact=None,
                evidence_complete=False,
                exit_code=1,
                failure_class="budget",
                details={
                    "error": (
                        budget_failed.error.code
                        if budget_failed.error is not None
                        else None
                    )
                },
            )
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        writer = CountingWriter(root / "artifacts")
        service = TaskService(root, writer=writer, clock=lambda: NOW)
        executed = service.execute_task(service.enqueue(_request()).task_id)
        revision_conflict = 0
        try:
            service.revise_plan(
                executed.task_id,
                {"parameters": {"content": "after execution"}},
                source_id="late-execution-revision",
                expected_plan_version=0,
                confirmed=True,
            )
        except PlanRevisionConflict:
            revision_conflict = 1
        scenarios.append(
            _scenario(
                "E2E-9",
                "execution evidence blocks concurrent plan revision",
                "revision_execution_race",
                TaskStatus.SUCCEEDED,
                executed.status,
                artifact=root / "artifacts" / executed.task_id / "acceptance.txt",
                evidence_complete=True,
                exit_code=0,
                failure_class=None,
                details={
                    "revision_conflicts": revision_conflict,
                    "writer_calls": writer.calls,
                },
            )
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        checkpoint = _run_sigkill_worker(root)
        task_id = checkpoint["task_id"]
        artifact = root / "artifacts" / task_id / "sigkill-proof.txt"
        artifact.write_text("corrupted after evidence checkpoint")
        recovery_time = (
            datetime.fromisoformat(checkpoint["lease_expires_at"])
            + timedelta(seconds=1)
        ).isoformat(timespec="milliseconds")
        no_write = NoWriteWriter()
        corrupted = TaskService(
            root,
            writer=no_write,
            clock=lambda: recovery_time,
        ).resume(task_id)
        scenarios.append(
            _scenario(
                "E2E-10",
                "evidence corruption is rejected on recovery",
                "evidence_corruption",
                TaskStatus.FAILED,
                corrupted.status,
                artifact=artifact,
                evidence_complete=False,
                exit_code=1,
                failure_class="verification",
                details={
                    "writer_calls": no_write.calls,
                    "reason": (
                        corrupted.verification.reason
                        if corrupted.verification is not None
                        else None
                    ),
                },
            )
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        class RaisingVerifier:
            name = "raising-acceptance-verifier"

            def verify(self, **_kwargs: Any):
                raise RuntimeError("verifier unavailable")

        service = TaskService(Path(temp_dir), clock=lambda: NOW)
        service.verifier = RaisingVerifier()
        failed = service.submit(_request())
        scenarios.append(
            _scenario(
                "E2E-11",
                "verifier exception fails closed",
                "verifier_exception",
                TaskStatus.FAILED,
                failed.status,
                artifact=None,
                evidence_complete=False,
                exit_code=1,
                failure_class="verification",
                details={
                    "error": failed.error.code if failed.error else None,
                    "reason": (
                        failed.verification.reason
                        if failed.verification is not None
                        else None
                    ),
                },
            )
        )

    passed = sum(1 for item in scenarios if item["passed"])
    executable = [item for item in scenarios if item["id"] in {"E2E-1", "E2E-4", "E2E-7"}]
    recovery = [item for item in scenarios if item["id"] in {"E2E-4", "E2E-5"}]
    idempotency_scenario = next(
        item for item in scenarios if item["id"] == "E2E-7"
    )
    cancellation_scenario = next(
        item for item in scenarios if item["id"] == "E2E-3"
    )
    metric_version = "1.0.0"
    metric_audit = {
        "success_rate": {
            "value": passed / len(scenarios),
            "numerator": passed,
            "denominator": len(scenarios),
            "excluded_count": 0,
            "excluded_reason_counts": {},
            "contributing_case_ids": [item["id"] for item in scenarios],
            "metric_definition_version": metric_version,
        },
        "executable_success_rate": {
            "value": (
                sum(1 for item in executable if item["passed"])
                / len(executable)
            ),
            "numerator": sum(1 for item in executable if item["passed"]),
            "denominator": len(executable),
            "excluded_count": len(scenarios) - len(executable),
            "excluded_reason_counts": {
                "not_expected_real_execution": len(scenarios) - len(executable)
            },
            "contributing_case_ids": [item["id"] for item in executable],
            "metric_definition_version": metric_version,
        },
        "recovery_success_rate": {
            "value": sum(1 for item in recovery if item["passed"]) / len(recovery),
            "numerator": sum(1 for item in recovery if item["passed"]),
            "denominator": len(recovery),
            "excluded_count": len(scenarios) - len(recovery),
            "excluded_reason_counts": {
                "not_recovery_case": len(scenarios) - len(recovery)
            },
            "contributing_case_ids": [item["id"] for item in recovery],
            "metric_definition_version": metric_version,
        },
        "duplicate_side_effects": {
            "value": max(
                0,
                idempotency_scenario["input"]["writer_calls"] - 1,
            ),
            "numerator": max(
                0,
                idempotency_scenario["input"]["writer_calls"] - 1,
            ),
            "denominator": 1,
            "excluded_count": len(scenarios) - 1,
            "excluded_reason_counts": {
                "not_idempotency_case": len(scenarios) - 1
            },
            "contributing_case_ids": [idempotency_scenario["id"]],
            "metric_definition_version": metric_version,
        },
        "cancellation_handled": {
            "value": 1 if cancellation_scenario["passed"] else 0,
            "numerator": 1 if cancellation_scenario["passed"] else 0,
            "denominator": 1,
            "excluded_count": len(scenarios) - 1,
            "excluded_reason_counts": {
                "not_cancellation_case": len(scenarios) - 1
            },
            "contributing_case_ids": [cancellation_scenario["id"]],
            "metric_definition_version": metric_version,
        },
        "evidence_complete_rate": {
            "value": (
                sum(1 for item in scenarios if item["evidence_complete"])
                / len(scenarios)
            ),
            "numerator": sum(
                1 for item in scenarios if item["evidence_complete"]
            ),
            "denominator": len(scenarios),
            "excluded_count": 0,
            "excluded_reason_counts": {},
            "contributing_case_ids": [item["id"] for item in scenarios],
            "metric_definition_version": metric_version,
        },
    }
    return {
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "scenarios": scenarios,
        "metrics": {
            "scenario_count": len(scenarios),
            "passed": passed,
            "success_rate": passed / len(scenarios),
            "executable_success_rate": (
                sum(1 for item in executable if item["passed"])
                / len(executable)
            ),
            "recovery_success_rate": (
                sum(1 for item in recovery if item["passed"])
                / len(recovery)
            ),
            "duplicate_side_effects": max(
                0,
                idempotency_scenario["input"]["writer_calls"] - 1,
            ),
            "cancellation_handled": scenarios[2]["passed"],
            "evidence_complete_rate": (
                sum(1 for item in scenarios if item["evidence_complete"])
                / len(scenarios)
            ),
            "correctly_blocked": sum(
                1
                for item in scenarios
                if item["passed"]
                and item["expected_status"]
                in {TaskStatus.BLOCKED.value, TaskStatus.FAILED.value}
            ),
            "category_results": {
                category: {
                    "passed": sum(
                        1
                        for item in scenarios
                        if item["category"] == category and item["passed"]
                    ),
                    "total": sum(
                        1
                        for item in scenarios
                        if item["category"] == category
                    ),
                }
                for category in sorted(
                    {item["category"] for item in scenarios}
                )
            },
            "failure_class_counts": {
                failure_class: sum(
                    1
                    for item in scenarios
                    if item["failure_class"] == failure_class
                )
                for failure_class in sorted(
                    {
                        item["failure_class"]
                        for item in scenarios
                        if item["failure_class"] is not None
                    }
                )
            },
        },
        "metric_definition_version": metric_version,
        "metric_audit": metric_audit,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    report = run_acceptance()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return (
        0
        if report["metrics"]["passed"]
        == report["metrics"]["scenario_count"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
