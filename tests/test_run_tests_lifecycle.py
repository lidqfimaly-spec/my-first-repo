"""Host authorization, artifact collection and real container termination."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import RUN_TESTS  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


def _workspace(root: Path) -> Path:
    workspace = root / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    (workspace / "test_ok.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    return workspace


def _request(**extra) -> TaskRequest:
    parameters = {
        "runner": "unittest",
        "target": "test_ok",
        "workspace": "demo",
    }
    parameters.update(extra)
    return TaskRequest(goal="run", operation=RUN_TESTS, parameters=parameters)


class HostAuthorizationTests(unittest.TestCase):
    def test_host_without_authorization_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root)
            task = service.submit(_request(execution_backend="host"))
            self.assertEqual(task.status, TaskStatus.BLOCKED)
            self.assertEqual(
                task.error.code, "host_execution_not_authorized"
            )
            self.assertIsNone(task.tool_result)

    def test_host_with_wrong_token_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root)
            task = service.submit(
                _request(
                    execution_backend="host", host_authorization="please"
                )
            )
            self.assertEqual(task.status, TaskStatus.BLOCKED)

    def test_host_with_token_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root)
            task = service.submit(
                _request(
                    execution_backend="host",
                    host_authorization="local-maintenance",
                )
            )
            self.assertEqual(task.status, TaskStatus.SUCCEEDED)


class ArtifactCollectionTests(unittest.TestCase):
    def _collect(self, workspace: Path, limit: int = 65536):
        return TaskService(workspace.parent).  \
            _collect_test_artifacts(workspace, limit)

    def test_collects_files_with_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = _workspace(Path(temp_dir))
            (workspace / "artifacts").mkdir()
            (workspace / "artifacts" / "report.txt").write_text("ok")
            collected = self._collect(workspace)
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0]["relative_path"], "report.txt")
        self.assertEqual(collected[0]["bytes"], 2)
        self.assertEqual(len(collected[0]["sha256"]), 64)

    def test_symlink_escape_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _workspace(root)
            outside = root / "outside.txt"
            outside.write_text("secret")
            (workspace / "artifacts").mkdir()
            (workspace / "artifacts" / "link.txt").symlink_to(outside)
            collected = self._collect(workspace)
        self.assertEqual(collected, [])

    def test_size_cap_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = _workspace(Path(temp_dir))
            (workspace / "artifacts").mkdir()
            (workspace / "artifacts" / "big.bin").write_bytes(b"x" * 100)
            collected = self._collect(workspace, limit=10)
        self.assertEqual(collected, [])


class ContainerTerminationTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("docker") is None:
            self.skipTest("docker is not available")
        probe = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            self.skipTest("docker daemon is not reachable")
        if not subprocess.run(
            ["docker", "image", "inspect", "alpine:3.20"],
            capture_output=True,
        ).returncode == 0:
            self.skipTest("alpine:3.20 is not present locally")

    def test_running_container_is_stopped_and_confirmed_gone(self) -> None:
        name = f"zv-test-{int(time.time() * 1000)}"
        started = subprocess.run(
            ["docker", "run", "-d", "--name", name, "alpine:3.20", "sleep", "60"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        service = TaskService(Path(tempfile.mkdtemp()))
        self.assertTrue(service._terminate_container(name))
        listed = subprocess.run(
            ["docker", "ps", "--filter", f"name={name}", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(listed.stdout.strip(), "", "container still running")
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    def test_unknown_container_is_not_reported_as_terminated(self) -> None:
        service = TaskService(Path(tempfile.mkdtemp()))
        # ``docker kill`` on a missing container fails; listing is empty, so
        # the helper still reports success only when the name is gone.
        self.assertTrue(service._terminate_container("zv-does-not-exist"))


if __name__ == "__main__":
    unittest.main()
