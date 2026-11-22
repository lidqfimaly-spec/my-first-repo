"""Authoritative, machine-readable product capability registry."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .planner import (
    CONTROLLED_SHELL,
    EDIT_TEXT_FILE,
    INSPECT_WORKSPACE,
    MODIFY_TEXT_ARTIFACT,
    READ_TEXT_ARTIFACT,
    RESEARCH_REPORT,
    RUN_TESTS,
    WRITE_TEXT_ARTIFACT,
)


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
        READ_TEXT_ARTIFACT: {
            "status": "available",
            "implemented": True,
            "verified": True,
            "policy_allowed": True,
            "access": [
                "service:submit",
                "deterministic_planner",
            ],
            "execution_modes": ["real"],
            "requirements": [
                "canonical source task UUID",
                "verified source artifact",
                "UTF-8 content within the 64 KiB limit",
            ],
            "verification": (
                "independent source reread with size, SHA-256 and content checks"
            ),
            "limitations": [
                "reads only artifacts owned by a previously verified task",
                "does not read arbitrary workspace paths",
            ],
        },
        MODIFY_TEXT_ARTIFACT: {
            "status": "available",
            "implemented": True,
            "verified": True,
            "policy_allowed": True,
            "access": [
                "service:submit",
                "deterministic_planner",
            ],
            "execution_modes": ["real"],
            "requirements": [
                "canonical source task UUID",
                "verified source artifact",
                "exact source SHA-256",
                "bounded replace or append content",
                "non-existing output path in the new task directory",
            ],
            "verification": (
                "independent output readback with size, SHA-256, content and "
                "source-lineage checks"
            ),
            "limitations": [
                "creates a new task-scoped version; never overwrites the source",
                "supports replace or append of text, not arbitrary binary edits",
            ],
        },
        INSPECT_WORKSPACE: {
            "status": "available",
            "implemented": True,
            "verified": True,
            "policy_allowed": True,
            "access": [
                "service:submit",
                "deterministic_planner",
                "multi_step_task_engine",
            ],
            "execution_modes": ["real"],
            "requirements": [
                "registered workspace",
                "bounded relative path",
                "protected paths excluded",
                "bounded file count and bytes",
            ],
            "verification": (
                "independent repeat inspection with path, byte and SHA-256 checks"
            ),
            "limitations": [
                "read-only inspection only",
                "does not traverse symbolic links",
                "not model-selectable through the LLM planner",
            ],
        },
        EDIT_TEXT_FILE: {
            "status": "available",
            "implemented": True,
            "verified": True,
            "policy_allowed": True,
            "access": [
                "service:submit",
                "deterministic_planner",
                "multi_step_task_engine",
            ],
            "execution_modes": ["real"],
            "requirements": [
                "registered workspace",
                "explicit local-code-edit authorization",
                "bounded relative file path",
                "exact source SHA-256",
                "exactly one old_text match",
                "protected paths excluded",
            ],
            "verification": (
                "atomic edit with durable backup and independent readback "
                "content, byte-count and SHA-256 checks"
            ),
            "limitations": [
                "text replacement only; no arbitrary binary edit",
                "no arbitrary shell execution",
                "not model-selectable through the LLM planner",
            ],
        },
        RESEARCH_REPORT: {
            "status": "available",
            "implemented": True,
            "verified": True,
            "policy_allowed": True,
            "access": [
                "service:submit",
                "deterministic_planner",
                "multi_step_task_engine",
            ],
            "execution_modes": ["real"],
            "requirements": [
                "registered local workspace",
                "bounded UTF-8 source files",
                "exact source quotes",
                "explicit uncertainty labels",
                "non-overwriting Markdown output path",
            ],
            "verification": (
                "independent source rehash, exact citation matching, and "
                "report byte and SHA-256 readback"
            ),
            "limitations": [
                "offline local sources only; no network or browser access",
                "proves citation presence, not source truth or authenticity",
                "not model-selectable through the LLM planner",
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
                (
                    "one-time host execution grant issued out of band"
                    " (`zevorinth authorize-host-test`) for the host backend,"
                    " bound to this workspace and target, single use,"
                    " short lived"
                ),
            ],
            "verification": (
                "independent test evidence and execution-plan revalidation"
            ),
            "error_classification": {
                "container_backend_unavailable": (
                    "the container runtime could not be reached or probed;"
                    " an environment condition, never a task timeout and never"
                    " a reason to fall back to the host"
                ),
                "test_runner_error": (
                    "the runner raised an unexpected exception; an executor"
                    " defect, not a test outcome"
                ),
                "workspace_unreadable": (
                    "the registered workspace exists but cannot be read;"
                    " reported instead of claiming the test target is missing"
                ),
                "test_spawn_failed": (
                    "the test process could not be created (for example"
                    " Popen raised OSError); never used for a test that ran"
                    " and exited non-zero, which is a test outcome"
                ),
                "test_execution_failed": (
                    "the runner completed but verification did not classify a"
                    " specific reason"
                ),
                "tests_failed": "the tests themselves failed",
                "test_timeout": "the tests exceeded the bounded timeout",
                "host_authorization_required": (
                    "host backend requested without a grant"
                ),
                "host_authorization_invalid": (
                    "the token is malformed or unknown, or the stored grant"
                    " record cannot be read; an unreadable authorization is"
                    " not an authorization"
                ),
                "host_authorization_expired": (
                    "the grant's lifetime ended before execution"
                ),
                "host_authorization_scope_mismatch": (
                    "the grant covers a different workspace or target than the"
                    " one about to run"
                ),
                "host_authorization_already_used": (
                    "the single-use transition already happened; a grant is"
                    " never handed back"
                ),
                "host_authorization_revoked": (
                    "a human revoked the grant before it was consumed"
                ),
                "host_authorization_storage_failed": (
                    "the grant store could not be read or written; refusing"
                    " rather than treating an unreadable authorization as no"
                    " restriction"
                ),
                "host_authorization_audit_failed": (
                    "the audit entry could not be appended; the grant is spent"
                    " and the run is refused, because an execution nobody can"
                    " find in the audit trail must not happen"
                ),
            },
            "limitations": [
                "container availability is environment-dependent",
                "host backend is not trusted isolation",
                (
                    "no automatic fallback between backends: a container"
                    " backend failure is reported, never retried on the host"
                ),
                (
                    "a consumed grant is never restored, including when the"
                    " test run itself fails"
                ),
                "verified only on the current macOS/POSIX environment",
            ],
        },
        CONTROLLED_SHELL: {
            "status": "available",
            "implemented": True,
            "verified": True,
            "policy_allowed": True,
            "access": [
                "service:submit",
                "deterministic_planner",
            ],
            "execution_modes": ["real", "simulated"],
            "requirements": [
                "local runtime home",
                "command in the fixed whitelist",
                "parameter array (no shell string)",
                "bounded timeout (1..30s)",
                "output limit (64 KiB)",
                "working directory within the workspace",
            ],
            "verification": (
                "exit code, output bytes, timeout status, and "
                "truncation marker captured in workspace_effects"
            ),
            "limitations": [
                "whitelist of read-only commands only (ls/cat/wc/head/tail/grep/find/pwd/date/echo/file/stat/diff/sort/uniq/basename/dirname/cut/tr/rev/strings)",
                "no interpreters (python/node), no write commands (rm/mv/cp), no network (curl/wget)",
                "no shell=True; arguments passed as a list",
                "path arguments validated against workspace escape and sensitive paths",
                "forbidden flags rejected (-delete/-exec/-ok/-print0/-ls)",
                "stdin disconnected; minimal environment inherited",
            ],
        },
    },
    "capabilities": {
        "proposal_candidate_validation": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": [
                "service:submit_code_proposal",
                "service:submit_research_proposal",
            ],
            "requirements": [
                "closed versioned candidate schema",
                "trusted provider identity and session nonce",
                "matching input-context digest",
                "deterministic code or citation validation",
            ],
            "verification": (
                "candidate bytes remain untrusted until existing workspace, "
                "citation, Policy and Verifier checks pass"
            ),
            "limitations": [
                "proposal providers are injectable and disabled by default "
                "for live model or network use",
                "candidate validation grants no execution authority",
                "code test execution still requires the existing test backend "
                "authorization",
            ],
        },
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
        "multi_step_task_engine": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["service:submit_plan", "service:approve_step"],
            "requirements": [
                "explicit acyclic step graph",
                "at most 8 steps and 3 attempts per step",
                "existing verified child-task tools",
                "explicit approval for approval-gated steps",
            ],
            "limitations": [
                "scheduling is deterministic and sequential in this version",
                "not model-selectable through the LLM planner",
                "partial success is terminal and requires a follow-up task",
            ],
        },
        "product_acceptance_runner": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": [
                "cli:eval-product",
                "python:zevorinth.product_evaluation",
            ],
            "requirements": [
                "isolated temporary runtime home",
                "local registered workspaces",
                "explicit host authorization for code-test acceptance",
            ],
            "limitations": [
                "current-host evidence only",
                "engine-slice acceptance only; V1 control-center user journey is incomplete",
                "code analysis and research synthesis are caller-authored in this version",
                "does not cover network, live-model, container, multi-user, or multi-host execution",
                "not a production trust or certification claim",
            ],
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
        "event_streaming": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["cli:workbench", "loopback_http_read_only"],
            "requirements": [
                "loopback-only bind",
                "existing read-only task database",
                "browser EventSource support",
            ],
            "limitations": [
                "pushes read-only event projections for one selected task",
                "no task, event, or workspace mutation",
                "bounded poll interval and stream lifetime",
                "no remote or multi-user access",
            ],
        },
        "model_usage_projection": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["cli:workbench", "loopback_http_read_only"],
            "requirements": [
                "loopback-only bind",
                "existing read-only task database",
                "planning events with planning_metadata payloads",
            ],
            "limitations": [
                "read-only projection of persisted planning diagnostics",
                "token totals are None when provider usage is missing, never zero",
                "no cost estimation without a configured price table",
                "covers planning calls only; tool execution timing is separate",
            ],
        },
        "conversational_intake": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": [
                "cli:control-center",
                "loopback_control_http",
            ],
            "requirements": [
                "loopback-only bind",
                "matching loopback Origin and per-process CSRF token",
                "deterministic IntentClassifier (no LLM dependency for routing)",
                "existing ConversationService and EventStore persistence",
            ],
            "limitations": [
                "conversation creation and message submission are mutations on the control surface",
                "system responses (ConversationProcessResult) are returned but not persisted as assistant messages",
                "NEW_TASK intent creates a queued or awaiting_clarification task; execution still requires a separate control action",
                "no natural-language mid-task modification (SUPPLEMENT/MODIFY_PENDING return needs_attention)",
                "conversation read endpoints (detail, messages) are on the control surface only",
            ],
        },
        "workspace_management": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": [
                "loopback_control_http",
            ],
            "requirements": [
                "loopback-only bind",
                "matching loopback Origin and per-process control token",
                "workspace name must match ^[A-Za-z0-9_-]{1,64}$",
            ],
            "limitations": [
                "creating a workspace does not authorize execution: a host"
                " run_tests in that workspace still requires a one-time grant"
                " issued by a separate CLI command",
                "creation finishes by calling resolve_workspace, so the same"
                " fence applies (no symlinks, must resolve inside"
                " workspaces/)",
                "listing reports counts and the newest mtime, never file"
                " contents",
                "listing returns relative paths only; the creation response"
                " returns the resolved absolute path so the user can verify"
                " what was created",
                "no rename or delete: a workspace is removed outside the app,"
                " deliberately",
            ],
        },
        "cross_task_memory": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": [
                "cli:control-center",
                "loopback_control_http",
            ],
            "requirements": [
                "loopback-only bind",
                "matching loopback Origin and per-process CSRF token",
                "existing SQLite database (task_memories table auto-created)",
            ],
            "limitations": [
                "five memory types: user_preference, project_context, task_history, decision_record, knowledge",
                "three scopes: global, project, task",
                "simple LIKE text search (no vector/embedding search)",
                "memories are mutable: corrections supersede old records, never hard-delete",
                "content bounded to 16 KiB; key bounded to 256 chars",
                "memory endpoints on /control/api surface only (recording is a mutation)",
                "no automatic task outcome recording yet (manual or TaskService integration TBD)",
            ],
        },
        "multi_session_management": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": [
                "cli:control-center",
                "loopback_control_http",
            ],
            "requirements": [
                "loopback-only bind",
                "matching loopback Origin and per-process CSRF token",
                "existing ConversationService and EventStore persistence",
            ],
            "limitations": [
                "multiple conversations each with their own active task",
                "GET /control/api/conversations lists conversations with active task status",
                "conversation switcher in frontend (select + new conversation button)",
                "NEW_TASK in a conversation with an active task returns active_task_conflict (by design)",
                "creating a new conversation allows starting a new task while the old one runs",
                "no true task parallelism (single-threaded execution); tasks run sequentially per conversation",
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
                "task creation, task notes, clarification, revision, start, queued cancellation, pause, resume, step approval/rejection and pending-step replanning are enabled",
                "the existing read-only /api mutation boundary remains unchanged",
                "no standalone run_tests start, automatic retry, automatic recovery, arbitrary shell or unrestricted external write is enabled",
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
        "manual_retry": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": [
                "loopback_control_http",
                "service:python_api",
            ],
            "requirements": [
                "task in retryable status (failed/interrupted/planning_failed)",
                "side-effect-free tool (controlled_shell/inspect_workspace/read_text_artifact/research_report)",
                "transient error code (reachable today: timeout; shell_failed fallback)",
                "attempt_count < MAX_RETRY_ATTEMPTS (3) across the whole retry chain",
                "user-triggered POST /control/api/tasks/{id}/retry with a canonical UUIDv4 request_id",
            ],
            "limitations": [
                "manual and user-triggered: nothing in the execution loop retries on its own",
                "non-idempotent tools (write_text_artifact/modify_text_artifact/edit_text_file/run_tests) are never retryable",
                "permanent errors (policy violations, invalid parameters, verification failures) are never retryable",
                "unknown error codes fail closed, so plain shell exit codes (exit_2 and friends) are not retryable",
                "lease-expiry interruptions currently record error code 'interrupted', which the policy treats as non-retryable",
                "retry creates a NEW task linked via metadata.retry_of / retry_root; the original stays terminal",
                "retry re-submits the original request; it never resumes a partial result",
            ],
        },
        "automatic_retry": {
            "status": "not_implemented",
            "implemented": False,
            "verified": False,
            "limitations": [
                "no retry trigger exists inside the execution loop; manual_retry is user-triggered only",
            ],
        },
        "automatic_recovery": {
            "status": "not_implemented",
            "implemented": False,
            "verified": False,
            "limitations": [
                "no recovery scheduler supervises failed or interrupted tasks",
                "manual_retry is user-triggered and must not be described as automatic recovery",
            ],
        },
        "interrupted_task_detection": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["service:python_api"],
            "requirements": [
                "TaskService instantiated with recover_leases=True (the default)",
                "task holds a stale execution lease, or is running/verifying without one",
            ],
            "limitations": [
                "detection only marks the task interrupted and records a task_interrupted event",
                "detection does not restart, resume or retry the task",
            ],
        },
        "interrupted_task_resume": {
            "status": "available",
            "implemented": True,
            "verified": True,
            "access": ["service:python_api", "loopback_control_http"],
            "requirements": [
                "explicit resume request against an interrupted task",
                "multi-step plans skip steps that already succeeded",
            ],
            "limitations": [
                "explicitly triggered; not a background supervisor",
                "single-tool resume paths exist for research report, workspace inspection, workspace edit and test tasks",
                "steps that already produced side effects are not replayed",
            ],
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
            "status": "available",
            "implemented": True,
            "verified": True,
            "superseded_by": "controlled_shell",
            "requirements": [
                "controlled_shell task operation with whitelist and parameter array",
            ],
            "limitations": [
                "not arbitrary shell; only whitelisted read-only commands",
                "no shell=True; parameter array only",
                "see controlled_shell task operation for full boundaries",
            ],
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
