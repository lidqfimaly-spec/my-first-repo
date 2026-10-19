from __future__ import annotations

import hashlib
import json
import multiprocessing
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.event_store import EventStore, SchemaVersionError  # noqa: E402
from zevorinth.models import (  # noqa: E402
    ExecutionMode,
    PlannerMode,
    Task,
    TaskRequest,
    TaskStatus,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


def _legacy_task() -> Task:
    return Task(
        task_id=str(uuid4()),
        status=TaskStatus.QUEUED,
        request=TaskRequest(
            goal="legacy task",
            operation=WRITE_TEXT_ARTIFACT,
            parameters={"artifact_path": "legacy.txt", "content": "legacy"},
        ),
        execution_mode=ExecutionMode.NOT_EXECUTED,
        attempt_id=str(uuid4()),
        phase="queued",
        created_at=NOW,
        updated_at=NOW,
    )


def _create_v2_database(path: Path) -> Task:
    task = _legacy_task()
    task_json = task.to_dict()
    task_json.pop("conversation_id", None)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO metadata(key, value)
            VALUES('schema_version', '2');

            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                task_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id)
            );

            CREATE INDEX idx_events_task_sequence
                ON events(task_id, sequence);
            """
        )
        connection.execute(
            """
            INSERT INTO tasks(
                task_id, status, execution_mode, task_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                task.task_id,
                task.status.value,
                task.execution_mode.value,
                json.dumps(task_json, ensure_ascii=False, sort_keys=True),
                task.created_at,
                task.updated_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO events(
                event_id, task_id, event_type, occurred_at, payload_json
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                task.task_id,
                "task_created",
                NOW,
                json.dumps({"goal_present": True}, sort_keys=True),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return task


def _initialize_worker(
    path_text: str,
    barrier: object,
    queue: object,
) -> None:
    try:
        barrier.wait(timeout=10)
        EventStore(Path(path_text)).initialize()
        queue.put(("ok", None))
    except BaseException as exc:
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _mutate_database(path: Path, statements: tuple[str, ...]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def _database_snapshot(path: Path) -> tuple[object, ...]:
    connection = sqlite3.connect(path)
    try:
        journal_mode = connection.execute(
            "PRAGMA journal_mode"
        ).fetchone()[0]
        objects = tuple(
            connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                ORDER BY type, name
                """
            ).fetchall()
        )
        counts = {}
        for table in ("tasks", "events"):
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone():
                counts[table] = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
        return journal_mode, objects, tuple(sorted(counts.items()))
    finally:
        connection.close()


class ConversationSchemaTests(unittest.TestCase):
    def test_blank_database_creates_schema_v3(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            EventStore(path).initialize()

            connection = sqlite3.connect(path)
            try:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                user_version = connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(tasks)")
                }
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
            finally:
                connection.close()

            self.assertEqual(version, "3")
            self.assertEqual(user_version, 0)
            self.assertTrue(
                {
                    "conversations",
                    "conversation_turns",
                    "messages",
                    "conversation_events",
                }.issubset(tables)
            )
            self.assertIn("conversation_id", columns)
            self.assertIn("uq_messages_client_id", indexes)
            self.assertIn("uq_tasks_one_active_per_conversation", indexes)

            store = EventStore(path)
            with store._connection() as connection:
                self.assertEqual(
                    connection.execute("PRAGMA foreign_keys").fetchone()[0],
                    1,
                )

    def test_v2_database_migrates_and_legacy_task_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            path.parent.mkdir(parents=True)
            legacy = _create_v2_database(path)

            store = EventStore(path)
            store.initialize()

            migrated = store.require_task(legacy.task_id)
            self.assertEqual(migrated.status, TaskStatus.QUEUED)
            self.assertIsNone(migrated.conversation_id)
            self.assertEqual(migrated.request.goal, "legacy task")
            self.assertEqual(len(store.list_events(legacy.task_id)), 1)

            connection = sqlite3.connect(path)
            try:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                conversation_id = connection.execute(
                    "SELECT conversation_id FROM tasks WHERE task_id = ?",
                    (legacy.task_id,),
                ).fetchone()[0]
                task_fks = connection.execute(
                    "PRAGMA foreign_key_list(tasks)"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(version, "3")
            self.assertIsNone(conversation_id)
            self.assertTrue(
                any(
                    row[2] == "conversations"
                    and row[3] == "conversation_id"
                    and row[4] == "conversation_id"
                    for row in task_fks
                )
            )

    def test_repeated_initialization_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            store = EventStore(path)
            store.initialize()
            store.initialize()
            store.initialize()

            connection = sqlite3.connect(path)
            try:
                version_rows = connection.execute(
                    "SELECT COUNT(*) FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                task_count = connection.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(version_rows, 1)
            self.assertEqual(version, "3")
            self.assertEqual(task_count, 0)

    def test_migration_failure_rolls_back_all_ddl(self) -> None:
        class FailingMigrationStore(EventStore):
            def _create_v3_indexes(self, connection: sqlite3.Connection) -> None:
                super()._create_v3_indexes(connection)
                raise RuntimeError("injected migration failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            path.parent.mkdir(parents=True)
            legacy = _create_v2_database(path)
            before = _database_snapshot(path)

            with self.assertRaises(RuntimeError):
                FailingMigrationStore(path).initialize()

            after = _database_snapshot(path)
            self.assertEqual(after[1:], before[1:])
            self.assertEqual(before[0], "delete")
            self.assertEqual(after[0], "wal")

            connection = sqlite3.connect(path)
            try:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(tasks)")
                }
                task_count = connection.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0]
                event_count = connection.execute(
                    "SELECT COUNT(*) FROM events"
                ).fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(version, "2")
            self.assertNotIn("conversations", tables)
            self.assertNotIn("conversation_id", columns)
            self.assertEqual(task_count, 1)
            self.assertEqual(event_count, 1)
            self.assertEqual(EventStore(path).require_task(legacy.task_id).status, TaskStatus.QUEUED)

    def test_newer_schema_version_fails_before_schema_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            path.parent.mkdir(parents=True)
            _create_v2_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE metadata SET value = '99' WHERE key = 'schema_version'"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(SchemaVersionError):
                EventStore(path).initialize()

            connection = sqlite3.connect(path)
            try:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(tasks)")
                }
                journal_mode = connection.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(version, "99")
            self.assertNotIn("conversations", tables)
            self.assertNotIn("conversation_id", columns)
            self.assertEqual(journal_mode, "delete")

    def test_user_tables_without_schema_metadata_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            path.parent.mkdir(parents=True)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE TABLE tasks (task_id TEXT PRIMARY KEY)"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(SchemaVersionError):
                EventStore(path).initialize()

            connection = sqlite3.connect(path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                connection.close()
            self.assertEqual(tables, {"tasks"})

    def test_preflight_defers_partial_schema_until_locked_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            path.parent.mkdir(parents=True)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE TABLE tasks (task_id TEXT PRIMARY KEY)"
                )
                connection.commit()
            finally:
                connection.close()

            store = EventStore(path)
            self.assertIsNone(store._preflight_schema_version())
            with self.assertRaises(SchemaVersionError):
                store.initialize()

            connection = sqlite3.connect(path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                connection.close()
            self.assertEqual(tables, {"tasks"})

    def test_concurrent_initialization_is_serialized_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            path.parent.mkdir(parents=True)
            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            queue = context.Queue()
            processes = [
                context.Process(
                    target=_initialize_worker,
                    args=(str(path), barrier, queue),
                )
                for _ in range(2)
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

            self.assertEqual(
                results,
                [("ok", None), ("ok", None)],
            )
            connection = sqlite3.connect(path)
            try:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                table_count = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(version, "3")
            self.assertGreaterEqual(table_count, 7)

    def test_concurrent_v2_migration_is_completed_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            path.parent.mkdir(parents=True)
            legacy = _create_v2_database(path)
            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            queue = context.Queue()
            processes = [
                context.Process(
                    target=_initialize_worker,
                    args=(str(path), barrier, queue),
                )
                for _ in range(2)
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

            self.assertEqual(results, [("ok", None), ("ok", None)])
            store = EventStore(path)
            self.assertEqual(store.require_task(legacy.task_id).status, TaskStatus.QUEUED)
            connection = sqlite3.connect(path)
            try:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                conversation_table = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'conversations'"
                ).fetchone()[0]
                task_count = connection.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(version, "3")
            self.assertEqual(conversation_table, 1)
            self.assertEqual(task_count, 1)

    def test_message_and_turn_constraints_are_enforced_by_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            EventStore(path).initialize()
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                connection.execute(
                    """
                    INSERT INTO conversations(
                        conversation_id, status, created_at, updated_at
                    ) VALUES('c1', 'active', ?, ?)
                    """,
                    (NOW, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO conversation_turns(
                        turn_id, conversation_id, turn_index, status,
                        created_at, updated_at
                    ) VALUES('t1', 'c1', 0, 'received', ?, ?)
                    """,
                    (NOW, NOW),
                )
                message_values = (
                    "m1",
                    "c1",
                    "t1",
                    "user",
                    "user_input",
                    "hello",
                    hashlib.sha256(b"hello").hexdigest(),
                    5,
                    "client-1",
                    NOW,
                    "{}",
                )
                connection.execute(
                    """
                    INSERT INTO messages(
                        message_id, conversation_id, turn_id, role, kind,
                        content, content_sha256, byte_count, client_message_id,
                        created_at, metadata_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    message_values,
                )
                connection.commit()
            finally:
                connection.close()

            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO conversation_turns(
                            turn_id, conversation_id, turn_index, status,
                            created_at, updated_at
                        ) VALUES('t2', 'c1', 0, 'received', ?, ?)
                        """,
                        (NOW, NOW),
                    )
            finally:
                connection.close()

            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO messages(
                            message_id, conversation_id, turn_id, role, kind,
                            content, content_sha256, byte_count,
                            client_message_id, created_at, metadata_json
                        ) VALUES('m2', 'c1', 't1', 'user', 'user_input',
                                 'again', ?, 5, 'client-1', ?, '{}')
                        """,
                        (hashlib.sha256(b"again").hexdigest(), NOW),
                    )
            finally:
                connection.close()

            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO messages(
                            message_id, conversation_id, turn_id, role, kind,
                            content, content_sha256, byte_count,
                            client_message_id, created_at, metadata_json
                        ) VALUES('m3', 'c1', 'missing-turn', 'user', 'user_input',
                                 'bad', ?, 3, 'client-3', ?, '{}')
                        """,
                        (hashlib.sha256(b"bad").hexdigest(), NOW),
                    )
            finally:
                connection.close()

            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                connection.execute(
                    """
                    INSERT INTO conversations(
                        conversation_id, status, created_at, updated_at
                    ) VALUES('c2', 'active', ?, ?)
                    """,
                    (NOW, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO conversation_turns(
                        turn_id, conversation_id, turn_index, status,
                        created_at, updated_at
                    ) VALUES('t2', 'c2', 0, 'received', ?, ?)
                    """,
                    (NOW, NOW),
                )
                connection.commit()
            finally:
                connection.close()

            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO messages(
                            message_id, conversation_id, turn_id, role, kind,
                            content, content_sha256, byte_count,
                            client_message_id, created_at, metadata_json
                        ) VALUES('m4', 'c2', 't1', 'user', 'user_input',
                                 'cross', ?, 5, 'client-4', ?, '{}')
                        """,
                        (hashlib.sha256(b"cross").hexdigest(), NOW),
                    )
            finally:
                connection.close()

    def test_task_association_and_active_uniqueness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            EventStore(path).initialize()
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                connection.execute(
                    """
                    INSERT INTO conversations(
                        conversation_id, status, created_at, updated_at
                    ) VALUES('c1', 'active', ?, ?)
                    """,
                    (NOW, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO conversations(
                        conversation_id, status, created_at, updated_at
                    ) VALUES('c2', 'active', ?, ?)
                    """,
                    (NOW, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, status, execution_mode, task_json,
                        conversation_id, created_at, updated_at
                    ) VALUES('task-1', 'queued', 'not_executed', '{}',
                             'c1', ?, ?)
                    """,
                    (NOW, NOW),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO tasks(
                            task_id, status, execution_mode, task_json,
                            conversation_id, created_at, updated_at
                        ) VALUES('task-2', 'queued', 'not_executed', '{}',
                                 'c1', ?, ?)
                        """,
                        (NOW, NOW),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO conversation_turns(
                            turn_id, conversation_id, turn_index, status,
                            task_id, created_at, updated_at
                        ) VALUES('cross-task', 'c2', 0, 'dispatched',
                                 'task-1', ?, ?)
                        """,
                        (NOW, NOW),
                    )
                connection.commit()
            finally:
                connection.close()

            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                connection.execute(
                    """
                    UPDATE tasks SET status = 'succeeded'
                    WHERE task_id = 'task-1'
                    """
                )
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, status, execution_mode, task_json,
                        conversation_id, created_at, updated_at
                    ) VALUES('task-2', 'queued', 'not_executed', '{}',
                             'c1', ?, ?)
                    """,
                    (NOW, NOW),
                )
                connection.commit()
                connection.execute(
                    """
                    INSERT INTO conversation_turns(
                        turn_id, conversation_id, turn_index, status,
                        task_id, created_at, updated_at
                    ) VALUES('linked-turn', 'c1', 0, 'dispatched',
                             'task-2', ?, ?)
                    """,
                    (NOW, NOW),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO conversation_turns(
                            turn_id, conversation_id, turn_index, status,
                            task_id, created_at, updated_at
                        ) VALUES('t1', 'c1', 1, 'dispatched',
                                 'missing-task', ?, ?)
                        """,
                        (NOW, NOW),
                    )
            finally:
                connection.close()

    def test_migrated_legacy_task_still_executes_through_task_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "data" / "zevorinth.sqlite3"
            path.parent.mkdir(parents=True)
            legacy = _create_v2_database(path)

            service = TaskService(root)
            result = service.execute_task(legacy.task_id)

            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertIsNone(result.conversation_id)
            self.assertTrue(
                (root / "artifacts" / legacy.task_id / "legacy.txt").is_file()
            )

    def test_task_old_positional_arguments_remain_stable(self) -> None:
        values = (
            "task-positional",
            TaskStatus.QUEUED,
            TaskRequest(
                goal="positional",
                operation=WRITE_TEXT_ARTIFACT,
                parameters={"artifact_path": "positional.txt", "content": "text"},
            ),
            ExecutionMode.NOT_EXECUTED,
            "attempt-positional",
            "queued",
            NOW,
            NOW,
            None,
            None,
            None,
            None,
            [],
            None,
            "owner",
            "lease",
            "2026-10-20T01:00:00.000+00:00",
            2,
            PlannerMode.LLM,
            {"planner": "test"},
            ["question"],
        )
        task = Task(*values)

        self.assertEqual(task.task_id, "task-positional")
        self.assertEqual(task.status, TaskStatus.QUEUED)
        self.assertEqual(task.attempt_id, "attempt-positional")
        self.assertEqual(task.execution_owner, "owner")
        self.assertEqual(task.claim_generation, 2)
        self.assertEqual(task.planner_mode, PlannerMode.LLM)
        self.assertEqual(task.clarification_questions, ["question"])
        self.assertIsNone(task.conversation_id)

        payload = task.to_dict()
        self.assertIsNone(payload["conversation_id"])
        legacy_payload = dict(payload)
        legacy_payload.pop("conversation_id")
        restored = Task.from_dict(legacy_payload)
        self.assertIsNone(restored.conversation_id)
        restored_with_conversation = Task.from_dict(
            {**payload, "conversation_id": "conversation-1"}
        )
        self.assertEqual(
            restored_with_conversation.conversation_id,
            "conversation-1",
        )

    def test_tasks_conversation_id_fk_accepts_null_and_rejects_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            EventStore(path).initialize()
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                connection.execute(
                    """
                    INSERT INTO conversations(
                        conversation_id, status, created_at, updated_at
                    ) VALUES('c1', 'active', ?, ?)
                    """,
                    (NOW, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, status, execution_mode, task_json,
                        conversation_id, created_at, updated_at
                    ) VALUES('task-linked', 'queued', 'not_executed', '{}',
                             'c1', ?, ?)
                    """,
                    (NOW, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, status, execution_mode, task_json,
                        conversation_id, created_at, updated_at
                    ) VALUES('task-unlinked', 'succeeded', 'not_executed', '{}',
                             NULL, ?, ?)
                    """,
                    (NOW, NOW),
                )
                connection.commit()
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO tasks(
                            task_id, status, execution_mode, task_json,
                            conversation_id, created_at, updated_at
                        ) VALUES('task-orphan', 'succeeded', 'not_executed', '{}',
                                 'missing', ?, ?)
                        """,
                        (NOW, NOW),
                    )
                fk_refs = connection.execute(
                    "PRAGMA foreign_key_list(tasks)"
                ).fetchall()
            finally:
                connection.close()

            self.assertTrue(
                any(
                    row[2] == "conversations"
                    and row[3] == "conversation_id"
                    and row[4] == "conversation_id"
                    for row in fk_refs
                )
            )

    def test_migration_preserves_legacy_task_indexes_and_triggers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
            path.parent.mkdir(parents=True)
            legacy = _create_v2_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE INDEX idx_legacy_task_status ON tasks(status)"
                )
                connection.execute(
                    """
                    CREATE TRIGGER trg_legacy_task_update
                    AFTER UPDATE ON tasks
                    BEGIN
                        SELECT 1;
                    END
                    """
                )
                connection.commit()
            finally:
                connection.close()

            EventStore(path).initialize()
            connection = sqlite3.connect(path)
            try:
                objects = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('index', 'trigger')"
                    )
                }
                task_count = connection.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0]
            finally:
                connection.close()

            self.assertIn("idx_legacy_task_status", objects)
            self.assertIn("trg_legacy_task_update", objects)
            self.assertEqual(task_count, 1)
            self.assertEqual(EventStore(path).require_task(legacy.task_id).status, TaskStatus.QUEUED)

    def test_invalid_v3_schemas_fail_closed_without_repair(self) -> None:
        mutations = {
            "tasks_missing_column": (
                "ALTER TABLE tasks DROP COLUMN task_json",
            ),
            "events_missing_column": (
                "ALTER TABLE events DROP COLUMN payload_json",
            ),
            "metadata_wrong_structure": (
                "ALTER TABLE metadata RENAME TO metadata_old",
                "CREATE TABLE metadata (id TEXT PRIMARY KEY, value TEXT NOT NULL)",
            ),
            "tasks_missing_fk": (
                "DROP TABLE tasks",
                """
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    conversation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (task_id, conversation_id)
                )
                """,
                """
                CREATE UNIQUE INDEX uq_tasks_task_conversation
                ON tasks(task_id, conversation_id)
                """,
                """
                CREATE UNIQUE INDEX uq_tasks_one_active_per_conversation
                ON tasks(conversation_id)
                WHERE conversation_id IS NOT NULL
                  AND status IN (
                    'queued', 'running', 'awaiting_clarification',
                    'blocked', 'verifying', 'interrupted'
                  )
                """,
                """
                CREATE INDEX idx_tasks_conversation_id
                ON tasks(conversation_id)
                WHERE conversation_id IS NOT NULL
                """,
            ),
            "turns_missing_unique": (
                "DROP TABLE conversation_turns",
                """
                CREATE TABLE conversation_turns (
                    turn_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL CHECK (turn_index >= 0),
                    status TEXT NOT NULL,
                    intent_kind TEXT,
                    task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (conversation_id)
                        REFERENCES conversations(conversation_id),
                    FOREIGN KEY (task_id, conversation_id)
                        REFERENCES tasks(task_id, conversation_id)
                )
                """,
                """
                CREATE INDEX idx_conversation_turns_task
                ON conversation_turns(task_id)
                """,
            ),
            "conversations_missing_check": (
                "DROP TABLE conversations",
                """
                CREATE TABLE conversations (
                    conversation_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """,
            ),
            "conversations_wrong_case_check": (
                "DROP TABLE conversations",
                """
                CREATE TABLE conversations (
                    conversation_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                        CHECK (status IN ('ACTIVE', 'ARCHIVED')),
                    version INTEGER NOT NULL DEFAULT 1
                        CHECK (version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """,
            ),
            "active_index_wrong_predicate": (
                "DROP INDEX uq_tasks_one_active_per_conversation",
                """
                CREATE UNIQUE INDEX uq_tasks_one_active_per_conversation
                ON tasks(conversation_id)
                WHERE conversation_id IS NOT NULL
                """,
            ),
            "active_index_wrong_case_statuses": (
                "DROP INDEX uq_tasks_one_active_per_conversation",
                """
                CREATE UNIQUE INDEX uq_tasks_one_active_per_conversation
                ON tasks(conversation_id)
                WHERE conversation_id IS NOT NULL
                  AND status IN (
                    'QUEUED', 'RUNNING', 'AWAITING_CLARIFICATION',
                    'BLOCKED', 'VERIFYING', 'INTERRUPTED'
                  )
                """,
            ),
            "fake_v3_same_index_names": (
                "DROP TABLE conversation_events",
                "DROP TABLE messages",
                "DROP TABLE conversation_turns",
                "DROP TABLE events",
                "DROP TABLE tasks",
                "DROP TABLE conversations",
                """
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY,
                    conversation_id TEXT
                )
                """,
                """
                CREATE TABLE events (
                    sequence INTEGER PRIMARY KEY
                )
                """,
                """
                CREATE TABLE conversations (
                    conversation_id TEXT PRIMARY KEY
                )
                """,
                """
                CREATE TABLE conversation_turns (
                    turn_id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    turn_index INTEGER
                )
                """,
                """
                CREATE TABLE messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    turn_id TEXT
                )
                """,
                """
                CREATE TABLE conversation_events (
                    sequence INTEGER PRIMARY KEY
                )
                """,
                """
                CREATE INDEX idx_events_task_sequence
                ON events(sequence)
                """,
                """
                CREATE UNIQUE INDEX uq_messages_client_id
                ON messages(conversation_id)
                """,
                """
                CREATE INDEX idx_messages_turn
                ON messages(turn_id)
                """,
                """
                CREATE INDEX idx_conversation_turns_task
                ON conversation_turns(turn_id)
                """,
                """
                CREATE INDEX idx_conversation_events_conversation
                ON conversation_events(sequence)
                """,
                """
                CREATE UNIQUE INDEX uq_tasks_task_conversation
                ON tasks(task_id, conversation_id)
                """,
                """
                CREATE UNIQUE INDEX uq_tasks_one_active_per_conversation
                ON tasks(conversation_id)
                """,
                """
                CREATE INDEX idx_tasks_conversation_id
                ON tasks(conversation_id)
                """,
            ),
        }
        for case_name, statements in mutations.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "data" / "zevorinth.sqlite3"
                EventStore(path).initialize()
                _mutate_database(path, statements)
                before = _database_snapshot(path)

                with self.assertRaises(SchemaVersionError):
                    EventStore(path).initialize()

                after = _database_snapshot(path)
                self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
