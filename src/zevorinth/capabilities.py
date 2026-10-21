"""Authoritative, machine-readable product capability registry."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .planner import RUN_TESTS, WRITE_TEXT_ARTIFACT


CAPABILITY_REGISTRY_VERSION = "capability-registry-v1"
CAPABILITY_STATUSES = frozenset(
    {
        "available",
        "conditional",
        "internal_only",
        "not_implemented",
        "unverified",
    }
)

CAPABILITY_REGISTRY: dict[str, Any] = {
    "version": CAPABILITY_REGISTRY_VERSION,
    "product_stage": "3.75",
    "deployment_scope": "single_host_local_alpha",
    "task_operations": {
        WRITE_TEXT_ARTIFACT: {
            "status": "available",
            "implemented": True,
            "verified": True,
            "policy_allowed": True,
            "access": [
                "cli:run",
                "service:submit",
                "deterministic_planner",
                "llm_planner",
            ],
            "execution_modes": ["real", "simulated"],
            "requirements": [
                "local runtime home",
                "non-overwriting artifact path",
                "non-empty UTF-8 content within the 64 KiB limit",
            ],
            "verification": (
                "independent file readback with size and SHA-256 checks"
            ),
            "limitations": [
                "creates one new text artifact; no append or overwrite",
            ],
        },
        RUN_TESTS: {
            "status": "conditional",
            "implemented": True,
            "verified": True,
            "policy_allowed": True,
            "access": [
                "cli:test",
                "service:submit",
                "deterministic_planner",
                "llm_planner",
            ],
            "execution_modes": ["real"],
            "requirements": [
                "registered workspace",
                "bounded unittest target",
                "valid isolation evidence",
                "container runtime for trusted isolation",
                "explicit local-maintenance authorization for host backend",
            ],
            "verification": (
                "independent test evidence and execution-plan revalidation"
            ),
            "limitations": [
                "container availability is environment-dependent",
                "host backend is not trusted isolation",
                "verified only on the current macOS/POSIX environment",
            ],
        },
    },
    "capabilities": {
        "deterministic_planner": {
            "status": "available",
            "implemented": True,
            "verified": True,
        },
        "llm_planner": {
            "status": "conditional",
            "implemented": True,
            "verified": True,
            "requirements": [
                "explicit llm planner selection",
                "provider and model configuration",
                "provider API key",
            ],
            "limitations": [
                "general model quality is not established by fixed pilots",
            ],
        },
        "independent_verification": {
            "status": "available",
            "implemented": True,
            "verified": True,
        },
        "task_persistence_and_events": {
            "status": "available",
            "implemented": True,
            "verified": True,
        },
        "lease_fencing_budget": {
            "status": "available",
            "implemented": True,
            "verified": True,
        },
        "clarification_and_resume": {
            "status": "available",
            "implemented": True,
            "verified": True,
        },
        "cancellation": {
            "status": "available",
            "implemented": True,
            "verified": True,
        },
        "controlled_cleanup": {
            "status": "conditional",
            "implemented": True,
            "verified": True,
            "requirements": ["explicit operator invocation and ownership checks"],
            "limitations": ["no automatic cleanup queue"],
        },
        "task_lifecycle_audit": {
            "status": "available",
            "implemented": True,
            "verified": True,
        },
        "planner_quality_offline": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["cli:eval-planner-quality", "python_api"],
            "limitations": [
                "scripted outputs validate the evaluator and dataset, not live model quality",
                "no live model requests are included",
            ],
        },
        "planner_live_baseline_protocol": {
            "status": "conditional",
            "implemented": True,
            "verified": True,
            "access": [
                "cli:eval-planner-quality",
                "python_api",
                "report:planner_quality",
            ],
            "requirements": [
                "separate explicit authorization",
                "exact provider, model, cases, budget, and output path",
                "explicit --authorize-live confirmation",
            ],
            "limitations": [
                "default-deny live planning runner",
                "two DeepSeek baselines completed on fixed synthetic datasets",
                "latest applicability-v1 strict baseline is 14 of 19 cases",
                "the dataset still includes scripted-only controls and is not a clean live benchmark",
            ],
        },
        "planner_live_applicability_contract": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["python_api", "report:planner_quality"],
            "limitations": [
                "applies only to reports generated under contract v1",
                "19 strict cases, 3 exploratory cases, and 3 scripted-only cases",
                "not retroactively applicable to the Phase 3.79 live result",
            ],
        },
        "recovery_audit": {
            "status": "available",
            "implemented": True,
            "verified": True,
        },
        "recovery_preview_and_approval": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "limitations": ["approval does not itself execute recovery"],
        },
        "prepublish_write_recovery": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["cli:recover-prepublish", "service:python_api"],
            "requirements": [
                "source task failed with publish_lock_timeout",
                "no artifact or verification evidence",
                "artifact target absent",
                "staging directory clean",
                "explicit recovery authorization",
            ],
            "limitations": [
                "one deterministic replacement task per source task",
                "not general task recovery",
            ],
        },
        "controlled_recovery_marker": {
            "status": "internal_only",
            "implemented": True,
            "verified": True,
            "access": ["python_api"],
            "limitations": [
                "single fixed marker action",
                "not general task recovery",
            ],
        },
        "automatic_retry": {
            "status": "not_implemented",
            "implemented": False,
            "verified": False,
        },
        "automatic_recovery": {
            "status": "not_implemented",
            "implemented": False,
            "verified": False,
        },
        "automatic_cleanup": {
            "status": "not_implemented",
            "implemented": False,
            "verified": False,
        },
        "general_task_recovery": {
            "status": "not_implemented",
            "implemented": False,
            "verified": False,
        },
        "general_shell": {
            "status": "not_implemented",
            "implemented": False,
            "verified": False,
        },
        "browser": {
            "status": "not_implemented",
            "implemented": False,
            "verified": False,
        },
        "web_ui": {
            "status": "not_implemented",
            "implemented": False,
            "verified": False,
        },
        "multi_host_execution": {
            "status": "not_implemented",
            "implemented": False,
            "verified": False,
        },
        "l2_l3_trust": {
            "status": "not_implemented",
            "implemented": False,
            "verified": False,
        },
        "cross_os_semantics": {
            "status": "unverified",
            "implemented": False,
            "verified": False,
        },
        "general_llm_planning_quality": {
            "status": "unverified",
            "implemented": False,
            "verified": False,
        },
    },
    "not_available_task_types": [
        "general_shell",
        "browser",
        "general_task_recovery",
    ],
}

SUPPORTED_TASK_OPERATIONS = frozenset(CAPABILITY_REGISTRY["task_operations"])


def capability_manifest() -> dict[str, Any]:
    """Return a detached, JSON-safe capability manifest."""
    return deepcopy(CAPABILITY_REGISTRY)


def task_operation_names() -> list[str]:
    return sorted(SUPPORTED_TASK_OPERATIONS)


def capability_summary() -> dict[str, int]:
    entries = [
        *CAPABILITY_REGISTRY["task_operations"].values(),
        *CAPABILITY_REGISTRY["capabilities"].values(),
    ]
    return {
        status: sum(
            1 for entry in entries if entry.get("status") == status
        )
        for status in sorted(CAPABILITY_STATUSES)
    }
