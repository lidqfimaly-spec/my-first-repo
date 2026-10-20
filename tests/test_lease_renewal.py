from __future__ import annotations

import multiprocessing
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import RealArtifactWriter  # noqa: E402
from zevorinth.artifact_tool import task_publication_lock  # noqa: E402
from zevorinth.event_store import (  # noqa: E402
    ClaimLost,
    EventStore,
    EventStoreError,
    LeaseRenewalError,
)
from zevorinth.lease_renewal import LeaseRenewer  # noqa: E402
from zevorinth.models import (  # noqa: E402
    TaskRequest,
    TaskStatus,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT, DeterministicPlanner  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
import zevorinth.service as service_module  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"
EXPIRES = "2026-10-20T00:00:10.000+00:00"


class SignalingStore(EventStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.renewed = threading.Event()

    def renew_lease(self, task_id: str, **kwargs: Any):
        task = super().renew_lease(task_id, **kwargs)
        self.renewed.set()
        return task


class FailingRenewalStore(EventStore):
    def renew_lease(self, task_id: str, **kwargs: Any):
        raise EventStoreError("injected renewal database failure")


class QueueRenewalStore(EventStore):
    def __init__(self, path: Path, queue: Any) -> None:
        super().__init__(path)
        self.queue = queue

    def renew_lease(self, task_id: str, **kwargs: Any):
        task = super().renew_lease(task_id, **kwargs)
        self.queue.put(("renewed", task.lease_expires_at))
        return task


def _execute_while_waiting_for_lock(
    root_text: str,
    task_id: str,
    entered: Any,
    queue: Any,
) -> None:
    root = Path(root_text)

    def checkpoint(
        stage: str,
        _task_id: str,
        _attempt_id: str,
        _claim_generation: int | None,
    ) -> None:
        if stage == "after_staging_write":
            entered.set()

    service = TaskService(
        root,
        writer=RealArtifactWriter(
            root / "artifacts",
            checkpoint_hook=checkpoint,
        ),
        lease_seconds=1.0,
        lease_renewal_interval_seconds=0.05,
        max_execution_seconds=5,
    )
    service.store = QueueRenewalStore(
        root / "data" / "zevorinth.sqlite3",
        queue,
    )
    try:
        task = service.execute_task(task_id)
        queue.put(("result", task.status.value))
    except BaseException as exc:
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


class WaitingFailurePlanner:
    mode = DeterministicPlanner.mode
    name = "waiting-failure-planner"

    def plan_request(self, request: TaskRequest):
        renewer = service_module._ACTIVE_RENEWER.get()
        if renewer is None or not renewer.failed_event.wait(timeout=5):
            raise AssertionError("renewal failure was not observed")
        return DeterministicPlanner().plan_request(request)


class ExtendedLeasePlanner(DeterministicPlanner):
    required_lease_seconds = 5.0


def _request() -> TaskRequest:
    return TaskRequest(
        goal="lease renewal",
        operation=WRITE_TEXT_ARTIFACT,
        parameters={
            "artifact_path": "renewal.txt",
            "content": "renewed lease\n",
        },
    )


class LeaseRenewalTests(unittest.TestCase):
    def _running_task(
        self,
        service: TaskService,
        *,
        expires_at: str = EXPIRES,
    ) -> tuple[Any, str]:
        task = service.enqueue(_request())
        attempt_id = str(uuid4())
        claim = service.store.claim_task(
            task.task_id,
            owner_id="owner-a",
            lease_token="lease-a",
            attempt_id=attempt_id,
            now=NOW,
            expires_at=expires_at,
            allowed_statuses=TaskService.RUNNABLE_STATUSES,
        )
        self.assertTrue(claim.acquired)
        service.store.transition(
            task.task_id,
            TaskStatus.RUNNING,
            "task_planned",
            NOW,
            lease_token="lease-a",
        )
        return task, attempt_id

    def test_renewal_extends_lease_and_blocks_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(
                Path(temp_dir),
                clock=lambda: NOW,
                lease_seconds=30,
            )
            task, _ = self._running_task(service)

            renewed = service.store.renew_lease(
                task.task_id,
                lease_token="lease-a",
                claim_generation=1,
                now=NOW,
                expires_at="2026-10-20T00:00:30.000+00:00",
            )
            self.assertEqual(
                renewed.lease_expires_at,
                "2026-10-20T00:00:30.000+00:00",
            )
            takeover = service.store.claim_task(
                task.task_id,
                owner_id="owner-b",
                lease_token="lease-b",
                attempt_id=str(uuid4()),
                now="2026-10-20T00:00:20.000+00:00",
                expires_at="2026-10-20T00:01:00.000+00:00",
                allowed_statuses=TaskService.RESUMABLE_STATUSES,
            )
            self.assertFalse(takeover.acquired)
            self.assertEqual(takeover.reason, "active_lease")
            self.assertEqual(
                [event.event_type for event in service.events(task.task_id)].count(
                    "lease_renewed"
                ),
                1,
            )

    def test_renewal_uses_planner_extended_lease_duration(self) -> None:
        captured: dict[str, Any] = {}

        class FakeRenewer:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured.update(kwargs)

            def start(self) -> None:
                pass

            def ensure_healthy(self) -> None:
                pass

            def stop(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(
                Path(temp_dir),
                planner=ExtendedLeasePlanner(),
                lease_seconds=1.0,
                max_execution_seconds=2.0,
            )
            task = service.enqueue(_request())
            claim = service._claim(
                task.task_id,
                allowed=TaskService.RUNNABLE_STATUSES,
            )
            self.assertTrue(claim.acquired)
            with patch(
                "zevorinth.service.LeaseRenewer",
                FakeRenewer,
            ):
                returned = service._run_with_renewal(
                    claim.task,
                    str(claim.task.lease_token),
                    lambda: claim.task,
                )

            self.assertIs(returned, claim.task)
            self.assertEqual(captured["lease_seconds"], 5.0)
            self.assertEqual(captured["interval_seconds"], 5.0 / 3.0)
            self.assertLessEqual(captured["max_duration_seconds"], 2.0)
            self.assertGreater(captured["max_duration_seconds"], 0)

    def test_expired_or_superseded_lease_cannot_be_renewed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(
                Path(temp_dir),
                clock=lambda: NOW,
                lease_seconds=30,
            )
            task, _ = self._running_task(
                service,
                expires_at="2026-10-20T00:00:01.000+00:00",
            )
            with self.assertRaises(ClaimLost):
                service.store.renew_lease(
                    task.task_id,
                    lease_token="lease-b",
                    claim_generation=1,
                    now=NOW,
                    expires_at="2026-10-20T00:00:30.000+00:00",
                )
            with self.assertRaises(ClaimLost):
                service.store.renew_lease(
                    task.task_id,
                    lease_token="lease-a",
                    claim_generation=2,
                    now=NOW,
                    expires_at="2026-10-20T00:00:30.000+00:00",
                )
            with self.assertRaises(ClaimLost):
                service.store.renew_lease(
                    task.task_id,
                    lease_token="lease-a",
                    claim_generation=1,
                    now="2026-10-20T00:00:02.000+00:00",
                    expires_at="2026-10-20T00:00:32.000+00:00",
                )
            takeover = service.store.claim_task(
                task.task_id,
                owner_id="owner-b",
                lease_token="lease-b",
                attempt_id=str(uuid4()),
                now="2026-10-20T00:00:02.000+00:00",
                expires_at="2026-10-20T00:01:00.000+00:00",
                allowed_statuses=TaskService.RESUMABLE_STATUSES,
            )
            self.assertTrue(takeover.acquired)
            self.assertEqual(takeover.task.claim_generation, 2)
            with self.assertRaises(ClaimLost):
                service.store.renew_lease(
                    task.task_id,
                    lease_token="lease-a",
                    claim_generation=1,
                    now="2026-10-20T00:00:03.000+00:00",
                    expires_at="2026-10-20T00:00:33.000+00:00",
                )

    def test_cancelled_task_cannot_renew(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(
                Path(temp_dir),
                clock=lambda: NOW,
                lease_seconds=30,
            )
            task, _ = self._running_task(service)
            service.store.transition(
                task.task_id,
                TaskStatus.CANCELLED,
                "task_cancelled",
                NOW,
                lease_token="lease-a",
            )
            with self.assertRaises(ClaimLost):
                service.store.renew_lease(
                    task.task_id,
                    lease_token="lease-a",
                    claim_generation=1,
                    now=NOW,
                    expires_at="2026-10-20T00:00:30.000+00:00",
                )

    def test_renewal_duration_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(
                Path(temp_dir),
                clock=lambda: NOW,
                lease_seconds=30,
            )
            task, _ = self._running_task(service)
            renewer = LeaseRenewer(
                service.store,
                task_id=task.task_id,
                lease_token="lease-a",
                claim_generation=1,
                clock=lambda: NOW,
                lease_seconds=30,
                interval_seconds=0.02,
                max_duration_seconds=0.08,
            )
            renewer.start()
            try:
                self.assertTrue(renewer.failed_event.wait(timeout=2))
                with self.assertRaises(LeaseRenewalError) as raised:
                    renewer.ensure_healthy()
            finally:
                renewer.stop()
            self.assertEqual(
                raised.exception.code,
                "lease_renewal_limit_exceeded",
            )
            self.assertEqual(
                service.require_task(task.task_id).claim_generation,
                1,
            )

    def test_renewal_worker_exception_is_observable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(
                Path(temp_dir),
                clock=lambda: NOW,
                lease_seconds=30,
            )
            task, _ = self._running_task(service)

            def broken_clock() -> str:
                raise RuntimeError("clock failed")

            renewer = LeaseRenewer(
                service.store,
                task_id=task.task_id,
                lease_token="lease-a",
                claim_generation=1,
                clock=broken_clock,
                lease_seconds=30,
                interval_seconds=0.02,
                max_duration_seconds=1,
            )
            renewer.start()
            try:
                self.assertTrue(renewer.failed_event.wait(timeout=2))
                with self.assertRaises(LeaseRenewalError) as raised:
                    renewer.ensure_healthy()
            finally:
                renewer.stop()

            self.assertEqual(
                raised.exception.code,
                "lease_renewal_worker_failed",
            )

    def test_terminal_task_lease_cannot_be_renewed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            task = service.submit(_request())
            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            with self.assertRaises(ClaimLost):
                service.store.renew_lease(
                    task.task_id,
                    lease_token="stale-lease",
                    claim_generation=task.claim_generation,
                    now=NOW,
                    expires_at="2026-10-20T00:00:30.000+00:00",
                )

    def test_renewal_failure_stops_before_tool_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(
                root,
                clock=lambda: NOW,
                lease_seconds=0.2,
                lease_renewal_interval_seconds=0.02,
            )
            task = service.enqueue(_request())
            service.store = FailingRenewalStore(
                root / "data" / "zevorinth.sqlite3"
            )
            service.planner = WaitingFailurePlanner()

            with self.assertRaises(LeaseRenewalError) as raised:
                service.execute_task(task.task_id)

            self.assertEqual(raised.exception.code, "lease_renewal_failed")
            event_types = [
                event.event_type for event in service.events(task.task_id)
            ]
            self.assertIn("planning_started", event_types)
            self.assertNotIn("tool_started", event_types)
            self.assertFalse(
                (root / "artifacts" / task.task_id / "renewal.txt").exists()
            )

    def test_long_blocking_tool_call_keeps_lease_alive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            started_at = time.monotonic()
            base_time = datetime.fromisoformat(NOW)

            def advancing_clock() -> str:
                return (
                    base_time
                    + timedelta(seconds=time.monotonic() - started_at)
                ).isoformat(timespec="milliseconds")

            service = TaskService(
                root,
                clock=advancing_clock,
                lease_seconds=0.2,
                lease_renewal_interval_seconds=0.02,
                max_execution_seconds=5,
            )
            signaling_store = SignalingStore(
                root / "data" / "zevorinth.sqlite3"
            )
            service.store = signaling_store
            entered = threading.Event()
            release = threading.Event()

            def checkpoint(
                stage: str,
                _task_id: str,
                _attempt_id: str,
                _claim_generation: int | None,
            ) -> None:
                if stage == "after_staging_write":
                    entered.set()
                    if not release.wait(timeout=10):
                        raise TimeoutError("test did not release staging writer")

            writer = RealArtifactWriter(
                root / "artifacts",
                checkpoint_hook=checkpoint,
            )
            service.writer = writer
            task = service.enqueue(_request())
            result: dict[str, Any] = {}

            def execute() -> None:
                try:
                    result["task"] = service.execute_task(task.task_id)
                except BaseException as exc:
                    result["error"] = exc

            worker = threading.Thread(target=execute)
            try:
                worker.start()
                self.assertTrue(entered.wait(timeout=5))
                self.assertTrue(signaling_store.renewed.wait(timeout=5))
                renewed = service.require_task(task.task_id)
                self.assertGreater(
                    datetime.fromisoformat(str(renewed.lease_expires_at)),
                    base_time + timedelta(seconds=0.2),
                )
                release.set()
                worker.join(timeout=10)
            finally:
                release.set()
                worker.join(timeout=5)

            self.assertNotIn("error", result, result.get("error"))
            self.assertEqual(result["task"].status, TaskStatus.SUCCEEDED)
            self.assertEqual(
                [
                    event.event_type
                    for event in service.events(task.task_id)
                ].count("tool_started"),
                1,
            )
            self.assertEqual(
                (root / "artifacts" / task.task_id / "renewal.txt").read_text(),
                "renewed lease\n",
            )

    def test_renewal_continues_while_waiting_for_publish_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, lease_seconds=1.0)
            task = service.enqueue(_request())
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            entered = context.Event()
            worker = context.Process(
                target=_execute_while_waiting_for_lock,
                args=(str(root), task.task_id, entered, queue),
            )
            messages: list[tuple[str, Any]] = []
            renewed: tuple[str, Any] | None = None
            result: tuple[str, Any] | None = None
            try:
                with task_publication_lock(
                    root / "artifacts",
                    task.task_id,
                    timeout_seconds=5,
                ):
                    worker.start()
                    self.assertTrue(entered.wait(timeout=5))
                    while renewed is None:
                        message = queue.get(timeout=5)
                        messages.append(message)
                        if message[0] == "renewed":
                            renewed = message
                worker.join(timeout=10)
                while result is None:
                    message = queue.get(timeout=10)
                    messages.append(message)
                    if message[0] in {"result", "error"}:
                        result = message
            finally:
                if worker.is_alive():
                    worker.kill()
                worker.join(timeout=5)
                queue.close()
                queue.join_thread()

            self.assertIsNotNone(renewed)
            self.assertEqual(result, ("result", "succeeded"), messages)
            final = service.require_task(task.task_id)
            self.assertEqual(final.status, TaskStatus.SUCCEEDED)
            self.assertEqual(final.claim_generation, 1)


if __name__ == "__main__":
    unittest.main()
