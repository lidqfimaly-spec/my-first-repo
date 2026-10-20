"""Phase 3.62: database and evidence integrity fault isolation."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.test_lifecycle_audit_coverage import _host_test_task  # noqa: E402
from zevorinth.event_store import EventStoreError  # noqa: E402
from zevorinth.models import TaskRequest  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


NOW = "2026-10-20T00:00:00.000+00:00"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StorageIntegrityFaultTests(unittest.TestCase):
    def test_corrupt_database_copy_fails_without_touching_original(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="zv-storage-source-"))
        service = TaskService(root)
        task = service.enqueue(
            TaskRequest(goal="write", operation=None, parameters={})
        )
        original_path = service.database_path
        original_hash = _sha(original_path)

        corrupt_root = Path(tempfile.mkdtemp(prefix="zv-storage-corrupt-"))
        corrupt_db = corrupt_root / "data" / "zevorinth.sqlite3"
        corrupt_db.parent.mkdir(parents=True)
        corrupt_db.write_bytes(b"not a sqlite database")
        with self.assertRaises((sqlite3.DatabaseError, EventStoreError)):
            TaskService(corrupt_root)

        self.assertEqual(_sha(original_path), original_hash)
        self.assertIsNotNone(service.store.require_task(task.task_id))

    def test_corrupt_task_json_returns_structured_storage_failure(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="zv-storage-json-"))
        service = TaskService(root)
        task = service.enqueue(
            TaskRequest(goal="write", operation=None, parameters={})
        )
        with sqlite3.connect(service.database_path) as connection:
            connection.execute(
                "UPDATE tasks SET task_json=? WHERE task_id=?",
                ("not-json", task.task_id),
            )
            connection.commit()
        before = _sha(service.database_path)

        report = service.audit_lifecycle(task.task_id, now=NOW)

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "unknown")
        self.assertIn(
            "storage_unreadable",
            [item["code"] for item in report["issues"]],
        )
        self.assertFalse(report["task_recovery"]["safe_to_retry"])
        self.assertTrue(
            report["task_recovery"]["manual_intervention"]
        )
        self.assertEqual(_sha(service.database_path), before)

    def test_corrupt_or_missing_evidence_is_unverified(self) -> None:
        for mode in ("corrupt", "missing"):
            with self.subTest(mode=mode):
                service, task, _ = _host_test_task()
                evidence = replace(
                    task.tool_result.test_evidence,
                    evidence_id="auto-corrupt",
                    config_digest="a" * 64,
                    command_digest="b" * 64,
                )
                tool_result = replace(
                    task.tool_result, test_evidence=evidence
                )
                snapshot = dict(task.execution_plan_snapshot)
                snapshot.update(
                    {
                        "evidence_id": "auto-corrupt",
                        "config_digest": "a" * 64,
                        "command_digest": "b" * 64,
                    }
                )
                service.store.record_event_and_update(
                    task.task_id,
                    "evidence_fixture",
                    NOW,
                    updates={
                        "tool_result": tool_result,
                        "execution_plan_version": task.plan_version,
                        "execution_plan_snapshot": snapshot,
                    },
                )
                evidence_path = (
                    service.project_root
                    / "evidence"
                    / "auto-corrupt.json"
                )
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                if mode == "corrupt":
                    evidence_path.write_text("{broken", encoding="utf-8")
                else:
                    if evidence_path.exists():
                        evidence_path.unlink()
                before = (
                    evidence_path.read_bytes()
                    if evidence_path.exists()
                    else None
                )

                report = service.audit_lifecycle(task.task_id)

                self.assertEqual(
                    report["evidence_state"]["state"], "blocked"
                )
                self.assertIn(
                    "succeeded_with_unverified_evidence",
                    [item["code"] for item in report["issues"]],
                )
                after = (
                    evidence_path.read_bytes()
                    if evidence_path.exists()
                    else None
                )
                self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
