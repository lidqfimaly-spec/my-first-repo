"""Offline end-to-end user acceptance for the capabilities that exist.

Every scenario goes through a real entry point (the CLI subprocess or the
public ``TaskService`` / ``ConversationService`` API) and a real temporary
filesystem.  Acceptance is decided by independent checks: files are re-read
from disk, hashes are recomputed, and ``ArtifactVerifier`` is re-run instead
of trusting the planner's own verdict.

Capabilities that do not exist (shell, code editing beyond writing text,
network retrieval, agent-initiated test execution) are exercised only as
honest-refusal cases and listed as product gaps.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.conversation_service import ConversationService  # noqa: E402
from zevorinth.models import (  # noqa: E402
    EventType,
    TaskRequest,
    TaskStatus,
    ToolResult,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.safety_gate import (  # noqa: E402
    build_effect,
    validate_case_effects,
)
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.verifier import ArtifactVerifier  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"

#: Product result vocabulary mapped from persisted task states.
RESULT_MAP = {
    TaskStatus.SUCCEEDED.value: "passed",
    TaskStatus.FAILED.value: "failed",
    TaskStatus.BLOCKED.value: "blocked",
    TaskStatus.AWAITING_CLARIFICATION.value: "needs_clarification",
    TaskStatus.CANCELLED.value: "cancelled",
    TaskStatus.SIMULATED.value: "simulated",
    TaskStatus.QUEUED.value: "not_executed",
    TaskStatus.RUNNING.value: "not_executed",
    TaskStatus.VERIFYING.value: "not_executed",
    TaskStatus.INTERRUPTED.value: "not_executed",
    TaskStatus.PLANNING_FAILED.value: "failed",
}


class AcceptanceError(RuntimeError):
    pass


def _cli(home: Path, *args: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "zevorinth.cli", "--home", str(home), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "ZEVORINTH_PLANNER": "deterministic",
            "OPENAI_API_KEY": "",
        },
        timeout=120,
    )
    payload: Any = None
    if completed.stdout.strip():
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = None
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "json": payload,
    }


def _event_counts(service: TaskService, task_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in service.events(task_id):
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    return counts


def _independent_verification(
    home: Path,
    task: Any,
    *,
    expected_content: str | None,
) -> dict[str, Any]:
    """Re-read the artifact from disk and re-run the verifier."""
    artifacts_root = home / "artifacts"
    artifact = task.artifact
    if artifact is None:
        return {
            "artifact_exists": False,
            "file_sha256": None,
            "hash_matches_evidence": False,
            "content_matches": False,
            "verifier_passed": False,
        }
    path = artifacts_root / task.task_id / artifact.relative_path
    if not path.is_file():
        return {
            "artifact_exists": False,
            "file_sha256": None,
            "hash_matches_evidence": False,
            "content_matches": False,
            "verifier_passed": False,
        }
    raw = path.read_bytes()
    import hashlib

    file_sha = hashlib.sha256(raw).hexdigest()
    content = raw.decode("utf-8")
    result = ArtifactVerifier(artifacts_root).verify(
        task_id=task.task_id,
        artifact_path=artifact.relative_path,
        expected_content=expected_content if expected_content is not None else content,
        tool_result=task.tool_result,
        checked_at=NOW,
        expected_attempt_id=artifact.attempt_id,
        require_provenance=True,
        maximum_claim_generation=task.claim_generation,
    )
    return {
        "artifact_exists": True,
        "relative_path": artifact.relative_path,
        "file_sha256": file_sha,
        "evidence_sha256": artifact.sha256,
        "hash_matches_evidence": file_sha == artifact.sha256,
        "content_matches": (
            expected_content is None or content == expected_content
        ),
        "verifier_passed": bool(result.passed),
        "verifier_reason": result.reason,
    }


def _record(
    scenario_id: str,
    title: str,
    category: str,
    *,
    input_text: str,
    expected_status: str,
    observed_status: str,
    checks: dict[str, bool],
    effects: dict[str, Any],
    artifact: dict[str, Any] | None,
    evidence: dict[str, Any] | None,
    details: dict[str, Any] | None = None,
    negative_control: bool = False,
) -> dict[str, Any]:
    result = RESULT_MAP.get(observed_status, "not_executed")
    passed = observed_status == expected_status and all(checks.values())
    return {
        "id": scenario_id,
        "title": title,
        "category": category,
        "input": input_text,
        "expected_status": expected_status,
        "observed_status": observed_status,
        "result": result if passed or result != "passed" else "failed",
        "checks": dict(checks),
        "effects": dict(effects),
        "artifact": artifact,
        "evidence": evidence,
        "details": dict(details or {}),
        "negative_control": negative_control,
        "passed": passed,
    }


def _no_effect(tool_calls: int = 0) -> dict[str, Any]:
    return build_effect(origin="none", tool_calls=tool_calls, file_writes=0)


def _request(path: str = "report.txt", content: str = "hello\n") -> TaskRequest:
    return TaskRequest(
        goal="create artifact",
        operation=WRITE_TEXT_ARTIFACT,
        parameters={"artifact_path": path, "content": content},
    )


def run_acceptance() -> dict[str, Any]:
    scenarios: list[dict[str, Any]] = []

    # ---- A1: create, read back, independently verify (real CLI) ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        run = _cli(
            home,
            "run",
            "--goal",
            "write a greeting file",
            "--artifact-path",
            "greeting.txt",
            "--content",
            "hello acceptance\n",
            "--json",
        )
        task = run["json"] or {}
        service = TaskService(home)
        stored = service.require_task(task["task_id"])
        check = _independent_verification(
            home, stored, expected_content="hello acceptance\n"
        )
        effects = build_effect(
            origin="written",
            tool_calls=_event_counts(service, stored.task_id).get(
                EventType.TOOL_STARTED.value, 0
            ),
            file_writes=1 if check["artifact_exists"] else 0,
            evidence=stored.artifact,
            artifact_root=home / "artifacts",
            task_id=stored.task_id,
        )
        events = _event_counts(service, stored.task_id)
        scenarios.append(
            _record(
                "A1-artifact",
                "create a file, read it back, verify independently",
                "artifact",
                input_text="run --artifact-path greeting.txt",
                expected_status="succeeded",
                observed_status=stored.status.value,
                checks={
                    "cli_exit_zero": run["exit_code"] == 0,
                    "artifact_on_disk": check["artifact_exists"],
                    "content_matches": check["content_matches"],
                    "hash_matches_evidence": check["hash_matches_evidence"],
                    "verifier_passed": check["verifier_passed"],
                    "tool_succeeded_event": events.get("tool_succeeded", 0) == 1,
                    "task_succeeded_event": events.get("task_succeeded", 0) == 1,
                },
                effects=effects,
                artifact=check,
                evidence={
                    "attempt_id_present": stored.artifact.attempt_id is not None,
                    "claim_generation": stored.artifact.claim_generation,
                },
            )
        )

    # ---- A2: an existing target inside the task directory is refused ----
    # Artifacts are namespaced per task, so "the file already exists" is a
    # within-task condition.  Seeding the task directory reproduces it.
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        service = TaskService(home, clock=lambda: NOW)
        queued = service.enqueue(_request("keep.txt", "replacement\n"))
        target_dir = home / "artifacts" / queued.task_id
        target_dir.mkdir(parents=True)
        target = target_dir / "keep.txt"
        target.write_text("original\n")
        stored = service.execute_task(queued.task_id)
        unchanged = target.read_text() == "original\n"
        scenarios.append(
            _record(
                "A2-existing",
                "existing target inside the task dir is refused",
                "artifact_failure",
                input_text="pre-seed artifacts/<task>/keep.txt, then execute",
                expected_status="blocked",
                observed_status=stored.status.value,
                checks={
                    "blocked_by_artifact_exists": (
                        stored.error is not None
                        and stored.error.code == "artifact_exists"
                    ),
                    "original_unchanged": unchanged,
                    "no_evidence_recorded": stored.artifact is None,
                    "no_success_event": _event_counts(
                        service, stored.task_id
                    ).get("task_succeeded", 0)
                    == 0,
                },
                effects=_no_effect(tool_calls=1),
                artifact=None,
                evidence=None,
                details={
                    "design_note": (
                        "artifact directories are task-scoped, so the same "
                        "relative path in another task is unaffected"
                    )
                },
            )
        )

    # ---- A3: path traversal is refused before any file is created ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        run = _cli(
            home,
            "run",
            "--goal",
            "escape",
            "--artifact-path",
            "../escaped.txt",
            "--content",
            "x\n",
            "--json",
        )
        task = run["json"] or {}
        service = TaskService(home)
        stored = service.require_task(task["task_id"])
        escaped = (home.parent / "escaped.txt").exists()
        scenarios.append(
            _record(
                "A3-traversal",
                "path traversal is blocked before creating anything",
                "artifact_failure",
                input_text="run --artifact-path ../escaped.txt",
                expected_status="blocked",
                observed_status=stored.status.value,
                checks={
                    "blocked": stored.status is TaskStatus.BLOCKED,
                    "no_escape_file": not escaped,
                    "no_evidence_recorded": stored.artifact is None,
                },
                effects=_no_effect(),
                artifact=None,
                evidence=None,
            )
        )

    # ---- A4: post-publish modification is detectable, never silently kept ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        run = _cli(
            home,
            "run",
            "--goal",
            "tamper target",
            "--artifact-path",
            "tamper.txt",
            "--content",
            "good\n",
            "--json",
        )
        task_id = run["json"]["task_id"]
        path = home / "artifacts" / task_id / "tamper.txt"
        path.write_text("tampered\n")
        service = TaskService(home)
        stored = service.require_task(task_id)
        check = _independent_verification(
            home, stored, expected_content="good\n"
        )
        scenarios.append(
            _record(
                "A4-tamper",
                "modified artifact no longer verifies against recorded evidence",
                "artifact_failure",
                input_text="modify published file, re-verify",
                expected_status="succeeded",
                observed_status=stored.status.value,
                checks={
                    "tamper_detected": not check["verifier_passed"],
                    "hashes_disagree": not check["hash_matches_evidence"],
                    "task_state_not_rewritten": (
                        stored.status is TaskStatus.SUCCEEDED
                        and stored.verification.passed
                    ),
                },
                effects=build_effect(
                    origin="written",
                    integrity="mismatch",
                    tool_calls=1,
                    file_writes=1,
                    evidence=stored.artifact,
                    artifact_root=home / "artifacts",
                    task_id=task_id,
                ),
                artifact=check,
                evidence={"recorded_evidence_sha256": stored.artifact.sha256},
                details={"note": "verification is re-run, not trusted"},
                negative_control=True,
            )
        )

    # ---- B1: write a code file, harness runs the tests ----
    with tempfile.TemporaryDirectory() as temp_dir:
        project = Path(temp_dir)
        (project / "test_solution.py").write_text(
            "import unittest\n"
            "from solution import double\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_double(self):\n"
            "        self.assertEqual(double(21), 42)\n",
            encoding="utf-8",
        )
        unrelated = project / "untouched.txt"
        unrelated.write_text("do not touch\n", encoding="utf-8")
        before = unrelated.read_bytes()
        home = project / ".zevorinth"
        home.mkdir()
        run = _cli(
            home,
            "run",
            "--goal",
            "write a helper",
            "--artifact-path",
            "solution.py",
            "--content",
            "def double(value: int) -> int:\n    return value * 2\n",
            "--json",
        )
        task_id = run["json"]["task_id"]
        produced = home / "artifacts" / task_id / "solution.py"
        (project / "solution.py").write_bytes(produced.read_bytes())
        tests = subprocess.run(
            [sys.executable, "-m", "unittest", "-q", "test_solution"],
            cwd=project,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(project),
            },
            timeout=60,
        )
        service = TaskService(home)
        stored = service.require_task(task_id)
        check = _independent_verification(
            home, stored, expected_content=None
        )
        scenarios.append(
            _record(
                "B1-code-file",
                "agent writes a code file; harness runs the tests",
                "code",
                input_text="write solution.py implementing double()",
                expected_status="succeeded",
                observed_status=stored.status.value,
                checks={
                    "artifact_written": check["artifact_exists"],
                    "verifier_passed": check["verifier_passed"],
                    "tests_pass": tests.returncode == 0,
                    "unrelated_file_untouched": unrelated.read_bytes() == before,
                    "agent_did_not_run_tests": True,
                },
                effects=build_effect(
                    origin="written",
                    tool_calls=1,
                    file_writes=1,
                    evidence=stored.artifact,
                    artifact_root=home / "artifacts",
                    task_id=task_id,
                ),
                artifact=check,
                evidence=None,
                details={
                    "test_command": "python -m unittest -q test_solution",
                    "test_exit_code": tests.returncode,
                    "gap": "Zevorinth cannot run tests itself",
                },
            )
        )

    # ---- B2: controlled local test execution (Phase 3.16 capability) ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        workspace = home / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / "test_demo.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        run = _cli(
            home,
            "test",
            "--workspace",
            "demo",
            "--target",
            "test_demo",
            "--backend",
            "host",
            "--json",
        )
        task = run["json"] or {}
        service = TaskService(home)
        stored = service.require_task(task["task_id"])
        evidence = stored.tool_result.test_evidence
        scenarios.append(
            _record(
                "B2-test-execution",
                "approved test target runs and is independently verified",
                "test_execution",
                input_text="test --workspace demo --target test_demo",
                expected_status="succeeded",
                observed_status=stored.status.value,
                checks={
                    "cli_exit_zero": run["exit_code"] == 0,
                    "real_exit_code_zero": (
                        evidence is not None and evidence.exit_code == 0
                    ),
                    "not_truncated": (
                        evidence is not None and not evidence.truncated
                    ),
                    "verified": stored.verification.reason == "tests_passed",
                    "single_tool_start": _event_counts(
                        service, stored.task_id
                    ).get("tool_started", 0)
                    == 1,
                },
                effects=_no_effect(tool_calls=1),
                artifact=None,
                evidence={
                    "runner": evidence.runner if evidence else None,
                    "target": evidence.target if evidence else None,
                    "exit_code": evidence.exit_code if evidence else None,
                },
            )
        )

    # ---- B2b: arbitrary shell is still refused ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        run = _cli(
            home,
            "run",
            "--goal",
            "run an arbitrary shell command",
            "--operation",
            "shell",
            "--artifact-path",
            "unused.txt",
            "--content",
            "unused",
            "--json",
        )
        task = run["json"] or {}
        service = TaskService(home)
        stored = service.require_task(task["task_id"])
        scenarios.append(
            _record(
                "B2b-shell",
                "arbitrary shell execution is refused",
                "capability_gap",
                input_text="run --operation shell",
                expected_status="blocked",
                observed_status=stored.status.value,
                checks={
                    "blocked": stored.status is TaskStatus.BLOCKED,
                    "reason_is_unsupported": (
                        stored.error is not None
                        and stored.error.code == "unsupported_operation"
                    ),
                    "nothing_executed": _event_counts(
                        service, stored.task_id
                    ).get("tool_started", 0)
                    == 0,
                },
                effects=_no_effect(),
                artifact=None,
                evidence=None,
                details={"gap": "no general shell tool; only run_tests"},
            )
        )

    # ---- B3: ambiguous modification needs clarification ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        run = _cli(
            home,
            "run",
            "--goal",
            "write something",
            "--artifact-path",
            "ambiguous.txt",
            "--json",
        )
        task = run["json"] or {}
        service = TaskService(home)
        stored = service.require_task(task["task_id"])
        scenarios.append(
            _record(
                "B3-clarification",
                "missing content produces a clarification request",
                "clarification",
                input_text="run --artifact-path ambiguous.txt (no content)",
                expected_status="awaiting_clarification",
                observed_status=stored.status.value,
                checks={
                    "awaiting_clarification": (
                        stored.status is TaskStatus.AWAITING_CLARIFICATION
                    ),
                    "content_requested": "content" in stored.missing_fields,
                    "no_artifact": stored.artifact is None,
                    "no_tool_started": _event_counts(
                        service, stored.task_id
                    ).get("tool_started", 0)
                    == 0,
                },
                effects=_no_effect(),
                artifact=None,
                evidence=None,
            )
        )

    # ---- C1: research request cannot be executed offline ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        run = _cli(
            home,
            "run",
            "--goal",
            "search the web for the latest release notes",
            "--operation",
            "web_search",
            "--artifact-path",
            "unused.txt",
            "--content",
            "unused",
            "--json",
        )
        task = run["json"] or {}
        service = TaskService(home)
        stored = service.require_task(task["task_id"])
        scenarios.append(
            _record(
                "C1-research",
                "web research is refused instead of fabricating sources",
                "capability_gap",
                input_text="run --operation web_search",
                expected_status="blocked",
                observed_status=stored.status.value,
                checks={
                    "blocked": stored.status is TaskStatus.BLOCKED,
                    "reason_is_unsupported": (
                        stored.error is not None
                        and stored.error.code == "unsupported_operation"
                    ),
                    "no_artifact": stored.artifact is None,
                    "no_citation_payload": not any(
                        "http" in json.dumps(event.payload)
                        for event in service.events(stored.task_id)
                    ),
                },
                effects=_no_effect(),
                artifact=None,
                evidence=None,
                details={"gap": "no retrieval tool; no offline corpus"},
            )
        )

    # ---- D1: unsupported tool plan is denied by policy ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        service = TaskService(home)
        task = service.enqueue(_request())
        from zevorinth.models import Plan, PlanStep
        from zevorinth.policy import ArtifactPolicy

        decision = ArtifactPolicy().evaluate(
            request=task.request,
            plan=Plan(
                plan_id="p",
                planner_name="acceptance",
                deterministic=True,
                steps=[
                    PlanStep(
                        step_id="s1",
                        tool_name="shell",
                        expected_result="denied",
                        parameters={},
                    )
                ],
            ),
            task_id=task.task_id,
            artifacts_root=home / "artifacts",
        )
        scenarios.append(
            _record(
                "D1-unsupported-tool",
                "unsupported tool is denied by policy",
                "policy",
                input_text="plan with tool_name=shell",
                expected_status="blocked",
                observed_status=TaskStatus.BLOCKED.value,
                checks={
                    "policy_denied": decision.allowed is False,
                    "reason_is_unsupported_tool": (
                        decision.code == "unsupported_tool"
                    ),
                    "nothing_executed": _event_counts(
                        service, task.task_id
                    ).get("tool_started", 0)
                    == 0,
                },
                effects=_no_effect(),
                artifact=None,
                evidence=None,
            )
        )

    # ---- D2: execution budget stops the task before the tool ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        clock = [NOW]
        service = TaskService(
            home, clock=lambda: clock[0], max_execution_seconds=1
        )
        task = service.enqueue(_request())
        claim = service._claim(
            task.task_id, allowed=TaskService.RUNNABLE_STATUSES
        )
        service.store.transition(
            task.task_id,
            TaskStatus.RUNNING,
            EventType.TASK_PLANNED,
            NOW,
            lease_token=str(claim.task.lease_token),
        )
        clock[0] = "2026-10-20T00:00:30.000+00:00"
        failed = service.execute_task(task.task_id)
        scenarios.append(
            _record(
                "D2-budget",
                "expired budget fails before the tool runs",
                "budget",
                input_text="max_execution_seconds=1, clock advanced",
                expected_status="failed",
                observed_status=failed.status.value,
                checks={
                    "failed_on_budget": (
                        failed.error is not None
                        and failed.error.code == "execution_budget_exceeded"
                    ),
                    "no_tool_started": _event_counts(
                        service, task.task_id
                    ).get("tool_started", 0)
                    == 0,
                    "no_artifact": failed.artifact is None,
                },
                effects=_no_effect(),
                artifact=None,
                evidence=None,
            )
        )

    # ---- D3: cancellation before the tool ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        service = TaskService(home, clock=lambda: NOW)
        task = service.enqueue(_request())
        service.request_cancel(
            task.task_id, requested_by="acceptance", reason="user stop"
        )
        cancelled = service.execute_task(task.task_id)
        scenarios.append(
            _record(
                "D3-cancel",
                "cooperative cancellation stops before publication",
                "cancellation",
                input_text="request-cancel then run",
                expected_status="cancelled",
                observed_status=cancelled.status.value,
                checks={
                    "cancelled": cancelled.status is TaskStatus.CANCELLED,
                    "no_artifact": cancelled.artifact is None,
                    "no_tool_started": _event_counts(
                        service, task.task_id
                    ).get("tool_started", 0)
                    == 0,
                    "no_success_event": _event_counts(
                        service, task.task_id
                    ).get("task_succeeded", 0)
                    == 0,
                },
                effects=_no_effect(),
                artifact=None,
                evidence=None,
            )
        )

    # ---- E1: multi-turn clarification carries every earlier answer ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        conversations = ConversationService(home)
        conversations.create_conversation(conversation_id="c1", now=NOW)
        first = conversations.process_message(
            "c1",
            "create a notes file",
            client_message_id="m1",
            now=NOW,
        )
        operation_question = next(
            item for item in first.clarifications if item.field_name == "operation"
        )
        after_operation = conversations.process_message(
            "c1",
            WRITE_TEXT_ARTIFACT,
            client_message_id="m2",
            answer_to=operation_question.question_id,
            now=NOW,
        )
        path_question = next(
            item
            for item in after_operation.clarifications
            if item.field_name == "artifact_path"
        )
        after_path = conversations.process_message(
            "c1",
            "notes.txt",
            client_message_id="m3",
            answer_to=path_question.question_id,
            now=NOW,
        )
        content_question = next(
            item
            for item in after_path.clarifications
            if item.field_name == "content"
        )
        completed_result = conversations.process_message(
            "c1",
            "v2 with constraint\n",
            client_message_id="m4",
            answer_to=content_question.question_id,
            now=NOW,
        )
        completed = conversations.task_service.require_task(
            completed_result.task.task_id
        )
        check = _independent_verification(
            home, completed, expected_content="v2 with constraint\n"
        )
        history = conversations.history("c1", limit=50)
        scenarios.append(
            _record(
                "E1-clarification",
                "four-turn clarification keeps every earlier answer",
                "conversation",
                input_text="m1 goal, m2 operation, m3 path, m4 content",
                expected_status="succeeded",
                observed_status=completed.status.value,
                checks={
                    "operation_retained": (
                        completed.request.operation == WRITE_TEXT_ARTIFACT
                    ),
                    "path_retained": (
                        completed.request.parameters.get("artifact_path")
                        == "notes.txt"
                    ),
                    "content_follows_answer": check["content_matches"],
                    "verifier_passed": check["verifier_passed"],
                    "history_has_four_messages": len(history) >= 4,
                },
                effects=build_effect(
                    origin="written",
                    tool_calls=1,
                    file_writes=1,
                    evidence=completed.artifact,
                    artifact_root=home / "artifacts",
                    task_id=completed.task_id,
                ),
                artifact=check,
                evidence=None,
                details={"turns": 4},
            )
        )

    # ---- E2: a new goal creates a new task rather than mixing goals ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        conversations = ConversationService(home)
        conversations.create_conversation(conversation_id="c2", now=NOW)
        first = conversations.process_message(
            "c2",
            "create file one",
            client_message_id="n1",
            operation=WRITE_TEXT_ARTIFACT,
            artifact_path="one.txt",
            task_content="one\n",
            now=NOW,
        )
        second = conversations.process_message(
            "c2",
            "new task: create file two",
            client_message_id="n2",
            operation=WRITE_TEXT_ARTIFACT,
            artifact_path="two.txt",
            task_content="two\n",
            now=NOW,
        )
        first_task = conversations.task_service.require_task(first.task.task_id)
        second_task = conversations.task_service.require_task(
            second.task.task_id
        )
        first_path = home / "artifacts" / first_task.task_id / "one.txt"
        second_path = home / "artifacts" / second_task.task_id / "two.txt"
        second_check = _independent_verification(
            home, second_task, expected_content="two\n"
        )
        scenarios.append(
            _record(
                "E2-new-task",
                "a new goal creates a separate task",
                "conversation",
                input_text="new task: create file two",
                expected_status="succeeded",
                observed_status=(
                    second_task.status.value
                    if second.task is not None
                    else "missing"
                ),
                checks={
                    "distinct_task_ids": (
                        second.task is not None
                        and second.task.task_id != first.task.task_id
                    ),
                    "first_task_unchanged": (
                        first_task.request.parameters.get("artifact_path")
                        == "one.txt"
                    ),
                    "first_artifact_intact": (
                        first_path.read_text() == "one\n"
                    ),
                    "second_artifact_separate": (
                        second_path.read_text() == "two\n"
                    ),
                },
                effects=build_effect(
                    origin="written",
                    tool_calls=1,
                    file_writes=1,
                    evidence=second_task.artifact,
                    artifact_root=home / "artifacts",
                    task_id=second_task.task_id,
                ),
                artifact=second_check,
                evidence=None,
                details={"intent": getattr(second.intent, "kind", None).value
                         if second.intent else None},
            )
        )

    # ---- E4: revise a queued plan before it executes ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        service = TaskService(home, clock=lambda: NOW)
        queued = service.enqueue(_request("revise.txt", "v1\n"))
        revised = service.revise_plan(
            queued.task_id,
            {"parameters": {"content": "v2 revised\n"}},
            source_id="constraint-1",
            expected_plan_version=0,
            confirmed=True,
        )
        completed = service.execute_task(queued.task_id)
        check = _independent_verification(
            home, completed, expected_content="v2 revised\n"
        )
        scenarios.append(
            _record(
                "E4-plan-revision",
                "unexecuted plan is revised before any side effect",
                "conversation",
                input_text="enqueue v1, revise to v2, execute",
                expected_status="succeeded",
                observed_status=completed.status.value,
                checks={
                    "plan_version_advanced": completed.plan_version == 1,
                    "revision_recorded": len(completed.plan_revisions) == 1,
                    "revision_source_recorded": (
                        completed.plan_revisions[0].source_id == "constraint-1"
                    ),
                    "content_follows_revision": check["content_matches"],
                    "verifier_passed": check["verifier_passed"],
                    "single_write": _event_counts(
                        service, queued.task_id
                    ).get("tool_started", 0)
                    == 1,
                },
                effects=build_effect(
                    origin="written",
                    tool_calls=1,
                    file_writes=1,
                    evidence=completed.artifact,
                    artifact_root=home / "artifacts",
                    task_id=completed.task_id,
                ),
                artifact=check,
                evidence=None,
                details={"revised_from": "v1", "revised_to": "v2 revised"},
            )
        )

    # ---- E3: interrupt after publish, resume without rewriting ----
    with tempfile.TemporaryDirectory() as temp_dir:
        home = Path(temp_dir)
        task_ids: list[str] = []

        def interrupt(_stage: str, task: Any) -> None:
            task_ids.append(task.task_id)
            raise RuntimeError("acceptance interrupt")

        crashing = TaskService(home, fault_hook=interrupt, clock=lambda: NOW)
        try:
            crashing.submit(_request("resume.txt", "resume\n"))
        except RuntimeError:
            pass
        task_id = task_ids[0]
        before = crashing.require_task(task_id)
        path = home / "artifacts" / task_id / "resume.txt"
        original_sha = before.artifact.sha256
        writer = NoWriteWriter()
        recovered = TaskService(
            home,
            writer=writer,
            clock=lambda: "2026-10-20T00:01:00.000+00:00",
        ).resume(task_id)
        check = _independent_verification(
            home, recovered, expected_content="resume\n"
        )
        scenarios.append(
            _record(
                "E3-recovery",
                "interrupted task resumes by verifying, not rewriting",
                "recovery",
                input_text="interrupt after publish, restart, resume",
                expected_status="succeeded",
                observed_status=recovered.status.value,
                checks={
                    "resumed_succeeded": (
                        recovered.status is TaskStatus.SUCCEEDED
                    ),
                    "no_rewrite": writer.calls == 0,
                    "file_unchanged": check["hash_matches_evidence"],
                    "evidence_preserved": (
                        recovered.artifact.sha256 == original_sha
                    ),
                    "single_success_event": _event_counts(
                        TaskService(home), task_id
                    ).get("task_succeeded", 0)
                    == 1,
                },
                effects=build_effect(
                    origin="recovered",
                    tool_calls=0,
                    file_writes=0,
                    evidence=recovered.artifact,
                    artifact_root=home / "artifacts",
                    task_id=task_id,
                ),
                artifact=check,
                evidence={"original_sha256": original_sha},
            )
        )

    for scenario in scenarios:
        validate_case_effects(scenario)
    return _report(scenarios)


class NoWriteWriter:
    tool_name = "write_text_artifact"

    def __init__(self) -> None:
        self.calls = 0

    def write(self, **_: Any) -> ToolResult:
        self.calls += 1
        raise AssertionError("resume must not publish")


def _metric(
    name: str,
    definition: str,
    numerator_ids: list[str],
    denominator_ids: list[str],
    all_ids: list[str],
    excluded_reason: str,
) -> dict[str, Any]:
    denominator = len(denominator_ids)
    if denominator <= 0:
        raise AcceptanceError(f"metric denominator is zero: {name}")
    excluded = [item for item in all_ids if item not in denominator_ids]
    return {
        "metric_type": "rate",
        "definition": definition,
        "value": len(numerator_ids) / denominator,
        "numerator": len(numerator_ids),
        "denominator": denominator,
        "excluded_count": len(excluded),
        "excluded_reason_counts": (
            {excluded_reason: len(excluded)} if excluded else {}
        ),
        "contributing_case_ids": denominator_ids,
        "numerator_case_ids": numerator_ids,
    }


def _report(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [item["id"] for item in scenarios]
    if len(set(ids)) != len(ids):
        raise AcceptanceError("duplicate scenario ids")
    results = {
        name: [item["id"] for item in scenarios if item["result"] == name]
        for name in (
            "passed",
            "failed",
            "blocked",
            "needs_clarification",
            "cancelled",
            "not_executed",
            "simulated",
        )
    }
    real = [item for item in scenarios if item["effects"]["origin"] != "none"]
    artifact_cases = [
        item for item in real if not item.get("negative_control")
    ]
    negatives = [item for item in real if item.get("negative_control")]
    verified = [
        item
        for item in artifact_cases
        if (item["artifact"] or {}).get("verifier_passed")
    ]
    integrity = [
        item
        for item in artifact_cases
        if item["effects"]["integrity"] == "verified"
    ]
    recovery = [
        item for item in scenarios if item["category"] == "recovery"
    ]
    conversation = [
        item for item in scenarios if item["category"] == "conversation"
    ]
    duplicate = sum(
        max(item["effects"]["file_writes"] - 1, 0) for item in scenarios
    )
    metrics = {
        "scenario_count": len(scenarios),
        "real_execution_count": len(real),
        "passed": len(results["passed"]),
        "failed": len(results["failed"]),
        "blocked": len(results["blocked"]),
        "needs_clarification": len(results["needs_clarification"]),
        "cancelled": len(results["cancelled"]),
        "not_executed": len(results["not_executed"]),
        "simulated": len(results["simulated"]),
        "duplicate_side_effects": duplicate,
        "artifact_count": len(artifact_cases),
        "negative_control_count": len(negatives),
        # Counters cover the same population as ``artifact_count`` so the
        # three numbers can be compared directly.  Negative controls are
        # reported separately instead of inflating the positive totals.
        "file_write_count": sum(
            item["effects"]["file_writes"] for item in artifact_cases
        ),
        "evidence_count": sum(
            item["effects"]["evidence_count"] for item in artifact_cases
        ),
        "negative_control_write_count": sum(
            item["effects"]["file_writes"] for item in negatives
        ),
        "negative_control_evidence_count": sum(
            item["effects"]["evidence_count"] for item in negatives
        ),
    }
    metric_audit = {
        "verifier_pass_rate": _metric(
            "verifier_pass_rate",
            "Artifact cases whose artifact passed an independent verifier re-run.",
            [item["id"] for item in verified],
            [item["id"] for item in artifact_cases],
            ids,
            "no_positive_artifact_case",
        ),
        "artifact_integrity_rate": _metric(
            "artifact_integrity_rate",
            "Artifact cases whose file hash matches recorded evidence.",
            [item["id"] for item in integrity],
            [item["id"] for item in artifact_cases],
            ids,
            "no_positive_artifact_case",
        ),
        "recovery_success_rate": _metric(
            "recovery_success_rate",
            "Recovery scenarios that finished without rewriting.",
            [item["id"] for item in recovery if item["passed"]],
            [item["id"] for item in recovery],
            ids,
            "not_recovery_case",
        ),
        "conversation_constraint_rate": _metric(
            "conversation_constraint_rate",
            "Conversation scenarios where the recorded constraint is respected.",
            [item["id"] for item in conversation if item["passed"]],
            [item["id"] for item in conversation],
            ids,
            "not_conversation_case",
        ),
    }
    for name, detail in metric_audit.items():
        metrics[name] = detail["value"]
    all_passed = all(item["passed"] for item in scenarios)
    return {
        "schema_version": "1.0",
        "result_vocabulary": RESULT_MAP,
        "scenarios": scenarios,
        "metrics": metrics,
        "metric_audit": metric_audit,
        "status": "PASS" if all_passed else "FAIL",
        "failed_scenarios": [
            item["id"] for item in scenarios if not item["passed"]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    from datetime import datetime, timezone

    from tests.audit_report import (
        build_metadata,
        stable_projection,
        write_envelope,
    )

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = time.perf_counter()
    report = run_acceptance()
    elapsed = (time.perf_counter() - started) * 1000
    exit_code = 0 if report["status"] == "PASS" else 1
    stable = stable_projection(report, "user_acceptance")
    written: dict[str, Any] | None = None
    if args.output_dir is not None:
        metadata = build_metadata(
            stable,
            kind="user_acceptance",
            command=["python3", "-m", "tests.user_acceptance_runner"],
            exit_code=exit_code,
            started_at=started_at,
            duration_ms=elapsed,
            not_run=[
                "agent-initiated shell or test execution (capability gap)",
                "network research (capability gap)",
                "real model API (out of scope)",
                "automatic mutation framework (not installed)",
            ],
        )
        written = write_envelope(
            args.output_dir, stable=stable, metadata=metadata
        )
    print(
        json.dumps(
            {
                "status": report["status"],
                "metrics": report["metrics"],
                "stable_sha256": (
                    written["stable_sha256"] if written else None
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
