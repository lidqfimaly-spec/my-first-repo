from __future__ import annotations

import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import RealArtifactWriter  # noqa: E402
from zevorinth.models import (  # noqa: E402
    ExecutionMode,
    Task,
    TaskRequest,
    TaskStatus,
    ToolResult,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.verifier import ArtifactVerifier  # noqa: E402


class NoWriteWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self) -> None:
        self.calls = 0

    def write(self, **_: Any) -> ToolResult:
        self.calls += 1
        raise AssertionError("concurrent recovery must not rewrite the artifact")


class ProcessBlockingWriter(RealArtifactWriter):
    def __init__(self, artifacts_root: Path, entered: Any, release: Any) -> None:
        super().__init__(artifacts_root)
        self.entered = entered
        self.release = release
        self.calls = 0

    def write(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("parent did not release blocked writer")
        return super().write(**kwargs)


class ProcessBlockingVerifier:
    name = "blocking-test-verifier"

    def __init__(self, artifacts_root: Path, entered: Any, release: Any) -> None:
        self.entered = entered
        self.release = release
        self.real = ArtifactVerifier(artifacts_root)

    def verify(self, **kwargs: Any):
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("parent did not release blocked verifier")
        return self.real.verify(**kwargs)


def _execute_worker(
    root_text: str,
    task_id: str,
    result_queue: Any,
    *,
    owner_id: str,
    block_write: bool = False,
    block_verify: bool = False,
    entered: Any = None,
    release: Any = None,
    gate: Any = None,
) -> None:
    root = Path(root_text)
    writer: Any = NoWriteWriter() if block_verify else None
    if block_write:
        writer = ProcessBlockingWriter(root / "artifacts", entered, release)
    service = TaskService(root, writer=writer, owner_id=owner_id)
    if block_verify:
        service.verifier = ProcessBlockingVerifier(
            root / "artifacts", entered, release
        )
    try:
        if gate is not None:
            gate.wait(timeout=10)
        task = service.execute_task(task_id)
        result_queue.put(
            {
                "ok": True,
                "status": task.status.value,
                "writer_calls": getattr(service.writer, "calls", 0),
            }
        )
    except BaseException as exc:
        service = locals().get("service")
        result_queue.put(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "writer_calls": (
                    getattr(service.writer, "calls", 0)
                    if service is not None
                    else 0
                ),
            }
        )


def _resume_worker(
    root_text: str,
    task_id: str,
    result_queue: Any,
    *,
    owner_id: str,
    block_verify: bool,
    entered: Any,
    release: Any,
) -> None:
    root = Path(root_text)
    writer = NoWriteWriter()
    service = TaskService(root, writer=writer, owner_id=owner_id)
    if block_verify:
        service.verifier = ProcessBlockingVerifier(
            root / "artifacts", entered, release
        )
    try:
        task = service.resume(task_id)
        result_queue.put(
            {
                "ok": True,
                "status": task.status.value,
                "writer_calls": writer.calls,
            }
        )
    except BaseException as exc:
        result_queue.put(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "writer_calls": writer.calls,
            }
        )


def _request(path: str, content: str) -> TaskRequest:
    return TaskRequest(
        goal="concurrency proof",
        operation=WRITE_TEXT_ARTIFACT,
        parameters={"artifact_path": path, "content": content},
    )


