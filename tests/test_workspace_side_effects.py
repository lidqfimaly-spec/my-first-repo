from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.event_store import EventStore  # noqa: E402
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import RUN_TESTS  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.workspace_snapshot import (  # noqa: E402
    EffectPolicy,
    Snapshot,
    SnapshotError,
    diff_snapshots,
    scan_workspace,
    validate_diff_record,
)


PASSING = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        self.assertTrue(True)\n"
)


def _snapshot(directory: Path, **kwargs: Any) -> Snapshot:
    return scan_workspace(
        directory,
        workspace_id="demo",
        task_id=kwargs.pop("task_id", "task"),
        attempt_id=kwargs.pop("attempt_id", "attempt"),
        claim_generation=kwargs.pop("claim_generation", 1),
        captured_at=kwargs.pop("captured_at", "now"),
        **kwargs,
    )


def _kinds(snapshot: Snapshot) -> dict[str, str]:
    return {entry.relative_path: entry.kind for entry in snapshot.entries}


class SnapshotTests(unittest.TestCase):
    def test_empty_workspace_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = _snapshot(Path(temp_dir))
        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.entries, [])
        self.assertEqual(snapshot.unreadable, [])

    def test_files_nested_dirs_and_hidden_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.txt").write_text("one", encoding="utf-8")
            (root / "sub").mkdir()
            (root / "sub" / "b.txt").write_text("two", encoding="utf-8")
            (root / ".hidden").write_text("h", encoding="utf-8")
            snapshot = _snapshot(root)
        paths = [entry.relative_path for entry in snapshot.entries]
        self.assertEqual(paths, [".hidden", "a.txt", "sub", "sub/b.txt"])
        self.assertEqual(paths, sorted(paths))
        self.assertTrue(snapshot.complete)
        self.assertTrue(all(entry.sha256 for entry in snapshot.entries if entry.kind == "file"))

    def test_symlinks_are_recorded_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            os.symlink(outside, root / "link")
            snapshot = _snapshot(root)
        kinds = _kinds(snapshot)
        self.assertEqual(kinds["link"], "symlink")
        self.assertNotIn("link/secret.txt", kinds)
        entry = next(e for e in snapshot.entries if e.relative_path == "link")
        self.assertEqual(entry.link_target, str(outside))

    def test_unreadable_directory_marks_snapshot_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            locked = root / "locked"
            locked.mkdir()
            (locked / "x.txt").write_text("x", encoding="utf-8")
            os.chmod(locked, 0)
            try:
                snapshot = _snapshot(root)
            finally:
                os.chmod(locked, stat.S_IRWXU)
        self.assertFalse(snapshot.complete)
        self.assertTrue(
            any(item["path"] == "locked" for item in snapshot.unreadable)
        )

    def test_file_limit_marks_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in range(5):
                (root / f"f{index}.txt").write_text("x", encoding="utf-8")
            snapshot = _snapshot(root, max_files=3)
        self.assertFalse(snapshot.complete)
        self.assertIn("file_limit", snapshot.limits_hit)
        self.assertEqual(len(snapshot.entries), 3)

    def test_total_byte_limit_marks_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.txt").write_text("x" * 100, encoding="utf-8")
            (root / "b.txt").write_text("y" * 100, encoding="utf-8")
            snapshot = _snapshot(root, max_total_bytes=120)
        self.assertFalse(snapshot.complete)
        self.assertTrue(
            any("total_bytes" in item for item in snapshot.limits_hit)
        )

    def test_file_too_large_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "big.bin").write_bytes(b"x" * 2048)
            snapshot = _snapshot(root, max_file_bytes=1024)
        self.assertFalse(snapshot.complete)
        entry = snapshot.entries[0]
        self.assertIsNone(entry.sha256)
        self.assertEqual(entry.size, 2048)

    def test_cancel_marks_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = _snapshot(Path(temp_dir), cancel_check=lambda: True)
        self.assertFalse(snapshot.complete)
        self.assertIn("cancelled", snapshot.limits_hit)

    def test_concurrent_modification_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "changing.txt"
            target.write_text("first", encoding="utf-8")
            from zevorinth import workspace_snapshot as module

            real_read = module._read_bytes

            def mutate_then_read(path: str, limit: int) -> bytes:
                data = real_read(path, limit)
                target.write_text("second content", encoding="utf-8")
                return data

            with patch.object(module, "_read_bytes", mutate_then_read):
                snapshot = _snapshot(root)
        self.assertFalse(snapshot.complete)
        self.assertTrue(
            any("unstable" in item for item in snapshot.limits_hit)
        )

    def test_snapshot_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "b.txt").write_text("b", encoding="utf-8")
            (root / "a.txt").write_text("a", encoding="utf-8")
            first = _snapshot(root).to_dict()
            second = _snapshot(root).to_dict()
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(
            [entry["path"] for entry in first["entries"]],
            ["a.txt", "b.txt"],
        )


