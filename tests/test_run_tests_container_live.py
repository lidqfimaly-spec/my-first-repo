"""Live end-to-end run_tests through the verified container backend."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.evidence_store import EvidenceStore  # noqa: E402
from zevorinth.isolation import (  # noqa: E402
    _live_image_digest,
    _live_runtime_identity,
    assert_container_command_safe,
    build_container_command,
)
from zevorinth.isolation_evidence import (  # noqa: E402
    PROBE_BOUNDARIES,
    build_evidence,
)
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import RUN_TESTS  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


IMAGE = "python:3.13-alpine"
BOUNDARY_PROBE = r"""
r(){ printf '%s=%s\n' "$1" "$2"; }
cat /workspace/inside.txt >/dev/null 2>&1 && r read_inside allowed || r read_inside failed
echo ok > /workspace/written.txt 2>/dev/null && r write_inside allowed || r write_inside failed
cat /host-outside-decoy.txt >/dev/null 2>&1 && r read_outside leaked || r read_outside denied
echo x > /outside.txt 2>/dev/null && r write_outside leaked || r write_outside denied
wget -T 2 -q -O /dev/null http://1.1.1.1 >/dev/null 2>&1 && r network allowed || r network denied
cat /root/.aws/credentials >/dev/null 2>&1 && r credential leaked || r credential denied
sh -c 'exit 0' >/dev/null 2>&1 && r subprocess allowed || r subprocess denied
"""
EXPECTED = {
    "read_inside": "allowed",
    "write_inside": "allowed",
    "read_outside": "denied",
    "write_outside": "denied",
    "network": "denied",
    "credential": "denied",
    "subprocess": "allowed",
}
PASSING = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
FAILING = "import unittest\nclass T(unittest.TestCase):\n    def test_no(self):\n        self.assertEqual(1, 2)\n"


def _ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return _live_image_digest(IMAGE).startswith("python@")


def _request(target: str, evidence_id: str) -> TaskRequest:
    return TaskRequest(
        goal="run tests in container",
        operation=RUN_TESTS,
        parameters={
            "runner": "unittest",
            "target": target,
            "workspace": "demo",
            "execution_backend": "container",
            "image": IMAGE,
            "evidence_id": evidence_id,
            "timeout_seconds": 60,
        },
    )


class LiveContainerRunTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _ready():
            self.skipTest("no local python:3.13-alpine image or docker runtime")

    def _prepare(self, root: Path, module: str, content: str):
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / f"{module}.py").write_text(content, encoding="utf-8")
        (workspace / "inside.txt").write_text("inside\n", encoding="utf-8")
        (root / "host-outside-decoy.txt").write_text("host\n", encoding="utf-8")
        return workspace

    def _store_evidence(self, root: Path, workspace: Path, request_id: str) -> str:
        probe_cmd = build_container_command(
            image=IMAGE, workspace=workspace, argv=["/bin/sh", "-c", BOUNDARY_PROBE]
        )
        assert_container_command_safe(probe_cmd)
        run = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=120)
        observed = dict(
            line.split("=", 1) for line in run.stdout.splitlines() if "=" in line
        )
        self.assertEqual(observed, EXPECTED, run.stderr)
        evidence_id = "live-evidence-1"
        evidence = build_evidence(
            environment_id="live",
            runtime_identity=_live_runtime_identity(),
            image_digest=_live_image_digest(IMAGE),
            container_config_digest="",
            request_id=request_id,
            probes=[
                {
                    "probe_id": f"p-{b}",
                    "boundary": b,
                    "status": "PASS",
                    "expected_result": EXPECTED[b],
                    "observed_result": observed[b],
                    "exit_code": run.returncode,
                    "evidence_artifact_ids": [b],
                }
                for b in PROBE_BOUNDARIES
            ],
            evidence_id=evidence_id,
            ttl_seconds=600,
        )
        return evidence_id, evidence

    def test_passing_and_failing_targets_run_in_container(self) -> None:
        for name, content, expected_status in (
            ("test_ok", PASSING, TaskStatus.SUCCEEDED),
            ("test_bad", FAILING, TaskStatus.FAILED),
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                workspace = self._prepare(root, name, content)
                service = TaskService(root)
                queued = service.enqueue(_request(name, ""))
                request_id = queued.task_id
                target = str(name)
                argv = ["python", "-B", "-m", "unittest", "-q", target]
                config = hashlib.sha256(
                    " ".join(
                        build_container_command(
                            image=IMAGE, workspace=workspace, argv=argv
                        )
                    ).encode("utf-8")
                ).hexdigest()
                command = hashlib.sha256(
                    json.dumps(argv, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                evidence_id, evidence = self._store_evidence(
                    root, workspace, request_id
                )
                evidence["container_config_digest"] = config
                evidence["command_digest"] = command
                from zevorinth.isolation_evidence import evidence_digest

                evidence["digest"] = evidence_digest(evidence)
                store = EvidenceStore(root)
                store.save_evidence(evidence)

                queued.request.parameters["evidence_id"] = evidence_id
                service.store.record_event_and_update(
                    queued.task_id, "test_fixture", "2026-10-20T00:00:00.000+00:00",
                    updates={"request": queued.request},
                )
                result = service.execute_task(queued.task_id)
                self.assertEqual(result.status, expected_status, result.error)
                if expected_status is TaskStatus.SUCCEEDED:
                    self.assertEqual(
                        result.tool_result.test_evidence.exit_code, 0
                    )


if __name__ == "__main__":
    unittest.main()
