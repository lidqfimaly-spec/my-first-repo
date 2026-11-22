"""P2-1: Cross-task memory service — record, recall, correct, forget."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.memory_service import (
    CONFIDENCE_LEVELS,
    MAX_CONTENT_BYTES,
    MAX_KEY_LENGTH,
    MAX_QUERY_LENGTH,
    MemoryError,
    MemoryService,
)


class MemoryRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = MemoryService(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_record_creates_active_memory(self) -> None:
        mem = self.service.record(
            type="user_preference",
            scope="global",
            key="language",
            content="用户偏好简体中文",
        )
        self.assertEqual(mem.type, "user_preference")
        self.assertEqual(mem.scope, "global")
        self.assertEqual(mem.key, "language")
        self.assertEqual(mem.status, "active")
        self.assertEqual(mem.confidence, "medium")
        self.assertIsNone(mem.superseded_by)

    def test_record_with_all_types(self) -> None:
        for mem_type in [
            "user_preference", "project_context", "task_history",
            "decision_record", "knowledge",
        ]:
            mem = self.service.record(
                type=mem_type,
                scope="project",
                key=f"test_{mem_type}",
                content=f"content for {mem_type}",
            )
            self.assertEqual(mem.type, mem_type)

    def test_record_invalid_type_rejected(self) -> None:
        with self.assertRaises(MemoryError) as caught:
            self.service.record(
                type="invalid_type",
                scope="global",
                key="test",
                content="content",
            )
        self.assertEqual(caught.exception.code, "invalid_memory_type")

    def test_record_invalid_scope_rejected(self) -> None:
        with self.assertRaises(MemoryError) as caught:
            self.service.record(
                type="knowledge",
                scope="invalid",
                key="test",
                content="content",
            )
        self.assertEqual(caught.exception.code, "invalid_memory_scope")

    def test_record_empty_key_rejected(self) -> None:
        with self.assertRaises(MemoryError) as caught:
            self.service.record(
                type="knowledge",
                scope="global",
                key="",
                content="content",
            )
        self.assertEqual(caught.exception.code, "invalid_memory_key")

    def test_record_empty_content_rejected(self) -> None:
        with self.assertRaises(MemoryError) as caught:
            self.service.record(
                type="knowledge",
                scope="global",
                key="test",
                content="",
            )
        self.assertEqual(caught.exception.code, "invalid_memory_content")

    def test_record_oversized_content_rejected(self) -> None:
        with self.assertRaises(MemoryError) as caught:
            self.service.record(
                type="knowledge",
                scope="global",
                key="test",
                content="x" * (MAX_CONTENT_BYTES + 1),
            )
        self.assertEqual(caught.exception.code, "memory_content_too_large")

    def test_record_supersedes_previous_with_same_key(self) -> None:
        first = self.service.record(
            type="user_preference",
            scope="global",
            key="theme",
            content="dark mode",
        )
        second = self.service.record(
            type="user_preference",
            scope="global",
            key="theme",
            content="light mode",
        )
        # Re-fetch the first to see its updated status
        first_updated = self.service.get(first.memory_id)
        self.assertEqual(first_updated.status, "superseded")
        self.assertEqual(first_updated.superseded_by, second.memory_id)
        self.assertEqual(second.status, "active")
        self.assertEqual(second.content, "light mode")

    def test_get_by_key_returns_active_only(self) -> None:
        self.service.record(
            type="project_context",
            scope="project",
            key="python_version",
            content="3.12",
        )
        self.service.record(
            type="project_context",
            scope="project",
            key="python_version",
            content="3.13",
        )
        mem = self.service.get_by_key(
            type="project_context",
            scope="project",
            key="python_version",
        )
        self.assertIsNotNone(mem)
        self.assertEqual(mem.content, "3.13")
        self.assertEqual(mem.status, "active")

    def test_get_by_key_returns_none_if_not_found(self) -> None:
        result = self.service.get_by_key(
            type="knowledge",
            scope="global",
            key="nonexistent",
        )
        self.assertIsNone(result)

    def test_recall_finds_matching_content(self) -> None:
        self.service.record(
            type="knowledge",
            scope="project",
            key="arch",
            content="Zevorinth uses 14-state state machine",
        )
        self.service.record(
            type="knowledge",
            scope="project",
            key="tools",
            content="Seven controlled tools available",
        )
        results = self.service.recall("state machine")
        self.assertEqual(len(results), 1)
        self.assertIn("state machine", results[0].content)

    def test_recall_filters_by_scope_and_type(self) -> None:
        self.service.record(
            type="knowledge",
            scope="global",
            key="g1",
            content="global knowledge about testing",
        )
        self.service.record(
            type="project_context",
            scope="project",
            key="p1",
            content="project uses pytest for testing",
        )
        results = self.service.recall(
            "testing", scope="project", type="project_context",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].scope, "project")
        self.assertEqual(results[0].type, "project_context")

    def test_recall_empty_query_returns_empty(self) -> None:
        self.service.record(
            type="knowledge",
            scope="global",
            key="test",
            content="test content",
        )
        results = self.service.recall("")
        self.assertEqual(results, [])

    def test_list_memories_returns_active_by_default(self) -> None:
        self.service.record(
            type="knowledge",
            scope="global",
            key="k1",
            content="content 1",
        )
        self.service.record(
            type="knowledge",
            scope="global",
            key="k1",
            content="content 2",
        )
        memories = self.service.list_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].content, "content 2")

    def test_list_memories_includes_superseded_with_all_status(self) -> None:
        self.service.record(
            type="knowledge",
            scope="global",
            key="k1",
            content="content 1",
        )
        self.service.record(
            type="knowledge",
            scope="global",
            key="k1",
            content="content 2",
        )
        memories = self.service.list_memories(status="all")
        self.assertEqual(len(memories), 2)

    def test_correct_supersedes_and_creates_new(self) -> None:
        original = self.service.record(
            type="knowledge",
            scope="project",
            key="fact",
            content="The earth is flat",
        )
        corrected = self.service.correct(
            original.memory_id, "The earth is an oblate spheroid"
        )
        # Re-fetch the original to see its updated status
        original_updated = self.service.get(original.memory_id)
        self.assertEqual(original_updated.status, "superseded")
        self.assertEqual(corrected.status, "active")
        self.assertEqual(corrected.content, "The earth is an oblate spheroid")
        self.assertEqual(corrected.key, original.key)

    def test_correct_nonexistent_rejected(self) -> None:
        with self.assertRaises(MemoryError) as caught:
            self.service.correct("nonexistent-id", "new content")
        self.assertEqual(caught.exception.code, "memory_not_found")

    def test_forget_marks_as_forgotten(self) -> None:
        mem = self.service.record(
            type="user_preference",
            scope="global",
            key="temp",
            content="temporary preference",
        )
        forgotten = self.service.forget(mem.memory_id)
        self.assertEqual(forgotten.status, "forgotten")
        active = self.service.get_by_key(
            type="user_preference",
            scope="global",
            key="temp",
        )
        self.assertIsNone(active)

    def test_forget_nonexistent_rejected(self) -> None:
        with self.assertRaises(MemoryError) as caught:
            self.service.forget("nonexistent-id")
        self.assertEqual(caught.exception.code, "memory_not_found")

    def test_record_task_outcome_creates_task_history(self) -> None:
        mem = self.service.record_task_outcome(
            task_id="abcdef12-3456-7890-abcd-ef1234567890",
            goal="创建 report.md",
            operation="write_text_artifact",
            status="succeeded",
            summary="Created report.md with 256 bytes, verified by independent readback.",
        )
        self.assertEqual(mem.type, "task_history")
        self.assertEqual(mem.scope, "project")
        self.assertEqual(mem.confidence, "high")
        self.assertIn("report.md", mem.content)
        self.assertIn("succeeded", mem.content)

    def test_record_task_outcome_failed_has_medium_confidence(self) -> None:
        mem = self.service.record_task_outcome(
            task_id="abcdef12-3456-7890-abcd-ef1234567890",
            goal="创建 report.md",
            operation="write_text_artifact",
            status="failed",
            summary="Content was too large.",
        )
        self.assertEqual(mem.confidence, "medium")

    def test_metadata_preserved(self) -> None:
        mem = self.service.record(
            type="decision_record",
            scope="project",
            key="chose_python",
            content="Chose Python 3.12 for compatibility",
            metadata={"alternatives": ["3.13", "3.14"], "reason": "venv"},
        )
        retrieved = self.service.get(mem.memory_id)
        self.assertEqual(retrieved.metadata["alternatives"], ["3.13", "3.14"])
        self.assertEqual(retrieved.metadata["reason"], "venv")

    def test_record_with_source_task_id(self) -> None:
        mem = self.service.record(
            type="task_history",
            scope="project",
            key="task:abc123",
            content="Task completed successfully",
            source_task_id="abc123",
        )
        self.assertEqual(mem.source_task_id, "abc123")

    def test_persistence_across_service_instances(self) -> None:
        mem = self.service.record(
            type="knowledge",
            scope="global",
            key="persistent",
            content="This should survive restart",
        )
        # Create a new service instance pointing to the same DB
        new_service = MemoryService(self.root)
        retrieved = new_service.get(mem.memory_id)
        self.assertEqual(retrieved.content, "This should survive restart")


class MemorySecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = MemoryService(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_confidence_levels_are_bounded(self) -> None:
        for level in ["high", "medium", "low"]:
            mem = self.service.record(
                type="knowledge",
                scope="global",
                key=f"conf_{level}",
                content=f"confidence {level}",
                confidence=level,
            )
            self.assertEqual(mem.confidence, level)

    def test_invalid_confidence_rejected(self) -> None:
        with self.assertRaises(MemoryError) as caught:
            self.service.record(
                type="knowledge",
                scope="global",
                key="test",
                content="content",
                confidence="ultimate",
            )
        self.assertEqual(caught.exception.code, "invalid_confidence")

    def test_key_length_bounded(self) -> None:
        with self.assertRaises(MemoryError) as caught:
            self.service.record(
                type="knowledge",
                scope="global",
                key="x" * (MAX_KEY_LENGTH + 1),
                content="content",
            )
        self.assertEqual(caught.exception.code, "memory_key_too_long")

    def test_query_length_bounded(self) -> None:
        self.service.record(
            type="knowledge",
            scope="global",
            key="test",
            content="test content",
        )
        # Long query should be truncated, not error
        results = self.service.recall("x" * (MAX_QUERY_LENGTH + 100))
        # Returns empty because the truncated query won't match
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