class DiffPolicyTests(unittest.TestCase):
    def _pair(self, before: Path, after: Path) -> tuple[Snapshot, Snapshot]:
        return _snapshot(before), _snapshot(after)

    def test_read_only_policy_flags_any_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.txt").write_text("one", encoding="utf-8")
            before = _snapshot(root)
            (root / "b.txt").write_text("two", encoding="utf-8")
            after = _snapshot(root)
        diff = diff_snapshots(before, after, EffectPolicy.build({}))
        self.assertEqual(diff["counts"]["added"], 1)
        self.assertEqual(diff["counts"]["unexpected"], 1)
        self.assertEqual(
            diff["changes"][0]["reason"], "read_only_policy"
        )

    def test_allowed_prefix_is_expected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            before = _snapshot(root)
            reports = root / "reports"
            reports.mkdir()
            (reports / "out.txt").write_text("x", encoding="utf-8")
            after = _snapshot(root)
        policy = EffectPolicy.build({"allowed_side_effects": ["reports"]})
        diff = diff_snapshots(before, after, policy)
        self.assertEqual(diff["counts"]["expected"], 2)
        self.assertEqual(diff["counts"]["unexpected"], 0)

    def test_protected_path_overrides_allowed_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "reports").mkdir()
            before = _snapshot(root)
            (root / "reports" / "guard.txt").write_text("x", encoding="utf-8")
            after = _snapshot(root)
        policy = EffectPolicy.build(
            {
                "allowed_side_effects": ["reports"],
                "protected_paths": ["reports/guard.txt"],
            }
        )
        diff = diff_snapshots(before, after, policy)
        self.assertEqual(diff["counts"]["unexpected"], 1)
        self.assertEqual(diff["changes"][0]["reason"], "protected_path")

    def test_deleted_and_modified_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.txt").write_text("one", encoding="utf-8")
            (root / "b.txt").write_text("two", encoding="utf-8")
            before = _snapshot(root)
            (root / "a.txt").write_text("changed", encoding="utf-8")
            (root / "b.txt").unlink()
            after = _snapshot(root)
        diff = diff_snapshots(before, after, EffectPolicy.build({}))
        self.assertEqual(diff["counts"]["modified"], 1)
        self.assertEqual(diff["counts"]["deleted"], 1)
        self.assertEqual(diff["change_count"], 2)

    def test_type_and_symlink_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "item").write_text("file", encoding="utf-8")
            os.symlink("target-a", root / "link")
            before = _snapshot(root)
            (root / "item").unlink()
            (root / "item").mkdir()
            (root / "link").unlink()
            os.symlink("target-b", root / "link")
            after = _snapshot(root)
        diff = diff_snapshots(before, after, EffectPolicy.build({}))
        changes = {item["path"]: item["change"] for item in diff["changes"]}
        self.assertEqual(changes["item"], "type_changed")
        self.assertEqual(changes["link"], "symlink_changed")

    def test_forged_detail_list_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            before = _snapshot(root)
            (root / "x.txt").write_text("x", encoding="utf-8")
            after = _snapshot(root)
        diff = diff_snapshots(before, after, EffectPolicy.build({}))
        self.assertEqual(diff["change_count"], 1)
        diff["changes"] = []
        with self.assertRaises(SnapshotError):
            validate_diff_record(diff)

    def test_count_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            before = _snapshot(root)
            (root / "x.txt").write_text("x", encoding="utf-8")
            after = _snapshot(root)
        diff = diff_snapshots(before, after, EffectPolicy.build({}))
        diff["counts"]["added"] = 5
        with self.assertRaises(SnapshotError):
            validate_diff_record(diff)

    def test_diff_serialization_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            before = _snapshot(root)
            (root / "z.txt").write_text("z", encoding="utf-8")
            (root / "a.txt").write_text("a", encoding="utf-8")
            after = _snapshot(root)
        first = diff_snapshots(before, after, EffectPolicy.build({}))
        second = diff_snapshots(before, after, EffectPolicy.build({}))
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )
        self.assertEqual(
            [item["path"] for item in first["changes"]],
            ["a.txt", "z.txt"],
        )

    def test_large_diff_is_truncated_and_not_claimed_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            before = _snapshot(root)
            for index in range(205):
                (root / f"f{index:03d}.txt").write_text("x", encoding="utf-8")
            after = _snapshot(root)
        diff = diff_snapshots(before, after, EffectPolicy.build({}))
        self.assertEqual(diff["change_count"], 205)
        self.assertTrue(diff["truncated"])
        self.assertFalse(diff["detail_complete"])
        self.assertEqual(diff["retained_count"], 200)
        self.assertEqual(len(diff["changes"]), 200)
        validate_diff_record(diff)
        forged = json.loads(json.dumps(diff))
        forged["detail_complete"] = True
        with self.assertRaises(SnapshotError):
            validate_diff_record(forged)


