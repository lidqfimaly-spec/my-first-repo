from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import (  # noqa: E402
    ArtifactToolError,
    SimulatedArtifactWriter,
)
from zevorinth.models import (  # noqa: E402
    ExecutionMode,
    Task,
    TaskRequest,
    TaskStatus,
    ToolResult,
)
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


def request(
    *,
    artifact_path: str | None = "result.txt",
    content: str | None = "verified content\n",
    goal: str | None = "create a verified artifact",
    operation: str | None = WRITE_TEXT_ARTIFACT,
) -> TaskRequest:
    return TaskRequest(
        goal=goal,
        operation=operation,
        parameters={"artifact_path": artifact_path, "content": content},
    )


class FailingWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self) -> None:
        self.calls = 0

    def write(self, **_: Any) -> ToolResult:
        self.calls += 1
        raise ArtifactToolError("injected_write_failure", "controlled disk failure")


class FailIfCalledWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self) -> None:
        self.calls = 0

    def write(self, **_: Any) -> ToolResult:
        self.calls += 1
        raise AssertionError("writer must not run during recovery verification")


class AcceptanceTests(unittest.TestCase):
    def test_a1_real_execution_success_is_independently_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            task = service.submit(request())

            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            self.assertEqual(task.execution_mode, ExecutionMode.REAL)
            self.assertIsNotNone(task.verification)
            self.assertTrue(task.verification.passed)
            self.assertEqual(task.plan.planner_name, "deterministic-v1")
            self.assertEqual(len(task.plan.steps), 1)
            self.assertFalse(task.plan.deterministic is False)

            artifact_path = root / "artifacts" / task.task_id / "result.txt"
            actual = artifact_path.read_bytes()
            self.assertEqual(actual, "verified content\n".encode())
            self.assertEqual(task.verification.artifact.byte_count, len(actual))
            self.assertEqual(
                task.verification.artifact.sha256,
                hashlib.sha256(actual).hexdigest(),
            )

            events = service.events(task.task_id)
            event_types = [event.event_type for event in events]
            self.assertLess(event_types.index("tool_started"), event_types.index("tool_succeeded"))
            self.assertLess(
                event_types.index("verification_started"),
                event_types.index("verification_completed"),
            )
            self.assertEqual(event_types.count("task_succeeded"), 1)
            self.assertEqual([event.sequence for event in events], sorted(event.sequence for event in events))

            reopened = TaskService(root)
            persisted = reopened.require_task(task.task_id)
            self.assertEqual(persisted.status, TaskStatus.SUCCEEDED)
            self.assertEqual(persisted.verification.reason, "artifact_verified")

    def test_a2_tool_failure_is_not_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            failing = FailingWriter()
            service = TaskService(root, writer=failing)
            task = service.submit(request())

            self.assertEqual(failing.calls, 1)
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.execution_mode, ExecutionMode.REAL)
            self.assertEqual(task.error.code, "injected_write_failure")
            self.assertIsNone(task.verification)
            self.assertFalse((root / "artifacts" / task.task_id / "result.txt").exists())
            types = [event.event_type for event in service.events(task.task_id)]
            self.assertIn("tool_failed", types)
            self.assertNotIn("verification_completed", types)
            self.assertNotIn("task_succeeded", types)

            healthy = TaskService(root)
            next_task = healthy.submit(request(artifact_path="next.txt", content="next"))
            self.assertEqual(next_task.status, TaskStatus.SUCCEEDED)
            self.assertTrue(
                (root / "artifacts" / next_task.task_id / "next.txt").is_file()
            )

    def test_a3_missing_fields_require_clarification_then_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            task = service.submit(request(content=None))

            self.assertEqual(task.status, TaskStatus.AWAITING_CLARIFICATION)
            self.assertEqual(task.execution_mode, ExecutionMode.NOT_EXECUTED)
            self.assertEqual(task.missing_fields, ["content"])
            self.assertFalse((root / "artifacts" / task.task_id).exists())
            types = [event.event_type for event in service.events(task.task_id)]
            self.assertNotIn("tool_started", types)

            resumed = service.resume(
                task.task_id,
                {"parameters": {"content": "completed after clarification"}},
            )
            self.assertEqual(resumed.status, TaskStatus.SUCCEEDED)
            self.assertEqual(resumed.execution_mode, ExecutionMode.REAL)
            self.assertEqual(
                (root / "artifacts" / task.task_id / "result.txt").read_text(),
                "completed after clarification",
            )

    def test_a4_policy_blocks_paths_before_tool_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            outside = root / "outside.txt"

            traversal = service.submit(request(artifact_path="../outside.txt"))
            absolute = service.submit(request(artifact_path=str(outside)))
            self.assertEqual(traversal.status, TaskStatus.BLOCKED)
            self.assertEqual(absolute.status, TaskStatus.BLOCKED)
            self.assertEqual(traversal.execution_mode, ExecutionMode.NOT_EXECUTED)
            self.assertEqual(absolute.execution_mode, ExecutionMode.NOT_EXECUTED)
            self.assertFalse(outside.exists())
            for task in (traversal, absolute):
                types = [event.event_type for event in service.events(task.task_id)]
                self.assertIn("policy_rejected", types)
                self.assertNotIn("tool_started", types)

            blocked_event_count = len(service.events(traversal.task_id))
            unchanged = service.resume(traversal.task_id)
            self.assertEqual(unchanged.status, TaskStatus.BLOCKED)
            self.assertEqual(len(service.events(traversal.task_id)), blocked_event_count)

            corrected = service.resume(
                traversal.task_id,
                {"parameters": {"artifact_path": "safe.txt"}},
            )
            self.assertEqual(corrected.status, TaskStatus.SUCCEEDED)
            self.assertTrue(
                (root / "artifacts" / traversal.task_id / "safe.txt").is_file()
            )

    def test_a4_symlink_escape_and_overwrite_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            root.joinpath("artifacts").mkdir(exist_ok=True)
            outside = root / "outside"
            outside.mkdir()

            symlink_task = service.submit(request(artifact_path=None))
            (root / "artifacts" / symlink_task.task_id).symlink_to(
                outside, target_is_directory=True
            )
            symlink_resume = service.resume(
                symlink_task.task_id,
                {"parameters": {"artifact_path": "escape.txt"}},
            )
            self.assertEqual(symlink_resume.status, TaskStatus.BLOCKED)
            self.assertFalse((outside / "escape.txt").exists())

            overwrite_task = service.submit(request(artifact_path=None))
            task_dir = root / "artifacts" / overwrite_task.task_id
            task_dir.mkdir()
            (task_dir / "existing.txt").write_text("preserve me")
            overwrite_resume = service.resume(
                overwrite_task.task_id,
                {"parameters": {"artifact_path": "existing.txt"}},
            )
            self.assertEqual(overwrite_resume.status, TaskStatus.BLOCKED)
            self.assertEqual(overwrite_resume.error.code, "artifact_exists")
            self.assertEqual((task_dir / "existing.txt").read_text(), "preserve me")

            file_collision_task = service.submit(request(artifact_path=None))
            collision_path = root / "artifacts" / file_collision_task.task_id
            collision_path.write_text("not a directory")
            collision_resume = service.resume(
                file_collision_task.task_id,
                {"parameters": {"artifact_path": "blocked.txt"}},
            )
            self.assertEqual(collision_resume.status, TaskStatus.BLOCKED)
            self.assertEqual(
                collision_resume.error.code,
                "unsafe_artifact_directory",
            )
            self.assertEqual(collision_path.read_text(), "not a directory")

    def test_a4_symlinked_artifacts_root_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside"
            outside.mkdir()
            (root / "artifacts").symlink_to(outside, target_is_directory=True)

            task = TaskService(root).submit(request())
            self.assertEqual(task.status, TaskStatus.BLOCKED)
            self.assertEqual(task.execution_mode, ExecutionMode.NOT_EXECUTED)
            self.assertEqual(task.error.code, "unsafe_artifacts_root")
            self.assertFalse((outside / "result.txt").exists())

    def test_a5_interrupted_after_write_recovers_by_verifying_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            interrupted_ids: list[str] = []
            base_time = "2026-10-19T00:00:00.000+00:00"
            future_time = "2026-10-19T00:01:01.000+00:00"

            def simulate_process_death(stage: str, task: Task) -> None:
                self.assertEqual(stage, "after_write_before_verification")
                interrupted_ids.append(task.task_id)
                raise RuntimeError("simulated process death before verification")

            crashing_service = TaskService(
                root,
                fault_hook=simulate_process_death,
                clock=lambda: base_time,
            )
            with self.assertRaises(RuntimeError):
                crashing_service.submit(request(content="recover me\n"))

            task_id = interrupted_ids[0]
            before_restart = crashing_service.require_task(task_id)
            self.assertEqual(before_restart.status, TaskStatus.VERIFYING)
            self.assertTrue((root / "artifacts" / task_id / "result.txt").is_file())

            no_write = FailIfCalledWriter()
            restarted = TaskService(
                root,
                writer=no_write,
                clock=lambda: future_time,
            )
            recovered = restarted.require_task(task_id)
            self.assertEqual(recovered.status, TaskStatus.INTERRUPTED)
            self.assertEqual(no_write.calls, 0)

            resumed = restarted.resume(task_id)
            self.assertEqual(resumed.status, TaskStatus.SUCCEEDED)
            self.assertEqual(no_write.calls, 0)
            self.assertEqual(
                (root / "artifacts" / task_id / "result.txt").read_text(),
                "recover me\n",
            )
            events = restarted.events(task_id)
            self.assertEqual(
                [event.event_type for event in events].count("task_succeeded"), 1
            )

            event_count = len(events)
            repeated = restarted.resume(task_id)
            self.assertEqual(repeated.status, TaskStatus.SUCCEEDED)
            self.assertEqual(len(restarted.events(task_id)), event_count)

    def test_a5_modified_file_is_not_overwritten_during_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_ids: list[str] = []
            base_time = "2026-10-19T00:00:00.000+00:00"
            future_time = "2026-10-19T00:01:01.000+00:00"

            def interrupt(_: str, task: Task) -> None:
                task_ids.append(task.task_id)
                raise RuntimeError("interrupt")

            crashing = TaskService(
                root,
                fault_hook=interrupt,
                clock=lambda: base_time,
            )
            with self.assertRaises(RuntimeError):
                crashing.submit(request(content="original"))
            task_id = task_ids[0]
            artifact = root / "artifacts" / task_id / "result.txt"
            artifact.write_text("tampered")

            no_write = FailIfCalledWriter()
            restarted = TaskService(
                root,
                writer=no_write,
                clock=lambda: future_time,
            )
            self.assertEqual(restarted.require_task(task_id).status, TaskStatus.INTERRUPTED)
            failed = restarted.resume(task_id)

            self.assertEqual(failed.status, TaskStatus.FAILED)
            self.assertEqual(failed.error.code, "verification_failed")
            self.assertIn(
                failed.verification.reason,
                {"artifact_content_mismatch", "tool_evidence_mismatch"},
            )
            self.assertEqual(no_write.calls, 0)
            self.assertEqual(artifact.read_text(), "tampered")

    def test_a5_new_process_recognizes_and_verifies_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "tests" / "process_exit_helper.py"),
                    str(root),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=20,
            )

            self.assertEqual(completed.returncode, 23, completed.stderr)
            checkpoint = json.loads(completed.stdout)
            task_id = checkpoint["task_id"]
            lease_expires_at = datetime.fromisoformat(
                checkpoint["lease_expires_at"]
            )
            recovery_time = (
                lease_expires_at + timedelta(seconds=1)
            ).isoformat(timespec="milliseconds")
            artifact = root / "artifacts" / task_id / "process-proof.txt"
            self.assertTrue(artifact.is_file())
            self.assertEqual(artifact.read_text(), "survives a process exit\n")

            no_write = FailIfCalledWriter()
            restarted = TaskService(
                root,
                writer=no_write,
                clock=lambda: recovery_time,
            )
            self.assertEqual(restarted.require_task(task_id).status, TaskStatus.INTERRUPTED)
            resumed = restarted.resume(task_id)
            self.assertEqual(resumed.status, TaskStatus.SUCCEEDED)
            self.assertEqual(no_write.calls, 0)
            self.assertEqual(
                [event.event_type for event in restarted.events(task_id)].count(
                    "task_succeeded"
                ),
                1,
            )

    def test_a6_real_simulated_and_not_executed_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            real = TaskService(root).submit(request(artifact_path="real.txt"))
            simulated = TaskService(
                root, writer=SimulatedArtifactWriter()
            ).submit(request(artifact_path="simulated.txt"))
            blocked = TaskService(root).submit(request(artifact_path="../blocked.txt"))

            self.assertEqual(real.status, TaskStatus.SUCCEEDED)
            self.assertEqual(real.execution_mode, ExecutionMode.REAL)
            self.assertTrue((root / "artifacts" / real.task_id / "real.txt").is_file())

            self.assertEqual(simulated.status, TaskStatus.SIMULATED)
            self.assertEqual(simulated.execution_mode, ExecutionMode.SIMULATED)
            self.assertIsNone(simulated.artifact)
            self.assertIsNone(simulated.verification)
            self.assertFalse(
                (root / "artifacts" / simulated.task_id / "simulated.txt").exists()
            )

            self.assertEqual(blocked.status, TaskStatus.BLOCKED)
            self.assertEqual(blocked.execution_mode, ExecutionMode.NOT_EXECUTED)
            self.assertIsNone(blocked.artifact)

            real_events = [event.event_type for event in TaskService(root).events(real.task_id)]
            simulated_events = [
                event.event_type
                for event in TaskService(root).events(simulated.task_id)
            ]
            blocked_events = [
                event.event_type
                for event in TaskService(root).events(blocked.task_id)
            ]
            self.assertIn("verification_completed", real_events)
            self.assertIn("simulation_completed", simulated_events)
            self.assertNotIn("verification_completed", simulated_events)
            self.assertNotIn("tool_started", blocked_events)


if __name__ == "__main__":
    unittest.main()
