"""Isolation verification evidence and its independent verifier.

The executor never self-certifies: ``verify_evidence`` recomputes the digest,
checks identity (runtime, image, config), requires every required boundary to
be present exactly once with ``PASS``, and rejects stale or foreign records.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Mapping


VERIFIER_VERSION = "1.0.0"
EVIDENCE_SCHEMA_VERSION = "1.1"

#: Default validity window.  Fifteen minutes is long enough to run one
#: verification batch and short enough that a stale on-disk record is not a
#: standing authorization.
EVIDENCE_TTL_SECONDS = 900.0
#: Tolerated clock skew between issuance and verification.
CLOCK_SKEW_SECONDS = 5.0

PROBE_BOUNDARIES = (
    "read_inside",
    "write_inside",
    "read_outside",
    "write_outside",
    "network",
    "credential",
    "subprocess",
)
PROBE_STATUSES = frozenset(
    {"PASS", "FAIL", "NOT_EXECUTED", "ERROR", "INCONCLUSIVE"}
)
REQUIRED_EVIDENCE_FIELDS = (
    "schema_version",
    "verifier_version",
    "evidence_id",
    "issued_at",
    "expires_at",
    "request_id",
    "environment_id",
    "runtime_identity",
    "image_digest",
    "command_digest",
    "container_config_digest",
    "probes",
    "digest",
)


class EvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def evidence_digest(evidence: Mapping[str, Any]) -> str:
    payload = {
        key: value
        for key, value in evidence.items()
        if key != "digest"
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_evidence(
    *,
    environment_id: str,
    runtime_identity: str,
    image_digest: str,
    container_config_digest: str,
    probes: list[Mapping[str, Any]],
    evidence_id: str | None = None,
    request_id: str = "",
    command_digest: str = "",
    issued_at: float | None = None,
    ttl_seconds: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    if issued_at is None:
        issued = float(time.time() if now is None else now)
    else:
        issued = float(issued_at)
    ttl = EVIDENCE_TTL_SECONDS if ttl_seconds is None else float(ttl_seconds)
    if ttl <= 0:
        raise EvidenceError("invalid_ttl", "ttl must be positive")
    record = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "verifier_version": VERIFIER_VERSION,
        "evidence_id": evidence_id
        or hashlib.sha256(
            f"{environment_id}|{runtime_identity}|{image_digest}|{issued}".encode()
        ).hexdigest()[:32],
        "issued_at": issued,
        "expires_at": issued + ttl,
        "request_id": str(request_id),
        "environment_id": environment_id,
        "runtime_identity": runtime_identity,
        "image_digest": image_digest,
        "command_digest": str(command_digest),
        "container_config_digest": container_config_digest,
        "probes": [
            {
                "probe_id": str(probe["probe_id"]),
                "boundary": str(probe["boundary"]),
                "status": str(probe["status"]).upper(),
                "expected_result": str(probe.get("expected_result", "")),
                "observed_result": str(probe.get("observed_result", "")),
                "exit_code": probe.get("exit_code"),
                "evidence_artifact_ids": list(
                    probe.get("evidence_artifact_ids", [])
                ),
                "failure_reason": probe.get("failure_reason"),
            }
            for probe in probes
        ],
    }
    record["digest"] = evidence_digest(record)
    return record


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def verify_evidence(
    evidence: Mapping[str, Any] | None,
    *,
    expected_runtime: str,
    expected_image_digest: str,
    expected_config_digest: str,
    expected_verifier_version: str = VERIFIER_VERSION,
    expected_request_id: str | None = None,
    expected_command_digest: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Recompute the verdict from the record; never trust a self-report."""
    if not evidence:
        return {
            "state": "blocked",
            "reasons": ["evidence_missing"],
            "verifier_version": VERIFIER_VERSION,
        }
    missing = [key for key in REQUIRED_EVIDENCE_FIELDS if key not in evidence]
    if missing:
        return {
            "state": "blocked",
            "reasons": [f"evidence_incomplete:{name}" for name in missing],
            "verifier_version": VERIFIER_VERSION,
        }
    if evidence.get("verifier_version") != expected_verifier_version:
        return {
            "state": "blocked",
            "reasons": [
                "verifier_version_mismatch:"
                f"{evidence.get('verifier_version')}->{expected_verifier_version}"
            ],
            "verifier_version": VERIFIER_VERSION,
        }
    if evidence_digest(evidence) != evidence.get("digest"):
        return {
            "state": "failed",
            "reasons": ["evidence_digest_mismatch"],
            "verifier_version": VERIFIER_VERSION,
        }
    issued = _timestamp(evidence.get("issued_at"))
    expires = _timestamp(evidence.get("expires_at"))
    current = float(time.time() if now is None else now)
    if issued is None or expires is None:
        return {
            "state": "blocked",
            "reasons": ["evidence_time_malformed"],
            "verifier_version": VERIFIER_VERSION,
        }
    if expires <= issued:
        return {
            "state": "blocked",
            "reasons": ["evidence_time_invalid"],
            "verifier_version": VERIFIER_VERSION,
        }
    if issued > current + CLOCK_SKEW_SECONDS:
        return {
            "state": "blocked",
            "reasons": ["evidence_issued_in_the_future"],
            "verifier_version": VERIFIER_VERSION,
        }
    if current > expires:
        return {
            "state": "expired",
            "reasons": ["evidence_expired"],
            "verifier_version": VERIFIER_VERSION,
        }
    if expected_request_id is not None and str(
        evidence.get("request_id", "")
    ) != str(expected_request_id):
        return {
            "state": "blocked",
            "reasons": ["evidence_request_mismatch"],
            "verifier_version": VERIFIER_VERSION,
        }
    if expected_command_digest is not None and str(
        evidence.get("command_digest", "")
    ) != str(expected_command_digest):
        return {
            "state": "blocked",
            "reasons": ["command_digest_mismatch"],
            "verifier_version": VERIFIER_VERSION,
        }
    for name, expected in (
        ("runtime_identity", expected_runtime),
        ("image_digest", expected_image_digest),
        ("container_config_digest", expected_config_digest),
    ):
        if evidence.get(name) != expected:
            return {
                "state": "blocked",
                "reasons": [f"identity_mismatch:{name}"],
                "verifier_version": VERIFIER_VERSION,
            }
    probes = evidence.get("probes")
    if not isinstance(probes, list):
        return {
            "state": "failed",
            "reasons": ["probes_not_a_list"],
            "verifier_version": VERIFIER_VERSION,
        }
    ids = [str(probe.get("probe_id")) for probe in probes]
    if len(ids) != len(set(ids)):
        return {
            "state": "failed",
            "reasons": ["duplicate_probe_ids"],
            "verifier_version": VERIFIER_VERSION,
        }
    by_boundary: dict[str, list[Mapping[str, Any]]] = {}
    for probe in probes:
        boundary = str(probe.get("boundary"))
        by_boundary.setdefault(boundary, []).append(probe)
    unknown = sorted(set(by_boundary) - set(PROBE_BOUNDARIES))
    if unknown:
        return {
            "state": "failed",
            "reasons": [f"unknown_boundary:{name}" for name in unknown],
            "verifier_version": VERIFIER_VERSION,
        }
    missing_boundaries = [
        name for name in PROBE_BOUNDARIES if name not in by_boundary
    ]
    if missing_boundaries:
        return {
            "state": "blocked",
            "reasons": [
                f"boundary_not_executed:{name}" for name in missing_boundaries
            ],
            "verifier_version": VERIFIER_VERSION,
        }
    for boundary, entries in by_boundary.items():
        if len(entries) != 1:
            return {
                "state": "failed",
                "reasons": [f"duplicate_boundary:{boundary}"],
                "verifier_version": VERIFIER_VERSION,
            }
        status = str(entries[0].get("status", "")).upper()
        if status not in PROBE_STATUSES:
            return {
                "state": "failed",
                "reasons": [f"invalid_probe_status:{boundary}"],
                "verifier_version": VERIFIER_VERSION,
            }
        if status == "FAIL":
            return {
                "state": "failed",
                "reasons": [f"boundary_failed:{boundary}"],
                "verifier_version": VERIFIER_VERSION,
            }
        if status == "NOT_EXECUTED":
            return {
                "state": "blocked",
                "reasons": [f"boundary_not_executed:{boundary}"],
                "verifier_version": VERIFIER_VERSION,
            }
        if status in {"ERROR", "INCONCLUSIVE"}:
            return {
                "state": "unverified",
                "reasons": [f"boundary_inconclusive:{boundary}"],
                "verifier_version": VERIFIER_VERSION,
            }
    return {
        "state": "verified",
        "reasons": [],
        "verifier_version": VERIFIER_VERSION,
        "digest": evidence.get("digest"),
    }