def _workspace(root: Path, files: dict[str, str] | None = None) -> Path:
    workspace = root / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    for name, content in (files or {"test_ok.py": PASSING}).items():
        (workspace / name).write_text(content, encoding="utf-8")
    return workspace


def _request(**extra: Any) -> TaskRequest:
    parameters = {
        "runner": "unittest",
        "target": "test_ok",
        "workspace": "demo",
        "execution_backend": "host",
        "host_authorization": "local-maintenance",
    }
    parameters.update(extra)
    return TaskRequest(goal="run", operation=RUN_TESTS, parameters=parameters)


class LifecycleSideEffectTests(unittest.TestCase):
    def test_clean_run_succeeds_with_empty_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root)
            task = service.submit(_request())
            evidence = task.tool_result.test_evidence
            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            self.assertEqual(evidence.workspace_diff["change_count"], 0)
            self.assertTrue(evidence.before_snapshot["complete"])
            self.assertTrue(evidence.after_snapshot["complete"])

    def test_allowed_report_write_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(
                root,
                {
                    "test_ok.py": (
                        "import pathlib, unittest\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_ok(self):\n"
                        "        pathlib.Path('reports').mkdir(exist_ok=True)\n"
                        "        pathlib.Path('reports/out.txt').write_text('x')\n"
                    )
                },
            )
            service = TaskService(root)
            task = service.submit(
                _request(allowed_side_effects=["reports"])
            )
            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            diff = task.tool_result.test_evidence.workspace_diff
            self.assertEqual(diff["counts"]["expected"], 2)
            self.assertEqual(diff["counts"]["unexpected"], 0)

    def test_unexpected_write_fails_even_when_tests_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _workspace(root)
            (workspace / "source.py").write_text("original\n", encoding="utf-8")
            (workspace / "test_ok.py").write_text(
                "import pathlib, unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        pathlib.Path('source.py').write_text('tampered')\n",
                encoding="utf-8",
            )
            service = TaskService(root)
            task = service.submit(_request())
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(
                task.verification.reason, "unexpected_workspace_change"
            )
            self.assertEqual(
                (workspace / "source.py").read_text(), "tampered"
            )
            self.assertNotIn(
                "task_succeeded",
                [e.event_type for e in service.events(task.task_id)],
            )

    def test_protected_file_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _workspace(
                root,
                {
                    "test_ok.py": (
                        "import pathlib, unittest\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_ok(self):\n"
                        "        pathlib.Path('reports').mkdir(exist_ok=True)\n"
                        "        pathlib.Path('reports/guard.txt').write_text('x')\n"
                    )
                },
            )
            service = TaskService(root)
            task = service.submit(
                _request(
                    allowed_side_effects=["reports"],
                    protected_paths=["reports/guard.txt"],
                )
            )
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(
                task.verification.reason, "unexpected_workspace_change"
            )

    def test_before_snapshot_failure_blocks_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _workspace(root)
            locked = workspace / "locked"
            locked.mkdir()
            os.chmod(locked, 0)
            try:
                service = TaskService(root)
                task = service.submit(_request())
            finally:
                os.chmod(locked, stat.S_IRWXU)
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "snapshot_incomplete_before")
            self.assertNotIn(
                "tool_started",
                [e.event_type for e in service.events(task.task_id)],
            )

    def test_timeout_still_records_after_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(
                root,
                {
                    "test_ok.py": (
                        "import time, unittest\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_ok(self):\n"
                        "        time.sleep(30)\n"
                    )
                },
            )
            service = TaskService(root)
            task = service.submit(_request(timeout_seconds=1))
            evidence = task.tool_result.test_evidence
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "test_timeout")
            self.assertIsNotNone(evidence.after_snapshot)
            self.assertTrue(evidence.after_snapshot["complete"])

    def test_unknown_recovery_blocks_without_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root, clock=lambda: "2026-10-20T00:00:00.000+00:00")
            task = service.enqueue(_request())
            claim = service._claim(
                task.task_id, allowed=TaskService.RUNNABLE_STATUSES
            )
            token = str(claim.task.lease_token)
            service.store.record_event_and_update(
                task.task_id,
                "tool_started",
                "2026-10-20T00:00:00.000+00:00",
                updates={"phase": "test_started"},
                lease_token=token,
            )
            service.store.expire_claim(
                task.task_id, now="2026-10-20T00:01:00.000+00:00"
            )
            resumed = TaskService(
                root, clock=lambda: "2026-10-20T00:01:00.000+00:00"
            ).resume(task.task_id)
            self.assertEqual(resumed.status, TaskStatus.BLOCKED)
            self.assertEqual(resumed.error.code, "test_execution_unknown")
            starts = [
                e.event_type
                for e in TaskService(root).events(task.task_id)
            ].count("tool_started")
            self.assertEqual(starts, 1)

    def test_verification_resume_does_not_rerun(self) -> None:
        class FaultStore(EventStore):
            def _insert_event(self, connection, event):
                if event.event_type == "verification_started":
                    raise RuntimeError("injected")
                return super()._insert_event(connection, event)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root, clock=lambda: "2026-10-20T00:00:00.000+00:00")
            service.store = FaultStore(service.database_path)
            task = service.enqueue(_request())
            with self.assertRaises(RuntimeError):
                service.execute_task(task.task_id)
            service.store.expire_claim(
                task.task_id, now="2026-10-20T00:01:00.000+00:00"
            )
            resumed = TaskService(
                root, clock=lambda: "2026-10-20T00:01:00.000+00:00"
            ).resume(task.task_id)
            self.assertEqual(resumed.status, TaskStatus.SUCCEEDED)
            starts = [
                e.event_type
                for e in TaskService(root).events(task.task_id)
            ].count("tool_started")
            self.assertEqual(starts, 1)

    def test_stale_snapshot_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root)
            task = service.submit(_request())
            evidence = task.tool_result.test_evidence
            tampered = dict(evidence.before_snapshot)
            tampered["claim_generation"] = 99
            from zevorinth.verifier import TestResultVerifier

            result = TestResultVerifier().verify(
                task=task,
                evidence=type(evidence).from_dict(
                    {**evidence.to_dict(), "before_snapshot": tampered}
                ),
                expected_parameters={
                    "runner": "unittest",
                    "target": "test_ok",
                    "workspace": "demo",
                },
                checked_at="now",
            )
            self.assertFalse(result.passed)
            self.assertEqual(result.reason, "snapshot_identity_mismatch")

    def test_diff_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(
                root,
                {
                    "test_ok.py": (
                        "import pathlib, unittest\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_ok(self):\n"
                        "        pathlib.Path('x.txt').write_text('x')\n"
                    )
                },
            )
            service = TaskService(root)
            task = service.submit(_request())
            evidence = task.tool_result.test_evidence
            forged = json.loads(json.dumps(evidence.workspace_diff))
            forged["changes"] = []
            forged["retained_count"] = 0
            forged["change_count"] = 0
            forged["detail_complete"] = True
            forged["truncated"] = False
            forged["changes_truncated"] = False
            forged["counts"]["added"] = 0
            forged["counts"]["unexpected"] = 0
            from zevorinth.verifier import TestResultVerifier

            result = TestResultVerifier().verify(
                task=task,
                evidence=type(evidence).from_dict(
                    {**evidence.to_dict(), "workspace_diff": forged}
                ),
                expected_parameters={
                    "runner": "unittest",
                    "target": "test_ok",
                    "workspace": "demo",
                },
                checked_at="now",
            )
            self.assertFalse(result.passed)
            self.assertEqual(result.reason, "diff_recompute_mismatch")


if __name__ == "__main__":
    unittest.main()
