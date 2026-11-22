"""Cross-task memory persistence and recall.

Memory types (per PRODUCT_CHARTER section 8):
- user_preference: user preferences and habits
- project_context: project-level context and conventions
- task_history: completed task summaries and outcomes
- decision_record: explicit decisions made during tasks
- knowledge: domain knowledge nuggets

Memory scope:
- global: applies across all projects
- project: applies to the current project
- task: specific to a single task

Memories are mutable: corrections supersede old records, never delete.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


MEMORY_TYPES = frozenset(
    {
        "user_preference",
        "project_context",
        "task_history",
        "decision_record",
        "knowledge",
    }
)
MEMORY_SCOPES = frozenset({"global", "project", "task"})
CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})
MAX_CONTENT_BYTES = 16 * 1024
MAX_KEY_LENGTH = 256
MAX_QUERY_LENGTH = 512
DEFAULT_RECALL_LIMIT = 10
MAX_RECALL_LIMIT = 100


class MemoryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class MemoryRecord:
    memory_id: str
    type: str
    scope: str
    key: str
    content: str
    source_task_id: str | None
    source_event_id: str | None
    confidence: str
    status: str
    superseded_by: str | None
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "type": self.type,
            "scope": self.scope,
            "key": self.key,
            "content": self.content,
            "source_task_id": self.source_task_id,
            "source_event_id": self.source_event_id,
            "confidence": self.confidence,
            "status": self.status,
            "superseded_by": self.superseded_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MemoryRecord":
        return cls(
            memory_id=str(row["memory_id"]),
            type=str(row["type"]),
            scope=str(row["scope"]),
            key=str(row["key"]),
            content=str(row["content"]),
            source_task_id=(
                str(row["source_task_id"])
                if row["source_task_id"] is not None
                else None
            ),
            source_event_id=(
                str(row["source_event_id"])
                if row["source_event_id"] is not None
                else None
            ),
            confidence=str(row["confidence"]),
            status=str(row["status"]),
            superseded_by=(
                str(row["superseded_by"])
                if row["superseded_by"] is not None
                else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            metadata=json.loads(str(row["metadata_json"] or "{}")),
        )


_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS task_memories (
        memory_id TEXT PRIMARY KEY,
        type TEXT NOT NULL
            CHECK (type IN ('user_preference', 'project_context',
                           'task_history', 'decision_record', 'knowledge')),
        scope TEXT NOT NULL
            CHECK (scope IN ('global', 'project', 'task')),
        key TEXT NOT NULL,
        content TEXT NOT NULL,
        source_task_id TEXT,
        source_event_id TEXT,
        confidence TEXT NOT NULL DEFAULT 'medium'
            CHECK (confidence IN ('high', 'medium', 'low')),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'superseded', 'forgotten')),
        superseded_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
"""

_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_task_memories_search "
    "ON task_memories(type, scope, status)"
)


