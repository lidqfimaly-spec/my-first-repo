"""Shared evidence-assurance contract for audit and user-visible outputs."""

from __future__ import annotations

from typing import Any


AUTHENTICITY = "not_authenticated"
TRUST_ROOT = "none"
SAME_PRIVILEGE_REWRITE_PROTECTED = False
SECURITY_CERTIFICATION = False

ASSURANCE_LEVELS = {
    "L0": {
        "label": "missing_or_inconsistent",
        "meaning": "evidence is missing, corrupt, contradictory or unreadable",
        "implemented": True,
        "allowed_behavior": [
            "read_only_inspection",
            "manual_review",
        ],
        "prohibited_behavior": [
            "success_display",
            "automatic_retry",
            "automatic_cleanup",
            "security_certification",
        ],
    },
    "L1": {
        "label": "internal_consistency_only",
        "meaning": (
            "local records and current observation are internally consistent"
        ),
        "implemented": True,
        "allowed_behavior": [
            "display_internal_consistency",
            "manual_inspection",
            "retain_existing_controlled_result",
        ],
        "prohibited_behavior": [
            "claim_authenticity",
            "claim_security_certification",
            "automatic_retry",
            "automatic_cleanup",
            "general_recovery",
        ],
    },
    "L2": {
        "label": "source_authenticated",
        "meaning": "evidence source identity is independently authenticated",
        "implemented": False,
        "allowed_behavior": [],
        "prohibited_behavior": [
            "claim_l2_until_a_trust_root_is_implemented",
        ],
    },
    "L3": {
        "label": "independent_attestation",
        "meaning": (
            "audit or runtime is attested independently of local permissions"
        ),
        "implemented": False,
        "allowed_behavior": [],
        "prohibited_behavior": [
            "claim_l3_until_external_attestation_is_implemented",
        ],
    },
}


def assurance_level(level: str) -> dict[str, Any]:
    definition = ASSURANCE_LEVELS[level]
    return {
        "level": level,
        **definition,
        "allowed_behavior": list(definition["allowed_behavior"]),
        "prohibited_behavior": list(definition["prohibited_behavior"]),
        "evidence_authenticated": (
            definition["implemented"] and level in {"L2", "L3"}
        ),
        "security_certification": (
            definition["implemented"] and level == "L3"
        ),
    }


def trust_boundary_fields() -> dict[str, Any]:
    return {
        "authenticity": AUTHENTICITY,
        "trust_root": TRUST_ROOT,
        "same_privilege_rewrite_protected": (
            SAME_PRIVILEGE_REWRITE_PROTECTED
        ),
        "security_certification": SECURITY_CERTIFICATION,
    }


def with_assurance(
    report: dict[str, Any],
    level: str,
    *,
    include_trust_boundary: bool = False,
) -> dict[str, Any]:
    enriched = {
        **report,
        "assurance_level": level,
        "assurance": assurance_level(level),
    }
    if include_trust_boundary:
        enriched.update(trust_boundary_fields())
    enriched["security_certification"] = SECURITY_CERTIFICATION
    return enriched


def level_for_status(status: str) -> str:
    return (
        "L1"
        if status
        in {
            "completed",
            "consistent",
            "denied",
            "failed",
            "not_claimed",
            "succeeded",
            "verified_failure",
        }
        else "L0"
    )


def with_status_assurance(
    report: dict[str, Any],
    *,
    include_trust_boundary: bool = True,
) -> dict[str, Any]:
    status = str(report.get("status", "unverified"))
    return with_assurance(
        report,
        level_for_status(status),
        include_trust_boundary=include_trust_boundary,
    )
