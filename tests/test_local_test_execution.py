from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.local_test_runner import MAX_OUTPUT_BYTES  # noqa: E402
from zevorinth.models import (  # noqa: E402
    TaskRequest,
    TaskStatus,
    TestExecutionEvidence,
)
from zevorinth.planner import RUN_TESTS  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.verifier import TestResultVerifier  # noqa: E402
from zevorinth.workspace_snapshot import (  # noqa: E402
    EffectPolicy,
    Snapshot,
    diff_snapshots,
)


PASSING = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(1 + 1, 2)\n"
)
FAILING = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_no(self):\n"
    "        self.assertEqual(1, 2)\n"
)
SLOW = (
    "import time, unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_slow(self):\n"
    "        time.sleep(30)\n"
)
NOISY = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_noisy(self):\n"
    "        print('x' * 50000)\n"
)


def _workspace(
    root: Path, name: str = "demo", files: dict | None = None
) -> Path:
    workspace = root / "workspaces" / name
    workspace.mkdir(parents=True)
    for filename, content in (files or {"test_demo.py": PASSING}).items():
        (workspace / filename).write_text(content, encoding="utf-8")
    return workspace


def _request(
    workspace: str = "demo", target: str = "test_demo", **extra: Any
) -> TaskRequest:
    parameters = {
        "runner": "unittest",
        "target": target,
        "workspace": workspace,
        "execution_backend": "host",
    }
    parameters.update(extra)
    return TaskRequest(
        goal="run local tests", operation=RUN_TESTS, parameters=parameters
    )


def _events(service: TaskService, task_id: str) -> list[str]:
    return [event.event_type for event in service.events(task_id)]


class LocalTestExecutionTests(unittest.TestCase):
    def test_passing_target_succeeds_with_real_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root)
            task = service.submit(_request())
            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            evidence = task.tool_result.test_evidence
            self.assertEqual(evidence.exit_code, 0)
            self.assertFalse(evidence.timed_out)
            self.assertFalse(evidence.cancelled)
            self.assertFalse(evidence.truncated)
            self.assertEqual(evidence.attempt_id, task.attempt_id)
            self.assertEqual(evidence.claim_generation, task.claim_generation)
            self.assertEqual(task.verification.reason, "tests_passed")
            event_types = _events(service, task.task_id)
            self.assertEqual(event_types.count("tool_started"), 1)
            self.assertEqual(event_types.count("task_succeeded"), 1)

    def test_failing_target_is_not_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root, files={"test_bad.py": FAILING})
            service = TaskService(root)
            task = service.submit(_request(target="test_bad"))
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "tests_failed")
            self.assertEqual(task.tool_result.test_evidence.exit_code, 1)
            self.assertNotIn("task_succeeded", _events(service, task.task_id))
            self.assertFalse(task.verification.passed)

    def test_output_within_limit_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root)
            task = service.submit(_request(max_output_bytes=4096))
            evidence = task.tool_result.test_evidence
            self.assertFalse(evidence.truncated)
            # ``unittest -q`` writes its progress dots to stderr.
            self.assertGreater(
                evidence.stdout_bytes + evidence.stderr_bytes, 0
            )
            self.assertEqual(len(evidence.stderr_sha256), 64)
            self.assertLessEqual(len(evidence.stdout_excerpt), 2000)

    def test_output_over_limit_is_truncated_and_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root, files={"test_noisy.py": NOISY})
            service = TaskService(root)
            task = service.submit(
                _request(target="test_noisy", max_output_bytes=256)
            )
            evidence = task.tool_result.test_evidence
            self.assertTrue(evidence.truncated)
            self.assertGreater(evidence.stdout_bytes, 256)
            self.assertTrue(evidence.stdout_bytes >= 50000)
            self.assertLessEqual(len(evidence.stdout_excerpt), 2000)
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(
                task.verification.reason, "test_output_truncated"
            )
            self.assertNotIn("task_succeeded", _events(service, task.task_id))

    def test_cli_entry_runs_a_real_target(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "zevorinth.cli",
                    "--home",
                    str(root),
                    "test",
                    "--workspace",
                    "demo",
                    "--target",
                    "test_demo",
                    "--backend",
                    "host",
                    "--json",
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PYTHONPATH": str(PROJECT_ROOT / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "OPENAI_API_KEY": "",
                },
                timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "succeeded")
            self.assertEqual(
                payload["tool_result"]["test_evidence"]["exit_code"], 0
            )