class ConcurrencyTests(unittest.TestCase):
    def test_c1_same_task_has_one_process_owner_and_one_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed = TaskService(root)
            task = seed.enqueue(_request("shared.txt", "single owner\n"))

            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            entered = context.Event()
            release = context.Event()
            first = context.Process(
                target=_execute_worker,
                args=(str(root), task.task_id, queue),
                kwargs={
                    "owner_id": "owner-a",
                    "block_write": True,
                    "entered": entered,
                    "release": release,
                },
            )
            second: Any = None
            try:
                first.start()
                self.assertTrue(entered.wait(timeout=10))
                second = context.Process(
                    target=_execute_worker,
                    args=(str(root), task.task_id, queue),
                    kwargs={"owner_id": "owner-b"},
                )
                second.start()
                rejected = queue.get(timeout=10)
                self.assertTrue(rejected["ok"], rejected)
                self.assertEqual(rejected["status"], "running")
                self.assertEqual(rejected["writer_calls"], 0)

                release.set()
                first.join(timeout=10)
                second.join(timeout=10)
                winner = queue.get(timeout=10)
                self.assertTrue(winner["ok"], winner)
                self.assertEqual(winner["status"], "succeeded")
                self.assertEqual(winner["writer_calls"], 1)
            finally:
                release.set()
                for process in (first, second):
                    if process is not None and process.is_alive():
                        process.kill()
                    if process is not None:
                        process.join(timeout=5)
                queue.close()
                queue.join_thread()

            final = TaskService(root).require_task(task.task_id)
            self.assertEqual(final.status, TaskStatus.SUCCEEDED)
            self.assertEqual(final.claim_generation, 1)
            self.assertIsNone(final.lease_token)
            events = [event.event_type for event in TaskService(root).events(task.task_id)]
            self.assertEqual(events.count("task_claimed"), 1)
            self.assertEqual(events.count("claim_rejected"), 1)
            self.assertEqual(events.count("task_succeeded"), 1)
            self.assertEqual(
                (root / "artifacts" / task.task_id / "shared.txt").read_text(),
                "single owner\n",
            )

    def test_c2_recovery_and_normal_execution_contend_for_one_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_ids: list[str] = []

            def interrupt(_: str, task: Task) -> None:
                task_ids.append(task.task_id)
                raise RuntimeError("interrupt before verification")

            base_time = "2026-10-19T00:00:00.000+00:00"
            recovery_time = "2026-10-19T00:01:01.000+00:00"
            crashing = TaskService(
                root,
                fault_hook=interrupt,
                clock=lambda: base_time,
            )
            with self.assertRaises(RuntimeError):
                crashing.submit(_request("recovery.txt", "recover once\n"))
            task_id = task_ids[0]

            recovered = TaskService(
                root,
                owner_id="observer",
                clock=lambda: recovery_time,
            )
            self.assertEqual(recovered.require_task(task_id).status, TaskStatus.INTERRUPTED)

            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            entered = context.Event()
            release = context.Event()
            resumer = context.Process(
                target=_resume_worker,
                args=(str(root), task_id, queue),
                kwargs={
                    "owner_id": "resumer",
                    "block_verify": True,
                    "entered": entered,
                    "release": release,
                },
            )
            executor: Any = None
            try:
                resumer.start()
                self.assertTrue(entered.wait(timeout=10))
                executor = context.Process(
                    target=_execute_worker,
                    args=(str(root), task_id, queue),
                    kwargs={"owner_id": "executor"},
                )
                executor.start()
                rejected = queue.get(timeout=10)
                self.assertTrue(rejected["ok"], rejected)
                self.assertEqual(rejected["writer_calls"], 0)

                release.set()
                resumer.join(timeout=10)
                executor.join(timeout=10)
                resumed = queue.get(timeout=10)
                self.assertTrue(resumed["ok"], resumed)
                self.assertEqual(resumed["status"], "succeeded")
                self.assertEqual(resumed["writer_calls"], 0)
            finally:
                release.set()
                for process in (resumer, executor):
                    if process is not None and process.is_alive():
                        process.kill()
                    if process is not None:
                        process.join(timeout=5)
                queue.close()
                queue.join_thread()

            final = TaskService(root).require_task(task_id)
            self.assertEqual(final.status, TaskStatus.SUCCEEDED)
            events = [event.event_type for event in TaskService(root).events(task_id)]
            self.assertEqual(events.count("task_succeeded"), 1)
            self.assertGreaterEqual(events.count("claim_rejected"), 1)

    def test_c3_different_tasks_run_concurrently_without_cross_linking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed = TaskService(root)
            task_a = seed.enqueue(_request("a.txt", "task A"))
            task_b = seed.enqueue(_request("b.txt", "task B"))

            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            gate = context.Barrier(2)
            processes = [
                context.Process(
                    target=_execute_worker,
                    args=(str(root), task.task_id, queue),
                    kwargs={"owner_id": owner_id, "gate": gate},
                )
                for task, owner_id in ((task_a, "owner-a"), (task_b, "owner-b"))
            ]
            try:
                for process in processes:
                    process.start()
                results = [queue.get(timeout=15), queue.get(timeout=15)]
                for process in processes:
                    process.join(timeout=10)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.kill()
                    process.join(timeout=5)
                queue.close()
                queue.join_thread()

            self.assertTrue(all(result["ok"] for result in results), results)
            self.assertTrue(all(result["status"] == "succeeded" for result in results))
            self.assertTrue((root / "artifacts" / task_a.task_id / "a.txt").is_file())
            self.assertTrue((root / "artifacts" / task_b.task_id / "b.txt").is_file())
            self.assertEqual(
                (root / "artifacts" / task_a.task_id / "a.txt").read_text(),
                "task A",
            )
            self.assertEqual(
                (root / "artifacts" / task_b.task_id / "b.txt").read_text(),
                "task B",
            )
            final_a = TaskService(root).require_task(task_a.task_id)
            final_b = TaskService(root).require_task(task_b.task_id)
            self.assertEqual(final_a.status, TaskStatus.SUCCEEDED)
            self.assertEqual(final_b.status, TaskStatus.SUCCEEDED)
            self.assertNotEqual(final_a.artifact.sha256, final_b.artifact.sha256)
            self.assertTrue(
                all(
                    event.task_id == task_a.task_id
                    for event in TaskService(root).events(task_a.task_id)
                )
            )
            self.assertTrue(
                all(
                    event.task_id == task_b.task_id
                    for event in TaskService(root).events(task_b.task_id)
                )
            )


if __name__ == "__main__":
    unittest.main()
