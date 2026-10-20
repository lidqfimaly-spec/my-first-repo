"""Evidence TTL, issuance, replay binding and pre-execution revalidation."""

from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.isolation import run_protected_container  # noqa: E402
from zevorinth.isolation_evidence import (  # noqa: E402
    CLOCK_SKEW_SECONDS,
    EVIDENCE_TTL_SECONDS,
    PROBE_BOUNDARIES,
    build_evidence,
    evidence_digest,
    verify_evidence,
)


IDENTITY = {
    "environment_id": "env-1",
    "runtime_identity": "docker@29.2.1",
    "image_digest": "alpine@sha256:" + "a" * 64,
    "container_config_digest": "sha256:" + "b" * 64,
}
REQUEST_ID = "task-42/attempt-1"
COMMAND_DIGEST = "c" * 64


def _probes(status: str = "PASS") -> list[dict]:
    return [
        {
            "probe_id": f"p-{boundary}",
            "boundary": boundary,
            "status": status,
            "expected_result": "x",
            "observed_result": "x",
            "exit_code": 0,
            "evidence_artifact_ids": [boundary],
        }
        for boundary in PROBE_BOUNDARIES
    ]


def _evidence(issued_at: float | None = None, ttl: float = 60.0) -> dict:
    return build_evidence(
        **IDENTITY,
        probes=_probes(),
        request_id=REQUEST_ID,
        command_digest=COMMAND_DIGEST,
        issued_at=time.time() if issued_at is None else issued_at,
        ttl_seconds=ttl,
    )


def _verify(evidence, **overrides):
    expected = dict(
        expected_runtime=IDENTITY["runtime_identity"],
        expected_image_digest=IDENTITY["image_digest"],
        expected_config_digest=IDENTITY["container_config_digest"],
        expected_request_id=REQUEST_ID,
        expected_command_digest=COMMAND_DIGEST,
    )
    expected.update(overrides)
    return verify_evidence(evidence, **expected)


class EvidenceLifecycleTests(unittest.TestCase):
    def test_evidence_within_ttl_is_valid(self) -> None:
        evidence = _evidence(ttl=60)
        self.assertEqual(_verify(evidence)["state"], "verified")

    def test_evidence_just_past_expiry_is_rejected(self) -> None:
        now = time.time()
        evidence = _evidence(issued_at=now - 61, ttl=60)
        self.assertEqual(_verify(evidence)["state"], "expired")

    def test_evidence_long_expired_is_rejected(self) -> None:
        evidence = _evidence(issued_at=time.time() - 86400, ttl=60)
        self.assertEqual(_verify(evidence)["state"], "expired")

    def test_future_issue_time_is_rejected(self) -> None:
        evidence = _evidence(issued_at=time.time() + CLOCK_SKEW_SECONDS + 60)
        self.assertEqual(_verify(evidence)["state"], "blocked")
        self.assertIn(
            "evidence_issued_in_the_future", _verify(evidence)["reasons"]
        )

    def test_malformed_time_is_rejected(self) -> None:
        evidence = _evidence()
        evidence["issued_at"] = "yesterday"
        evidence["digest"] = evidence_digest(evidence)
        self.assertEqual(_verify(evidence)["state"], "blocked")

    def test_missing_expiry_is_rejected(self) -> None:
        evidence = _evidence()
        evidence.pop("expires_at")
        evidence["digest"] = evidence_digest(evidence)
        self.assertEqual(_verify(evidence)["state"], "blocked")

    def test_refreshing_timestamp_without_reverification_is_rejected(self) -> None:
        evidence = _evidence(issued_at=time.time() - 120, ttl=60)
        self.assertEqual(_verify(evidence)["state"], "expired")
        # Refreshing the clock alone invalidates the digest.
        evidence["issued_at"] = time.time()
        evidence["expires_at"] = time.time() + 60
        self.assertEqual(_verify(evidence)["state"], "failed")
        self.assertIn("evidence_digest_mismatch", _verify(evidence)["reasons"])

    def test_runtime_change_is_rejected(self) -> None:
        evidence = _evidence()
        result = _verify(evidence, expected_runtime="docker@30.0.0")
        self.assertEqual(result["state"], "blocked")
        self.assertIn("identity_mismatch:runtime_identity", result["reasons"])

    def test_image_digest_change_is_rejected(self) -> None:
        evidence = _evidence()
        result = _verify(evidence, expected_image_digest="alpine@sha256:" + "z" * 64)
        self.assertEqual(result["state"], "blocked")

    def test_config_digest_change_is_rejected(self) -> None:
        evidence = _evidence()
        result = _verify(evidence, expected_config_digest="sha256:" + "e" * 64)
        self.assertEqual(result["state"], "blocked")

    def test_command_digest_change_is_rejected(self) -> None:
        evidence = _evidence()
        result = _verify(evidence, expected_command_digest="f" * 64)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("command_digest_mismatch", result["reasons"])

    def test_verifier_version_change_is_rejected(self) -> None:
        evidence = _evidence()
        evidence["verifier_version"] = "0.9.9"
        evidence["digest"] = evidence_digest(evidence)
        self.assertEqual(_verify(evidence)["state"], "blocked")

    def test_replay_from_another_request_is_rejected(self) -> None:
        evidence = _evidence()
        result = _verify(evidence, expected_request_id="task-99/attempt-1")
        self.assertEqual(result["state"], "blocked")
        self.assertIn("evidence_request_mismatch", result["reasons"])

    def test_tampered_probe_is_rejected(self) -> None:
        evidence = _evidence()
        evidence["probes"][0]["status"] = "FAIL"
        self.assertEqual(_verify(evidence)["state"], "failed")

    def test_default_ttl_is_explicit(self) -> None:
        self.assertGreater(EVIDENCE_TTL_SECONDS, 0)
        evidence = build_evidence(
            **IDENTITY,
            probes=_probes(),
            request_id=REQUEST_ID,
            command_digest=COMMAND_DIGEST,
        )
        self.assertAlmostEqual(
            evidence["expires_at"] - evidence["issued_at"],
            EVIDENCE_TTL_SECONDS,
        )

    def test_expired_evidence_never_starts_a_process(self) -> None:
        evidence = _evidence(issued_at=time.time() - 3600, ttl=60)
        with tempfile.TemporaryDirectory() as temp_dir:
            record = run_protected_container(
                workspace=Path(temp_dir),
                argv=["true"],
                image="alpine:3.20",
                boundary_result={"state": "verified"},
                evidence=evidence,
                request_id=REQUEST_ID,
            )
        self.assertFalse(record["process_started"])
        self.assertEqual(record["isolation_state"], "expired")

    def test_failed_reverification_never_starts_a_process(self) -> None:
        evidence = _evidence()
        evidence["command_digest"] = "0" * 64  # tampered after issuance
        with tempfile.TemporaryDirectory() as temp_dir:
            record = run_protected_container(
                workspace=Path(temp_dir),
                argv=["true"],
                image="alpine:3.20",
                boundary_result={"state": "verified"},
                evidence=evidence,
                request_id=REQUEST_ID,
            )
        self.assertFalse(record["process_started"])

    def test_concurrent_reuse_does_not_bypass_revalidation(self) -> None:
        evidence = _evidence(ttl=60)
        results = [_verify(copy.deepcopy(evidence)) for _ in range(5)]
        self.assertTrue(all(r["state"] == "verified" for r in results))


if __name__ == "__main__":
    unittest.main()