class MemoryService:
    """Cross-task memory persistence over the same SQLite database."""

    def __init__(
        self,
        project_root: Path,
        *,
        database_path: Path | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        if database_path is None:
            database_path = (
                self.project_root / "data" / "zevorinth.sqlite3"
            )
        self.database_path = database_path
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database_path),
            isolation_level=None,
            timeout=30.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _ensure_table(self) -> None:
        self.database_path.parent.mkdir(
            mode=0o700, parents=True, exist_ok=True
        )
        with self._connect() as connection:
            connection.execute(_CREATE_TABLE_SQL)
            connection.execute(_CREATE_INDEX_SQL)

    @staticmethod
    def _now() -> str:
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "+00:00")
        )

    def record(
        self,
        *,
        type: str,
        scope: str,
        key: str,
        content: str,
        source_task_id: str | None = None,
        source_event_id: str | None = None,
        confidence: str = "medium",
        metadata: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> MemoryRecord:
        """Create a new memory, superseding any active record with the same key."""
        if type not in MEMORY_TYPES:
            raise MemoryError(
                "invalid_memory_type",
                f"type must be one of {sorted(MEMORY_TYPES)}",
            )
        if scope not in MEMORY_SCOPES:
            raise MemoryError(
                "invalid_memory_scope",
                f"scope must be one of {sorted(MEMORY_SCOPES)}",
            )
        if not isinstance(key, str) or not key.strip():
            raise MemoryError(
                "invalid_memory_key",
                "key must be a non-empty string",
            )
        if len(key) > MAX_KEY_LENGTH:
            raise MemoryError(
                "memory_key_too_long",
                f"key must not exceed {MAX_KEY_LENGTH} characters",
            )
        if not isinstance(content, str) or not content.strip():
            raise MemoryError(
                "invalid_memory_content",
                "content must be a non-empty string",
            )
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_CONTENT_BYTES:
            raise MemoryError(
                "memory_content_too_large",
                f"content must not exceed {MAX_CONTENT_BYTES} bytes",
            )
        if confidence not in CONFIDENCE_LEVELS:
            raise MemoryError(
                "invalid_confidence",
                f"confidence must be one of {sorted(CONFIDENCE_LEVELS)}",
            )

        timestamp = now or self._now()
        memory_id = str(uuid4())
        metadata_json = json.dumps(
            dict(metadata or {}), ensure_ascii=False, sort_keys=True
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Supersede any active record with the same type+scope+key.
            existing = connection.execute(
                """
                SELECT memory_id FROM task_memories
                WHERE type = ? AND scope = ? AND key = ? AND status = 'active'
                """,
                (type, scope, key),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE task_memories
                    SET status = 'superseded',
                        superseded_by = ?,
                        updated_at = ?
                    WHERE memory_id = ?
                    """,
                    (memory_id, timestamp, existing["memory_id"]),
                )
            connection.execute(
                """
                INSERT INTO task_memories(
                    memory_id, type, scope, key, content,
                    source_task_id, source_event_id, confidence,
                    status, superseded_by, created_at, updated_at,
                    metadata_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, ?)
                """,
                (
                    memory_id,
                    type,
                    scope,
                    key,
                    content,
                    source_task_id,
                    source_event_id,
                    confidence,
                    timestamp,
                    timestamp,
                    metadata_json,
                ),
            )
            connection.commit()

        return self.get(memory_id)

    def get(self, memory_id: str) -> MemoryRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
        if row is None:
            raise MemoryError(
                "memory_not_found",
                "the memory record does not exist",
            )
        return MemoryRecord.from_row(row)

    def get_by_key(
        self,
        *,
        type: str,
        scope: str,
        key: str,
    ) -> MemoryRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM task_memories
                WHERE type = ? AND scope = ? AND key = ? AND status = 'active'
                """,
                (type, scope, key),
            ).fetchone()
        return MemoryRecord.from_row(row) if row else None

    def recall(
        self,
        query: str,
        *,
        scope: str | None = None,
        type: str | None = None,
        limit: int = DEFAULT_RECALL_LIMIT,
    ) -> list[MemoryRecord]:
        """Search active memories by simple text match on content."""
        if not isinstance(query, str) or not query.strip():
            return []
        if len(query) > MAX_QUERY_LENGTH:
            query = query[:MAX_QUERY_LENGTH]
        if limit < 1:
            limit = 1
        if limit > MAX_RECALL_LIMIT:
            limit = MAX_RECALL_LIMIT

        conditions = ["status = 'active'"]
        params: list[Any] = []
        conditions.append("content LIKE ?")
        params.append(f"%{query}%")
        if scope is not None:
            conditions.append("scope = ?")
            params.append(scope)
        if type is not None:
            conditions.append("type = ?")
            params.append(type)

        where = " AND ".join(conditions)
        sql = (
            f"SELECT * FROM task_memories WHERE {where} "
            f"ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(limit)

        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [MemoryRecord.from_row(row) for row in rows]

    def list_memories(
        self,
        *,
        scope: str | None = None,
        type: str | None = None,
        status: str = "active",
        limit: int = 50,
    ) -> list[MemoryRecord]:
        conditions = ["1 = 1"]
        params: list[Any] = []
        if scope is not None:
            conditions.append("scope = ?")
            params.append(scope)
        if type is not None:
            conditions.append("type = ?")
            params.append(type)
        if status != "all":
            conditions.append("status = ?")
            params.append(status)
        if limit < 1:
            limit = 1
        if limit > MAX_RECALL_LIMIT:
            limit = MAX_RECALL_LIMIT

        where = " AND ".join(conditions)
        sql = (
            f"SELECT * FROM task_memories WHERE {where} "
            f"ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(limit)

        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [MemoryRecord.from_row(row) for row in rows]

    def correct(
        self,
        memory_id: str,
        new_content: str,
        *,
        now: str | None = None,
    ) -> MemoryRecord:
        """Supersede an existing memory with corrected content."""
        old = self.get(memory_id)
        if old.status != "active":
            raise MemoryError(
                "memory_not_active",
                "only active memories can be corrected",
            )
        return self.record(
            type=old.type,
            scope=old.scope,
            key=old.key,
            content=new_content,
            source_task_id=old.source_task_id,
            source_event_id=old.source_event_id,
            confidence=old.confidence,
            metadata=old.metadata,
            now=now,
        )

    def forget(
        self,
        memory_id: str,
        *,
        now: str | None = None,
    ) -> MemoryRecord:
        """Mark a memory as forgotten (soft delete)."""
        timestamp = now or self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM task_memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise MemoryError(
                    "memory_not_found",
                    "the memory record does not exist",
                )
            if row["status"] != "active":
                connection.rollback()
                raise MemoryError(
                    "memory_not_active",
                    "only active memories can be forgotten",
                )
            connection.execute(
                """
                UPDATE task_memories
                SET status = 'forgotten', updated_at = ?
                WHERE memory_id = ?
                """,
                (timestamp, memory_id),
            )
            connection.commit()
        return self.get(memory_id)

    def record_task_outcome(
        self,
        *,
        task_id: str,
        goal: str,
        operation: str,
        status: str,
        summary: str,
        now: str | None = None,
    ) -> MemoryRecord:
        """Convenience: record a task_history memory from a completed task."""
        content = (
            f"Task {task_id[:8]}: {goal}\n"
            f"Operation: {operation}\n"
            f"Status: {status}\n"
            f"Summary: {summary}"
        )
        return self.record(
            type="task_history",
            scope="project",
            key=f"task:{task_id}",
            content=content,
            source_task_id=task_id,
            confidence="high" if status == "succeeded" else "medium",
            now=now,
        )
