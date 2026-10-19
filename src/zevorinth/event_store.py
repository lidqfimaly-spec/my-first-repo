"""SQLite-backed task snapshots and append-only ordered events."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from .models import (
    ClaimResult,
    Conversation,
    ConversationStatus,
    ConversationTurn,
    Event,
    EventType,
    Message,
    MessageRole,
    Task,
    TaskError,
    TaskStatus,
    TurnStatus,
)
from .state_machine import ensure_transition


class TaskNotFound(LookupError):
    pass


class ConversationNotFound(LookupError):
    pass


class ConversationTurnNotFound(LookupError):
    pass


class EventStoreError(RuntimeError):
    pass


class SchemaVersionError(EventStoreError):
    """Raised when the on-disk schema cannot be safely migrated."""


class StoreBusyError(EventStoreError):
    pass


class ClaimLost(EventStoreError):
    pass


class MessageConflict(EventStoreError):
    pass


class TaskLinkConflict(EventStoreError):
    pass


class ActiveTaskConflict(EventStoreError):
    pass


class EventStore:
    SCHEMA_VERSION = 3
    LEGACY_SCHEMA_VERSION = 2
    MAX_CONVERSATION_HISTORY = 200
    ACTIVE_TASK_STATUSES = (
        "queued",
        "running",
        "awaiting_clarification",
        "blocked",
        "verifying",
        "interrupted",
    )

    def __init__(self, database_path: Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.database_path = database_path.resolve()
        self.busy_timeout_ms = max(0, busy_timeout_ms)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.database_path.parent, 0o700)
        preflight_version = self._preflight_schema_version()
        with self._connection() as connection:
            version_before_transaction = self._read_schema_version(connection)
            migration_mode = (
                preflight_version == self.LEGACY_SCHEMA_VERSION
                or version_before_transaction == self.LEGACY_SCHEMA_VERSION
            )
            if migration_mode:
                connection.execute("PRAGMA foreign_keys = OFF")
            try:
                connection.execute("BEGIN IMMEDIATE")
                version = self._read_schema_version(connection)
                if version is None:
                    if self._has_user_tables(connection):
                        raise SchemaVersionError(
                            "database has user tables but no metadata.schema_version"
                        )
                    self._create_schema_v3(connection)
                elif version == self.SCHEMA_VERSION:
                    self._validate_schema_v3(connection)
                elif version == self.LEGACY_SCHEMA_VERSION:
                    self._migrate_v2_to_v3(connection)
                    violations = connection.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()
                    if violations:
                        raise SchemaVersionError(
                            "migrated database has foreign key violations: "
                            f"{len(violations)}"
                        )
                elif version > self.SCHEMA_VERSION:
                    raise SchemaVersionError(
                        f"database schema version {version} is newer than "
                        f"supported version {self.SCHEMA_VERSION}"
                    )
                else:
                    raise SchemaVersionError(
                        f"database schema version {version} is not supported"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                if migration_mode:
                    connection.execute("PRAGMA foreign_keys = ON")
        os.chmod(self.database_path, 0o600)

    def _preflight_schema_version(self) -> int | None:
        """Peek at a committed version and reject only a newer schema early.

        Structural checks are intentionally deferred until the write transaction
        holds BEGIN IMMEDIATE, where the schema snapshot is consistent with the
        initializer that owns the lock. A malformed or partially visible schema
        must not be mistaken for a safely initialized database here.
        """
        if not self.database_path.exists():
            return None
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1000.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if not self._table_exists(connection, "metadata"):
                return None
            try:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
            except sqlite3.DatabaseError:
                return None
            if row is None:
                return None
            try:
                version = int(row["value"])
            except (TypeError, ValueError):
                return None
            if version > self.SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database schema version {version} is newer than "
                    f"supported version {self.SCHEMA_VERSION}"
                )
            return version
        finally:
            connection.close()

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None

    def _read_schema_version(self, connection: sqlite3.Connection) -> int | None:
        if not self._table_exists(connection, "metadata"):
            return None
        try:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise SchemaVersionError(
                "metadata table has an invalid structure"
            ) from exc
        if row is None:
            raise SchemaVersionError(
                "metadata table exists but metadata.schema_version is missing"
            )
        try:
            return int(row["value"])
        except (TypeError, ValueError) as exc:
            raise SchemaVersionError(
                "metadata.schema_version is not an integer"
            ) from exc

    def _has_user_tables(self, connection: sqlite3.Connection) -> bool:
        rows = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
        return any(str(row["name"]) != "metadata" for row in rows)

    @staticmethod
    def _column_exists(
        connection: sqlite3.Connection,
        table: str,
        column: str,
    ) -> bool:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return any(str(row["name"]) == column for row in rows)

    @staticmethod
    def _metadata_table_sql() -> str:
        return """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """

    @staticmethod
    def _conversations_table_sql() -> str:
        return """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
                    CHECK (status IN ('active', 'archived')),
                version INTEGER NOT NULL DEFAULT 1
                    CHECK (version >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """

    @staticmethod
    def _tasks_table_sql(table_name: str = "tasks") -> str:
        return f"""
            CREATE TABLE {table_name} (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                task_json TEXT NOT NULL,
                conversation_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (task_id, conversation_id),
                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(conversation_id)
            )
        """

    @staticmethod
    def _legacy_tasks_table_sql() -> str:
        return """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                task_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """

    @staticmethod
    def _events_table_sql() -> str:
        return """
            CREATE TABLE events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id)
            )
        """

    @staticmethod
    def _events_index_sql() -> str:
        return """
            CREATE INDEX idx_events_task_sequence
            ON events(task_id, sequence)
        """

    @staticmethod
    def _conversation_turns_table_sql() -> str:
        return """
            CREATE TABLE conversation_turns (
                turn_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                turn_index INTEGER NOT NULL
                    CHECK (turn_index >= 0),
                status TEXT NOT NULL,
                intent_kind TEXT,
                task_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(conversation_id),
                FOREIGN KEY (task_id, conversation_id)
                    REFERENCES tasks(task_id, conversation_id),
                UNIQUE (conversation_id, turn_index),
                UNIQUE (turn_id, conversation_id)
            )
        """

    @staticmethod
    def _messages_table_sql() -> str:
        return """
            CREATE TABLE messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                role TEXT NOT NULL
                    CHECK (role IN ('user', 'assistant')),
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                byte_count INTEGER NOT NULL
                    CHECK (byte_count >= 0),
                client_message_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(conversation_id),
                FOREIGN KEY (turn_id, conversation_id)
                    REFERENCES conversation_turns(turn_id, conversation_id)
            )
        """

    @staticmethod
    def _conversation_events_table_sql() -> str:
        return """
            CREATE TABLE conversation_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                conversation_id TEXT NOT NULL,
                turn_id TEXT,
                task_id TEXT,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(conversation_id),
                FOREIGN KEY (turn_id, conversation_id)
                    REFERENCES conversation_turns(turn_id, conversation_id),
                FOREIGN KEY (task_id, conversation_id)
                    REFERENCES tasks(task_id, conversation_id)
            )
        """

    def _v3_index_sqls(self) -> list[tuple[str, str]]:
        active_statuses = ", ".join(
            f"'{status}'" for status in self.ACTIVE_TASK_STATUSES
        )
        return [
            (
                "uq_messages_client_id",
                """
            CREATE UNIQUE INDEX uq_messages_client_id
            ON messages(conversation_id, client_message_id)
            WHERE client_message_id IS NOT NULL
                """,
            ),
            (
                "idx_messages_turn",
                """
            CREATE INDEX idx_messages_turn
            ON messages(turn_id, created_at, message_id)
                """,
            ),
            (
                "idx_conversation_turns_task",
                """
            CREATE INDEX idx_conversation_turns_task
            ON conversation_turns(task_id)
                """,
            ),
            (
                "idx_conversation_events_conversation",
                """
            CREATE INDEX idx_conversation_events_conversation
            ON conversation_events(conversation_id, sequence)
                """,
            ),
            (
                "uq_tasks_task_conversation",
                """
            CREATE UNIQUE INDEX uq_tasks_task_conversation
            ON tasks(task_id, conversation_id)
                """,
            ),
            (
                "uq_tasks_one_active_per_conversation",
                f"""
            CREATE UNIQUE INDEX uq_tasks_one_active_per_conversation
            ON tasks(conversation_id)
            WHERE conversation_id IS NOT NULL
              AND status IN ({active_statuses})
                """,
            ),
            (
                "idx_tasks_conversation_id",
                """
            CREATE INDEX idx_tasks_conversation_id
            ON tasks(conversation_id)
            WHERE conversation_id IS NOT NULL
                """,
            ),
        ]

    def _schema_sql_objects(self) -> dict[str, str]:
        objects = {
            "metadata": self._metadata_table_sql(),
            "conversations": self._conversations_table_sql(),
            "tasks": self._tasks_table_sql("tasks"),
            "events": self._events_table_sql(),
            "idx_events_task_sequence": self._events_index_sql(),
            "conversation_turns": self._conversation_turns_table_sql(),
            "messages": self._messages_table_sql(),
            "conversation_events": self._conversation_events_table_sql(),
        }
        objects.update(dict(self._v3_index_sqls()))
        return objects

    @staticmethod
    def _normalize_schema_sql(sql: str) -> str:
        # SQLite quotes the table name after ALTER TABLE ... RENAME.
        sql = re.sub(
            r'(?is)^(\s*create\s+table\s+)["`\[]([^"`\]]+)["`\]]',
            r"\1\2",
            sql,
            count=1,
        )
        normalized: list[str] = []
        index = 0
        while index < len(sql):
            char = sql[index]
            if char == "'":
                end = index + 1
                while end < len(sql):
                    if sql[end] == "'":
                        if end + 1 < len(sql) and sql[end + 1] == "'":
                            end += 2
                            continue
                        end += 1
                        break
                    end += 1
                normalized.append(sql[index:end])
                index = end
                continue
            if char in {'"', "`", "["}:
                closing = "]" if char == "[" else char
                end = index + 1
                while end < len(sql):
                    if sql[end] == closing:
                        if closing in {"'", '"'} and end + 1 < len(sql) and sql[end + 1] == closing:
                            end += 2
                            continue
                        end += 1
                        break
                    end += 1
                normalized.append(sql[index:end])
                index = end
                continue
            if char.isspace():
                while index < len(sql) and sql[index].isspace():
                    index += 1
                normalized.append(" ")
                continue
            normalized.append(char.lower())
            index += 1
        return "".join(normalized).strip()

    @staticmethod
    def _create_metadata_table(connection: sqlite3.Connection) -> None:
        connection.execute(EventStore._metadata_table_sql())

    @staticmethod
    def _create_conversations_table(connection: sqlite3.Connection) -> None:
        connection.execute(EventStore._conversations_table_sql())

    @staticmethod
    def _create_tasks_table(connection: sqlite3.Connection) -> None:
        connection.execute(EventStore._tasks_table_sql("tasks"))

    @staticmethod
    def _create_events_table(connection: sqlite3.Connection) -> None:
        connection.execute(EventStore._events_table_sql())
        connection.execute(EventStore._events_index_sql())

    @staticmethod
    def _create_conversation_children(connection: sqlite3.Connection) -> None:
        connection.execute(EventStore._conversation_turns_table_sql())
        connection.execute(EventStore._messages_table_sql())
        connection.execute(EventStore._conversation_events_table_sql())

    def _create_v3_indexes(self, connection: sqlite3.Connection) -> None:
        for _, sql in self._v3_index_sqls():
            connection.execute(sql)

    def _create_schema_v3(self, connection: sqlite3.Connection) -> None:
        self._create_metadata_table(connection)
        self._create_conversations_table(connection)
        self._create_tasks_table(connection)
        self._create_events_table(connection)
        self._create_conversation_children(connection)
        self._create_v3_indexes(connection)
        self._write_schema_version(connection, self.SCHEMA_VERSION)
        self._validate_schema_v3(connection)

    def _migrate_v2_to_v3(self, connection: sqlite3.Connection) -> None:
        self._require_table(connection, "tasks")
        self._require_table(connection, "events")
        self._validate_legacy_tasks_columns(connection)
        self._create_conversations_table(connection)
        task_index_sqls = self._index_sqls(connection, "tasks")
        task_trigger_sqls = self._trigger_sqls(connection, "tasks")
        self._rebuild_tasks_table(
            connection,
            task_index_sqls,
            task_trigger_sqls,
        )
        self._create_conversation_children(connection)
        self._create_v3_indexes(connection)
        self._write_schema_version(connection, self.SCHEMA_VERSION)
        self._validate_schema_v3(connection)

    def _validate_schema_v3(self, connection: sqlite3.Connection) -> None:
        for name, expected_sql in self._schema_sql_objects().items():
            row = connection.execute(
                "SELECT type, sql FROM sqlite_master WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None or row["sql"] is None:
                raise SchemaVersionError(f"schema object is missing: {name}")
            actual_sql = self._normalize_schema_sql(str(row["sql"]))
            expected = self._normalize_schema_sql(expected_sql)
            if actual_sql != expected:
                raise SchemaVersionError(
                    f"schema object has an unexpected definition: {name}"
                )

    def _validate_legacy_tasks_columns(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        expected = {
            "task_id",
            "status",
            "execution_mode",
            "task_json",
            "created_at",
            "updated_at",
        }
        actual = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(tasks)")
        }
        allowed = expected | {"conversation_id"}
        if not expected.issubset(actual) or actual - allowed:
            raise SchemaVersionError(
                "v2 tasks table has an unexpected column set"
            )
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone()
        if row is None or row["sql"] is None:
            raise SchemaVersionError("v2 tasks table definition is missing")
        if self._normalize_schema_sql(str(row["sql"])) != self._normalize_schema_sql(
            self._legacy_tasks_table_sql()
        ):
            raise SchemaVersionError(
                "v2 tasks table has an unsupported schema; refusing to rebuild"
            )

    @staticmethod
    def _index_sqls(
        connection: sqlite3.Connection,
        table: str,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL
            ORDER BY name
            """,
            (table,),
        ).fetchall()
        return [str(row["sql"]) for row in rows]

    @staticmethod
    def _trigger_sqls(
        connection: sqlite3.Connection,
        table: str,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'trigger' AND tbl_name = ? AND sql IS NOT NULL
            ORDER BY name
            """,
            (table,),
        ).fetchall()
        return [str(row["sql"]) for row in rows]

    def _rebuild_tasks_table(
        self,
        connection: sqlite3.Connection,
        index_sqls: list[str],
        trigger_sqls: list[str],
    ) -> None:
        if self._table_exists(connection, "tasks_v3"):
            raise SchemaVersionError("temporary table tasks_v3 already exists")
        connection.execute(self._tasks_table_sql("tasks_v3"))
        if self._column_exists(connection, "tasks", "conversation_id"):
            selected_conversation = "conversation_id"
        else:
            selected_conversation = "NULL"
        connection.execute(
            f"""
            INSERT INTO tasks_v3(
                task_id, status, execution_mode, task_json,
                conversation_id, created_at, updated_at
            )
            SELECT
                task_id, status, execution_mode, task_json,
                {selected_conversation}, created_at, updated_at
            FROM tasks
            """
        )
        connection.execute("DROP TABLE tasks")
        connection.execute("ALTER TABLE tasks_v3 RENAME TO tasks")
        for sql in index_sqls:
            connection.execute(sql)
        for sql in trigger_sqls:
            connection.execute(sql)

    @staticmethod
    def _require_table(connection: sqlite3.Connection, table: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None:
            raise SchemaVersionError(f"required table is missing: {table}")

    @staticmethod
    def _require_index(connection: sqlite3.Connection, index: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index,),
        ).fetchone()
        if row is None:
            raise SchemaVersionError(f"required index is missing: {index}")

    @staticmethod
    def _write_schema_version(
        connection: sqlite3.Connection,
        version: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(version),),
        )

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
            self._enable_wal(connection)
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            raise

    def _enable_wal(self, connection: sqlite3.Connection) -> None:
        """Set WAL without turning a concurrent initialization into a false busy."""
        deadline = time.monotonic() + self.busy_timeout_ms / 1000.0
        while True:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if (
                    ("locked" not in message and "busy" not in message)
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(0.01)

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

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def create_conversation(
        self,
        *,
        conversation_id: str | None = None,
        now: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Conversation:
        timestamp = now or self._now()
        conversation = Conversation(
            conversation_id=conversation_id or str(uuid4()),
            status=ConversationStatus.ACTIVE,
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
            metadata=dict(metadata or {}),
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO conversations(
                    conversation_id, status, version,
                    created_at, updated_at, metadata_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation.conversation_id,
                    conversation.status.value,
                    conversation.version,
                    conversation.created_at,
                    conversation.updated_at,
                    json.dumps(
                        conversation.metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
            self._insert_conversation_event(
                connection,
                conversation.conversation_id,
                turn_id=None,
                task_id=None,
                event_type="conversation_created",
                occurred_at=timestamp,
                payload={"status": conversation.status.value},
            )
            connection.commit()
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT conversation_id, status, version,
                       created_at, updated_at, metadata_json
                FROM conversations
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return Conversation(
            conversation_id=str(row["conversation_id"]),
            status=ConversationStatus(row["status"]),
            version=int(row["version"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def append_user_message(
        self,
        conversation_id: str,
        content: str,
        *,
        client_message_id: str | None = None,
        now: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[ConversationTurn, Message]:
        if not isinstance(content, str) or not content:
            raise ValueError("content must be a non-empty string")
        if client_message_id is not None and not client_message_id.strip():
            raise ValueError("client_message_id must not be empty")
        timestamp = now or self._now()
        turn_id = str(uuid4())
        message_id = str(uuid4())
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone() is None:
                raise ConversationNotFound(conversation_id)
            if client_message_id is not None:
                existing = connection.execute(
                    """
                    SELECT m.message_id, m.conversation_id, m.turn_id,
                           m.role, m.kind, m.content, m.content_sha256,
                           m.byte_count, m.client_message_id, m.created_at,
                           m.metadata_json,
                           t.turn_index, t.status AS turn_status,
                           t.intent_kind, t.task_id,
                           t.created_at AS turn_created_at,
                           t.updated_at AS turn_updated_at
                    FROM messages AS m
                    JOIN conversation_turns AS t ON t.turn_id = m.turn_id
                    WHERE m.conversation_id = ?
                      AND m.client_message_id = ?
                    """,
                    (conversation_id, client_message_id),
                ).fetchone()
                if existing is not None:
                    existing_message = Message(
                        message_id=str(existing["message_id"]),
                        conversation_id=str(existing["conversation_id"]),
                        turn_id=str(existing["turn_id"]),
                        role=MessageRole(existing["role"]),
                        kind=str(existing["kind"]),
                        content=str(existing["content"]),
                        content_sha256=str(existing["content_sha256"]),
                        byte_count=int(existing["byte_count"]),
                        client_message_id=(
                            str(existing["client_message_id"])
                            if existing["client_message_id"] is not None
                            else None
                        ),
                        created_at=str(existing["created_at"]),
                        metadata=json.loads(existing["metadata_json"]),
                    )
                    if (
                        existing_message.content_sha256 == digest
                        and existing_message.content == content
                        and existing_message.byte_count == len(encoded)
                    ):
                        existing_turn = ConversationTurn(
                            turn_id=existing_message.turn_id,
                            conversation_id=conversation_id,
                            turn_index=int(existing["turn_index"]),
                            status=TurnStatus(existing["turn_status"]),
                            intent_kind=(
                                str(existing["intent_kind"])
                                if existing["intent_kind"] is not None
                                else None
                            ),
                            task_id=(
                                str(existing["task_id"])
                                if existing["task_id"] is not None
                                else None
                            ),
                            created_at=str(existing["turn_created_at"]),
                            updated_at=str(existing["turn_updated_at"]),
                        )
                        connection.rollback()
                        return existing_turn, existing_message
                    raise MessageConflict(
                        "client_message_id exists with different content"
                    )
            row = connection.execute(
                """
                SELECT COALESCE(MAX(turn_index), -1) + 1
                FROM conversation_turns
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            turn_index = int(row[0])
            turn = ConversationTurn(
                turn_id=turn_id,
                conversation_id=conversation_id,
                turn_index=turn_index,
                status=TurnStatus.RECEIVED,
                intent_kind=None,
                task_id=None,
                created_at=timestamp,
                updated_at=timestamp,
            )
            message = Message(
                message_id=message_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                role=MessageRole.USER,
                kind="user_input",
                content=content,
                content_sha256=digest,
                byte_count=len(encoded),
                client_message_id=client_message_id,
                created_at=timestamp,
                metadata=dict(metadata or {}),
            )
            connection.execute(
                """
                INSERT INTO conversation_turns(
                    turn_id, conversation_id, turn_index, status,
                    intent_kind, task_id, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn.turn_id,
                    turn.conversation_id,
                    turn.turn_index,
                    turn.status.value,
                    turn.intent_kind,
                    turn.task_id,
                    turn.created_at,
                    turn.updated_at,
                ),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO messages(
                        message_id, conversation_id, turn_id, role, kind,
                        content, content_sha256, byte_count,
                        client_message_id, created_at, metadata_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.message_id,
                        message.conversation_id,
                        message.turn_id,
                        message.role.value,
                        message.kind,
                        message.content,
                        message.content_sha256,
                        message.byte_count,
                        message.client_message_id,
                        message.created_at,
                        json.dumps(
                            message.metadata,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise MessageConflict(
                    "client_message_id already exists in this conversation"
                ) from exc
            self._insert_conversation_event(
                connection,
                conversation_id,
                turn_id=turn.turn_id,
                task_id=None,
                event_type="message_recorded",
                occurred_at=timestamp,
                payload={
                    "message_id": message.message_id,
                    "turn_id": turn.turn_id,
                    "content_sha256": message.content_sha256,
                    "byte_count": message.byte_count,
                },
            )
            updated = connection.execute(
                """
                UPDATE conversations
                SET updated_at = ?, version = version + 1
                WHERE conversation_id = ?
                """,
                (timestamp, conversation_id),
            )
            if updated.rowcount != 1:
                raise ConversationNotFound(conversation_id)
            connection.commit()
        return turn, message

    def list_conversation_messages(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
    ) -> list[Message]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if limit > self.MAX_CONVERSATION_HISTORY:
            raise ValueError(
                f"limit exceeds the {self.MAX_CONVERSATION_HISTORY} message cap"
            )
        with self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone() is None:
                raise ConversationNotFound(conversation_id)
            rows = connection.execute(
                """
                SELECT m.message_id, m.conversation_id, m.turn_id,
                       m.role, m.kind, m.content, m.content_sha256,
                       m.byte_count, m.client_message_id, m.created_at,
                       m.metadata_json
                FROM messages AS m
                JOIN conversation_turns AS t ON t.turn_id = m.turn_id
                WHERE m.conversation_id = ?
                ORDER BY t.turn_index ASC, m.created_at ASC, m.message_id ASC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [
            Message(
                message_id=str(row["message_id"]),
                conversation_id=str(row["conversation_id"]),
                turn_id=str(row["turn_id"]),
                role=MessageRole(row["role"]),
                kind=str(row["kind"]),
                content=str(row["content"]),
                content_sha256=str(row["content_sha256"]),
                byte_count=int(row["byte_count"]),
                client_message_id=(
                    str(row["client_message_id"])
                    if row["client_message_id"] is not None
                    else None
                ),
                created_at=str(row["created_at"]),
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]

    @staticmethod
    def _insert_conversation_event(
        connection: sqlite3.Connection,
        conversation_id: str,
        *,
        turn_id: str | None,
        task_id: str | None,
        event_type: str,
        occurred_at: str,
        payload: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO conversation_events(
                event_id, conversation_id, turn_id, task_id,
                event_type, occurred_at, payload_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                conversation_id,
                turn_id,
                task_id,
                event_type,
                occurred_at,
                json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
            ),
        )

    def create_task(
        self,
        task: Task,
        event: Event,
        *,
        turn_id: str | None = None,
    ) -> tuple[Task, Event]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if task.conversation_id is not None:
                if connection.execute(
                    "SELECT 1 FROM conversations WHERE conversation_id = ?",
                    (task.conversation_id,),
                ).fetchone() is None:
                    raise ConversationNotFound(task.conversation_id)
            if turn_id is not None:
                if task.conversation_id is None:
                    raise TaskLinkConflict(
                        "turn association requires a conversation_id"
                    )
                turn = connection.execute(
                    """
                    SELECT conversation_id, task_id
                    FROM conversation_turns
                    WHERE turn_id = ?
                    """,
                    (turn_id,),
                ).fetchone()
                if turn is None:
                    raise ConversationTurnNotFound(turn_id)
                if turn["conversation_id"] != task.conversation_id:
                    raise TaskLinkConflict(
                        "task and turn belong to different conversations"
                    )
                if turn["task_id"] is not None:
                    raise TaskLinkConflict("turn is already linked to a task")
            if task.conversation_id is not None:
                placeholders = ", ".join(
                    "?" for _ in self.ACTIVE_TASK_STATUSES
                )
                active = connection.execute(
                    f"""
                    SELECT 1 FROM tasks
                    WHERE conversation_id = ?
                      AND status IN ({placeholders})
                    """,
                    (task.conversation_id, *self.ACTIVE_TASK_STATUSES),
                ).fetchone()
                if active is not None:
                    raise ActiveTaskConflict(
                        "conversation already has an active task"
                    )
            self._insert_task(connection, task)
            stored_event = self._insert_event(connection, event)
            if turn_id is not None:
                cursor = connection.execute(
                    """
                    UPDATE conversation_turns
                    SET task_id = ?, updated_at = ?
                    WHERE turn_id = ?
                      AND conversation_id = ?
                      AND task_id IS NULL
                    """,
                    (
                        task.task_id,
                        task.updated_at,
                        turn_id,
                        task.conversation_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise TaskLinkConflict("turn association changed concurrently")
                self._insert_conversation_event(
                    connection,
                    task.conversation_id,
                    turn_id=turn_id,
                    task_id=task.task_id,
                    event_type="task_linked",
                    occurred_at=task.created_at,
                    payload={
                        "task_id": task.task_id,
                        "turn_id": turn_id,
                    },
                )
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
            INSERT INTO tasks(
                task_id, status, execution_mode, task_json,
                conversation_id, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.task_id,
                task.status.value,
                task.execution_mode.value,
                json.dumps(task.to_dict(), ensure_ascii=False, sort_keys=True),
                task.conversation_id,
                task.created_at,
                task.updated_at,
            ),
        )

    def _update_task(self, connection: sqlite3.Connection, task: Task) -> None:
        cursor = connection.execute(
            """
            UPDATE tasks
            SET status = ?, execution_mode = ?, task_json = ?,
                conversation_id = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (
                task.status.value,
                task.execution_mode.value,
                json.dumps(task.to_dict(), ensure_ascii=False, sort_keys=True),
                task.conversation_id,
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
