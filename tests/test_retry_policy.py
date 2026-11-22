"""P1-4: Bounded manual retry — idempotent tools, retryable errors, bounded attempts."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import zevorinth.service as service_module
from zevorinth.local_test_runner import RUN_TESTS_TOOL
from zevorinth.models import (
    ExecutionMode,
    TaskRequest,
    TaskStatus,
    ToolResult,
)
from zevorinth.retry_policy import (
    DEFAULT_MAX_ATTEMPTS,
    IDEMPOTENT_TOOLS,
    MAX_RETRY_ATTEMPTS,
    NON_IDEMPOTENT_TOOLS,
    NON_RETRYABLE_ERROR_CODES,
    NON_RETRYABLE_STATUSES,
    RETRYABLE_ERROR_CODES,
    RETRYABLE_STATUSES,
    TaskRetryError,
    get_retry_decision,
    is_idempotent,
    is_retryable_error,
    is_retryable_status,
    retry_request_digest,
    should_retry,
)
from zevorinth.planner import (
    CONTROLLED_SHELL,
    EDIT_TEXT_FILE,
    INSPECT_WORKSPACE,
    MODIFY_TEXT_ARTIFACT,
    READ_TEXT_ARTIFACT,
    RESEARCH_REPORT,
    WRITE_TEXT_ARTIFACT,
)


class RetryPolicyTests(unittest.TestCase):
    def test_idempotent_tools_are_read_only(self) -> None:
        for tool in IDEMPOTENT_TOOLS:
            self.assertNotIn(tool, NON_IDEMPOTENT_TOOLS)

    def test_non_idempotent_tools_are_write_or_modify(self) -> None:
        expected = {
            WRITE_TEXT_ARTIFACT,
            MODIFY_TEXT_ARTIFACT,
            EDIT_TEXT_FILE,
            RUN_TESTS_TOOL,
        }
        self.assertEqual(NON_IDEMPOTENT_TOOLS, expected)

    def test_run_tests_is_never_treated_as_idempotent(self) -> None:
        # R4: running a suite executes workspace code, which can mutate
        # files or reach the network; the name is not a side-effect proof.
        self.assertFalse(is_idempotent(RUN_TESTS_TOOL))
        self.assertNotIn(RUN_TESTS_TOOL, IDEMPOTENT_TOOLS)
        self.assertIn(RUN_TESTS_TOOL, NON_IDEMPOTENT_TOOLS)

    def test_controlled_shell_is_idempotent(self) -> None:
        self.assertTrue(is_idempotent(CONTROLLED_SHELL))

    def test_write_text_artifact_is_not_idempotent(self) -> None:
        self.assertFalse(is_idempotent(WRITE_TEXT_ARTIFACT))

    def test_edit_text_file_is_not_idempotent(self) -> None:
        self.assertFalse(is_idempotent(EDIT_TEXT_FILE))

    def test_timeout_is_retryable(self) -> None:
        self.assertTrue(is_retryable_error("timeout"))

    def test_lease_expired_is_retryable(self) -> None:
        self.assertTrue(is_retryable_error("lease_expired"))

    def test_policy_violation_is_not_retryable(self) -> None:
        self.assertFalse(is_retryable_error("command_not_whitelisted"))

    def test_verification_failure_is_not_retryable(self) -> None:
        self.assertFalse(is_retryable_error("verification_failed"))

    def test_unknown_error_is_not_retryable(self) -> None:
        self.assertFalse(is_retryable_error("some_unknown_error"))

    def test_none_error_is_not_retryable(self) -> None:
        self.assertFalse(is_retryable_error(None))

    def test_failed_status_is_retryable(self) -> None:
        self.assertTrue(is_retryable_status("failed"))

    def test_succeeded_status_is_not_retryable(self) -> None:
        self.assertFalse(is_retryable_status("succeeded"))

    def test_should_retry_idempotent_tool_with_transient_error(self) -> None:
        self.assertTrue(
            should_retry(
                tool_name=CONTROLLED_SHELL,
                error_code="timeout",
                task_status="failed",
                attempt_count=0,
            )
        )

    def test_should_not_retry_non_idempotent_tool(self) -> None:
        self.assertFalse(
            should_retry(
                tool_name=WRITE_TEXT_ARTIFACT,
                error_code="timeout",
                task_status="failed",
                attempt_count=0,
            )
        )

    def test_should_not_retry_permanent_error(self) -> None:
        self.assertFalse(
            should_retry(
                tool_name=CONTROLLED_SHELL,
                error_code="command_not_whitelisted",
                task_status="failed",
                attempt_count=0,
            )
        )

    def test_should_not_retry_succeeded_task(self) -> None:
        self.assertFalse(
            should_retry(
                tool_name=CONTROLLED_SHELL,
                error_code="timeout",
                task_status="succeeded",
                attempt_count=0,
            )
        )

    def test_should_not_retry_after_max_attempts(self) -> None:
        self.assertFalse(
            should_retry(
                tool_name=CONTROLLED_SHELL,
                error_code="timeout",
                task_status="failed",
                attempt_count=MAX_RETRY_ATTEMPTS,
            )
        )

    def test_should_retry_below_max_attempts(self) -> None:
        self.assertTrue(
            should_retry(
                tool_name=CONTROLLED_SHELL,
                error_code="timeout",
                task_status="failed",
                attempt_count=MAX_RETRY_ATTEMPTS - 1,
            )
        )


class RetryDecisionTests(unittest.TestCase):
    def test_decision_includes_all_fields(self) -> None:
        decision = get_retry_decision(
            tool_name=CONTROLLED_SHELL,
            error_code="timeout",
            task_status="failed",
            attempt_count=1,
        )
        self.assertIn("retry", decision)
        self.assertIn("reason", decision)
        self.assertIn("tool_name", decision)
        self.assertIn("error_code", decision)
        self.assertIn("status", decision)
        self.assertIn("attempt_count", decision)
        self.assertIn("max_attempts", decision)
        self.assertIn("idempotent", decision)

    def test_decision_retry_true_for_valid_case(self) -> None:
        decision = get_retry_decision(
            tool_name=CONTROLLED_SHELL,
            error_code="timeout",
            task_status="failed",
            attempt_count=0,
        )
        self.assertTrue(decision["retry"])
        self.assertEqual(decision["reason"], "all retry conditions met")

    def test_decision_explains_why_not_retryable(self) -> None:
        decision = get_retry_decision(
            tool_name=WRITE_TEXT_ARTIFACT,
            error_code="timeout",
            task_status="failed",
            attempt_count=0,
        )
        self.assertFalse(decision["retry"])
        self.assertIn("not idempotent", decision["reason"])

    def test_decision_explains_max_attempts(self) -> None:
        decision = get_retry_decision(
            tool_name=CONTROLLED_SHELL,
            error_code="timeout",
            task_status="failed",
            attempt_count=MAX_RETRY_ATTEMPTS,
        )
        self.assertFalse(decision["retry"])
        self.assertIn("max_attempts", decision["reason"])


class RetryMutationTests(unittest.TestCase):
    """Mutation audit: verify guards are enforced."""

    def test_non_idempotent_set_cannot_contain_read_only_tools(self) -> None:
        """If a read-only tool is moved to non-idempotent, it should fail."""
        read_only = {CONTROLLED_SHELL, INSPECT_WORKSPACE, READ_TEXT_ARTIFACT}
        overlap = read_only & NON_IDEMPOTENT_TOOLS
        self.assertEqual(
            overlap,
            set(),
            f"read-only tools incorrectly marked non-idempotent: {overlap}",
        )

    def test_retryable_errors_cannot_include_policy_violations(self) -> None:
        """Policy violations must never be retryable."""
        policy_errors = {
            "command_not_whitelisted",
            "forbidden_flag",
            "sensitive_path_rejected",
            "unsupported_operation",
            "unsupported_tool",
        }
        overlap = policy_errors & RETRYABLE_ERROR_CODES
        self.assertEqual(
            overlap,
            set(),
            f"policy violations incorrectly marked retryable: {overlap}",
        )

    def test_max_attempts_is_bounded(self) -> None:
        self.assertLessEqual(MAX_RETRY_ATTEMPTS, 5)
        self.assertGreaterEqual(MAX_RETRY_ATTEMPTS, 1)

    def test_succeeded_is_never_retryable(self) -> None:
        self.assertNotIn("succeeded", RETRYABLE_STATUSES)
        self.assertIn("succeeded", NON_RETRYABLE_STATUSES)


class RetryEndToEndTests(unittest.TestCase):
    """End-to-end: retry a failed controlled_shell task via TaskService."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_retry_non_retryable_task_returns_original(self) -> None:
        from zevorinth.service import TaskService
        from zevorinth.models import TaskRequest

        service = TaskService(self.root)
        # Create a task that fails with a non-retryable error
        task = service.submit(
            TaskRequest(
                goal="删除文件",
                operation="controlled_shell",
                parameters={
                    "command": "rm",
                    "arguments": ["file.txt"],
                },
            )
        )
        # Task should be failed/blocked (rm not whitelisted)
        self.assertIn(task.status.value, ("failed", "blocked", "planning_failed"))
        # Retry should return the original task (not retryable)
        retried = service.retry_task(task.task_id)
        self.assertEqual(retried.task_id, task.task_id)

    def test_retry_succeeded_task_returns_original(self) -> None:
        from zevorinth.service import TaskService
        from zevorinth.models import TaskRequest

        service = TaskService(self.root)
        task = service.submit(
            TaskRequest(
                goal="列出文件",
                operation="controlled_shell",
                parameters={
                    "command": "echo",
                    "arguments": ["hello"],
                },
            )
        )
        self.assertEqual(task.status.value, "succeeded")
        retried = service.retry_task(task.task_id)
        self.assertEqual(retried.task_id, task.task_id)


