from __future__ import annotations

import fcntl
import multiprocessing
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import (  # noqa: E402
    ArtifactToolError,
    RealArtifactWriter,
    task_publication_lock,
)
from zevorinth.event_store import (  # noqa: E402
    ClaimLost,
    EventStore,
    StoreBusyError,
)
from zevorinth.models import (  # noqa: E402
    ExecutionMode,
    TaskRequest,
    TaskStatus,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"
ARTIFACT_NAME = "fenced.txt"


def _request(content: str) -> TaskRequest:
    return TaskRequest(
        goal="side effect fencing",
        operation=WRITE_TEXT_ARTIFACT,
        parameters={
            "artifact_path": ARTIFACT_NAME,
            "content": content,
        },
    )


def _execute_worker(
    root_text: str,
    task_id: str,
    owner_id: str,
    checkpoint_stage: str,
    entered: Any,
    release: Any,
    queue: Any,
    role: str,
) -> None:
    root = Path(root_text)

    def checkpoint(
        stage: str,
        checkpoint_task_id: str,
        attempt_id: str,
        claim_generation: int | None,
    ) -> None:
        if stage != checkpoint_stage:
            return
        entered.set()
        if not release.wait(timeout=10):
            raise TimeoutError("parent did not release side-effect worker")

    writer = RealArtifactWriter(
        root / "artifacts",
        checkpoint_hook=checkpoint,
    )
    service = TaskService(
        root,
        writer=writer,
        owner_id=owner_id,
        clock=lambda: NOW,
        lease_seconds=30,
    )
    try:
        task = service.execute_task(task_id)
        queue.put(
            {
                "ok": True,
                "status": task.status.value,
                "role": role,
                "claim_generation": task.claim_generation,
                "artifact": (
                    task.artifact.to_dict() if task.artifact else None
                ),
                "error": task.error.code if task.error else None,
            }
        )
    except ClaimLost:
        queue.put(
            {
                "ok": False,
                "role": role,
                "error": "ClaimLost",
            }
        )
    except BaseException as exc:
        queue.put(
            {
                "ok": False,
                "role": role,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


def _resume_worker(
    root_text: str,
    task_id: str,
    owner_id: str,
    recovery_time: str,
    attempting: Any,
    entered: Any,
    queue: Any,
    role: str,
) -> None:
    root = Path(root_text)

    def checkpoint(
        stage: str,
        checkpoint_task_id: str,
        attempt_id: str,
        claim_generation: int | None,
    ) -> None:
        if stage == "after_staging_write":
            entered.set()

    writer = RealArtifactWriter(
        root / "artifacts",
        checkpoint_hook=checkpoint,
    )
    service = TaskService(
        root,
        writer=writer,
        owner_id=owner_id,
        clock=lambda: recovery_time,
        lease_seconds=30,
    )
    try:
        attempting.set()
        task = service.resume(task_id)
        queue.put(
            {
                "ok": True,
                "role": role,
                "status": task.status.value,
                "claim_generation": task.claim_generation,
                "error": task.error.code if task.error else None,
            }
        )
    except ClaimLost:
        queue.put({"ok": False, "role": role, "error": "ClaimLost"})
    except BaseException as exc:
        queue.put(
            {
                "ok": False,
                "role": role,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


def _plain_execute_worker(
    root_text: str,
    task_id: str,
    owner_id: str,
    barrier: Any,
    queue: Any,
) -> None:
    root = Path(root_text)
    service = TaskService(root, owner_id=owner_id)
    try:
        barrier.wait(timeout=10)
        task = service.execute_task(task_id)
        queue.put(
            {
                "ok": True,
                "status": task.status.value,
                "claim_generation": task.claim_generation,
                "attempt_id": task.attempt_id,
                "artifact": (
                    task.artifact.to_dict() if task.artifact else None
                ),
            }
        )
    except BaseException as exc:
        queue.put(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


def _hold_publish_lock(
    lock_path: str,
    entered: Any,
    release: Any,
) -> None:
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        entered.set()
        release.wait(timeout=10)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class SideEffectFencingTests(unittest.TestCase):
    def test_takeover_claim_uses_publication_coordination_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(
                root,
                clock=lambda: NOW,
                lease_seconds=30,
            )
            task = service.enqueue(_request("coordinated takeover\n"))
            first_attempt = str(uuid4())
            claim = service.store.claim_task(
                task.task_id,
                owner_id="owner-a",
                lease_token="lease-a",
                attempt_id=first_attempt,
                now=NOW,
                expires_at="2026-10-20T00:00:01.000+00:00",
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
            service.store.expire_claim(
                task.task_id,
                now="2026-10-20T00:00:02.000+00:00",
            )
            coordination_calls: list[str] = []

            @contextmanager
            def fake_lock(*_args: Any, **_kwargs: Any):
                coordination_calls.append("entered")
                yield
                coordination_calls.append("exited")

            original_claim = service.store.claim_task

            def coordinated_claim(*args: Any, **kwargs: Any):
                coordination_calls.append("db_claim")
                return original_claim(*args, **kwargs)

            with (
                patch(
                    "zevorinth.service.task_publication_lock",
                    fake_lock,
                ),
                patch.object(
                    service.store,
                    "claim_task",
                    side_effect=coordinated_claim,
                ),
            ):
                takeover = service._claim(
                    task.task_id,
                    allowed=TaskService.RESUMABLE_STATUSES,
                )

            self.assertTrue(takeover.acquired)
            self.assertEqual(takeover.task.claim_generation, 2)
            self.assertEqual(
                coordination_calls,
                ["entered", "db_claim", "exited"],
            )

    def test_writer_holds_lock_through_authority_and_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed = TaskService(
                root,
                clock=lambda: NOW,
                lease_seconds=30,
            )
            task = seed.enqueue(_request("held lock\n"))
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            entered = context.Event()
            release = context.Event()
            worker = context.Process(
                target=_execute_worker,
                args=(
                    str(root),
                    task.task_id,
                    "lock-holder",
                    "after_publish_authority",
                    entered,
                    release,
                    queue,
                    "lock-holder",
                ),
            )
            try:
                worker.start()
                self.assertTrue(entered.wait(timeout=10))
                with self.assertRaises(ArtifactToolError) as raised:
                    with task_publication_lock(
                        root / "artifacts",
                        task.task_id,
                        timeout_seconds=0.1,
                    ):
                        self.fail("publish lock was not held")
            finally:
                release.set()
                worker.join(timeout=10)
                if worker.is_alive():
                    worker.kill()
                    worker.join(timeout=5)
                    self.fail("lock-holder worker did not finish")
                worker_result = queue.get(timeout=10)
                queue.close()
                queue.join_thread()

            self.assertEqual(raised.exception.code, "publish_lock_timeout")
            self.assertTrue(worker_result["ok"], worker_result)
            self.assertEqual(worker_result["status"], "succeeded")
            self.assertEqual(
                (root / "artifacts" / task.task_id / ARTIFACT_NAME).read_text(),
                "held lock\n",
            )

    def test_publish_lock_wait_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            task_id = str(uuid4())
            attempt_id = str(uuid4())
            task_dir = root / task_id
            (task_dir / ".staging").mkdir(parents=True)
            locks_dir = root / ".locks"
            locks_dir.mkdir()
            lock_path = locks_dir / f"{task_id}.lock"
            context = multiprocessing.get_context("spawn")
            entered = context.Event()
            release = context.Event()
            holder = context.Process(
                target=_hold_publish_lock,
                args=(str(lock_path), entered, release),
            )
            try:
                holder.start()
                self.assertTrue(entered.wait(timeout=10))
                writer = RealArtifactWriter(
                    root,
                    publish_lock_timeout_seconds=0.1,
                )
                with self.assertRaises(ArtifactToolError) as raised:
                    writer.write(
                        task_id=task_id,
                        attempt_id=attempt_id,
                        artifact_path=ARTIFACT_NAME,
                        content="blocked\n",
                        started_at="a",
                        finished_at="b",
                        lease_token="lease-a",
                        claim_generation=1,
                        authorize=lambda _token, _generation: None,
                    )
            finally:
                release.set()
                if holder.is_alive():
                    holder.kill()
                holder.join(timeout=5)

            self.assertEqual(raised.exception.code, "publish_lock_timeout")
            self.assertFalse((task_dir / ARTIFACT_NAME).exists())
            self.assertFalse(any((task_dir / ".staging").iterdir()))

    def test_takeover_wait_timeout_does_not_advance_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(
                root,
                clock=lambda: NOW,
                lease_seconds=30,
                publish_lock_timeout_seconds=0.1,
            )
            task = service.enqueue(_request("blocked takeover\n"))
            claim = service.store.claim_task(
                task.task_id,
                owner_id="owner-a",
                lease_token="lease-a",
                attempt_id=str(uuid4()),
                now=NOW,
                expires_at="2026-10-20T00:00:01.000+00:00",
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
            service.store.expire_claim(
                task.task_id,
                now="2026-10-20T00:00:02.000+00:00",
            )
            lock_path = root / "artifacts" / ".locks" / f"{task.task_id}.lock"
            lock_path.parent.mkdir(parents=True)
            context = multiprocessing.get_context("spawn")
            entered = context.Event()
            release = context.Event()
            holder = context.Process(
                target=_hold_publish_lock,
                args=(str(lock_path), entered, release),
            )
            try:
                holder.start()
                self.assertTrue(entered.wait(timeout=10))
                with self.assertRaises(StoreBusyError):
                    service.resume(task.task_id)
            finally:
                release.set()
                if holder.is_alive():
                    holder.kill()
                holder.join(timeout=5)

            final = service.require_task(task.task_id)
            self.assertEqual(final.status, TaskStatus.INTERRUPTED)
            self.assertEqual(final.claim_generation, 1)
            self.assertIsNone(final.lease_token)
            self.assertFalse(
                (root / "artifacts" / task.task_id / ARTIFACT_NAME).exists()
            )

    def test_authority_check_rejects_wrong_token_attempt_or_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(
                root,
                clock=lambda: NOW,
                lease_seconds=30,
            )
            task = service.enqueue(_request("authority proof\n"))
            attempt_id = str(uuid4())
            claim = service.store.claim_task(
                task.task_id,
                owner_id="owner-a",
                lease_token="lease-a",
                attempt_id=attempt_id,
                now=NOW,
                expires_at="2026-10-20T00:01:00.000+00:00",
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

            with self.assertRaises(ClaimLost):
                service.store.assert_task_authority(
                    task.task_id,
                    lease_token="lease-b",
                    attempt_id=attempt_id,
                    claim_generation=1,
                    now=NOW,
                )
            with self.assertRaises(ClaimLost):
                service.store.assert_task_authority(
                    task.task_id,
                    lease_token="lease-a",
                    attempt_id=str(uuid4()),
                    claim_generation=1,
                    now=NOW,
                )
            with self.assertRaises(ClaimLost):
                service.store.assert_task_authority(
                    task.task_id,
                    lease_token="lease-a",
                    attempt_id=attempt_id,
                    claim_generation=2,
                    now=NOW,
                )

    def test_authority_check_rejects_expired_same_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(
                root,
                clock=lambda: NOW,
                lease_seconds=30,
            )
            task = service.enqueue(_request("expiry proof\n"))
            attempt_id = str(uuid4())
            claim = service.store.claim_task(
                task.task_id,
                owner_id="owner-a",
                lease_token="lease-a",
                attempt_id=attempt_id,
                now=NOW,
                expires_at="2026-10-20T00:00:01.000+00:00",
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

            current = service.store.assert_task_authority(
                task.task_id,
                lease_token="lease-a",
                attempt_id=attempt_id,
                claim_generation=1,
                now=NOW,
            )
            self.assertEqual(current.claim_generation, 1)
            with self.assertRaises(ClaimLost):
                service.store.assert_task_authority(
                    task.task_id,
                    lease_token="lease-a",
                    attempt_id=attempt_id,
                    claim_generation=1,
                    now="2026-10-20T00:00:02.000+00:00",
                )

    def _recovery_time(self, task_id: str, root: Path) -> str:
        task = EventStore(root / "data" / "zevorinth.sqlite3").require_task(
            task_id
        )
        assert task.lease_expires_at is not None
        return (
            datetime.fromisoformat(task.lease_expires_at)
            + timedelta(seconds=1)
        ).isoformat(timespec="milliseconds")

    def test_stale_attempt_cannot_overwrite_newer_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed = TaskService(
                root,
                clock=lambda: NOW,
                lease_seconds=30,
            )
            task = seed.enqueue(_request("newer owner content\n"))
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            entered = context.Event()
            release = context.Event()
            stale = context.Process(
                target=_execute_worker,
                args=(
                    str(root),
                    task.task_id,
                    "stale-owner",
                    "after_staging_write",
                    entered,
                    release,
                    queue,
                    "stale-pre-publish",
                ),
            )
            try:
                stale.start()
                self.assertTrue(entered.wait(timeout=10))
                artifact = root / "artifacts" / task.task_id / ARTIFACT_NAME
                self.assertFalse(artifact.exists())

                recovery_time = self._recovery_time(task.task_id, root)
                newer_service = TaskService(
                    root,
                    owner_id="newer-owner",
                    clock=lambda: recovery_time,
                    lease_seconds=30,
                )
                newer = newer_service.resume(task.task_id)
                self.assertEqual(newer.status, TaskStatus.SUCCEEDED)
                newer_inode = artifact.stat().st_ino
                newer_evidence = newer.artifact.to_dict()
                self.assertEqual(newer.claim_generation, 2)
                self.assertEqual(
                    newer_evidence["claim_generation"],
                    newer.claim_generation,
                )
                self.assertEqual(
                    newer_evidence["attempt_id"],
                    newer.attempt_id,
                )

                release.set()
                stale.join(timeout=10)
                stale_result = queue.get(timeout=10)
            finally:
                release.set()
                if stale.is_alive():
                    stale.kill()
                stale.join(timeout=5)
                queue.close()
                queue.join_thread()

            self.assertFalse(stale_result["ok"], stale_result)
            self.assertEqual(stale_result["error"], "ClaimLost")
            self.assertEqual(artifact.stat().st_ino, newer_inode)
            self.assertEqual(artifact.read_text(), "newer owner content\n")
            final = EventStore(
                root / "data" / "zevorinth.sqlite3"
            ).require_task(task.task_id)
            self.assertEqual(final.status, TaskStatus.SUCCEEDED)
            self.assertEqual(final.claim_generation, 2)
            self.assertEqual(
                final.artifact.claim_generation,
                final.claim_generation,
            )
            self.assertFalse(any((artifact.parent / ".staging").iterdir()))

    def test_authority_to_publish_window_fails_closed_without_overwrite(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed = TaskService(
                root,
                clock=lambda: NOW,
                lease_seconds=30,
            )
            task = seed.enqueue(_request("single publish\n"))
            artifact = root / "artifacts" / task.task_id / ARTIFACT_NAME
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            stale_entered = context.Event()
            stale_release = context.Event()
            newer_attempting = context.Event()
            newer_entered = context.Event()
            stale = context.Process(
                target=_execute_worker,
                args=(
                    str(root),
                    task.task_id,
                    "stale-owner",
                    "after_publish_authority",
                    stale_entered,
                    stale_release,
                    queue,
                    "stale-after-authority",
                ),
            )
            newer: Any = None
            try:
                stale.start()
                self.assertTrue(stale_entered.wait(timeout=10))
                recovery_time = self._recovery_time(task.task_id, root)
                newer = context.Process(
                    target=_resume_worker,
                    args=(
                        str(root),
                        task.task_id,
                        "newer-owner",
                        recovery_time,
                        newer_attempting,
                        newer_entered,
                        queue,
                        "newer",
                    ),
                )
                newer.start()
                self.assertTrue(newer_attempting.wait(timeout=10))
                self.assertFalse(newer_entered.is_set())
                self.assertFalse(artifact.exists())

                stale_release.set()
                stale.join(timeout=10)
                newer.join(timeout=10)
                results = [queue.get(timeout=10), queue.get(timeout=10)]
            finally:
                stale_release.set()
                for process in (stale, newer):
                    if process is not None and process.is_alive():
                        process.kill()
                    if process is not None:
                        process.join(timeout=5)
                queue.close()
                queue.join_thread()

            stale_result = next(
                result
                for result in results
                if result["role"] == "stale-after-authority"
            )
            newer_result = next(
                result for result in results if result["role"] == "newer"
            )
            self.assertEqual(artifact.read_text(), "single publish\n")
            final = EventStore(
                root / "data" / "zevorinth.sqlite3"
            ).require_task(task.task_id)
            self.assertIn(
                final.status,
                {TaskStatus.SUCCEEDED, TaskStatus.BLOCKED},
            )
            if final.status is TaskStatus.SUCCEEDED:
                self.assertTrue(stale_result["ok"], stale_result)
                self.assertEqual(stale_result["status"], "succeeded")
            else:
                self.assertFalse(stale_result["ok"], stale_result)
                self.assertEqual(stale_result["error"], "ClaimLost")
                self.assertTrue(newer_result["ok"], newer_result)
                self.assertEqual(newer_result["status"], "blocked")
                self.assertEqual(newer_result["error"], "artifact_exists")
            event_types = [
                event.event_type
                for event in EventStore(
                    root / "data" / "zevorinth.sqlite3"
                ).list_events(task.task_id)
            ]
            expected_successes = (
                1 if final.status is TaskStatus.SUCCEEDED else 0
            )
            self.assertEqual(
                event_types.count("task_succeeded"),
                expected_successes,
            )

    def test_same_relative_path_in_different_tasks_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed = TaskService(root)
            first = seed.enqueue(_request("task A\n"))
            second = seed.enqueue(_request("task B\n"))
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            barrier = context.Barrier(2)
            processes = [
                context.Process(
                    target=_plain_execute_worker,
                    args=(
                        str(root),
                        task.task_id,
                        owner_id,
                        barrier,
                        queue,
                    ),
                )
                for task, owner_id in (
                    (first, "owner-a"),
                    (second, "owner-b"),
                )
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
            self.assertEqual(
                {result["status"] for result in results},
                {"succeeded"},
            )
            self.assertEqual(
                (root / "artifacts" / first.task_id / ARTIFACT_NAME).read_text(),
                "task A\n",
            )
            self.assertEqual(
                (root / "artifacts" / second.task_id / ARTIFACT_NAME).read_text(),
                "task B\n",
            )
            for result in results:
                evidence = result["artifact"]
                self.assertEqual(evidence["attempt_id"], result["attempt_id"])
                self.assertEqual(
                    evidence["claim_generation"],
                    result["claim_generation"],
                )


if __name__ == "__main__":
    unittest.main()
