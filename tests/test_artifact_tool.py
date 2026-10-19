from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import (  # noqa: E402
    MAX_ARTIFACT_BYTES,
    ArtifactToolError,
    RealArtifactWriter,
    SimulatedArtifactWriter,
    validate_artifact_name,
)
from zevorinth.models import ExecutionMode  # noqa: E402


class ArtifactToolTests(unittest.TestCase):
    def test_real_writer_creates_file_and_reports_actual_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            task_id = str(uuid4())
            attempt_id = str(uuid4())
            content = "衍序\nverified artifact\n"
            writer = RealArtifactWriter(root)

            result = writer.write(
                task_id=task_id,
                attempt_id=attempt_id,
                artifact_path="证明.txt",
                content=content,
                started_at="2026-10-19T00:00:00Z",
                finished_at="2026-10-19T00:00:01Z",
            )

            path = root / task_id / "证明.txt"
            actual = path.read_bytes()
            self.assertEqual(result.execution_mode, ExecutionMode.REAL)
            self.assertTrue(result.success)
            self.assertEqual(actual, content.encode("utf-8"))
            self.assertEqual(result.artifact.byte_count, len(actual))
            self.assertEqual(result.artifact.sha256, hashlib.sha256(actual).hexdigest())

    def test_writer_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            task_id = str(uuid4())
            writer = RealArtifactWriter(root)
            kwargs = {
                "task_id": task_id,
                "attempt_id": str(uuid4()),
                "artifact_path": "result.txt",
                "content": "first",
                "started_at": "a",
                "finished_at": "b",
            }
            writer.write(**kwargs)
            kwargs["content"] = "second"
            with self.assertRaises(ArtifactToolError) as raised:
                writer.write(**kwargs)
            self.assertEqual(raised.exception.code, "artifact_exists")
            self.assertEqual((root / task_id / "result.txt").read_text(), "first")

    def test_invalid_and_unsafe_names_are_rejected(self) -> None:
        invalid = ["", "../x", "/tmp/x", "a/b", r"a\b", ".env", ".", "..", "x\x00y"]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ArtifactToolError):
                    validate_artifact_name(value)

    def test_symlinked_task_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            outside = Path(temp_dir) / "outside"
            root.mkdir()
            outside.mkdir()
            task_id = str(uuid4())
            (root / task_id).symlink_to(outside, target_is_directory=True)
            writer = RealArtifactWriter(root)

            with self.assertRaises(ArtifactToolError) as raised:
                writer.write(
                    task_id=task_id,
                    attempt_id=str(uuid4()),
                    artifact_path="x.txt",
                    content="x",
                    started_at="a",
                    finished_at="b",
                )
            self.assertIn(raised.exception.code, {"path_escape", "unsafe_artifact_directory"})
            self.assertFalse((outside / "x.txt").exists())

    def test_empty_and_oversized_content_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = RealArtifactWriter(Path(temp_dir) / "artifacts")
            base = {
                "task_id": str(uuid4()),
                "attempt_id": str(uuid4()),
                "artifact_path": "x.txt",
                "started_at": "a",
                "finished_at": "b",
            }
            with self.assertRaises(ArtifactToolError):
                writer.write(content="", **base)
            with self.assertRaises(ArtifactToolError):
                writer.write(content="x" * (MAX_ARTIFACT_BYTES + 1), **base)

    def test_simulation_never_creates_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            task_id = str(uuid4())
            result = SimulatedArtifactWriter().write(
                task_id=task_id,
                attempt_id=str(uuid4()),
                artifact_path="x.txt",
                content="x",
                started_at="a",
                finished_at="b",
            )
            self.assertEqual(result.execution_mode, ExecutionMode.SIMULATED)
            self.assertIsNone(result.artifact)
            self.assertFalse((root / task_id / "x.txt").exists())


if __name__ == "__main__":
    unittest.main()
