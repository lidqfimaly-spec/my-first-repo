"""S1-3: the four test-execution failure classes stay distinct.

A single ``failed`` status hides four different truths:

* ``container_backend_unavailable`` — the environment cannot run containers.
  This is not a test outcome and not a timeout, and it must never be
  "recovered" by running the workspace code on the host instead.
* ``test_runner_error`` — the executor itself raised. That is a defect in
  Zevorinth, not a result about the user's code.
* ``test_execution_failed`` / ``tests_failed`` — the tests really ran and did
  not pass.
* ``host_authorization_*`` — the run was never authorized.

Collapsing any of these into ``test_timeout`` (the historical shape) invites
a retry that cannot help and hides an environment problem as a user problem.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.host_grant_support import authorized_service, grant_for  # noqa: E402
from zevorinth import local_test_runner  # noqa: E402
from zevorinth.isolation import ContainerBackendUnavailable  # noqa: E402
from zevorinth.models import (  # noqa: E402
    TaskRequest,
    TaskStatus,
    VerificationResult,
)
from zevorinth.planner import RUN_TESTS  # noqa: E402
from zevorinth.retry_policy import is_retryable_error  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402

PASSING = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        self.assertTrue(True)\n"
)
FAILING = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_bad(self):\n"
    "        self.assertEqual(1, 2)\n"
)


def _workspace(root: Path, source: str = PASSING) -> Path:
    workspace = root / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    (workspace / "test_ok.py").write_text(source, encoding="utf-8")
    return workspace


def _request(**extra) -> TaskRequest:
    parameters = {
        "runner": "unittest",
        "target": "test_ok",
        "workspace": "demo",
    }
    parameters.update(extra)
    return TaskRequest(goal="run", operation=RUN_TESTS, parameters=parameters)


class ErrorClassificationTests(unittest.TestCase):
    def test_missing_grant_is_classified_and_never_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root)
            calls: list[object] = []
            service.test_runner = lambda **kwargs: calls.append(kwargs)
            task = service.submit(_request(execution_backend="host"))
            self.assertEqual(task.status, TaskStatus.BLOCKED)
            self.assertEqual(task.error.code, "host_authorization_required")
            self.assertEqual(calls, [])
            # Refused at plan time: no tool event, because no process ran.
            events = [
                event.event_type for event in service.events(task.task_id)
            ]
            self.assertIn("policy_rejected", events)
            self.assertNotIn("tool_started", events)

    def test_runner_exception_is_a_runner_error_not_a_test_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service, _, token = authorized_service(
                root, workspace="demo", target="test_ok"
            )

            def exploding_runner(**kwargs):
                del kwargs
                raise RuntimeError("executor exploded")

            service.test_runner = exploding_runner
            task = service.submit(
                _request(execution_backend="host", host_authorization=token)
            )
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "test_runner_error")
            self.assertIn("RuntimeError", task.error.message)
            self.assertIsNone(task.tool_result.test_evidence)

    def test_runner_raising_a_typed_error_keeps_its_own_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service, _, token = authorized_service(
                root, workspace="demo", target="test_ok"
            )

            def typed_runner(**kwargs):
                del kwargs
                raise local_test_runner.TestExecutionError(
                    "output_limit_exceeded", "too much output"
                )

            service.test_runner = typed_runner
            task = service.submit(
                _request(execution_backend="host", host_authorization=token)
            )
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "output_limit_exceeded")

    def test_container_backend_unavailable_is_environment_not_outcome(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root)
            host_calls: list[object] = []
            service.test_runner = lambda **kwargs: host_calls.append(kwargs)
            with mock.patch.object(
                service,
                "_run_container_test",
                side_effect=ContainerBackendUnavailable(
                    "container runtime probe failed: TimeoutExpired"
                ),
            ):
                task = service.submit(
                    _request(
                        execution_backend="container",
                        evidence_id="not-present",
                    )
                )
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(
                task.error.code, "container_backend_unavailable"
            )
            # No silent fallback to the host: the host runner was never
            # consulted, so this environment problem cannot turn into an
            # unisolated run.
            self.assertEqual(host_calls, [])
            self.assertIsNone(task.tool_result.test_evidence)
            self.assertNotEqual(task.error.code, "test_timeout")

    def test_failing_tests_are_reported_as_test_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root, FAILING)
            service, _, token = authorized_service(
                root, workspace="demo", target="test_ok"
            )
            task = service.submit(
                _request(execution_backend="host", host_authorization=token)
            )
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "tests_failed")
            self.assertIsNotNone(task.tool_result.test_evidence)
            self.assertNotEqual(task.tool_result.test_evidence.exit_code, 0)

    def test_unclassified_verification_reason_is_test_execution_failed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service, _, token = authorized_service(
                root, workspace="demo", target="test_ok"
            )

            verdict = VerificationResult(
                verifier_name="test-result-verifier",
                passed=False,
                exists=True,
                path_safe=True,
                content_matches=False,
                evidence_matches=True,
                reason="something_we_have_not_classified",
                artifact=None,
                checked_at="2026-01-01T00:00:00+00:00",
            )
            service.test_verifier = mock.Mock(
                verify=lambda **kwargs: verdict
            )
            task = service.submit(
                _request(execution_backend="host", host_authorization=token)
            )
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "test_execution_failed")

    def test_none_of_the_environment_classes_are_retryable(self) -> None:
        for code in (
            "container_backend_unavailable",
            "host_authorization_required",
            "host_authorization_invalid",
            "host_authorization_expired",
            "host_authorization_scope_mismatch",
            "host_authorization_already_used",
            "host_authorization_revoked",
        ):
            with self.subTest(code=code):
                self.assertFalse(is_retryable_error(code))

    def test_capability_registry_documents_the_classification(self) -> None:
        from zevorinth.capabilities import CAPABILITY_REGISTRY

        details = CAPABILITY_REGISTRY["task_operations"]["run_tests"]
        classification = details["error_classification"]
        for code in (
            "container_backend_unavailable",
            "test_runner_error",
            "test_execution_failed",
            "tests_failed",
            "test_timeout",
            "host_authorization_required",
        ):
            with self.subTest(code=code):
                self.assertIn(code, classification)
        limitations = " ".join(details["limitations"])
        self.assertIn("no automatic fallback between backends", limitations)


class GrantScopeAtExecutionTests(unittest.TestCase):
    """The grant must be bound to what actually runs, not to what was asked."""

    def test_grant_cannot_authorize_a_different_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            other = root / "workspaces" / "other"
            other.mkdir()
            (other / "test_ok.py").write_text(PASSING, encoding="utf-8")
            store, token = grant_for(
                root, workspace="demo", target="test_ok"
            )
            service = TaskService(root, host_grant_store=store)
            task = service.submit(
                _request(
                    workspace="other",
                    execution_backend="host",
                    host_authorization=token,
                )
            )
            self.assertEqual(task.status, TaskStatus.BLOCKED)
            self.assertEqual(
                task.error.code, "host_authorization_scope_mismatch"
            )
            self.assertEqual(store.list_grants()[0]["status"], "issued")


if __name__ == "__main__":
    unittest.main()
