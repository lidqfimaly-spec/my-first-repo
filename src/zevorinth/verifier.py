"""Independent artifact verification that does not trust tool output."""

from __future__ import annotations

import errno
import hashlib
from pathlib import Path
from typing import Any

from .artifact_tool import (
    ArtifactToolError,
    read_artifact,
)
from .models import (
    ArtifactEvidence,
    ExecutionMode,
    ToolResult,
    VerificationResult,
)


class ArtifactVerifier:
    name = "artifact-readback-v1"

    def __init__(self, artifacts_root: Path) -> None:
        self.artifacts_root = artifacts_root.absolute()

    def verify(
        self,
        *,
        task_id: str,
        artifact_path: str,
        expected_content: str,
        tool_result: ToolResult,
        checked_at: str,
        expected_attempt_id: str | None = None,
        require_provenance: bool = False,
        maximum_claim_generation: int | None = None,
    ) -> VerificationResult:
        """Verify bytes and attempt/generation provenance without trust."""
        if tool_result.execution_mode is not ExecutionMode.REAL:
            return VerificationResult(
                verifier_name=self.name,
                passed=False,
                exists=False,
                path_safe=False,
                content_matches=False,
                evidence_matches=False,
                reason="verification_requires_real_tool_execution",
                artifact=None,
                checked_at=checked_at,
            )

        try:
            raw = read_artifact(self.artifacts_root, task_id, artifact_path)
        except FileNotFoundError:
            return VerificationResult(
                verifier_name=self.name,
                passed=False,
                exists=False,
                path_safe=True,
                content_matches=False,
                evidence_matches=False,
                reason="artifact_missing",
                artifact=None,
                checked_at=checked_at,
            )
        except ArtifactToolError as exc:
            return VerificationResult(
                verifier_name=self.name,
                passed=False,
                exists=False,
                path_safe=False,
                content_matches=False,
                evidence_matches=False,
                reason=exc.code,
                artifact=None,
                checked_at=checked_at,
            )
        except OSError as exc:
            reason = "artifact_read_failed"
            if exc.errno in {errno.EACCES, errno.EPERM}:
                reason = "artifact_read_denied"
            return VerificationResult(
                verifier_name=self.name,
                passed=False,
                exists=False,
                path_safe=True,
                content_matches=False,
                evidence_matches=False,
                reason=reason,
                artifact=None,
                checked_at=checked_at,
            )

        expected = expected_content.encode("utf-8")
        actual_evidence = ArtifactEvidence(
            relative_path=artifact_path,
            byte_count=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            attempt_id=expected_attempt_id or tool_result.attempt_id,
            claim_generation=(
                tool_result.artifact.claim_generation
                if tool_result.artifact is not None
                else None
            ),
        )
        content_matches = raw == expected
        reported = tool_result.artifact
        evidence_matches = (
            reported is not None
            and reported.relative_path == actual_evidence.relative_path
            and reported.byte_count == actual_evidence.byte_count
            and reported.sha256 == actual_evidence.sha256
            and reported.attempt_id == actual_evidence.attempt_id
            and (
                not require_provenance
                or (
                    reported.claim_generation is not None
                    and reported.claim_generation > 0
                )
            )
            and (
                maximum_claim_generation is None
                or (
                    reported.claim_generation is not None
                    and reported.claim_generation <= maximum_claim_generation
                )
            )
        )
        passed = content_matches and evidence_matches
        if not content_matches:
            reason = "artifact_content_mismatch"
        elif not evidence_matches:
            reason = "tool_evidence_mismatch"
        else:
            reason = "artifact_verified"
        return VerificationResult(
            verifier_name=self.name,
            passed=passed,
            exists=True,
            path_safe=True,
            content_matches=content_matches,
            evidence_matches=evidence_matches,
            reason=reason,
            artifact=actual_evidence,
            checked_at=checked_at,
        )


class TestResultVerifier:
    """Independent check of a structured local test execution.

    The verifier never trusts ``ToolResult.success`` or the executor's phase;
    it re-derives the verdict from the bounded evidence record and the plan.
    """

    name = "local-test-result"

    def verify(
        self,
        *,
        task: Any,
        evidence: Any,
        expected_parameters: dict[str, Any],
        checked_at: str,
    ) -> VerificationResult:
        def result(passed: bool, reason: str, evidence_matches: bool) -> VerificationResult:
            return VerificationResult(
                verifier_name=self.name,
                passed=passed,
                exists=evidence is not None,
                path_safe=True,
                content_matches=(evidence is not None and evidence.exit_code == 0),
                evidence_matches=evidence_matches,
                reason=reason,
                artifact=None,
                checked_at=checked_at,
            )

        if evidence is None:
            return result(False, "test_evidence_missing", False)
        structural = (
            evidence.attempt_id == task.attempt_id
            and evidence.claim_generation == task.claim_generation
            and evidence.runner == expected_parameters.get("runner")
            and evidence.target == expected_parameters.get("target")
            and evidence.workspace == expected_parameters.get("workspace")
        )
        if not structural:
            return result(False, "test_evidence_mismatch", False)
        if evidence.cancelled:
            return result(False, "test_cancelled", True)
        if evidence.timed_out:
            return result(
                False,
                "test_timeout" if evidence.terminated_confirmed else "test_timeout_unconfirmed",
                True,
            )
        if evidence.exit_code is None:
            return result(False, "test_exit_unknown", False)
        if evidence.truncated:
            # Truncated output means the result cannot be fully audited.
            return result(False, "test_output_truncated", True)
        if evidence.exit_code == 0:
            return result(True, "tests_passed", True)
        return result(False, "tests_failed", True)
