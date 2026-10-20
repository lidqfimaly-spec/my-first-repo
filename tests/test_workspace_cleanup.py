from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import TaskRequest  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.workspace_cleanup import (  # noqa: E402
    execute_cleanup,
    ownership_records,
    plan_cleanup,
)
from zevorinth.workspace_effects import (  # noqa: E402
    build_workspace_effects,
    validate_workspace_effects,
)
from zevorinth.workspace_snapshot import scan_workspace  # noqa: E402


def _written(root: Path) -> tuple[TaskService, Any, Path]:
    service = TaskService(root)
    task = service.submit(
        TaskRequest(
            goal="write",
            operation="write_text_artifact",
            parameters={"artifact_path": "a.txt", "content": "hi\n"},
        )
    )
    return service, task, root / "artifacts" / task.task_id / "a.txt"


class CleanupTests(unittest.TestCase):
    def test_cleanup_is_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            record = service.cleanup_workspace_file(task.task_id, "a.txt")
            self.assertEqual(record["status"], "rejected")
            self.assertEqual(record["code"], "cleanup_disabled")
            self.assertTrue(target.exists())

    def test_unconfirmed_cleanup_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            record = service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True
            )
            self.assertEqual(record["code"], "cleanup_unauthorized")
            self.assertTrue(target.exists())

    def test_owned_unchanged_file_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            record = service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            self.assertEqual(record["status"], "completed")
            self.assertTrue(record["removed"])
            self.assertFalse(target.exists())
            events = [e.event_type for e in service.events(task.task_id)]
            self.assertIn("cleanup_completed", events)

    def test_repeated_cleanup_does_not_delete_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            again = service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            self.assertEqual(again["status"], "rejected")
            self.assertEqual(again["code"], "missing_target")
            self.assertFalse(target.exists())

    def test_modified_file_is_not_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            target.write_text("changed\n")
            record = service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            self.assertEqual(record["code"], "hash_mismatch")
            self.assertTrue(target.exists())

    def test_user_file_is_never_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, _target = _written(root)
            user_file = root / "artifacts" / task.task_id / "user.txt"
            user_file.write_text("mine\n")
            record = service.cleanup_workspace_file(
                task.task_id, "user.txt", enabled=True, authorized=True
            )
            self.assertEqual(record["code"], "not_owned")
            self.assertTrue(user_file.exists())

    def test_symlink_target_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            outside = root / "outside.txt"
            outside.write_text("hi\n")
            target.unlink()
            os.symlink(outside, target)
            record = service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            self.assertEqual(record["code"], "unsafe_path")
            self.assertTrue(outside.exists())

    def test_protected_path_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "keep.txt").write_text("keep\n")
            before = scan_workspace(
                workspace,
                workspace_id="demo",
                task_id="t",
                attempt_id="a",
                claim_generation=1,
                captured_at="before",
            )
            (workspace / "keep.txt").write_text("keep2\n")
            after = scan_workspace(
                workspace,
                workspace_id="demo",
                task_id="t",
                attempt_id="a",
                claim_generation=1,
                captured_at="after",
            )
            effects = build_workspace_effects(
                operation="run_tests",
                source="test",
                workspace="demo",
                task_id="t",
                attempt_id="a",
                claim_generation=1,
                before=before,
                after=after,
                policy_parameters={
                    "allowed_side_effects": ["."],
                    "protected_paths": ["keep.txt"],
                },
            )
            validate_workspace_effects(effects)
            self.assertEqual(ownership_records(effects), [])
            decision = plan_cleanup(
                effects=effects,
                workspace_root=workspace,
                relative_path="keep.txt",
                current_generation=1,
                enabled=True,
                authorized=True,
            )
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.code, "protected_path")

    def test_stale_generation_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            effects = task.tool_result.workspace_effects
            decision = plan_cleanup(
                effects=effects,
                workspace_root=root / "artifacts" / task.task_id,
                relative_path="a.txt",
                current_generation=task.claim_generation + 1,
                enabled=True,
                authorized=True,
            )
            self.assertEqual(decision.code, "stale_generation")
            self.assertTrue(target.exists())

    def test_change_between_plan_and_delete_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            decision = plan_cleanup(
                effects=task.tool_result.workspace_effects,
                workspace_root=root / "artifacts" / task.task_id,
                relative_path="a.txt",
                current_generation=task.claim_generation,
                enabled=True,
                authorized=True,
            )
            self.assertTrue(decision.allowed)
            target.write_text("changed after plan\n")
            outcome = execute_cleanup(decision)
            self.assertEqual(outcome["status"], "rejected")
            self.assertEqual(outcome["code"], "target_changed")
            self.assertTrue(target.exists())

    def test_cleanup_evidence_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, _target = _written(root)
            record = service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            self.assertTrue(record["evidence_persisted"])
            events = service.events(task.task_id)
            self.assertEqual(
                [e.event_type for e in events].count("cleanup_completed"), 1
            )
            payload = [e for e in events if e.event_type == "cleanup_completed"][0]
            self.assertEqual(payload.payload["sha256"], record["sha256"])

    def test_cleanup_does_not_change_task_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, _target = _written(root)
            before = service.require_task(task.task_id).status
            service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            after = service.require_task(task.task_id).status
            self.assertEqual(before, after)

    def test_forged_effects_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, target = _written(root)
            effects = dict(task.tool_result.workspace_effects)
            effects["total_changes"] = 99
            decision = plan_cleanup(
                effects=effects,
                workspace_root=root / "artifacts" / task.task_id,
                relative_path="a.txt",
                current_generation=task.claim_generation,
                enabled=True,
                authorized=True,
            )
            self.assertFalse(decision.allowed)
            self.assertTrue(target.exists())



class EffectContractTests(unittest.TestCase):
    def _record(self, root: Path) -> dict:
        workspace = root / "ws"
        workspace.mkdir()
        before = scan_workspace(
            workspace, workspace_id="w", task_id="t", attempt_id="a",
            claim_generation=1, captured_at="before",
        )
        (workspace / "out.txt").write_text("x", encoding="utf-8")
        after = scan_workspace(
            workspace, workspace_id="w", task_id="t", attempt_id="a",
            claim_generation=1, captured_at="after",
        )
        return build_workspace_effects(
            operation="run_tests", source="test", workspace="w",
            task_id="t", attempt_id="a", claim_generation=1,
            before=before, after=after,
            policy_parameters={"allowed_side_effects": ["out.txt"]},
        )

    def test_valid_record_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record = self._record(Path(temp_dir))
        validate_workspace_effects(record)
        self.assertEqual(record["diff"]["counts"]["expected"], 1)

    def test_each_inconsistency_is_rejected(self) -> None:
        import copy

        from zevorinth.workspace_snapshot import SnapshotError

        with tempfile.TemporaryDirectory() as temp_dir:
            base = self._record(Path(temp_dir))
        mutations = []
        broken = copy.deepcopy(base)
        broken.pop("diff")
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["operation"] = "shell"
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["diff"] = "not-an-object"
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["truncated"] = True
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["detail_complete"] = False
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["total_changes"] = 5
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["retained_changes"] = 5
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["complete"] = False
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["attempt_id"] = "other"
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["diff"]["counts"]["added"] = 9
        mutations.append(broken)
        for index, broken in enumerate(mutations):
            with self.assertRaises(SnapshotError, msg=f"case {index}"):
                validate_workspace_effects(broken)

    def test_not_applicable_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record = self._record(Path(temp_dir))
        self.assertTrue(record["applicable"])
        self.assertIn("diff", record)


if __name__ == "__main__":
    unittest.main()
