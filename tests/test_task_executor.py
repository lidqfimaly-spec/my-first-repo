"""S-A: background execution, cooperative cancel, readable progress.

These tests pin the *driver* contract, not engine internals:

* a queued task reaches a terminal state without any HTTP request blocking
* two tasks run in parallel
* the same task is never executed twice
* a saturated executor refuses rather than silently dropping work
* progress text is derived from events and never invents a percentage
* cancel is reported as *requested*, never as done
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.progress_text import describe_progress  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.task_executor import TaskExecutor  # noqa: E402
from zevorinth.workbench_server import create_server  # noqa: E402


def _wait_for_status(
    project_root: Path,
    task_id: str,
    *,
    timeout: float = 20.0,
) -> TaskStatus:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        service = TaskService(project_root, recover_leases=False)
        last = service.require_task(task_id).status
        if last in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.PARTIAL,
            TaskStatus.CANCELLED,
            TaskStatus.PLANNING_FAILED,
            TaskStatus.INTERRUPTED,
        }:
            return last
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish, last status={last}")


def _write_request(name: str, content: str = "hello\n") -> TaskRequest:
    return TaskRequest(
        goal=f"写入 {name}",
        operation="write_text_artifact",
        parameters={"artifact_path": name, "content": content},
    )


class TaskExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        (self.root / "artifacts").mkdir(parents=True, exist_ok=True)
        # Initialize the store once so every TaskService sees the schema.
        TaskService(self.root, recover_leases=False)
        self.executor = TaskExecutor(self.root, workers=2)
        self.executor.start()

    def tearDown(self) -> None:
        self.executor.shutdown(timeout=10.0)
        self._tmp.cleanup()

    def _enqueue(self, name: str = "note.txt") -> str:
        service = TaskService(self.root, recover_leases=False)
        task = service.enqueue(_write_request(name))
        return task.task_id

    # ---------------------------------------------------------------- happy
    def test_a_queued_task_reaches_success_without_a_caller_waiting(self) -> None:
        task_id = self._enqueue()
        outcome = self.executor.submit(task_id)
        self.assertEqual(outcome, "queued")
        # The submit call itself must not have executed anything: right after
        # it returns the task may still be queued or already claimed, but the
        # caller never blocked until terminal.
        status = _wait_for_status(self.root, task_id)
        self.assertIs(status, TaskStatus.SUCCEEDED)
        artifact = self.root / "artifacts" / task_id / "note.txt"
        self.assertTrue(artifact.exists())
        self.assertEqual(artifact.read_text(encoding="utf-8"), "hello\n")

    def test_two_tasks_run_at_the_same_time(self) -> None:
        first = self._enqueue("a.txt")
        second = self._enqueue("b.txt")
        self.assertEqual(self.executor.submit(first), "queued")
        self.assertEqual(self.executor.submit(second), "queued")
        self.assertIs(_wait_for_status(self.root, first), TaskStatus.SUCCEEDED)
        self.assertIs(_wait_for_status(self.root, second), TaskStatus.SUCCEEDED)
        stats = self.executor.stats()
        self.assertEqual(stats.workers, 2)
        self.assertGreaterEqual(stats.completed, 2)

    # ------------------------------------------------------------ integrity
    def test_submitting_the_same_task_twice_is_refused_not_replayed(self) -> None:
        task_id = self._enqueue()
        self.assertEqual(self.executor.submit(task_id), "queued")
        self.assertEqual(self.executor.submit(task_id), "duplicate")
        self.assertIs(_wait_for_status(self.root, task_id), TaskStatus.SUCCEEDED)
        stats = self.executor.stats()
        self.assertEqual(stats.rejected_duplicate, 1)

    def test_a_full_queue_is_refused_and_the_id_is_not_leaked(self) -> None:
        blocked = threading.Event()
        release = threading.Event()

        def slow_planner():  # noqa: ANN202 - test double
            from zevorinth.planner import DeterministicPlanner

            class _Slow(DeterministicPlanner):
                def plan_request(self, request):  # noqa: ANN001, ANN201
                    blocked.set()
                    release.wait(timeout=10.0)
                    return super().plan_request(request)

            return _Slow()

        executor = TaskExecutor(
            self.root,
            workers=1,
            queue_limit=1,
            planner_factory=slow_planner,
        )
        executor.start()
        try:
            first = self._enqueue("s1.txt")
            second = self._enqueue("s2.txt")
            third = self._enqueue("s3.txt")
            self.assertEqual(executor.submit(first), "queued")
            blocked.wait(timeout=5.0)
            self.assertEqual(executor.submit(second), "queued")
            self.assertEqual(executor.submit(third), "full")
            # A refused id must not stay reserved, otherwise a later legitimate
            # submit would be reported as a duplicate forever.
            self.assertNotIn(third, executor.in_flight())
            release.set()
            self.assertIs(_wait_for_status(self.root, first), TaskStatus.SUCCEEDED)
        finally:
            release.set()
            executor.shutdown(timeout=10.0)

    def test_shutdown_is_idempotent_and_stops_accepting(self) -> None:
        self.executor.shutdown(timeout=5.0)
        self.executor.shutdown(timeout=5.0)
        task_id = self._enqueue()
        self.assertEqual(self.executor.submit(task_id), "stopped")


class ProgressTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        (self.root / "artifacts").mkdir(parents=True, exist_ok=True)
        self.service = TaskService(self.root, recover_leases=False)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_queued_task_reports_queueing_not_a_percentage(self) -> None:
        task = self.service.enqueue(_write_request("q.txt"))
        progress = describe_progress(task, [])
        self.assertEqual(progress["status"], "queued")
        self.assertFalse(progress["terminal"])
        self.assertNotIn("%", progress["text"])
        self.assertEqual(progress["steps_done"], 0)

    def test_a_finished_task_reports_a_terminal_sentence(self) -> None:
        task = self.service.submit(_write_request("done.txt"))
        events = self.service.store.list_events(task.task_id)
        progress = describe_progress(task, events)
        self.assertEqual(progress["status"], "succeeded")
        self.assertTrue(progress["terminal"])
        self.assertEqual(progress["steps_total"], len(task.plan.steps))
        self.assertEqual(progress["steps_done"], progress["steps_total"])
        self.assertNotIn("%", progress["text"])

    def test_a_cancel_request_is_surfaced_without_claiming_completion(self) -> None:
        task = self.service.enqueue(_write_request("c.txt"))
        updated = self.service.request_cancel(
            task.task_id,
            requested_by="test",
            reason="user changed their mind",
        )
        progress = describe_progress(
            updated, self.service.store.list_events(task.task_id)
        )
        self.assertTrue(progress["cancel_requested"])
        self.assertIn("取消", progress["text"])
        # The engine has not reached a checkpoint, so it is not cancelled yet.
        self.assertFalse(progress["terminal"])

    def test_a_policy_rejection_is_reported_as_blocked_with_a_reason(self) -> None:
        # FACT about the engine: a policy rejection does NOT produce FAILED.
        # It produces BLOCKED + error.code, because the task stays resumable
        # with a corrected input. Progress must say that, not "被阻塞".
        task = self.service.submit(
            TaskRequest(
                goal="写入路径越界",
                operation="write_text_artifact",
                parameters={"artifact_path": "../escape.txt", "content": "x"},
            )
        )
        self.assertIs(task.status, TaskStatus.BLOCKED)
        progress = describe_progress(
            task, self.service.store.list_events(task.task_id)
        )
        self.assertEqual(progress["status"], "blocked")
        self.assertFalse(progress["terminal"])
        self.assertIn("安全检查", progress["text"])
        self.assertIn("相对路径", progress["text"])
        self.assertNotIn("invalid_artifact_path", progress["text"])

    def test_terminal_failure_text_uses_user_language(self) -> None:
        from zevorinth.models import Task, TaskError

        task = Task(
            task_id=str(uuid.uuid4()),
            status=TaskStatus.FAILED,
            request=_write_request("x.txt"),
            execution_mode="not_executed",
            attempt_id=str(uuid.uuid4()),
            phase="failed",
            planner_mode="deterministic",
            created_at="2026-11-22T00:00:00+00:00",
            updated_at="2026-11-22T00:00:00+00:00",
            error=TaskError(
                "text_match_count", "old_text must match exactly once"
            ),
        )
        progress = describe_progress(task, [])
        self.assertTrue(progress["terminal"])
        self.assertIn("失败", progress["text"])
        self.assertIn("恰好出现一次", progress["text"])
        self.assertNotIn("text_match_count", progress["text"])


class DispatchEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.server = create_server(
            self.root, port=0, control_enabled=True
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"
        self.origin = self.base
        session = self._get("/control/api/session")
        self.token = session["token"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)
        self._tmp.cleanup()

    # ---------------------------------------------------------------- utils
    def _get(self, path: str) -> dict:
        request = Request(self.base + path, method="GET")
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post(
        self, path: str, body: dict, *, expect: int = 200
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8")
        request = Request(
            self.base + path,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": self.origin,
                "X-Zevorinth-Control-Token": self.token,
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def _create_task(self, name: str = "note.txt") -> tuple[str, str]:
        request_id = str(uuid.uuid4())
        status, payload = self._post(
            "/control/api/tasks",
            {
                "request_id": request_id,
                "confirmed": True,
                "goal": f"写入 {name}",
                "operation": "write_text_artifact",
                "parameters": {"artifact_path": name, "content": "hi\n"},
            },
        )
        self.assertIn(status, {200, 201}, payload)
        return payload["task_id"], request_id

    # ---------------------------------------------------------------- tests
    def test_dispatch_returns_immediately_and_the_task_finishes(self) -> None:
        task_id, _ = self._create_task()
        started = time.monotonic()
        status, payload = self._post(
            f"/control/api/tasks/{task_id}/dispatch",
            {"request_id": str(uuid.uuid4())},
        )
        elapsed = time.monotonic() - started
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["status"], "dispatched")
        # The whole point of S-A: the response must not wait for execution.
        self.assertLess(elapsed, 2.0)
        self.assertEqual(
            self._wait_http_terminal(task_id), "succeeded"
        )

    def test_dispatching_a_non_queued_task_is_a_conflict(self) -> None:
        task_id, _ = self._create_task()
        self._post(
            f"/control/api/tasks/{task_id}/dispatch",
            {"request_id": str(uuid.uuid4())},
        )
        self._wait_http_terminal(task_id)
        status, payload = self._post(
            f"/control/api/tasks/{task_id}/dispatch",
            {"request_id": str(uuid.uuid4())},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "task_not_startable")

    def test_progress_endpoint_is_readable_and_honest(self) -> None:
        task_id, _ = self._create_task()
        payload = self._get(f"/api/tasks/{task_id}/progress")
        self.assertEqual(payload["task_id"], task_id)
        self.assertEqual(payload["steps_done"], 0)
        self.assertNotIn("%", payload["text"])
        self._post(
            f"/control/api/tasks/{task_id}/dispatch",
            {"request_id": str(uuid.uuid4())},
        )
        self._wait_http_terminal(task_id)
        payload = self._get(f"/api/tasks/{task_id}/progress")
        self.assertTrue(payload["terminal"])
        self.assertEqual(payload["steps_done"], payload["steps_total"])

    def test_cancel_request_is_cooperative_and_reported_as_requested(self) -> None:
        task_id, _ = self._create_task()
        # A queued task that nobody drove yet: the request is accepted and the
        # status stays queued — the engine cancels at its 'claim' checkpoint.
        status, payload = self._post(
            f"/control/api/tasks/{task_id}/cancel-request",
            {"request_id": str(uuid.uuid4()), "reason": "not needed"},
        )
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["status"], "cancel_requested")
        self.assertIn("checkpoint", payload["note"])
        self.assertNotEqual(payload["status"], "cancelled")

    def test_cancel_after_finishing_is_refused(self) -> None:
        task_id, _ = self._create_task()
        self._post(
            f"/control/api/tasks/{task_id}/dispatch",
            {"request_id": str(uuid.uuid4())},
        )
        self._wait_http_terminal(task_id)
        status, payload = self._post(
            f"/control/api/tasks/{task_id}/cancel-request",
            {"request_id": str(uuid.uuid4())},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "task_already_finished")

    def test_dispatch_requires_a_canonical_request_id(self) -> None:
        task_id, _ = self._create_task()
        status, payload = self._post(
            f"/control/api/tasks/{task_id}/dispatch",
            {"request_id": "not-a-uuid"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_action_request_id")

    def _wait_http_terminal(self, task_id: str, timeout: float = 20.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            detail = self._get(f"/api/tasks/{task_id}")
            status = detail["task"]["status"]
            if status in {
                "succeeded",
                "failed",
                "partial",
                "cancelled",
                "planning_failed",
                "interrupted",
            }:
                return status
            time.sleep(0.05)
        raise AssertionError(f"task {task_id} did not finish")


if __name__ == "__main__":
    unittest.main()
