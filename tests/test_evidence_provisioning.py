"""Provisioning: build, store and consume evidence for a real container run."""

from __future__ import annotations

import shutil
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.evidence_provisioning import provision_evidence  # noqa: E402
from zevorinth.isolation import _live_image_digest  # noqa: E402
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import RUN_TESTS  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


IMAGE = "python:3.13-alpine"


def _ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _live_image_digest(IMAGE).startswith("python@")
    except Exception:
        return False


class ProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _ready():
            self.skipTest("python:3.13-alpine not available")

    def test_provisioned_evidence_authorizes_real_container_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "test_ok.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n"
                "    def test_ok(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            service = TaskService(root)
            queued = service.enqueue(
                TaskRequest(
                    goal="run",
                    operation=RUN_TESTS,
                    parameters={
                        "runner": "unittest",
                        "target": "test_ok",
                        "workspace": "demo",
                        "execution_backend": "container",
                        "image": IMAGE,
                    },
                )
            )
            provisioned = provision_evidence(
                root,
                image=IMAGE,
                workspace_name="demo",
                target="test_ok",
                request_id=queued.task_id,
                evidence_id="provisioned-1",
            )
            self.assertEqual(provisioned["observed"]["read_outside"], "denied")
            queued.request.parameters["evidence_id"] = "provisioned-1"
            service.store.record_event_and_update(
                queued.task_id,
                "test_fixture",
                "2026-10-20T00:00:00.000+00:00",
                updates={"request": queued.request},
            )
            result = service.execute_task(queued.task_id)
            self.assertEqual(result.status, TaskStatus.SUCCEEDED, result.error)
            self.assertEqual(
                result.tool_result.test_evidence.exit_code, 0
            )

    def test_provisioning_rejects_wrong_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "workspaces" / "demo").mkdir(parents=True)
            with self.assertRaises(Exception):
                provision_evidence(
                    root,
                    image="python:does-not-exist",
                    workspace_name="demo",
                    target="test_ok",
                    request_id="t",
                    evidence_id="e",
                )


if __name__ == "__main__":
    unittest.main()