class RetryPositivePathTests(unittest.TestCase):
    """R6: the retry happy path must really create, persist and run a task."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _request() -> TaskRequest:
        return TaskRequest(
            goal="列出受控目录内容",
            operation=CONTROLLED_SHELL,
            parameters={"command": "echo", "arguments": ["ok"]},
        )

    @staticmethod
    def _failure(kwargs: dict, code: str = "timeout") -> ToolResult:
        return ToolResult(
            attempt_id=kwargs["attempt_id"],
            tool_name=CONTROLLED_SHELL,
            execution_mode=ExecutionMode.REAL,
            success=False,
            started_at=kwargs["started_at"],
            finished_at=kwargs["finished_at"],
            error_code=code,
            error_message=f"simulated {code}",
            workspace_effects={
                "command": kwargs["command"],
                "arguments": list(kwargs["arguments"]),
                "output": "",
                "exit_code": -1,
                "duration_seconds": 0.0,
                "output_bytes": 0,
                "truncated": False,
                "timed_out": code == "timeout",
            },
        )

    def _patched_shell(self, *, fail_times: int, code: str = "timeout"):
        """Fail the first N shell executions, then delegate to the real one."""
        real = service_module.execute_controlled_shell
        calls = {"n": 0}

        def fake(**kwargs):
            calls["n"] += 1
            if calls["n"] <= fail_times:
                return self._failure(kwargs, code)
            return real(**kwargs)

        return mock.patch.object(
            service_module, "execute_controlled_shell", side_effect=fake,
        )

    def _failed_task(self, service, *, code: str = "timeout"):
        with self._patched_shell(fail_times=1, code=code):
            task = service.submit(self._request())
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertIsNotNone(task.error)
        self.assertEqual(task.error.code, code)
        return task

    def test_retryable_failure_creates_a_new_task(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root, recover_leases=False)
        original = self._failed_task(service)

        with self._patched_shell(fail_times=0):
            outcome = service.retry_task_detailed(original.task_id)

        self.assertTrue(outcome["retried"], outcome["reason"])
        self.assertFalse(outcome["already_exists"])
        self.assertNotEqual(outcome["task_id"], original.task_id)
        self.assertEqual(outcome["retry_attempt"], 1)
        self.assertEqual(outcome["task"].status, TaskStatus.SUCCEEDED)

    def test_retry_metadata_is_persisted_and_reloadable(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root, recover_leases=False)
        original = self._failed_task(service)
        with self._patched_shell(fail_times=0):
            outcome = service.retry_task_detailed(original.task_id)

        reloaded = TaskService(
            self.root, recover_leases=False,
        ).store.require_task(outcome["task_id"])
        self.assertEqual(reloaded.metadata["retry_of"], original.task_id)
        self.assertEqual(reloaded.metadata["retry_root"], original.task_id)
        self.assertEqual(reloaded.metadata["retry_attempt"], 1)
        self.assertEqual(
            reloaded.metadata["retry_request_digest"],
            retry_request_digest(
                original.task_id,
                TaskRequest(
                    goal=original.request.goal,
                    operation=original.request.operation,
                    parameters=dict(original.request.parameters),
                ),
            ),
        )
        created = [
            event
            for event in service.events(outcome["task_id"])
            if event.event_type == "task_created"
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(
            created[0].payload["metadata"]["retry_of"],
            original.task_id,
        )

    def test_original_task_stays_failed_after_retry(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root, recover_leases=False)
        original = self._failed_task(service)
        with self._patched_shell(fail_times=0):
            service.retry_task_detailed(original.task_id)

        reread = TaskService(
            self.root, recover_leases=False,
        ).store.require_task(original.task_id)
        self.assertEqual(reread.status, TaskStatus.FAILED)
        self.assertEqual(reread.error.code, "timeout")

    def test_retry_attempts_count_up_and_the_fourth_is_rejected(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root, recover_leases=False)
        original = self._failed_task(service)
        created: list[str] = []

        with self._patched_shell(fail_times=0):
            for expected in (1, 2, 3):
                outcome = service.retry_task_detailed(original.task_id)
                self.assertTrue(outcome["retried"], outcome["reason"])
                self.assertEqual(outcome["retry_attempt"], expected)
                self.assertEqual(
                    service._count_retry_attempts(original.task_id),
                    expected,
                )
                created.append(outcome["task_id"])

            fourth = service.retry_task_detailed(original.task_id)

        self.assertFalse(fourth["retried"])
        self.assertIn("attempt_count", fourth["reason"])
        self.assertEqual(fourth["task"].task_id, original.task_id)
        self.assertEqual(len(set(created)), 3)
        self.assertEqual(service._count_retry_attempts(original.task_id), 3)

    def test_unknown_shell_exit_code_is_not_retryable(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root, recover_leases=False)
        original = self._failed_task(service, code="exit_2")

        outcome = service.retry_task_detailed(original.task_id)

        self.assertFalse(outcome["retried"])
        self.assertIn("exit_2", outcome["reason"])
        self.assertEqual(outcome["task"].task_id, original.task_id)
        self.assertFalse(is_retryable_error("exit_2"))
        self.assertTrue(is_retryable_error("timeout"))

    def test_same_request_id_is_idempotent(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root, recover_leases=False)
        original = self._failed_task(service)
        request_id = str(uuid4())

        with self._patched_shell(fail_times=0):
            first = service.retry_task_detailed(
                original.task_id, request_id=request_id,
            )
            second = service.retry_task_detailed(
                original.task_id, request_id=request_id,
            )

        self.assertTrue(first["retried"])
        self.assertEqual(first["task_id"], request_id)
        self.assertFalse(second["retried"])
        self.assertTrue(second["already_exists"])
        self.assertEqual(second["task_id"], request_id)
        self.assertEqual(service._count_retry_attempts(original.task_id), 1)

        # A different request_id must still be bounded by the attempt limit.
        third = service.retry_task_detailed(
            original.task_id, request_id=str(uuid4()),
        )
        self.assertTrue(third["retried"])
        self.assertEqual(third["retry_attempt"], 2)
        self.assertNotEqual(third["task_id"], request_id)
        self.assertEqual(service._count_retry_attempts(original.task_id), 2)

    def test_reused_request_id_for_another_task_conflicts(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root, recover_leases=False)
        first_original = self._failed_task(service)
        second_original = self._failed_task(service)
        request_id = str(uuid4())

        with self._patched_shell(fail_times=0):
            service.retry_task_detailed(
                first_original.task_id, request_id=request_id,
            )
            with self.assertRaises(TaskRetryError) as caught:
                service.retry_task_detailed(
                    second_original.task_id, request_id=request_id,
                )

        self.assertEqual(caught.exception.code, "retry_request_conflict")
        self.assertEqual(
            service._count_retry_attempts(second_original.task_id), 0,
        )

    def test_request_id_cannot_reuse_the_original_task_id(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root, recover_leases=False)
        original = self._failed_task(service)

        with self.assertRaises(TaskRetryError) as caught:
            service.retry_task_detailed(
                original.task_id, request_id=original.task_id,
            )
        self.assertEqual(caught.exception.code, "retry_request_id_conflict")

    def test_concurrent_duplicate_requests_create_one_task(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root, recover_leases=False)
        original = self._failed_task(service)
        request_id = str(uuid4())
        workers = 4
        barrier = threading.Barrier(workers)
        outcomes: list[dict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def run() -> None:
            worker = TaskService(self.root, recover_leases=False)
            barrier.wait(timeout=10)
            try:
                result = worker.retry_task_detailed(
                    original.task_id, request_id=request_id,
                )
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)
                return
            with lock:
                outcomes.append(result)

        with self._patched_shell(fail_times=0):
            threads = [
                threading.Thread(target=run, daemon=True)
                for _ in range(workers)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), workers)
        self.assertEqual(
            {outcome["task_id"] for outcome in outcomes}, {request_id},
        )
        self.assertEqual(
            sum(1 for outcome in outcomes if outcome["retried"]), 1,
        )
        self.assertEqual(service._count_retry_attempts(original.task_id), 1)
        persisted = TaskService(
            self.root, recover_leases=False,
        ).store.require_task(request_id)
        self.assertEqual(persisted.metadata["retry_of"], original.task_id)

    def test_failed_retry_is_not_reported_as_success(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root, recover_leases=False)
        original = self._failed_task(service)

        with self._patched_shell(fail_times=1):
            outcome = service.retry_task_detailed(original.task_id)

        self.assertTrue(outcome["retried"])
        self.assertEqual(outcome["task"].status, TaskStatus.FAILED)
        self.assertEqual(outcome["task"].error.code, "timeout")
        persisted = TaskService(
            self.root, recover_leases=False,
        ).store.require_task(outcome["task_id"])
        self.assertEqual(persisted.status, TaskStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
