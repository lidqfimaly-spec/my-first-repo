"""Independent artifact verification that does not trust tool output."""

from __future__ import annotations

import errno
import hashlib
from pathlib import Path

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
