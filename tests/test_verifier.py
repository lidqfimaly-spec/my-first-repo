from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import RealArtifactWriter  # noqa: E402
from zevorinth.models import ExecutionMode, ToolResult  # noqa: E402
from zevorinth.verifier import ArtifactVerifier  # noqa: E402


class VerifierTests(unittest.TestCase):
    def _create(self, root: Path, content: str = "expected") -> tuple[str, ToolResult]:
        task_id = str(uuid4())
        result = RealArtifactWriter(root).write(
            task_id=task_id,
            attempt_id=str(uuid4()),
            artifact_path="artifact.txt",
            content=content,
            started_at="a",
            finished_at="b",
        )
        return task_id, result

    def test_verifier_reads_independent_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            task_id, result = self._create(root, "expected\n")
            verification = ArtifactVerifier(root).verify(
                task_id=task_id,
                artifact_path="artifact.txt",
                expected_content="expected\n",
                tool_result=result,
                checked_at="checked",
            )
            self.assertTrue(verification.passed)
            self.assertTrue(verification.exists)
            self.assertTrue(verification.content_matches)
            self.assertTrue(verification.evidence_matches)
            self.assertEqual(verification.reason, "artifact_verified")

    def test_missing_artifact_does_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            task_id, result = self._create(root)
            (root / task_id / "artifact.txt").unlink()
            verification = ArtifactVerifier(root).verify(
                task_id=task_id,
                artifact_path="artifact.txt",
                expected_content="expected",
                tool_result=result,
                checked_at="checked",
            )
            self.assertFalse(verification.passed)
            self.assertEqual(verification.reason, "artifact_missing")

    def test_content_mismatch_does_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            task_id, result = self._create(root, "actual")
            verification = ArtifactVerifier(root).verify(
                task_id=task_id,
                artifact_path="artifact.txt",
                expected_content="expected",
                tool_result=result,
                checked_at="checked",
            )
            self.assertFalse(verification.passed)
            self.assertEqual(verification.reason, "artifact_content_mismatch")

    def test_tool_evidence_mismatch_does_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            task_id, result = self._create(root, "expected")
            result.artifact.sha256 = hashlib.sha256(b"different").hexdigest()
            verification = ArtifactVerifier(root).verify(
                task_id=task_id,
                artifact_path="artifact.txt",
                expected_content="expected",
                tool_result=result,
                checked_at="checked",
            )
            self.assertFalse(verification.passed)
            self.assertEqual(verification.reason, "tool_evidence_mismatch")

    def test_simulated_result_cannot_be_verified_as_real(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            simulated = ToolResult(
                attempt_id=str(uuid4()),
                tool_name="write_text_artifact",
                execution_mode=ExecutionMode.SIMULATED,
                success=True,
                started_at="a",
                finished_at="b",
            )
            verification = ArtifactVerifier(root).verify(
                task_id=str(uuid4()),
                artifact_path="artifact.txt",
                expected_content="expected",
                tool_result=simulated,
                checked_at="checked",
            )
            self.assertFalse(verification.passed)
            self.assertEqual(verification.reason, "verification_requires_real_tool_execution")


if __name__ == "__main__":
    unittest.main()
