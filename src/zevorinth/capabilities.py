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
    "product_stage": "3.92",
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
                "four DeepSeek baselines completed on fixed synthetic datasets",
                "latest applicability-v1 baseline accepted zero executable unsafe plans",
                "latest strict baseline is 15 of 19 cases",
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
        "planner_intent_fidelity_contract": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["python_api", "report:planner_quality"],
            "limitations": [
                "deterministic unsafe-fragment and parameter-loss checks",
                "not general natural-language entailment",
                "not retroactively applicable to Phase 3.82 strict score",
            ],
        },
        "planner_intent_fidelity_gate": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["llm_planner", "service:submit"],
            "requirements": [
                "valid intent fidelity assessment before planned output",
                "fail-closed on checker error or inconsistent result",
            ],
            "limitations": [
                "deterministic contract checks are not general natural-language entailment",
                "gate prevents plan acceptance but does not authenticate goal provenance",
            ],
        },
        "planner_text_precision_contract": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["python_api", "report:planner_text_precision"],
            "requirements": [
                "explicit trusted precision_mode or a precision-specific trusted value",
                "exact_text requires trusted expected_text",
                "format_constrained requires trusted content_format",
            ],
            "limitations": [
                "defaults to semantic mode to preserve ordinary writing behavior",
                "exact comparisons do not trim, normalize Unicode, or adjust punctuation",
                "not retroactively applicable to Phase 3.88 live scores",
            ],
        },
        "planner_text_precision_gate": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["llm_planner", "service:submit"],
            "requirements": [
                "contract validation before the model call",
                "code-point comparison after local candidate validation",
                "fail-closed contract errors and precision mismatches",
            ],
            "limitations": [
                "applies only to planned write_text_artifact candidates",
                "does not replace Policy, authorization, intent fidelity, or execution checks",
                "empty file content remains invalid before text precision",
            ],
        },
        "planner_text_precision_execution": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["python_api", "report:planner_text_execution"],
            "requirements": [
                "scripted or explicitly configured planner",
                "temporary local workspace",
                "real artifact write",
                "independent byte readback and artifact evidence check",
            ],
            "limitations": [
                "end-to-end regression uses fixed scripted candidates",
                "does not measure live model quality",
                "writes only inside a temporary workspace",
                "does not provide L2 or L3 provenance authentication",
            ],
        },
        "local_read_only_workbench": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["cli:workbench", "loopback_http_read_only"],
            "requirements": [
                "loopback-only bind",
                "existing read-only task database",
                "registered artifact evidence for previews",
            ],
            "limitations": [
                "task list and detail are read-only",
                "no task submission, retry, cancellation, recovery or deletion",
                "no remote hosting or multi-user authentication",
                "event payload display is bounded and redacted",
            ],
        },
        "task_control_center_clarification": {
            "status": "conditional",
            "implemented": True,
            "verified": True,
            "access": [
                "cli:control-center",
                "loopback_control_http",
                "loopback_control_request_status",
            ],
            "requirements": [
                "request-id idempotent queued-task creation with planner preview",
                "deterministic planner task for clarification, revision and supported start",
                "unclaimed queued task for queued cancellation",
                "task notes are explicitly non-lifecycle audit messages",
                "matching loopback Origin and per-process CSRF token",
                "explicit confirmation and fresh task version",
                "exactly the currently missing clarification fields",
                "exclusive SQLite task claim for clarification; optimistic plan version for revision",
            ],
            "limitations": [
                "task creation, add_task_note, answer_clarification, revise_unstarted_task, start_queued_task and cancel_queued_task are enabled",
                "the existing read-only /api mutation boundary remains unchanged",
                "no run_tests start, retry, running-task cancel, recovery, tool governance or high-risk file action is enabled",
                "cross-process actions are not merged; a losing claim fails closed",
                "requests without a terminal control event require manual reconciliation",
            ],
        },
        "live_evaluation_integration_contract": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["python_api", "report:live_evaluation_readiness"],
            "requirements": [
                "dataset, applicability, and active intent contracts match frozen hashes",
                "all metric denominators are present",
            ],
            "limitations": [
                "does not verify task side effects",
                "does not authorize a live model run",
            ],
        },
        "planner_precision_regression": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["python_api", "report:planner_precision"],
            "limitations": [
                "offline scripted regression only",
                "does not measure live model behavior",
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
            "status": "conditional",
            "implemented": True,
            "verified": True,
            "requirements": [
                "local process and browser access",
                "loopback-only workbench server",
            ],
            "limitations": [
                "read-only operator surface only",
                "no remote, multi-user or authentication support",
                "no execution controls",
            ],
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
