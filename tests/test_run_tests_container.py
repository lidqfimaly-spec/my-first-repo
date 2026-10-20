"""run_tests container backend gate and absence of host fallback."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

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
            task = service.submit(request)
            # A blocked run is reported as isolation failure, never as a
            # test failure (which would prove the test actually ran).
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "test_isolation_unverified")
            self.assertIsNone(task.tool_result, "no tool result was recorded")
            events = [e.event_type for e in service.events(task.task_id)]
            self.assertNotIn("task_succeeded", events)

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

    def test_host_backend_requires_explicit_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _workspace(root)
            service = TaskService(root)
            task = service.submit(_request(execution_backend="host"))
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "tests_failed")
            self.assertIsNotNone(task.tool_result)


if __name__ == "__main__":
    unittest.main()
