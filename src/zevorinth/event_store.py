"""SQLite-backed task snapshots and append-only ordered events."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from .models import ClaimResult, Event, EventType, Task, TaskError, TaskStatus
from .state_machine import ensure_transition


class TaskNotFound(LookupError):
    pass


class EventStoreError(RuntimeError):
    pass


class StoreBusyError(EventStoreError):
    pass


class ClaimLost(EventStoreError):
    pass


class EventStore:
    SCHEMA_VERSION = 2

    def __init__(self, database_path: Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.database_path = database_path.resolve()
        self.busy_timeout_ms = max(0, busy_timeout_ms)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.database_path.parent, 0o700)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_events_task_sequence
                    ON events(task_id, sequence);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )
            connection.commit()
        os.chmod(self.database_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=self.busy_timeout_ms / 1000.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            yield connection
        except sqlite3.OperationalError as exc:
            if connection is not None:
                connection.rollback()
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise StoreBusyError(
                    f"SQLite remained busy for {self.busy_timeout_ms} ms"
                ) from exc
            raise EventStoreError(f"SQLite operation failed: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            if connection is not None:
                connection.rollback()
            raise EventStoreError(f"SQLite database error: {exc}") from exc
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def create_task(self, task: Task, event: Event) -> tuple[Task, Event]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_task(connection, task)
            stored_event = self._insert_event(connection, event)
            connection.commit()
        return task, stored_event

    def get_task(self, task_id: str) -> Task | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT task_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return Task.from_dict(json.loads(row["task_json"]))

    def require_task(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        if task is None:
            raise TaskNotFound(task_id)
        return task

    def list_events(self, task_id: str) -> list[Event]:
        self.require_task(task_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT sequence, event_id, task_id, event_type, occurred_at, payload_json
                FROM events
                WHERE task_id = ?
                ORDER BY sequence ASC
                """,
                (task_id,),
            ).fetchall()
        return [
            Event(
                sequence=int(row["sequence"]),
                event_id=str(row["event_id"]),
                task_id=str(row["task_id"]),
                event_type=str(row["event_type"]),
                occurred_at=str(row["occurred_at"]),
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def transition(
        self,
        task_id: str,
        target: TaskStatus,
        event_type: EventType | str,
        occurred_at: str,
        payload: Mapping[str, Any] | None = None,
        updates: Mapping[str, Any] | None = None,
        lease_token: str | None = None,
    ) -> tuple[Task, Event]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._load_task(connection, task_id)
            self._require_lease(task, lease_token)
            ensure_transition(task.status, target)
            task.status = target
            task.updated_at = occurred_at
            for key, value in (updates or {}).items():
                setattr(task, key, value)
            event = Event.new(task_id, event_type, occurred_at, payload)
            self._update_task(connection, task)
            stored_event = self._insert_event(connection, event)
            connection.commit()
        return task, stored_event

    def record_event_and_update(
        self,
        task_id: str,
        event_type: EventType | str,
        occurred_at: str,
        payload: Mapping[str, Any] | None = None,
        updates: Mapping[str, Any] | None = None,
        lease_token: str | None = None,
    ) -> tuple[Task, Event]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._load_task(connection, task_id)
            self._require_lease(task, lease_token)
            task.updated_at = occurred_at
            for key, value in (updates or {}).items():
                setattr(task, key, value)
            event = Event.new(task_id, event_type, occurred_at, payload)
            self._update_task(connection, task)
            stored_event = self._insert_event(connection, event)
            connection.commit()
        return task, stored_event

    def find_tasks_by_statuses(self, statuses: set[TaskStatus]) -> list[Task]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        values = tuple(status.value for status in statuses)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT task_json FROM tasks WHERE status IN ({placeholders})",
                values,
            ).fetchall()
        return [Task.from_dict(json.loads(row["task_json"])) for row in rows]

    def claim_task(
        self,
        task_id: str,
        *,
        owner_id: str,
        lease_token: str,
        attempt_id: str,
        now: str,
        expires_at: str,
        allowed_statuses: frozenset[TaskStatus],
    ) -> ClaimResult:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._load_task(connection, task_id)
            events: list[Event] = []

            if task.status not in allowed_statuses:
                reason = f"status_not_runnable:{task.status.value}"
                events.append(
                    Event.new(
                        task_id,
                        EventType.CLAIM_REJECTED,
                        now,
                        {
                            "owner_id": owner_id,
                            "reason": reason,
                            "current_status": task.status.value,
                        },
                    )
                )
                self._insert_events(connection, events)
                connection.commit()
                return ClaimResult(False, task, reason)

            if task.lease_token and task.lease_expires_at:
                if self._lease_is_active(task.lease_expires_at, now):
                    reason = "active_lease"
                    events.append(
                        Event.new(
                            task_id,
                            EventType.CLAIM_REJECTED,
                            now,
                            {
                                "owner_id": owner_id,
                                "reason": reason,
                                "current_owner": task.execution_owner,
                                "current_generation": task.claim_generation,
                                "lease_expires_at": task.lease_expires_at,
                            },
                        )
                    )
                    self._insert_events(connection, events)
                    connection.commit()
                    return ClaimResult(False, task, reason)

                previous_status = task.status
                previous_owner = task.execution_owner
                events.append(
                    Event.new(
                        task_id,
                        EventType.LEASE_EXPIRED,
                        now,
                        {
                            "owner_id": previous_owner,
                            "previous_status": previous_status.value,
                            "lease_expires_at": task.lease_expires_at,
                        },
                    )
                )
                task.execution_owner = None
                task.lease_token = None
                task.lease_expires_at = None
                if task.status in {TaskStatus.RUNNING, TaskStatus.VERIFYING}:
                    ensure_transition(task.status, TaskStatus.INTERRUPTED)
                    task.status = TaskStatus.INTERRUPTED
                    task.phase = "lease_expired"
                    task.error = TaskError(
                        "interrupted",
                        "execution lease expired before terminal state",
                    )
                    events.append(
                        Event.new(
                            task_id,
                            EventType.TASK_INTERRUPTED,
                            now,
                            {
                                "reason": "execution_lease_expired",
                                "previous_status": previous_status.value,
                            },
                        )
                    )

            if task.status not in allowed_statuses:
                reason = f"status_not_runnable:{task.status.value}"
                events.append(
                    Event.new(
                        task_id,
                        EventType.CLAIM_REJECTED,
                        now,
                        {
                            "owner_id": owner_id,
                            "reason": reason,
                            "current_status": task.status.value,
                        },
                    )
                )
                self._update_task(connection, task)
                self._insert_events(connection, events)
                connection.commit()
                return ClaimResult(False, task, reason)

            previous_status = task.status
            task.execution_owner = owner_id
            task.lease_token = lease_token
            task.lease_expires_at = expires_at
            task.claim_generation += 1
            task.attempt_id = attempt_id
            task.phase = "claimed"
            task.updated_at = now
            events.append(
                Event.new(
                    task_id,
                    EventType.TASK_CLAIMED,
                    now,
                    {
                        "owner_id": owner_id,
                        "generation": task.claim_generation,
                        "previous_status": previous_status.value,
                        "lease_expires_at": expires_at,
                    },
                )
            )
            self._update_task(connection, task)
            self._insert_events(connection, events)
            connection.commit()
            return ClaimResult(True, task, "claimed")

    def expire_claim(self, task_id: str, *, now: str) -> Task:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._load_task(connection, task_id)
            if not task.lease_token or not task.lease_expires_at:
                connection.commit()
                return task
            if self._lease_is_active(task.lease_expires_at, now):
                connection.commit()
                return task

            previous_status = task.status
            previous_owner = task.execution_owner
            events = [
                Event.new(
                    task_id,
                    EventType.LEASE_EXPIRED,
                    now,
                    {
                        "owner_id": previous_owner,
                        "previous_status": previous_status.value,
                        "lease_expires_at": task.lease_expires_at,
                    },
                )
            ]
            task.execution_owner = None
            task.lease_token = None
            task.lease_expires_at = None
            task.updated_at = now
            if task.status in {TaskStatus.RUNNING, TaskStatus.VERIFYING}:
                ensure_transition(task.status, TaskStatus.INTERRUPTED)
                task.status = TaskStatus.INTERRUPTED
                task.phase = "lease_expired"
                task.error = TaskError(
                    "interrupted",
                    "execution lease expired before terminal state",
                )
                events.append(
                    Event.new(
                        task_id,
                        EventType.TASK_INTERRUPTED,
                        now,
                        {
                            "reason": "execution_lease_expired",
                            "previous_status": previous_status.value,
                        },
                    )
                )
            self._update_task(connection, task)
            self._insert_events(connection, events)
            connection.commit()
            return task

    def release_claim(
        self,
        task_id: str,
        *,
        lease_token: str,
        now: str,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[Task, Event]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._load_task(connection, task_id)
            self._require_lease(task, lease_token)
            task.execution_owner = None
            task.lease_token = None
            task.lease_expires_at = None
            task.updated_at = now
            event = Event.new(
                task_id,
                EventType.LEASE_RELEASED,
                now,
                payload or {},
            )
            self._update_task(connection, task)
            stored_event = self._insert_event(connection, event)
            connection.commit()
            return task, stored_event

    def _load_task(self, connection: sqlite3.Connection, task_id: str) -> Task:
        row = connection.execute(
            "SELECT task_json FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return Task.from_dict(json.loads(row["task_json"]))

    @staticmethod
    def _require_lease(task: Task, lease_token: str | None) -> None:
        if lease_token is not None and task.lease_token != lease_token:
            raise ClaimLost(
                f"task {task.task_id} no longer belongs to lease {lease_token}"
            )

    @staticmethod
    def _lease_is_active(expires_at: str, now: str) -> bool:
        try:
            expires = datetime.fromisoformat(expires_at)
            current = datetime.fromisoformat(now)
        except ValueError:
            return True
        return expires > current

    def _insert_task(self, connection: sqlite3.Connection, task: Task) -> None:
        connection.execute(
            """
            INSERT INTO tasks(task_id, status, execution_mode, task_json, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                task.task_id,
                task.status.value,
                task.execution_mode.value,
                json.dumps(task.to_dict(), ensure_ascii=False, sort_keys=True),
                task.created_at,
                task.updated_at,
            ),
        )

    def _update_task(self, connection: sqlite3.Connection, task: Task) -> None:
        cursor = connection.execute(
            """
            UPDATE tasks
            SET status = ?, execution_mode = ?, task_json = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (
                task.status.value,
                task.execution_mode.value,
                json.dumps(task.to_dict(), ensure_ascii=False, sort_keys=True),
                task.updated_at,
                task.task_id,
            ),
        )
        if cursor.rowcount != 1:
            raise TaskNotFound(task.task_id)

    def _insert_event(
        self, connection: sqlite3.Connection, event: Event
    ) -> Event:
        cursor = connection.execute(
            """
            INSERT INTO events(event_id, task_id, event_type, occurred_at, payload_json)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.task_id,
                event.event_type,
                event.occurred_at,
                json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        event.sequence = int(cursor.lastrowid)
        return event

    def _insert_events(
        self, connection: sqlite3.Connection, events: list[Event]
    ) -> None:
        for event in events:
            self._insert_event(connection, event)
