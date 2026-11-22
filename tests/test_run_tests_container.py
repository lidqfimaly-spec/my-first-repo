"""run_tests container backend gate and absence of host fallback."""

from __future__ import annotations

import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.host_grant_support import authorized_service  # noqa: E402
from zevorinth.isolation import ContainerBackendUnavailable  # noqa: E402
from zevorinth.retry_policy import is_retryable_error  # noqa: E402
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import RUN_TESTS  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


MARKER_CODE = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_always_fails(self):\n"
    "        self.assertEqual(1, 2)\n"
)


def _workspace(root: Path) -> Path:
    workspace = root / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    (workspace / "test_marker.py").write_text(MARKER_CODE, encoding="utf-8")
    return workspace


def _request(**extra) -> TaskRequest:
    parameters = {
        "runner": "unittest",
        "target": "test_marker",
        "workspace": "demo",
    }
    parameters.update(extra)
    return TaskRequest(goal="run", operation=RUN_TESTS, parameters=parameters)


class ContainerBackendGateTests(unittest.TestCase):
    def _assert_blocked_without_host_effect(self, request: TaskRequest) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _workspace(root)
            service = TaskService(root)
            # The evidence gate is what is under test here, so the live
            # runtime probes are stubbed: a developer machine without a
            # running Docker daemon must still be able to assert it.
            with mock.patch(
                "zevorinth.isolation._live_runtime_identity",
                return_value="stub-runtime",
            ), mock.patch(
                "zevorinth.isolation._live_image_digest",
                return_value="python@sha256:" + "a" * 64,
            ):
                task = service.submit(request)
            # A blocked run is reported as isolation failure, never as a
            # test failure (which would prove the test actually ran).
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "isolation_unverified")
            # The failure record exists, but no test evidence was produced:
            # the test itself never ran.
            self.assertIsNone(task.tool_result.test_evidence)
            events = [e.event_type for e in service.events(task.task_id)]
            self.assertNotIn("task_succeeded", events)

    def test_unreachable_runtime_is_reported_as_backend_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root)
            with mock.patch(
                "zevorinth.isolation._live_runtime_identity",
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
            # An environment condition, not a test outcome and not a timeout.
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(
                task.error.code, "container_backend_unavailable"
            )
            self.assertIsNone(task.tool_result.test_evidence)
            self.assertFalse(is_retryable_error("container_backend_unavailable"))

    def test_container_backend_without_evidence_is_blocked(self) -> None:
        self._assert_blocked_without_host_effect(
            _request(execution_backend="container")
        )

    def test_container_backend_with_missing_evidence_is_blocked(self) -> None:
        self._assert_blocked_without_host_effect(
            _request(execution_backend="container", evidence_id="not-present")
        )

    def test_container_backend_reports_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root)
            task = service.submit(_request(execution_backend="container"))
            self.assertIn("evidence_id_missing", task.error.message)

    def test_invalid_backend_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _workspace(root)
            service = TaskService(root)
            task = service.submit(_request(execution_backend="shell"))
            self.assertEqual(task.status, TaskStatus.BLOCKED)
            self.assertEqual(task.error.code, "invalid_execution_backend")
            self.assertIsNone(task.tool_result)

    def test_interrupted_container_task_is_not_rerun_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _workspace(root)
            service = TaskService(
                root, clock=lambda: "2026-10-20T00:00:00.000+00:00"
            )
            task = service.enqueue(_request(execution_backend="container"))
            claim = service._claim(
                task.task_id, allowed=TaskService.RUNNABLE_STATUSES
            )
            token = str(claim.task.lease_token)
            service.store.record_event_and_update(
                task.task_id,
                "tool_started",
                "2026-10-20T00:00:00.000+00:00",
                updates={"phase": "test_started"},
                lease_token=token,
            )
            service.store.expire_claim(
                task.task_id, now="2026-10-20T00:01:00.000+00:00"
            )
            resumed = TaskService(
                root, clock=lambda: "2026-10-20T00:01:00.000+00:00"
            ).resume(task.task_id)
            self.assertEqual(resumed.status, TaskStatus.BLOCKED)
            self.assertEqual(resumed.error.code, "test_execution_unknown")
            starts = [
                e.event_type
                for e in TaskService(root).events(task.task_id)
            ].count("tool_started")
            self.assertEqual(starts, 1)

    def test_host_backend_requires_explicit_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _workspace(root)
            service, _, token = authorized_service(
                root, workspace="demo", target="test_marker"
            )
            task = service.submit(
                _request(
                    execution_backend="host", host_authorization=token
                )
            )
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "tests_failed")
            self.assertIsNotNone(task.tool_result)

    def test_host_backend_without_a_grant_never_starts_a_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _workspace(root)
            service = TaskService(root)
            task = service.submit(_request(execution_backend="host"))
            self.assertEqual(task.status, TaskStatus.BLOCKED)
            self.assertEqual(task.error.code, "host_authorization_required")
            self.assertIsNone(task.tool_result)


if __name__ == "__main__":
    unittest.main()
