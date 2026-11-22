from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import (  # noqa: E402
    ArtifactToolError,
    read_artifact_text,
)
from zevorinth.models import (  # noqa: E402
    ExecutionMode,
    Task,
    TaskRequest,
    TaskStatus,
    ToolResult,
)
from zevorinth.planner import (  # noqa: E402
    MODIFY_TEXT_ARTIFACT,
    READ_TEXT_ARTIFACT,
    WRITE_TEXT_ARTIFACT,
)
from zevorinth.service import TaskService  # noqa: E402


def create_request(
    *,
    artifact_path: str = "source.txt",
    content: str = "source\n",
) -> TaskRequest:
    return TaskRequest(
        goal="create a verified source artifact",
        operation=WRITE_TEXT_ARTIFACT,
        parameters={
            "artifact_path": artifact_path,
            "content": content,
        },
    )


def read_request(source: Task) -> TaskRequest:
    assert source.artifact is not None
    return TaskRequest(
        goal="read the verified source artifact",
        operation=READ_TEXT_ARTIFACT,
        parameters={
            "source_task_id": source.task_id,
            "artifact_path": source.artifact.relative_path,
            "expected_sha256": source.artifact.sha256,
        },
    )


def modify_request(
    source: Task,
    *,
    content: str,
    mode: str = "replace",
    output_path: str = "result.txt",
    expected_sha256: str | None = None,
) -> TaskRequest:
    assert source.artifact is not None
    return TaskRequest(
        goal="create a verified continuation of the source artifact",
        operation=MODIFY_TEXT_ARTIFACT,
        parameters={
            "source_task_id": source.task_id,
            "artifact_path": source.artifact.relative_path,
            "output_path": output_path,
            "content": content,
            "mode": mode,
            "expected_sha256": (
                expected_sha256
                if expected_sha256 is not None
                else source.artifact.sha256
            ),
        },
    )


class FailIfCalledWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self) -> None:
        self.calls = 0

    def write(self, **_: Any) -> ToolResult:
        self.calls += 1
        raise AssertionError("writer must not run during verification-only resume")


