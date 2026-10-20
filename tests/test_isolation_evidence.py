"""Independent isolation-evidence verifier."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.isolation import run_protected_container  # noqa: E402
from zevorinth.isolation_evidence import (  # noqa: E402
    PROBE_BOUNDARIES,
    VERIFIER_VERSION,
    build_evidence,
    evidence_digest,
    verify_evidence,
)


def _probes(status: str = "PASS") -> list[dict]:
    return [
        {
            "probe_id": f"probe-{boundary}",
            "boundary": boundary,
            "status": status,
            "expected_result": "denied",
            "observed_result": "denied",
            "exit_code": 1,
            "evidence_artifact_ids": [f"artifact-{boundary}"],
        }
        for boundary in PROBE_BOUNDARIES
    ]


IDENTITY = {
    "environment_id": "env-1",
    "runtime_identity": "docker@24.0.0",
    "image_digest": "sha256:" + "a" * 64,
    "container_config_digest": "sha256:" + "b" * 64,
}


def _evidence(probes: list[dict] | None = None, **overrides) -> dict:
    payload = dict(IDENTITY)
    payload.update(overrides)
    return build_evidence(**payload, probes=probes or _probes())


def _verify(evidence, **overrides):
    expected = dict(
        expected_runtime=IDENTITY["runtime_identity"],
        expected_image_digest=IDENTITY["image_digest"],
        expected_config_digest=IDENTITY["container_config_digest"],
    )
    expected.update(overrides)
    return verify_evidence(evidence, **expected)


class EvidenceVerifierTests(unittest.TestCase):
    def test_complete_matching_evidence_is_verified(self) -> None:
        result = _verify(_evidence())
        self.assertEqual(result["state"], "verified", result)

    def test_missing_evidence_is_blocked(self) -> None:
        self.assertEqual(_verify(None)["state"], "blocked")

    def test_missing_boundary_is_blocked(self) -> None:
        evidence = _evidence()
        evidence["probes"] = evidence["probes"][:-1]
        evidence["digest"] = evidence_digest(evidence)
        result = _verify(evidence)
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(
            any("boundary_not_executed" in r for r in result["reasons"])
        )

    def test_not_executed_probe_blocks(self) -> None:
        evidence = _evidence(probes=_probes("NOT_EXECUTED"))
        self.assertEqual(_verify(evidence)["state"], "blocked")

    def test_failed_probe_fails(self) -> None:
        evidence = _evidence(probes=_probes("FAIL"))
        self.assertEqual(_verify(evidence)["state"], "failed")

    def test_inconclusive_probe_is_unverified(self) -> None:
        evidence = _evidence(probes=_probes("INCONCLUSIVE"))
        self.assertEqual(_verify(evidence)["state"], "unverified")

    def test_tampered_evidence_is_rejected(self) -> None:
        evidence = _evidence()
        evidence["probes"][0]["status"] = "FAIL"
        self.assertEqual(_verify(evidence)["state"], "failed")
        self.assertIn("evidence_digest_mismatch", _verify(evidence)["reasons"])

    def test_image_or_config_mismatch_is_blocked(self) -> None:
        evidence = _evidence()
        self.assertEqual(
            _verify(evidence, expected_image_digest="sha256:" + "c" * 64)[
                "state"
            ],
            "blocked",
        )
        self.assertEqual(
            _verify(evidence, expected_config_digest="sha256:" + "d" * 64)[
                "state"
            ],
            "blocked",
        )

    def test_stale_verifier_version_is_blocked(self) -> None:
        evidence = _evidence()
        evidence["verifier_version"] = "0.9.0"
        evidence["digest"] = evidence_digest(evidence)
        self.assertEqual(_verify(evidence)["state"], "blocked")

    def test_duplicate_probe_ids_fail(self) -> None:
        evidence = _evidence()
        evidence["probes"].append(copy.deepcopy(evidence["probes"][0]))
        evidence["digest"] = evidence_digest(evidence)
        self.assertEqual(_verify(evidence)["state"], "failed")

    def test_runtime_available_alone_is_not_verified(self) -> None:
        from zevorinth.isolation import container_runtime_state

        state = container_runtime_state()
        self.assertNotEqual(state["state"], "verified")
        self.assertNotEqual(_verify(None)["state"], "verified")


class ContainerGateTests(unittest.TestCase):
    def test_unverified_evidence_blocks_container_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record = run_protected_container(
                workspace=Path(temp_dir),
                argv=["true"],
                image="python:3.13-slim",
                boundary_result={"state": "verified"},
                evidence=_evidence(probes=_probes("NOT_EXECUTED")),
            )
        self.assertFalse(record["process_started"])
        self.assertEqual(record["isolation_state"], "blocked")

    def test_tampered_evidence_blocks_container_start(self) -> None:
        evidence = _evidence()
        evidence["runtime_identity"] = "docker@other"
        with tempfile.TemporaryDirectory() as temp_dir:
            record = run_protected_container(
                workspace=Path(temp_dir),
                argv=["true"],
                image="python:3.13-slim",
                boundary_result={"state": "verified"},
                evidence=evidence,
            )
        self.assertFalse(record["process_started"])


if __name__ == "__main__":
    unittest.main()
