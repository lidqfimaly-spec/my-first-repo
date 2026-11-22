"""Background task execution for the control plane (S-A / F-04 / R-03).

Why this module exists
----------------------
Before this module, the only way to run a queued task was to call
``TaskService.execute_task`` inline from a control action, while holding the
control center's global ``_action_lock``. That made the HTTP request live
exactly as long as the task and serialised every task behind one lock.

This module changes only the *driver*, never the engine:

* The engine still owns planning, policy, execution, verification, leases and
  every state transition. Nothing here may transition a task.
* Workers call ``execute_task(task_id, require_claim=True)`` exactly as the
  inline path does. The claim is what makes concurrent drivers safe: two
  drivers cannot own the same task at the same time.
* Approval gates, ``AWAITING_CLARIFICATION`` and ``PARTIAL`` are *not*
  auto-continued. A worker stops when the engine stops; continuing would
  bypass an approval boundary.

Deliberate non-guarantees
-------------------------
* No retry. Re-running a task that already started is the engine's decision
  (``IDEMPOTENT_TOOLS`` / "never replay after TOOL_STARTED"), not ours.
* No state repair. If execution raises unexpectedly we log it and leave the
  task to the engine's own lease/expiry semantics. Inventing a transition
  here would be a second, unverified state machine.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .models import TaskStatus
from .planner import DeterministicPlanner
from .service import TaskService

LOGGER = logging.getLogger(__name__)

DEFAULT_WORKERS = 2
DEFAULT_QUEUE_LIMIT = 32


@dataclass
class ExecutionRecord:
    """Diagnostics only — never read by the engine."""

    task_id: str
    source: str
    submitted_at: float
    started_at: float | None = None
    finished_at: float | None = None
    final_status: str | None = None
    error: str | None = None


@dataclass
class ExecutorStats:
    workers: int
    queue_limit: int
    queued: int
    in_flight: int
    completed: int
    failed: int
    rejected_full: int
    rejected_duplicate: int
    records: list[dict[str, Any]] = field(default_factory=list)


class TaskExecutor:
    """Bounded background driver for queued tasks."""

    def __init__(
        self,
        project_root: Path,
        *,
        workers: int = DEFAULT_WORKERS,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        planner_factory=None,
        record_limit: int = 50,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        if queue_limit < 1:
            raise ValueError("queue_limit must be >= 1")
        self.project_root = Path(project_root)
        self.workers = workers
        self.queue_limit = queue_limit
        self._planner_factory = planner_factory or DeterministicPlanner
        self._record_limit = record_limit

        self._queue: "queue.Queue[tuple[str, str] | None]" = queue.Queue(
            maxsize=queue_limit
        )
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._completed = 0
        self._failed = 0
        self._rejected_full = 0
        self._rejected_duplicate = 0
        self._records: list[ExecutionRecord] = []
        self._started = False

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            for index in range(self.workers):
                thread = threading.Thread(
                    target=self._worker_loop,
                    name=f"zevorinth-executor-{index}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop accepting work and join workers. Idempotent."""
        self._stopping.set()
        for _ in self._threads:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                # Workers drain the queue before observing the sentinel; a full
                # queue means they are busy, which is fine.
                break
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in list(self._threads):
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
        self._threads.clear()

    # --------------------------------------------------------------- submit
    def submit(self, task_id: str, *, source: str = "control_center") -> str:
        """Queue a task for background execution.

        Returns one of ``"queued"`` / ``"duplicate"`` / ``"full"`` / ``"stopped"``.
        ``duplicate`` is not an error: it means the same task_id is already
        queued or running, and the caller should treat it as "already moving".
        """
        if self._stopping.is_set():
            return "stopped"
        self.start()
        with self._lock:
            if task_id in self._in_flight:
                self._rejected_duplicate += 1
                return "duplicate"
            # Reserve the id before touching the queue so two concurrent
            # submits cannot both enqueue it.
            self._in_flight.add(task_id)
        try:
            self._queue.put_nowait((task_id, source))
        except queue.Full:
            with self._lock:
                self._in_flight.discard(task_id)
                self._rejected_full += 1
            return "full"
        return "queued"

    def in_flight(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._in_flight))

    def stats(self) -> ExecutorStats:
        with self._lock:
            records = [self._record_dict(r) for r in self._records]
            return ExecutorStats(
                workers=self.workers,
                queue_limit=self.queue_limit,
                queued=self._queue.qsize(),
                in_flight=len(self._in_flight),
                completed=self._completed,
                failed=self._failed,
                rejected_full=self._rejected_full,
                rejected_duplicate=self._rejected_duplicate,
                records=records,
            )

    # --------------------------------------------------------------- worker
    def _worker_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stopping.is_set():
                    return
                continue
            if item is None:
                return
            task_id, source = item
            try:
                self._execute(task_id, source)
            finally:
                with self._lock:
                    self._in_flight.discard(task_id)
                self._queue.task_done()

    def _execute(self, task_id: str, source: str) -> None:
        record = ExecutionRecord(
            task_id=task_id,
            source=source,
            submitted_at=time.time(),
            started_at=time.time(),
        )
        service: TaskService | None = None
        try:
            service = TaskService(
                self.project_root,
                planner=self._planner_factory(),
                # The control center never recovers leases for other owners;
                # background execution must behave the same way.
                recover_leases=False,
            )
            task = service.execute_task(
                task_id,
                require_claim=True,
                claim_metadata={
                    "source": source,
                    "driver": "background_executor",
                },
            )
            record.final_status = task.status.value
            if task.status is TaskStatus.RUNNING:
                # Cannot happen through the engine's own transitions; kept as
                # an explicit, loud diagnostic rather than a silent success.
                record.error = "engine_returned_running"
                LOGGER.error(
                    "background execution returned a still-running task "
                    "task_id=%s",
                    task_id,
                )
        except Exception as exc:  # noqa: BLE001 - diagnostics boundary
            record.error = f"{type(exc).__name__}: {exc}"
            LOGGER.error(
                "background execution failed task_id=%s exception_type=%s",
                task_id,
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            # Do not invent a transition. The engine is the only authority on
            # task state; a stuck task is recovered by lease expiry, not here.
        finally:
            record.finished_at = time.time()
            with self._lock:
                if record.error is None and record.final_status is not None:
                    self._completed += 1
                else:
                    self._failed += 1
                self._records.append(record)
                if len(self._records) > self._record_limit:
                    del self._records[: len(self._records) - self._record_limit]
            if service is not None:
                self._close_service(service)

    @staticmethod
    def _close_service(service: TaskService) -> None:
        store = getattr(service, "store", None)
        connection = getattr(store, "connection", None) if store else None
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - best effort cleanup
                LOGGER.debug("failed to close store connection", exc_info=True)

    @staticmethod
    def _record_dict(record: ExecutionRecord) -> dict[str, Any]:
        duration = None
        if record.started_at is not None:
            end = record.finished_at or time.time()
            duration = round(end - record.started_at, 3)
        return {
            "task_id": record.task_id,
            "source": record.source,
            "final_status": record.final_status,
            "error": record.error,
            "duration_seconds": duration,
        }