class LocalTestSecurityTests(unittest.TestCase):
    def _blocked(self, root: Path, request: TaskRequest) -> Any:
        service = TaskService(root)
        task = service.submit(request)
        self.assertEqual(task.status, TaskStatus.BLOCKED, task.error)
        self.assertNotIn("tool_started", _events(service, task.task_id))
        return task

    def test_unknown_runner_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            task = self._blocked(root, _request(runner="bash"))
            self.assertEqual(task.error.code, "unsupported_runner")

    def test_unknown_parameter_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            task = self._blocked(root, _request(command="rm -rf /"))
            self.assertEqual(task.error.code, "unsupported_parameter")

    def test_shell_injection_targets_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            for target in (
                "test_demo; rm -rf /",
                "test_demo && cat /etc/passwd",
                "$(whoami)",
                "-m http.server",
                "test_demo | tee /tmp/x",
            ):
                task = self._blocked(root, _request(target=target))
                self.assertEqual(
                    task.error.code, "invalid_test_target", target
                )

    def test_absolute_and_traversal_targets_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            for target in ("/etc/passwd", "../outside.test", "a/b"):
                task = self._blocked(root, _request(target=target))
                self.assertEqual(
                    task.error.code, "invalid_test_target", target
                )

    def test_workspace_traversal_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            for workspace in ("../etc", "/tmp", "a/b", "..", " "):
                task = self._blocked(root, _request(workspace=workspace))
                self.assertEqual(
                    task.error.code, "invalid_workspace", workspace
                )

    def test_symlinked_workspace_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "workspaces").mkdir()
            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            os.symlink(outside, root / "workspaces" / "linked")
            task = self._blocked(root, _request(workspace="linked"))
            self.assertEqual(task.error.code, "unsafe_workspace")

    def test_symlinked_target_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _workspace(root)
            outside = root / "outside.py"
            outside.write_text(PASSING, encoding="utf-8")
            os.symlink(outside, workspace / "escaped.py")
            task = self._blocked(root, _request(target="escaped"))
            self.assertEqual(task.error.code, "unsafe_test_target")

    def test_missing_workspace_and_target_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            self.assertEqual(
                self._blocked(root, _request(workspace="absent")).error.code,
                "workspace_missing",
            )
            self.assertEqual(
                self._blocked(
                    root, _request(target="no_such_module")
                ).error.code,
                "test_target_missing",
            )

    def test_invalid_limits_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            for extra, code in (
                ({"timeout_seconds": 0}, "invalid_timeout"),
                ({"timeout_seconds": 120}, "invalid_timeout"),
                ({"max_output_bytes": 10}, "invalid_output_limit"),
                (
                    {"max_output_bytes": MAX_OUTPUT_BYTES * 2},
                    "invalid_output_limit",
                ),
            ):
                task = self._blocked(root, _request(**extra))
                self.assertEqual(task.error.code, code, extra)

    def test_plan_with_shell_tool_is_denied(self) -> None:
        from zevorinth.models import Plan, PlanStep
        from zevorinth.policy import ArtifactPolicy

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            decision = ArtifactPolicy().evaluate(
                request=_request(),
                plan=Plan(
                    plan_id="p",
                    planner_name="t",
                    deterministic=True,
                    steps=[
                        PlanStep(
                            step_id="s",
                            tool_name="shell",
                            expected_result="x",
                            parameters={},
                        )
                    ],
                ),
                task_id=str(uuid4()),
                artifacts_root=root / "artifacts",
            )
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.code, "unsupported_tool")

    def test_timeout_kills_the_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root, files={"test_slow.py": SLOW})
            service = TaskService(root)
            started = time.monotonic()
            task = service.submit(
                _request(target="test_slow", timeout_seconds=1)
            )
            elapsed = time.monotonic() - started
            evidence = task.tool_result.test_evidence
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "test_timeout")
            self.assertTrue(evidence.timed_out)
            self.assertTrue(evidence.terminated_confirmed)
            self.assertIsNone(evidence.exit_code)
            self.assertLess(elapsed, 20)
            self.assertNotIn("task_succeeded", _events(service, task.task_id))

    def test_cancel_before_execution_starts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root)
            task = service.enqueue(_request())
            service.request_cancel(
                task.task_id, requested_by="t", reason="stop"
            )
            cancelled = service.execute_task(task.task_id)
            self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
            self.assertNotIn("tool_started", _events(service, task.task_id))

    def test_cancel_during_execution_terminates_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root, files={"test_slow.py": SLOW})
            service = TaskService(root)
            task = service.enqueue(
                _request(target="test_slow", timeout_seconds=30)
            )

            def cancel() -> None:
                time.sleep(0.4)
                service.request_cancel(
                    task.task_id, requested_by="t", reason="stop"
                )

            threading.Thread(target=cancel).start()
            cancelled = service.execute_task(task.task_id)
            evidence = cancelled.tool_result.test_evidence
            self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
            self.assertTrue(evidence.cancelled)
            self.assertTrue(evidence.terminated_confirmed)
            self.assertIsNone(evidence.exit_code)
            self.assertEqual(cancelled.verification.reason, "test_cancelled")


