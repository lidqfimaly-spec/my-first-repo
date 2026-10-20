"""Deterministic race injection for guarded cleanup."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.event_store import EventStore  # noqa: E402
from zevorinth.models import TaskRequest  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.workspace_cleanup import (  # noqa: E402
    execute_cleanup,
    plan_cleanup,
)


def _written(root: Path, name: str = "a.txt", content: str = "hi\n"):
    service = TaskService(root)
    task = service.submit(
        TaskRequest(
            goal="write",
            operation="write_text_artifact",
            parameters={"artifact_path": name, "content": content},
        )
    )
    return service, task, root / "artifacts" / task.task_id / name


def _plan(service: TaskService, task: Any, name: str, root: Path):
    return plan_cleanup(
        effects=task.tool_result.workspace_effects,
        workspace_root=root / "artifacts" / task.task_id,
        relative_path=name,
        current_generation=task.claim_generation,
        enabled=True,
        authorized=True,
    )


class CleanupRaceTests(unittest.TestCase):
    def _run_with(
        self,
        mutate: Callable[[Path], None],
        stage: str,
    ) -> tuple[dict[str, Any], dict[str, bool]]:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            decision = _plan(service, task, "a.txt", root)
            self.assertTrue(decision.allowed)

            def checkpoint(current: str) -> None:
                if current == stage:
                    mutate(target)

            outcome = execute_cleanup(
                decision,
                workspace_root=root / "artifacts" / task.task_id,
                checkpoint=checkpoint,
            )
            state = {
                "exists": target.exists(),
                "is_symlink": target.is_symlink(),
            }
            return outcome, state

    def test_content_changed_before_verification(self) -> None:
        outcome, state = self._run_with(
            lambda path: path.write_text("mutated\n"), "opened"
        )
        self.assertEqual(outcome["status"], "rejected")
        self.assertEqual(outcome["code"], "hash_mismatch")
        self.assertTrue(state["exists"])

    def test_replaced_by_symlink_before_unlink(self) -> None:
        outside_holder: dict[str, Path] = {}

        def mutate(path: Path) -> None:
            outside = path.parent / "outside.txt"
            outside.write_text("hi\n")
            outside_holder["outside"] = outside
            path.unlink()
            os.symlink(outside, path)

        outcome, state = self._run_with(mutate, "before_quarantine")
        self.assertEqual(outcome["status"], "unverified")
        self.assertEqual(outcome["code"], "target_replaced")
        self.assertTrue(state["is_symlink"])

    def test_replaced_by_another_regular_file(self) -> None:
        def mutate(path: Path) -> None:
            replacement = path.parent / "replacement.txt"
            replacement.write_text("hi\n")
            os.replace(replacement, path)

        outcome, state = self._run_with(mutate, "before_quarantine")
        self.assertEqual(outcome["code"], "target_replaced")
        self.assertTrue(state["exists"])

    def test_parent_directory_replaced_keeps_new_tree_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            task_dir = target.parent
            decision = _plan(service, task, "a.txt", root)
            moved = root / "old-task-dir"

            def checkpoint(stage: str) -> None:
                if stage != "before_quarantine":
                    return
                task_dir.rename(moved)
                task_dir.mkdir()
                (task_dir / "a.txt").write_text("new tree\n")

            outcome = execute_cleanup(
                decision,
                workspace_root=task_dir,
                checkpoint=checkpoint,
            )
            # The handle still pointed at the original directory.
            self.assertFalse((moved / "a.txt").exists())
            self.assertTrue((task_dir / "a.txt").exists())

    def test_stale_generation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            decision = plan_cleanup(
                effects=task.tool_result.workspace_effects,
                workspace_root=root / "artifacts" / task.task_id,
                relative_path="a.txt",
                current_generation=task.claim_generation + 1,
                enabled=True,
                authorized=True,
            )
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.code, "stale_generation")
            self.assertTrue(target.exists())

    def test_concurrent_requests_do_not_double_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            results: list[dict[str, Any]] = []
            barrier = threading.Barrier(2)

            def worker() -> None:
                barrier.wait(timeout=5)
                results.append(
                    service.cleanup_workspace_file(
                        task.task_id,
                        "a.txt",
                        enabled=True,
                        authorized=True,
                    )
                )

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            statuses = sorted(item["status"] for item in results)
            self.assertEqual(statuses.count("completed"), 1)
            self.assertEqual(statuses.count("rejected"), 1)
            self.assertFalse(target.exists())
            events = [
                e.event_type for e in service.events(task.task_id)
            ].count("cleanup_completed")
            self.assertEqual(events, 1)

    def test_event_persistence_failure_is_reported(self) -> None:
        class FailingStore(EventStore):
            def record_event_and_update(self, *args, **kwargs):
                raise RuntimeError("event store down")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, _target = _written(root)
            service.store = FailingStore(service.database_path)
            record = service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            self.assertFalse(record["evidence_persisted"])

    def test_disappearing_target_is_not_reported_as_removed(self) -> None:
        outcome, state = self._run_with(
            lambda path: path.unlink(), "before_quarantine"
        )
        self.assertIn(
            outcome["status"], {"rejected", "unverified", "failed"}
        )
        self.assertFalse(outcome["removed"])
        self.assertFalse(state["exists"])

    def test_repeat_request_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            first = service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            self.assertEqual(first["status"], "completed")
            # A new file with the same name must survive a repeat request.
            target.write_text("hi\n")
            second = service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            self.assertEqual(second["code"], "already_cleaned")
            self.assertTrue(target.exists())

    def test_escaping_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, _target = _written(root)
            for path in ("../x", "/etc/passwd", "a/../../x"):
                record = service.cleanup_workspace_file(
                    task.task_id, path, enabled=True, authorized=True
                )
                self.assertEqual(record["code"], "invalid_path", path)

    def test_batch_reports_partial_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            result = service.cleanup_workspace_files(
                task.task_id,
                ["a.txt", "never-created.txt"],
                enabled=True,
                authorized=True,
            )
            self.assertEqual(result["batch_status"], "partial")
            self.assertEqual(result["files"][0]["status"], "completed")
            self.assertEqual(result["files"][1]["status"], "rejected")
            self.assertFalse(target.exists())

    def test_batch_never_infers_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, _target = _written(root)
            sibling = root / "artifacts" / task.task_id / "sibling.txt"
            sibling.write_text("not owned\n")
            result = service.cleanup_workspace_files(
                task.task_id,
                ["sibling.txt"],
                enabled=True,
                authorized=True,
            )
            self.assertEqual(result["files"][0]["code"], "not_owned")
            self.assertTrue(sibling.exists())

    def test_new_file_after_quarantine_survives(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            decision = _plan(service, task, "a.txt", root)

            def checkpoint(stage: str) -> None:
                if stage == "before_unlink":
                    target.write_text("recreated by someone else\n")

            outcome = execute_cleanup(
                decision,
                workspace_root=root / "artifacts" / task.task_id,
                checkpoint=checkpoint,
            )
            self.assertEqual(outcome["status"], "completed")
            self.assertTrue(target.exists())
            self.assertEqual(
                target.read_text(), "recreated by someone else\n"
            )

    def test_quarantine_object_modified_before_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            task_dir = root / "artifacts" / task.task_id
            decision = _plan(service, task, "a.txt", root)

            def checkpoint(stage: str) -> None:
                if stage != "after_quarantine":
                    return
                for path in (task_dir / ".quarantine").rglob("a.txt"):
                    path.write_text("changed in quarantine\n")

            outcome = execute_cleanup(
                decision,
                workspace_root=task_dir,
                checkpoint=checkpoint,
            )
            self.assertEqual(outcome["status"], "unverified")
            self.assertEqual(outcome["code"], "quarantine_mismatch")
            self.assertFalse(outcome["removed"])
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
