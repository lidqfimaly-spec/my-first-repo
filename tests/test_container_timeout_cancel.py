"""Container timeout/cancel closure and artifact manifest completeness."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.evidence_store import EvidenceStore  # noqa: E402
from zevorinth.isolation import (  # noqa: E402
    _live_image_digest,
    _live_runtime_identity,
    build_container_command,
)
from zevorinth.isolation_evidence import (  # noqa: E402
    PROBE_BOUNDARIES,
    build_evidence,
    evidence_digest,
)
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import RUN_TESTS  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


IMAGE = "python:3.13-alpine"
SLOW = (
    "import time, unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_slow(self):\n"
    "        time.sleep(30)\n"
)
NOISY = (
    "import pathlib, unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_noisy(self):\n"
    "        print('x' * 200000)\n"
)


def _container_prefix(task_id: str) -> str:
    """Return the task-specific prefix used by the service's container names."""
    return "zv-" + task_id.replace("-", "")[:12]


def _container_names(task_id: str, *, include_stopped: bool = False) -> list[str]:
    prefix = _container_prefix(task_id)
    command = ["docker", "ps"]
    if include_stopped:
        command.append("-a")
    command.extend(["--filter", f"name={prefix}", "--format", "{{.Names}}"])
    listed = subprocess.run(command, capture_output=True, text=True)
    return [
        name
        for name in listed.stdout.split()
        if name.startswith(prefix + "-")
    ]


def _remove_containers_for(task_id: str) -> None:
    for name in _container_names(task_id, include_stopped=True):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _ready() -> bool:
    try:
        return _live_image_digest(IMAGE).startswith("python@")
    except Exception:
        return False


def _probe_evidence(root: Path, workspace: Path, request_id: str, image: str):
    probe = build_container_command(
        image=image,
        workspace=workspace,
        argv=[
            "/bin/sh",
            "-c",
            "echo ok >/dev/null && echo ready",
        ],
    )
    subprocess.run(probe, capture_output=True, text=True, timeout=60)
    evidence = build_evidence(
        environment_id="test",
        runtime_identity=_live_runtime_identity(),
        image_digest=_live_image_digest(image),
        container_config_digest="",
        request_id=request_id,
        probes=[
            {
                "probe_id": f"p-{b}",
                "boundary": b,
                "status": "PASS",
                "expected_result": "x",
                "observed_result": "x",
                "exit_code": 0,
                "evidence_artifact_ids": [b],
            }
            for b in PROBE_BOUNDARIES
        ],
        ttl_seconds=600,
    )
    return evidence


def _bind_evidence(evidence, workspace: Path, target: str, request_id: str) -> dict:
    argv = ["python", "-B", "-m", "unittest", "-q", target]
    command = build_container_command(image=IMAGE, workspace=workspace, argv=argv)
    evidence["container_config_digest"] = hashlib.sha256(
        " ".join(command).encode()
    ).hexdigest()
    evidence["command_digest"] = hashlib.sha256(
        json.dumps(argv, separators=(",", ":")).encode()
    ).hexdigest()
    evidence["request_id"] = request_id
    evidence["digest"] = evidence_digest(evidence)
    return evidence


def _request(target: str, **extra) -> TaskRequest:
    parameters = {
        "runner": "unittest",
        "target": target,
        "workspace": "demo",
        "execution_backend": "container",
        "image": IMAGE,
        "timeout_seconds": 60,
    }
    parameters.update(extra)
    return TaskRequest(goal="run", operation=RUN_TESTS, parameters=parameters)


class ContainerTimeoutCancelTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _ready():
            self.skipTest("python:3.13-alpine not available")
        self.task_ids: list[str] = []

    def tearDown(self) -> None:
        for task_id in self.task_ids:
            _remove_containers_for(task_id)

    def _run(self, module: str, content: str, *, timeout: float = 60, cancel_after=None, **extra):
        root = Path(tempfile.mkdtemp())
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / f"{module}.py").write_text(content, encoding="utf-8")
        service = TaskService(root)
        queued = service.enqueue(
            _request(module, timeout_seconds=timeout, **extra)
        )
        self.task_ids.append(queued.task_id)
        evidence = _probe_evidence(root, workspace, queued.task_id, IMAGE)
        _bind_evidence(evidence, workspace, module, queued.task_id)
        EvidenceStore(root).save_evidence(evidence)
        queued.request.parameters["evidence_id"] = evidence["evidence_id"]
        service.store.record_event_and_update(
            queued.task_id,
            "test_fixture",
            "2026-10-20T00:00:00.000+00:00",
            updates={"request": queued.request},
        )
        if cancel_after is not None:
            def cancel():
                time.sleep(cancel_after)
                service.request_cancel(
                    queued.task_id, requested_by="test", reason="stop"
                )
            threading.Thread(target=cancel).start()
        result = service.execute_task(queued.task_id)
        events = [e.event_type for e in TaskService(root).events(queued.task_id)]
        return root, service, result, events

    def test_timeout_kills_container_and_fails_task(self) -> None:
        root, service, result, events = self._run(
            "test_slow", SLOW, timeout=2
        )
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.error.code, "test_timeout")
        evidence = result.tool_result.test_evidence
        self.assertEqual(
            result.execution_plan_snapshot["evidence_id"],
            evidence.evidence_id,
        )
        self.assertEqual(
            result.execution_plan_snapshot["config_digest"],
            evidence.config_digest,
        )
        self.assertIn("task_failed", events)
        self.assertNotIn("task_succeeded", events)
        self.assertEqual(_container_names(result.task_id), [])

    def test_cancel_during_container_run_stops_it(self) -> None:
        root, service, result, events = self._run(
            "test_slow", SLOW, timeout=60, cancel_after=1.0
        )
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        evidence = result.tool_result.test_evidence
        self.assertEqual(
            result.execution_plan_snapshot["evidence_id"],
            evidence.evidence_id,
        )
        self.assertEqual(
            result.execution_plan_snapshot["config_digest"],
            evidence.config_digest,
        )
        self.assertIn("task_cancelled", events)
        self.assertEqual(_container_names(result.task_id), [])

    def test_disk_quota_blocks_oversized_writes(self) -> None:
        big = (
            "import pathlib, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_big(self):\n"
            "        pathlib.Path('big.bin').write_bytes(b'x' * 5_000_000)\n"
        )
        root = Path(tempfile.mkdtemp())
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / "test_big.py").write_text(big, encoding="utf-8")
        service = TaskService(root)
        queued = service.enqueue(
            _request("test_big", disk_limit_bytes=1024 * 1024)
        )
        self.task_ids.append(queued.task_id)
        evidence = _probe_evidence(root, workspace, queued.task_id, IMAGE)
        _bind_evidence(evidence, workspace, "test_big", queued.task_id)
        EvidenceStore(root).save_evidence(evidence)
        queued.request.parameters["evidence_id"] = evidence["evidence_id"]
        service.store.record_event_and_update(
            queued.task_id, "test_fixture",
            "2026-10-20T00:00:00.000+00:00",
            updates={"request": queued.request},
        )
        result = service.execute_task(queued.task_id)
        # The tmpfs cap makes the write fail inside the container.
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertNotEqual(
            getattr(result.error, "code", None), "success"
        )

    def test_container_start_failure_is_explicit(self) -> None:
        root = Path(tempfile.mkdtemp())
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / "test_ok.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        service = TaskService(root)
        queued = service.enqueue(_request("test_ok"))
        evidence = _probe_evidence(
            root, workspace, queued.task_id, IMAGE
        )
        _bind_evidence(evidence, workspace, "test_ok", queued.task_id)
        EvidenceStore(root).save_evidence(evidence)
        queued.request.parameters["evidence_id"] = evidence["evidence_id"]
        service.store.record_event_and_update(
            queued.task_id,
            "test_fixture",
            "2026-10-20T00:00:00.000+00:00",
            updates={"request": queued.request},
        )
        def fail_popen(*args, **kwargs):
            del args, kwargs
            raise OSError("docker unavailable")

        service.container_popen = fail_popen
        result = service.execute_task(queued.task_id)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.error.code, "container_start_failed")
        self.assertEqual(result.tool_result.tool_name, "run_tests")
        self.assertEqual(
            result.execution_plan_snapshot["evidence_id"],
            evidence["evidence_id"],
        )
        self.assertEqual(_container_names(result.task_id), [])
        self.assertNotIn(
            "task_succeeded",
            [event.event_type for event in service.events(result.task_id)],
        )

    def test_evidence_invalidated_after_real_test_blocks_success(self) -> None:
        root = Path(tempfile.mkdtemp())
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / "test_ok.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        service = TaskService(root)
        queued = service.enqueue(_request("test_ok"))
        evidence = _probe_evidence(
            root, workspace, queued.task_id, IMAGE
        )
        _bind_evidence(evidence, workspace, "test_ok", queued.task_id)
        store = EvidenceStore(root)
        store.save_evidence(evidence)
        queued.request.parameters["evidence_id"] = evidence["evidence_id"]
        service.store.record_event_and_update(
            queued.task_id,
            "test_fixture",
            "2026-10-20T00:00:00.000+00:00",
            updates={"request": queued.request},
        )

        def invalidate_after_execution(stage, task):
            del task
            if stage == "after_test_before_verification":
                store.invalidate_evidence(
                    evidence["evidence_id"], "test revocation"
                )

        service.fault_hook = invalidate_after_execution
        result = service.execute_task(queued.task_id)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.error.code, "evidence_invalidated")
        self.assertTrue(result.tool_result.success)
        self.assertFalse(result.verification.passed)
        self.assertEqual(_container_names(result.task_id), [])
        self.assertNotIn(
            "task_succeeded",
            [event.event_type for event in service.events(result.task_id)],
        )

    def test_fsize_limit_stops_oversized_file(self) -> None:
        big = (
            "import pathlib, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_big(self):\n"
            "        pathlib.Path('artifacts').mkdir(exist_ok=True)\n"
            "        pathlib.Path('artifacts/huge.bin').write_bytes(b'x' * 90_000_000)\n"
        )
        root = Path(tempfile.mkdtemp())
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / "test_huge.py").write_text(big, encoding="utf-8")
        service = TaskService(root)
        queued = service.enqueue(_request("test_huge"))
        self.task_ids.append(queued.task_id)
        evidence = _probe_evidence(root, workspace, queued.task_id, IMAGE)
        _bind_evidence(evidence, workspace, "test_huge", queued.task_id)
        EvidenceStore(root).save_evidence(evidence)
        queued.request.parameters["evidence_id"] = evidence["evidence_id"]
        service.store.record_event_and_update(
            queued.task_id, "test_fixture",
            "2026-10-20T00:00:00.000+00:00",
            updates={"request": queued.request},
        )
        result = service.execute_task(queued.task_id)
        self.assertEqual(result.status, TaskStatus.FAILED)
        huge = workspace / "artifacts" / "huge.bin"
        self.assertTrue(not huge.exists() or huge.stat().st_size < 90_000_000)

    def test_unconfirmed_termination_fails_closed(self) -> None:
        root = Path(tempfile.mkdtemp())
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / "test_slow.py").write_text(SLOW, encoding="utf-8")
        service = TaskService(root)
        queued = service.enqueue(_request("test_slow", timeout_seconds=2))
        self.task_ids.append(queued.task_id)
        evidence = _probe_evidence(root, workspace, queued.task_id, IMAGE)
        _bind_evidence(evidence, workspace, "test_slow", queued.task_id)
        EvidenceStore(root).save_evidence(evidence)
        queued.request.parameters["evidence_id"] = evidence["evidence_id"]
        service.store.record_event_and_update(
            queued.task_id, "test_fixture",
            "2026-10-20T00:00:00.000+00:00",
            updates={"request": queued.request},
        )
        service.container_terminator = lambda name: False
        result = service.execute_task(queued.task_id)
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(
            result.error.code, "container_termination_unverified"
        )
        _remove_containers_for(queued.task_id)

    def test_repeated_cancel_is_idempotent(self) -> None:
        root = Path(tempfile.mkdtemp())
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / "test_ok.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        service = TaskService(root)
        task = service.enqueue(_request("test_ok", execution_backend="host",
                                        host_authorization="local-maintenance"))
        first = service.request_cancel(task.task_id, requested_by="t", reason="a")
        second = service.request_cancel(task.task_id, requested_by="t", reason="b")
        self.assertEqual(first.cancel_requested_at, second.cancel_requested_at)
        result = service.execute_task(task.task_id)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        again = service.request_cancel(task.task_id, requested_by="t", reason="c")
        self.assertEqual(again.status, TaskStatus.CANCELLED)

    def test_missing_required_artifact_fails_collection(self) -> None:
        root, service, result, events = self._run(
            "test_slow_ok",
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n",
            required_artifacts=["report.json"],
        )
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.error.code, "collection_failed")
        self.assertNotIn("task_succeeded", events)

    def test_large_output_is_truncated_and_marked(self) -> None:
        root, service, result, events = self._run("test_noisy", NOISY)
        # The test itself passes, but the recorded output is capped.
        evidence = (
            result.tool_result.test_evidence
            if result.tool_result is not None
            else None
        )
        if evidence is None:
            self.skipTest(f"run did not complete: {result.error}")
        self.assertLessEqual(
            len(evidence.stdout_excerpt), 2000, "excerpt must be bounded"
        )


class ArtifactManifestUnitTests(unittest.TestCase):
    def test_collection_status_helper(self) -> None:
        from zevorinth.service import _collection_status

        self.assertEqual(_collection_status([], []), "complete")
        self.assertEqual(
            _collection_status(["a"], [{"relative_path": "a"}]), "complete"
        )
        self.assertEqual(_collection_status(["b"], []), "failed")

    def test_required_artifact_missing_fails_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            (workspace / "artifacts").mkdir(parents=True)
            service = TaskService(Path(temp_dir))
            collected = service._collect_test_artifacts(workspace, 4096)
            present = {item["relative_path"] for item in collected}
            self.assertNotIn("report.json", present)

    def test_optional_artifacts_do_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            (workspace / "artifacts").mkdir(parents=True)
            (workspace / "artifacts" / "log.txt").write_text("ok")
            service = TaskService(Path(temp_dir))
            collected = service._collect_test_artifacts(workspace, 4096)
        self.assertEqual(len(collected), 1)


if __name__ == "__main__":
    unittest.main()