def _snapshot(captured_at: str) -> dict:
    return Snapshot(
        workspace="demo",
        task_id="task",
        attempt_id="attempt",
        claim_generation=1,
        captured_at=captured_at,
        complete=True,
    ).to_dict()


def _evidence(**overrides: Any) -> TestExecutionEvidence:
    before = Snapshot.from_dict(_snapshot("before"))
    after = Snapshot.from_dict(_snapshot("after"))
    workspace_diff = diff_snapshots(
        before, after, EffectPolicy.build({})
    )
    base = {
        "runner": "unittest",
        "target": "test_demo",
        "workspace": "demo",
        "attempt_id": "attempt",
        "claim_generation": 1,
        "exit_code": 0,
        "started_at": "s",
        "finished_at": "f",
        "duration_ms": 1.0,
        "timed_out": False,
        "cancelled": False,
        "truncated": False,
        "stdout_sha256": "0" * 64,
        "stderr_sha256": "0" * 64,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "stdout_excerpt": "",
        "stderr_excerpt": "",
        "terminated_confirmed": True,
        "output_limit_bytes": 1024,
        "timeout_seconds": 10.0,
        "before_snapshot": before.to_dict(),
        "after_snapshot": after.to_dict(),
        "workspace_diff": workspace_diff,
    }
    base.update(overrides)
    return TestExecutionEvidence(**base)


class _TaskStub:
    task_id = "task"
    attempt_id = "attempt"
    claim_generation = 1


class TestVerifierBoundaryTests(unittest.TestCase):
    def _verify(self, evidence: TestExecutionEvidence, **params: Any):
        expected = {
            "runner": "unittest",
            "target": "test_demo",
            "workspace": "demo",
            "execution_backend": "host",
        }
        expected.update(params)
        return TestResultVerifier().verify(
            task=_TaskStub(),
            evidence=evidence,
            expected_parameters=expected,
            checked_at="now",
        )

    def test_zero_exit_is_verified(self) -> None:
        self.assertTrue(self._verify(_evidence()).passed)

    def test_nonzero_exit_is_not_verified(self) -> None:
        result = self._verify(_evidence(exit_code=1))
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "tests_failed")

    def test_truncated_output_blocks_success(self) -> None:
        result = self._verify(_evidence(truncated=True))
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "test_output_truncated")

    def test_missing_exit_code_is_not_success(self) -> None:
        result = self._verify(_evidence(exit_code=None))
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "test_exit_unknown")

    def test_stale_generation_is_rejected(self) -> None:
        result = self._verify(_evidence(claim_generation=0))
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "test_evidence_mismatch")

    def test_wrong_attempt_is_rejected(self) -> None:
        result = self._verify(_evidence(attempt_id="other"))
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "test_evidence_mismatch")

    def test_parameter_drift_is_rejected(self) -> None:
        result = self._verify(_evidence(), target="other_target")
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "test_evidence_mismatch")

    def test_missing_evidence_is_rejected(self) -> None:
        result = TestResultVerifier().verify(
            task=_TaskStub(),
            evidence=None,
            expected_parameters={
                "runner": "unittest",
                "target": "test_demo",
                "workspace": "demo",
            },
            checked_at="now",
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "test_evidence_missing")


if __name__ == "__main__":
    unittest.main()