class FileTaskCompletionTests(unittest.TestCase):
    def test_read_verified_source_records_bounded_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            source = service.submit(create_request(content="read me\n"))

            read = service.submit(read_request(source))

            self.assertEqual(read.status, TaskStatus.SUCCEEDED)
            self.assertEqual(read.execution_mode, ExecutionMode.REAL)
            self.assertIsNone(read.artifact)
            self.assertIsNotNone(read.tool_result)
            self.assertIsNotNone(read.tool_result.read_evidence)
            self.assertEqual(
                read.tool_result.read_evidence.content,
                "read me\n",
            )
            self.assertEqual(
                read.tool_result.read_evidence.source_task_id,
                source.task_id,
            )
            self.assertTrue(read.verification.passed)
            self.assertEqual(
                read.verification.reason,
                "source_artifact_verified",
            )
            events = [event.event_type for event in service.events(read.task_id)]
            self.assertIn("tool_started", events)
            self.assertIn("verification_completed", events)
            self.assertEqual(events.count("task_succeeded"), 1)
            self.assertTrue(
                service.audit_lifecycle(read.task_id)["ok"]
            )

    def test_modify_replace_creates_new_verified_version_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            source = service.submit(create_request(content="before\n"))

            modified = service.submit(
                modify_request(source, content="after\n")
            )

            self.assertEqual(modified.status, TaskStatus.SUCCEEDED)
            self.assertEqual(
                (root / "artifacts" / source.task_id / "source.txt").read_text(
                    encoding="utf-8"
                ),
                "before\n",
            )
            self.assertEqual(
                (root / "artifacts" / modified.task_id / "result.txt").read_text(
                    encoding="utf-8"
                ),
                "after\n",
            )
            self.assertTrue(modified.verification.passed)
            self.assertEqual(
                modified.tool_result.lineage["source_task_id"],
                source.task_id,
            )
            self.assertEqual(
                modified.tool_result.lineage["source_sha256"],
                source.artifact.sha256,
            )
            self.assertEqual(
                modified.tool_result.lineage["result_sha256"],
                modified.artifact.sha256,
            )
            self.assertTrue(
                service.audit_lifecycle(modified.task_id)["ok"]
            )

    def test_modify_append_preserves_source_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            source = service.submit(create_request(content="one\n"))

            modified = service.submit(
                modify_request(
                    source,
                    content="two\n",
                    mode="append",
                    output_path="combined.txt",
                )
            )

            self.assertEqual(modified.status, TaskStatus.SUCCEEDED)
            self.assertEqual(
                (
                    root
                    / "artifacts"
                    / modified.task_id
                    / "combined.txt"
                ).read_text(encoding="utf-8"),
                "one\ntwo\n",
            )

    def test_digest_mismatch_blocks_before_new_version_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            source = service.submit(create_request(content="source\n"))

            blocked = service.submit(
                modify_request(
                    source,
                    content="changed\n",
                    expected_sha256="0" * 64,
                )
            )

            self.assertEqual(blocked.status, TaskStatus.BLOCKED)
            self.assertEqual(blocked.error.code, "source_artifact_changed")
            self.assertFalse(
                (
                    root / "artifacts" / blocked.task_id / "result.txt"
                ).exists()
            )
            event_types = [
                event.event_type for event in service.events(blocked.task_id)
            ]
            self.assertNotIn("tool_started", event_types)

    def test_unverified_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir))
            queued = service.enqueue(create_request())

            blocked = service.submit(
                TaskRequest(
                    goal="modify an unverified source",
                    operation=MODIFY_TEXT_ARTIFACT,
                    parameters={
                        "source_task_id": queued.task_id,
                        "artifact_path": "source.txt",
                        "output_path": "result.txt",
                        "content": "changed",
                        "mode": "replace",
                        "expected_sha256": "0" * 64,
                    },
                )
            )

            self.assertEqual(blocked.status, TaskStatus.BLOCKED)
            self.assertEqual(
                blocked.error.code,
                "source_artifact_not_verified",
            )

            read_blocked = service.submit(
                TaskRequest(
                    goal="read an unverified source",
                    operation=READ_TEXT_ARTIFACT,
                    parameters={
                        "source_task_id": queued.task_id,
                        "artifact_path": "source.txt",
                    },
                )
            )
            self.assertEqual(read_blocked.status, TaskStatus.BLOCKED)
            self.assertEqual(
                read_blocked.error.code,
                "source_artifact_not_verified",
            )
            self.assertNotIn(
                "tool_started",
                [
                    event.event_type
                    for event in service.events(read_blocked.task_id)
                ],
            )

    def test_output_collision_is_blocked_before_tool_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            source = service.submit(create_request(content="source\n"))
            task_id = str(uuid4())
            queued = service.enqueue(
                modify_request(source, content="changed\n"),
                task_id=task_id,
            )
            target_dir = root / "artifacts" / queued.task_id
            target_dir.mkdir(parents=True)
            (target_dir / "result.txt").write_text(
                "preserve\n",
                encoding="utf-8",
            )

            blocked = service.execute_task(queued.task_id)

            self.assertEqual(blocked.status, TaskStatus.BLOCKED)
            self.assertEqual(blocked.error.code, "artifact_exists")
            self.assertEqual(
                (target_dir / "result.txt").read_text(encoding="utf-8"),
                "preserve\n",
            )

    def test_interrupted_modify_is_verified_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            interrupted: list[str] = []

            def interrupt(stage: str, task: Task) -> None:
                self.assertEqual(
                    stage,
                    "after_write_before_verification",
                )
                interrupted.append(task.task_id)
                raise RuntimeError("simulated crash before verification")

            base_time = "2026-10-19T00:00:00.000+00:00"
            future_time = "2026-10-19T00:01:01.000+00:00"
            service = TaskService(root, clock=lambda: base_time)
            source = service.submit(create_request(content="base\n"))
            service.fault_hook = interrupt
            with self.assertRaises(RuntimeError):
                service.submit(
                    modify_request(source, content="continued\n")
                )
            task_id = interrupted[0]
            output = root / "artifacts" / task_id / "result.txt"
            self.assertEqual(output.read_text(encoding="utf-8"), "continued\n")

            writer = FailIfCalledWriter()
            restarted = TaskService(
                root,
                writer=writer,
                clock=lambda: future_time,
            )
            self.assertEqual(
                restarted.require_task(task_id).status,
                TaskStatus.INTERRUPTED,
            )

            resumed = restarted.resume(task_id)

            self.assertEqual(resumed.status, TaskStatus.SUCCEEDED)
            self.assertEqual(writer.calls, 0)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "continued\n",
            )

    def test_read_text_rejects_invalid_utf8_and_oversize(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            task_id = str(uuid4())
            task_dir = root / task_id
            task_dir.mkdir(parents=True)
            (task_dir / "binary.txt").write_bytes(b"\xff")
            with self.assertRaises(ArtifactToolError) as raised:
                read_artifact_text(root, task_id, "binary.txt")
            self.assertEqual(
                raised.exception.code,
                "invalid_artifact_encoding",
            )
            (task_dir / "large.txt").write_bytes(b"x" * 17)
            with self.assertRaises(ArtifactToolError) as raised:
                read_artifact_text(
                    root,
                    task_id,
                    "large.txt",
                    max_bytes=16,
                )
            self.assertEqual(raised.exception.code, "content_too_large")

    def test_read_evidence_serializes_with_tool_result(self) -> None:
        from zevorinth.models import ArtifactReadEvidence

        result = ToolResult(
            attempt_id=str(uuid4()),
            tool_name=READ_TEXT_ARTIFACT,
            execution_mode=ExecutionMode.REAL,
            success=True,
            started_at="a",
            finished_at="b",
            read_evidence=ArtifactReadEvidence(
                source_task_id=str(uuid4()),
                relative_path="x.txt",
                byte_count=1,
                sha256=hashlib.sha256(b"x").hexdigest(),
                content="x",
            ),
            lineage={"operation": READ_TEXT_ARTIFACT},
        )

        restored = ToolResult.from_dict(result.to_dict())

        self.assertEqual(restored.read_evidence.content, "x")
        self.assertEqual(restored.lineage, {"operation": READ_TEXT_ARTIFACT})


if __name__ == "__main__":
    unittest.main()
