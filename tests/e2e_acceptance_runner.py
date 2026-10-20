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
        artifact = root / "artifacts" / task_id / "sigkill-proof.txt"
        scenarios.append(
            _scenario(
                "E2E-4",
                "SIGKILL after tool, restart and readback",
                TaskStatus.SUCCEEDED,
                recovered.status,
                artifact=artifact,
                evidence_complete=(
                    recovered.verification is not None
                    and recovered.verification.passed
                ),
                exit_code=0,
                failure_class=None,
                details={"writer_calls": no_write.calls},
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

    passed = sum(1 for item in scenarios if item["passed"])
    executable = [item for item in scenarios if item["id"] in {"E2E-1", "E2E-4", "E2E-7"}]
    recovery = [item for item in scenarios if item["id"] in {"E2E-4", "E2E-5"}]
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
                scenarios[-1]["input"]["writer_calls"] - 1,
            ),
            "cancellation_handled": scenarios[2]["passed"],
            "evidence_complete_rate": (
                sum(1 for item in scenarios if item["evidence_complete"])
                / len(scenarios)
            ),
        },
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
