"""Command-line interface for the bounded Phase 1 task runtime."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .artifact_tool import SimulatedArtifactWriter
from .capabilities import (
    CAPABILITY_REGISTRY,
    CAPABILITY_REGISTRY_VERSION,
    capability_manifest,
    capability_summary,
    task_operation_names,
)
from .conversation_service import ConversationService
from .controlled_recovery import audit_controlled_recovery
from .recovery_preview import RecoveryApprovalStore
from .event_store import (
    ActiveTaskConflict,
    ConversationNotFound,
    ConversationTurnNotFound,
    EventStore,
    EventStoreError,
    MessageConflict,
    PlanRevisionConflict,
    TaskLinkConflict,
    TaskNotFound,
)
from .evaluation import (
    DEFAULT_PILOT,
    DEFAULT_RETEST_PILOT,
    EvaluationConfigurationError,
    online_preflight,
    run_evaluation,
)
from .llm_planner import LLMPlanner, LLMPlannerConfig, openai_sdk_status
from .models import PlannerMode, Task, TaskRequest, TaskStatus
from .planner import WRITE_TEXT_ARTIFACT
from .planner import DeterministicPlanner
from .planner_quality import (
    planner_quality_online_preflight,
    run_live_planner_quality,
    run_planner_quality,
)
from .product_evaluation import write_product_acceptance
from .service import TaskService
from .safety_gate import (
    SafetyGateConfigurationError,
    run_safety_gate,
)
from .workbench_server import serve_workbench


STATUS_EXIT_CODES = {
    TaskStatus.SUCCEEDED: 0,
    TaskStatus.FAILED: 1,
    TaskStatus.PLANNING_FAILED: 7,
    TaskStatus.AWAITING_CLARIFICATION: 3,
    TaskStatus.SIMULATED: 3,
    TaskStatus.BLOCKED: 4,
    TaskStatus.INTERRUPTED: 5,
    TaskStatus.QUEUED: 5,
    TaskStatus.RUNNING: 5,
    TaskStatus.VERIFYING: 5,
    TaskStatus.PAUSED: 5,
    TaskStatus.AWAITING_APPROVAL: 5,
    TaskStatus.PARTIAL: 1,
    TaskStatus.CANCELLED: 5,
}

RECOVERY_AUDIT_EXIT_CODES = {
    "completed": 0,
    "consistent": 0,
    "verified_failure": 1,
    "not_claimed": 3,
    "denied": 4,
    "unverified": 5,
}


def _project_root(override: Path | None = None) -> Path:
    return (override or Path.cwd()).resolve()


def doctor_report(project_root: Path | None = None) -> dict[str, object]:
    """Return a truthful, machine-readable snapshot of current capabilities."""
    root = _project_root(project_root)
    llm_config = LLMPlannerConfig.from_environment()
    sdk_status = openai_sdk_status()
    product_stage = str(CAPABILITY_REGISTRY["product_stage"])
    operation_names = task_operation_names()
    model_operations = [
        name
        for name in operation_names
        if "llm_planner"
        in CAPABILITY_REGISTRY["task_operations"][name]["access"]
    ]
    trusted_operations = [
        name for name in operation_names if name not in model_operations
    ]
    return {
        "schema_version": EventStore.SCHEMA_VERSION,
        "project": "Zevorinth",
        "version": __version__,
        "phase": product_stage,
        "hardening_phase": product_stage,
        "legacy_phase": 2,
        "legacy_hardening_phase": "1.5",
        "project_root": str(root),
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "runtime_dependencies": [],
        "capabilities": {
            "task_runtime": True,
            "write_text_artifact": True,
            "read_text_artifact": True,
            "modify_text_artifact": True,
            "inspect_workspace": True,
            "edit_text_file": True,
            "research_report": True,
            "run_tests": True,
            "deterministic_planner": True,
            "model_provider": llm_config.validation_error() is None,
            "controlled_tool_execution": True,
            "result_verification": True,
            "durable_event_store": True,
            "resume_interrupted_task": True,
            "sqlite_execution_leases": True,
            "llm_planner_available": bool(sdk_status["compatible"]),
            "controlled_recovery_audit": True,
            "task_lifecycle_audit": True,
            "event_sequence_audit": True,
            "prepublish_write_recovery": True,
            "planner_quality_offline": True,
            "planner_live_baseline_runner": True,
            "planner_live_baseline_enabled": False,
            "planner_live_applicability_contract": True,
            "planner_intent_fidelity_contract": True,
            "planner_intent_fidelity_gate": True,
            "planner_text_precision_contract": True,
            "planner_text_precision_gate": True,
            "planner_text_precision_execution": True,
            "local_read_only_workbench": True,
            "task_control_center_clarification": True,
            "live_evaluation_integration_contract": True,
            "planner_precision_regression": True,
            "recovery_assurance_levels": ["L0", "L1"],
            "recovery_authentication": "not_authenticated",
            "web_ui": True,
            "interactive_web_ui": True,
            "control_center_task_creation": True,
            "control_center_task_notes": True,
            "control_center_actions": [
                "add_task_note",
                "answer_clarification",
                "approve_step",
                "cancel_queued_task",
                "pause_task",
                "reject_step",
                "replan_pending_steps",
                "resume_task",
                "revise_unstarted_task",
                "start_queued_task",
            ],
            "general_shell": False,
            "automatic_recovery": False,
            "automatic_cleanup": False,
            "l2_l3_trust": False,
        },
        "task_types": operation_names,
        "capability_registry_version": CAPABILITY_REGISTRY_VERSION,
        "capability_summary": capability_summary(),
        "capability_manifest": capability_manifest(),
        "llm_planner": {
            **llm_config.public_report(),
            "sdk": sdk_status,
        },
        "deployment_scope": {
            "single_host_shared_sqlite": True,
            "distributed_execution": False,
        },
        "notice": (
            f"Phase {product_stage} is a single-host local Alpha with "
            f"{len(operation_names)} task operations: "
            f"{', '.join(operation_names)}. LLM planning may select only "
            f"{', '.join(model_operations)}; "
            f"{', '.join(trusted_operations)} require trusted local task "
            "provenance. LLM planning never grants tools or bypasses local "
            "validation, Policy, verification, isolation evidence, or leases."
        ),
    }


def _print_human(report: dict[str, object]) -> None:
    python = report["python"]
    assert isinstance(python, dict)
    capabilities = report["capabilities"]
    assert isinstance(capabilities, dict)

    print(f"Zevorinth {report['version']} (phase {report['phase']})")
    print(f"Project root: {report['project_root']}")
    print(
        "Python: "
        f"{python['implementation']} {python['version']} ({python['executable']})"
    )
    print("Capabilities:")
    for name, implemented in capabilities.items():
        print(f"  {name}: {'implemented' if implemented else 'not implemented'}")
    print(
        f"Capability registry: {report['capability_registry_version']}"
    )
    print(f"Task types: {', '.join(report['task_types'])}")
    manifest = report["capability_manifest"]
    task_operations = manifest["task_operations"]
    for name in report["task_types"]:
        details = task_operations[name]
        print(
            f"  {name}: status={details['status']} "
            f"verified={details['verified']}"
        )
    llm = report.get("llm_planner")
    if isinstance(llm, dict):
        sdk = llm.get("sdk", {})
        sdk_version = sdk.get("version") if isinstance(sdk, dict) else None
        print(
            "LLM planner: "
            f"configured={llm.get('configured')} "
            f"model={llm.get('model') or 'not configured'} "
            f"api_key_configured={llm.get('api_key_configured')} "
            f"sdk_version={sdk_version or 'not installed'}"
        )
    print(f"Notice: {report['notice']}")


def _service(args: argparse.Namespace) -> TaskService:
    writer = SimulatedArtifactWriter() if getattr(args, "simulate", False) else None
    command = getattr(args, "command", None)
    if command in {"status", "events"}:
        planner = DeterministicPlanner()
    else:
        selected = getattr(args, "planner", None)
        if selected is None:
            selected = os.environ.get("ZEVORINTH_PLANNER", "deterministic")
        try:
            mode = PlannerMode(selected)
        except ValueError as exc:
            raise ValueError(
                "planner must be deterministic or llm"
            ) from exc
        planner = (
            LLMPlanner.from_environment()
            if mode is PlannerMode.LLM
            else DeterministicPlanner()
        )
    return TaskService(
        _project_root(args.home),
        writer=writer,
        planner=planner,
        lease_seconds=args.lease_seconds,
        max_execution_seconds=args.max_execution_seconds,
        lease_renewal_interval_seconds=args.lease_renewal_interval_seconds,
        busy_timeout_ms=args.busy_timeout_ms,
    )


def _request_from_args(args: argparse.Namespace) -> TaskRequest:
    return TaskRequest(
        goal=args.goal,
        operation=args.operation,
        parameters={
            "artifact_path": args.artifact_path,
            "content": args.content,
        },
    )


def _patch_from_args(args: argparse.Namespace) -> dict[str, object] | None:
    patch: dict[str, object] = {}
    parameters: dict[str, object] = {}
    if args.goal is not None:
        patch["goal"] = args.goal
    if args.operation is not None:
        patch["operation"] = args.operation
    if args.artifact_path is not None:
        parameters["artifact_path"] = args.artifact_path
    if args.content is not None:
        parameters["content"] = args.content
    if parameters:
        patch["parameters"] = parameters
    return patch or None


def _task_human(task: Task) -> str:
    lines = [
        f"Task: {task.task_id}",
        f"Status: {task.status.value}",
        f"Execution mode: {task.execution_mode.value}",
        f"Phase: {task.phase}",
        f"Attempt: {task.attempt_id}",
        f"Created: {task.created_at}",
        f"Updated: {task.updated_at}",
        f"Claim generation: {task.claim_generation}",
        f"Execution owner: {task.execution_owner or 'none'}",
        f"Lease expires: {task.lease_expires_at or 'none'}",
        f"Planner: {task.planner_mode.value}",
    ]
    if task.planning_metadata:
        lines.append(
            "Planning: "
            f"{task.planning_metadata.get('classification', 'unknown')} "
            f"(latency_ms={task.planning_metadata.get('latency_ms', 'n/a')}, "
            f"model={task.planning_metadata.get('model', 'n/a')})"
        )
    if task.plan:
        lines.append(
            f"Plan: {task.plan.planner_name} ({len(task.plan.steps)} step)"
        )
        for step in task.plan.steps:
            lines.append(f"  {step.step_id}: {step.tool_name} -> {step.expected_result}")
    if task.missing_fields:
        lines.append(f"Clarification needed: {', '.join(task.missing_fields)}")
    if task.artifact:
        lines.append(
            "Artifact: "
            f"{task.artifact.relative_path} "
            f"({task.artifact.byte_count} bytes, sha256={task.artifact.sha256})"
        )
    if task.verification:
        lines.append(
            "Verification: "
            f"{'passed' if task.verification.passed else 'failed'} "
            f"({task.verification.reason})"
        )
    if task.error:
        lines.append(f"Error: {task.error.code}: {task.error.message}")
    if task.status is TaskStatus.SIMULATED:
        lines.append("Real artifact created: no (simulation only)")
    if task.status is TaskStatus.BLOCKED:
        lines.append("Tool executed: no (policy or planning rejection)")
    return "\n".join(lines)


def _print_task(task: Task, as_json: bool) -> None:
    if as_json:
        print(json.dumps(task.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_task_human(task))


def _parse_json_object(raw: str | None) -> dict[str, object] | None:
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("metadata must be a JSON object")
    return value


def _print_conversation(conversation: object, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                conversation.to_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return
    print(f"Conversation: {conversation.conversation_id}")
    print(f"Status: {conversation.status.value}")
    print(f"Version: {conversation.version}")


def _print_history(messages: list[object], as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                [message.to_dict() for message in messages],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return
    for message in messages:
        print(
            f"{message.created_at} [{message.role.value}] "
            f"{message.message_id}: {message.content}"
        )


CONVERSATION_EXIT_CODES = {
    "completed": 0,
    "succeeded": 0,
    "status": 0,
    "already_processed": 0,
    "awaiting_clarification": 3,
    "needs_question_binding": 3,
    "ambiguous": 3,
    "requires_structured_update": 3,
    "requires_confirmation": 4,
    "active_task_conflict": 4,
    "requires_cancel_confirmation": 4,
    "blocked": 4,
    "failed": 1,
    "planning_failed": 7,
    "simulated": 3,
    "interrupted": 5,
    "cancelled": 5,
    "question_not_found": 2,
    "question_mismatch": 2,
    "answer_conflict": 2,
    "answer_pending": 5,
    "answer_rejected": 1,
}


def _conversation_exit_code(outcome: str) -> int:
    return CONVERSATION_EXIT_CODES.get(outcome, 5)


def _exit_code(task: Task) -> int:
    return STATUS_EXIT_CODES[task.status]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zevorinth")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="runtime root containing data/ and artifacts/ (default: current directory)",
    )
    parser.add_argument(
        "--lease-seconds",
        type=float,
        default=60.0,
        help="execution lease duration for cross-process coordination",
    )
    parser.add_argument(
        "--max-execution-seconds",
        type=float,
        default=600.0,
        help="maximum bounded execution time with lease renewal",
    )
    parser.add_argument(
        "--lease-renewal-interval-seconds",
        type=float,
        default=None,
        help="lease renewal interval (default: one third of lease duration)",
    )
    parser.add_argument(
        "--busy-timeout-ms",
        type=int,
        default=10_000,
        help="maximum SQLite lock wait before returning exit code 6",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor",
        help="report environment and implemented capabilities",
    )
    doctor.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    conversation = subparsers.add_parser(
        "conversation",
        help="create conversations, append idempotent messages, and read history",
    )
    conversation_sub = conversation.add_subparsers(
        dest="conversation_command",
        required=True,
    )
    create_conversation = conversation_sub.add_parser("create")
    create_conversation.add_argument("--conversation-id", default=None)
    create_conversation.add_argument("--metadata", default=None)
    create_conversation.add_argument("--json", action="store_true")
    send_message = conversation_sub.add_parser("send")
    send_message.add_argument("conversation_id")
    send_message.add_argument("--content", required=True)
    send_message.add_argument("--client-message-id", required=True)
    send_message.add_argument("--metadata", default=None)
    send_message.add_argument("--json", action="store_true")
    history = conversation_sub.add_parser("history")
    history.add_argument("conversation_id")
    history.add_argument("--limit", type=int, default=50)
    history.add_argument("--json", action="store_true")
    context = conversation_sub.add_parser("context")
    context.add_argument("conversation_id")
    context.add_argument("--summary", default=None)
    context.add_argument("--json", action="store_true")
    process = conversation_sub.add_parser("process")
    process.add_argument("conversation_id")
    process.add_argument("--content", required=True)
    process.add_argument("--client-message-id", required=True)
    process.add_argument("--answer-to", default=None)
    process.add_argument("--operation", default=None)
    process.add_argument("--artifact-path", default=None)
    process.add_argument("--task-content", default=None)
    process.add_argument("--json", action="store_true")
    recover = conversation_sub.add_parser("recover")
    recover.add_argument("task_id")
    recover.add_argument("--json", action="store_true")

    evaluation = subparsers.add_parser(
        "eval-planner",
        help="run scripted offline or explicitly selected online planner evaluation",
    )
    mode = evaluation.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--online", action="store_true")
    evaluation.add_argument(
        "--limit",
        type=int,
        default=None,
        help="maximum online cases (default: 5)",
    )
    evaluation.add_argument(
        "--pilot",
        choices=["phase-2.5", "phase-2.7-retest"],
        default=None,
        help="use a versioned online pilot case list",
    )
    evaluation.add_argument(
        "--case-id",
        action="append",
        default=None,
        help="explicit online case ID; may be repeated (maximum 5)",
    )
    evaluation.add_argument(
        "--preflight",
        action="store_true",
        help="show online plan and configuration without sending API requests",
    )
    evaluation.add_argument(
        "--output",
        type=Path,
        default=None,
        help="persistent JSON report path for an approved online run",
    )
    evaluation.add_argument("--json", action="store_true")

    safety_gate = subparsers.add_parser(
        "eval-safety-gate",
        help="run the versioned offline planner and local-boundary gate",
    )
    safety_gate.add_argument(
        "--offline",
        action="store_true",
        required=True,
    )
    safety_gate.add_argument("--output", type=Path, required=True)
    safety_gate.add_argument("--json", action="store_true")

    planner_quality = subparsers.add_parser(
        "eval-planner-quality",
        help="run the planner-only quality contract offline or by explicit live authorization",
    )
    quality_mode = planner_quality.add_mutually_exclusive_group(required=True)
    quality_mode.add_argument("--offline", action="store_true")
    quality_mode.add_argument("--online", action="store_true")
    planner_quality.add_argument(
        "--preflight",
        action="store_true",
        help="validate the fixed live-run contract without creating a client",
    )
    planner_quality.add_argument(
        "--authorize-live",
        action="store_true",
        help="explicitly authorize one bounded planner-only model run",
    )
    planner_quality.add_argument("--output", type=Path, default=None)
    planner_quality.add_argument("--json", action="store_true")

    product_evaluation = subparsers.add_parser(
        "eval-product",
        help="run isolated real-task acceptance for the current local V1 slices",
    )
    product_evaluation.add_argument("--output", type=Path, required=True)
    product_evaluation.add_argument("--json", action="store_true")

    workbench = subparsers.add_parser(
        "workbench",
        help="serve the loopback-only read-only local workbench",
    )
    workbench.add_argument("--port", type=int, default=8765)

    control_center = subparsers.add_parser(
        "control-center",
        help="serve the loopback-only workbench with confirmed lightweight actions",
    )
    control_center.add_argument("--port", type=int, default=8766)

    authorize_host_test = subparsers.add_parser(
        "authorize-host-test",
        help=(
            "issue one single-use grant for host test execution"
            " (interactive human action)"
        ),
    )
    authorize_host_test.add_argument("--workspace", required=True)
    authorize_host_test.add_argument("--target", required=True)
    authorize_host_test.add_argument(
        "--ttl-minutes", type=int, default=30,
    )
    authorize_host_test.add_argument(
        "--issuer", default=None,
        help="operator label recorded in the grant and the audit log",
    )
    authorize_host_test.add_argument("--json", action="store_true")

    revoke_host_test = subparsers.add_parser(
        "revoke-host-test",
        help="revoke a host test grant so it can never be consumed",
    )
    revoke_host_test.add_argument("grant_id")
    revoke_host_test.add_argument("--issuer", default=None)
    revoke_host_test.add_argument("--json", action="store_true")

    host_grants = subparsers.add_parser(
        "host-grants",
        help="list host test grants and their lifecycle state",
    )
    host_grants.add_argument("--json", action="store_true")

    run = subparsers.add_parser(
        "run",
        help="submit and execute one structured artifact task",
    )
    run.add_argument("--goal", default=None)
    run.add_argument("--operation", default=WRITE_TEXT_ARTIFACT)
    run.add_argument("--artifact-path", default=None)
    run.add_argument("--content", default=None)
    run.add_argument("--conversation-id", default=None)
    run.add_argument("--turn-id", default=None)
    run.add_argument(
        "--planner",
        choices=["deterministic", "llm"],
        default=None,
        help="planning implementation (default: ZEVORINTH_PLANNER or deterministic)",
    )
    run.add_argument(
        "--simulate",
        action="store_true",
        help="use an explicit simulation writer; creates no file",
    )
    run.add_argument("--json", action="store_true")

    test_run = subparsers.add_parser(
        "test",
        help="run one approved local test target in a registered workspace",
    )
    test_run.add_argument("--workspace", required=True)
    test_run.add_argument("--target", required=True)
    test_run.add_argument(
        "--runner", default="unittest", choices=["unittest"]
    )
    test_run.add_argument("--timeout", type=float, default=10.0)
    test_run.add_argument("--max-output", type=int, default=8192)
    test_run.add_argument(
        "--backend", choices=["container", "host"], default=None
    )
    test_run.add_argument("--evidence-id", default=None)
    test_run.add_argument("--image", default=None)
    test_run.add_argument(
        "--allow-host",
        action="store_true",
        help=(
            "select the non-isolated host backend; requires a single-use"
            " grant from `authorize-host-test`"
        ),
    )
    test_run.add_argument(
        "--host-authorization",
        default=None,
        help=(
            "single-use host execution grant token issued by"
            " `zevorinth authorize-host-test`"
        ),
    )
    test_run.add_argument("--json", action="store_true")

    cleanup_cmd = subparsers.add_parser(
        "cleanup",
        help="remove one file this task provably created (opt-in)",
    )
    cleanup_cmd.add_argument("task_id")
    cleanup_cmd.add_argument("--path", required=True)
    cleanup_cmd.add_argument(
        "--confirm",
        action="store_true",
        help="explicitly authorize this deletion",
    )
    cleanup_cmd.add_argument("--json", action="store_true")

    verify_iso = subparsers.add_parser(
        "verify-isolation",
        help="probe the container boundary and store isolation evidence",
    )
    verify_iso.add_argument("--workspace", required=True)
    verify_iso.add_argument("--target", required=True)
    verify_iso.add_argument("--image", required=True)
    verify_iso.add_argument("--request-id", required=True)
    verify_iso.add_argument("--evidence-id", required=True)
    verify_iso.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status", help="show a persisted task")
    status.add_argument("task_id")
    status.add_argument("--json", action="store_true")

    task_audit = subparsers.add_parser(
        "task-audit",
        help="read-only lifecycle consistency audit of a persisted task",
    )
    task_audit.add_argument("task_id")
    task_audit.add_argument("--checked-at", default=None)
    task_audit.add_argument("--json", action="store_true")

    recover_prepublish = subparsers.add_parser(
        "recover-prepublish",
        help="retry one verified pre-publication publish-lock timeout",
    )
    recover_prepublish.add_argument("task_id")
    recover_prepublish.add_argument(
        "--confirm",
        action="store_true",
        help="explicitly authorize this bounded recovery",
    )
    recover_prepublish.add_argument("--json", action="store_true")

    recovery_audit = subparsers.add_parser(
        "recovery-audit",
        help="read-only audit of controlled recovery evidence",
    )
    recovery_audit.add_argument("approval_id")
    recovery_audit.add_argument("--workspace", type=Path, required=True)
    recovery_audit.add_argument("--checked-at", default=None)
    recovery_audit.add_argument("--json", action="store_true")

    events = subparsers.add_parser("events", help="show ordered persisted events")
    events.add_argument("task_id")
    events.add_argument("--json", action="store_true")

    cleanup = subparsers.add_parser(
        "cleanup-staging",
        help="remove provably inactive staging files",
    )
    cleanup.add_argument("--json", action="store_true")

    cancel = subparsers.add_parser(
        "cancel",
        help="cancel a task that has no active execution lease",
    )
    cancel.add_argument("task_id")
    cancel.add_argument("--json", action="store_true")

    request_cancel = subparsers.add_parser(
        "request-cancel",
        help="persist a cooperative cancellation request",
    )
    request_cancel.add_argument("task_id")
    request_cancel.add_argument("--source", required=True)
    request_cancel.add_argument("--reason", default=None)
    request_cancel.add_argument("--json", action="store_true")

    revise = subparsers.add_parser(
        "revise",
        help="apply a confirmed plan revision without executing it",
    )
    revise.add_argument("task_id")
    revise.add_argument("--expected-plan-version", type=int, required=True)
    revise.add_argument("--source-id", required=True)
    revise.add_argument("--confirm", action="store_true")
    revise.add_argument("--goal", default=None)
    revise.add_argument("--operation", default=None)
    revise.add_argument("--artifact-path", default=None)
    revise.add_argument("--content", default=None)
    revise.add_argument("--json", action="store_true")

    resume = subparsers.add_parser(
        "resume",
        help="resume a clarification, blocked task, or interrupted task",
    )
    resume.add_argument("task_id")
    resume.add_argument("--goal", default=None)
    resume.add_argument("--operation", default=None)
    resume.add_argument("--artifact-path", default=None)
    resume.add_argument("--content", default=None)
    resume.add_argument(
        "--planner",
        choices=["deterministic", "llm"],
        default=None,
        help="planner used if interrupted planning must be retried",
    )
    resume.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        report = doctor_report(args.home)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            _print_human(report)
        return 0
    if args.command == "workbench":
        try:
            serve_workbench(
                _project_root(args.home),
                port=args.port,
            )
        except ValueError as exc:
            print(f"workbench configuration error: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(
                f"workbench server error: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 6
        except KeyboardInterrupt:
            return 0
        return 0
    if args.command == "control-center":
        try:
            serve_workbench(
                _project_root(args.home),
                port=args.port,
                control_enabled=True,
            )
        except ValueError as exc:
            print(f"control center configuration error: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(
                "control center server error: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 6
        except KeyboardInterrupt:
            return 0
        return 0
    if args.command in {
        "authorize-host-test",
        "revoke-host-test",
        "host-grants",
    }:
        from .host_authorization import (
            HostAuthorizationError,
            HostGrantStore,
            default_issuer,
            grant_root_permissions,
            require_interactive_issuer,
        )

        store = HostGrantStore()
        issuer = getattr(args, "issuer", None) or default_issuer()
        if args.command == "host-grants":
            payload = {
                "storage": grant_root_permissions(
                    store, project_root=_project_root(args.home)
                ),
                "grants": store.list_grants(),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
                return 0
            storage = payload["storage"]
            print(f"grant store: {storage['root']}")
            if not storage.get("exists"):
                print("  (no grants have been issued yet)")
                return 0
            print(f"  mode: {storage.get('mode')}")
            for grant in payload["grants"]:
                print(
                    f"  {grant['grant_id']}  {grant['status']:9s}"
                    f"  {grant['workspace']}/{grant['target']}"
                    f"  expires {grant['expires_at']}"
                    f"  issuer {grant['issuer']}"
                )
            return 0
        if args.command == "revoke-host-test":
            try:
                revoked = store.revoke(args.grant_id, issuer=issuer)
            except HostAuthorizationError as exc:
                print(
                    f"host authorization refused: {exc.code}: {exc.message}",
                    file=sys.stderr,
                )
                return 7
            if args.json:
                print(json.dumps(revoked, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    f"revoked grant {revoked['grant_id']}"
                    f" (status={revoked['status']}"
                    f", revoked_at={revoked['revoked_at']})"
                )
            return 0
        try:
            require_interactive_issuer()
            issued = store.issue(
                workspace=args.workspace,
                target=args.target,
                issuer=issuer,
                ttl_minutes=args.ttl_minutes,
            )
        except HostAuthorizationError as exc:
            print(
                f"host authorization refused: {exc.code}: {exc.message}",
                file=sys.stderr,
            )
            return 7
        if args.json:
            print(json.dumps(issued, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        grant = issued["grant"]
        print("host execution grant issued (single use)")
        print(f"  grant_id : {grant['grant_id']}")
        print(f"  workspace: {grant['workspace']}")
        print(f"  target   : {grant['target']}")
        print(f"  issuer   : {grant['issuer']}")
        print(f"  expires  : {grant['expires_at']}")
        print(f"  token    : {issued['token']}")
        print(
            "  pass it as the run_tests parameter host_authorization;"
            " it is consumed once and never restored"
        )
        return 0
    if args.command == "recovery-audit":
        report = audit_controlled_recovery(
            RecoveryApprovalStore(_project_root(args.home)),
            args.approval_id,
            args.workspace,
            now=args.checked_at,
        )
        if args.json:
            print(
                json.dumps(
                    report, ensure_ascii=False, indent=2, sort_keys=True
                )
            )
        else:
            assurance = report.get("assurance") or {}
            print(
                f"Recovery audit: {report['status']} "
                f"({report.get('reason', 'no additional detail')})"
            )
            print(
                f"Assurance: {report['assurance_level']} "
                f"({assurance.get('label', 'unknown')})"
            )
            print(f"Authenticity: {report['authenticity']}")
            print(f"Trust root: {report['trust_root']}")
            print(
                "Security certification: "
                f"{report['security_certification']}"
            )
            print(f"Manual review: {report['manual_review']}")
            print(f"Safe to retry: {report['safe_to_retry']}")
        return RECOVERY_AUDIT_EXIT_CODES.get(report["status"], 5)
    if args.command == "conversation":
        try:
            service = ConversationService(_project_root(args.home))
            if args.conversation_command == "create":
                conversation = service.create_conversation(
                    conversation_id=args.conversation_id,
                    metadata=_parse_json_object(args.metadata),
                )
                _print_conversation(conversation, args.json)
                return 0
            if args.conversation_command == "send":
                turn, message = service.append_user_message(
                    args.conversation_id,
                    args.content,
                    client_message_id=args.client_message_id,
                    metadata=_parse_json_object(args.metadata),
                )
                if args.json:
                    print(
                        json.dumps(
                            {
                                "turn": turn.to_dict(),
                                "message": message.to_dict(),
                            },
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    print(f"Turn: {turn.turn_id} (index={turn.turn_index})")
                    print(f"Message: {message.message_id} ({message.byte_count} bytes)")
                return 0
            if args.conversation_command == "process":
                result = service.process_message(
                    args.conversation_id,
                    args.content,
                    client_message_id=args.client_message_id,
                    answer_to=args.answer_to,
                    operation=args.operation,
                    artifact_path=args.artifact_path,
                    task_content=args.task_content,
                )
                if args.json:
                    print(
                        json.dumps(
                            result.to_dict(),
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    print(f"Outcome: {result.outcome}")
                    print(f"Intent: {result.intent.kind.value}")
                    if result.task:
                        print(f"Task: {result.task.task_id}")
                        print(f"Task status: {result.task.status.value}")
                    for question in result.clarifications:
                        print(
                            f"Question: {question.question_id} "
                            f"[{question.field_name}] {question.question_text}"
                        )
                return _conversation_exit_code(result.outcome)
            if args.conversation_command == "recover":
                task = service.recover_task(args.task_id)
                _print_task(task, args.json)
                return _exit_code(task)
            if args.conversation_command == "context":
                context_report = service.context(
                    args.conversation_id,
                    summary=args.summary,
                )
                if args.json:
                    print(
                        json.dumps(
                            context_report.to_dict(),
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    print(f"Conversation: {context_report.conversation_id}")
                    print(f"Version: {context_report.version}")
                    print(f"Goal: {context_report.goal or 'none'}")
                    print(f"Constraints: {len(context_report.constraints)}")
                    print(
                        "Confirmed decisions: "
                        f"{len(context_report.confirmed_decisions)}"
                    )
                    print(
                        "Pending steps: "
                        f"{len(context_report.pending_steps)}"
                    )
                    print(
                        "Unresolved questions: "
                        f"{len(context_report.unresolved_questions)}"
                    )
                    if context_report.summary:
                        print(
                            "Summary (derived, non-authoritative): "
                            f"{context_report.summary}"
                        )
                return 0
            messages = service.history(
                args.conversation_id,
                limit=args.limit,
            )
            _print_history(messages, args.json)
            return 0
        except ConversationNotFound:
            print(f"conversation not found: {args.conversation_id}", file=sys.stderr)
            return 2
        except TaskNotFound:
            print(f"task not found: {args.task_id}", file=sys.stderr)
            return 2
        except PlanRevisionConflict as exc:
            print(f"plan revision conflict: {exc}", file=sys.stderr)
            return 2
        except MessageConflict as exc:
            print(f"message conflict: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"invalid conversation request: {exc}", file=sys.stderr)
            return 2
        except EventStoreError as exc:
            print(f"storage error: {exc}", file=sys.stderr)
            return 6
    if args.command == "eval-planner":
        try:
            if args.offline and any(
                (args.pilot, args.case_id, args.preflight, args.output)
            ):
                raise EvaluationConfigurationError(
                    "offline evaluation does not accept online selection or output options"
                )
            if args.online and not any((args.pilot, args.case_id)):
                raise EvaluationConfigurationError(
                    "online evaluation requires --pilot or --case-id"
                )
            if args.online and args.output is None:
                raise EvaluationConfigurationError(
                    "online evaluation requires an explicit --output path"
                )
            output_path = args.output
            if args.preflight:
                if not args.online:
                    raise EvaluationConfigurationError(
                        "--preflight requires --online"
                    )
                if args.limit is not None:
                    raise EvaluationConfigurationError(
                        "--preflight does not accept --limit"
                    )
                pilot_path = None
                if args.pilot:
                    pilot_path = (
                        DEFAULT_RETEST_PILOT
                        if args.pilot == "phase-2.7-retest"
                        else DEFAULT_PILOT
                    )
                report = online_preflight(
                    pilot_path=pilot_path,
                    case_ids=args.case_id,
                    output_path=output_path,
                )
                if args.json:
                    print(
                        json.dumps(
                            report,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    print(
                        f"Online preflight: {report['pilot_version']} "
                        f"({len(report['selected_cases'])} cases)"
                    )
                    for case in report["selected_cases"]:
                        print(
                            f"  {case['case_id']} [{case['category']}] "
                            f"expected={case['expected_classification']}"
                        )
                    configuration = report["configuration"]
                    boundaries = report["request_boundaries"]
                    print(
                        "Configuration: "
                        f"ready={report['ready_for_approval']} "
                        f"model={configuration.get('model') or 'not configured'} "
                        f"api_key_configured={configuration.get('api_key_configured')} "
                        f"sdk_version={configuration.get('sdk_version')}"
                    )
                    print(
                        "Boundaries: "
                        f"max_requests={boundaries['maximum_requests']} "
                        f"retries={boundaries['retries']} "
                        f"timeout_seconds={boundaries['timeout_seconds_per_case']} "
                        f"max_output_tokens={boundaries['max_output_tokens_per_case']}"
                    )
                    print(
                        "Cost: unknown until a verified model price table is available"
                    )
                    print(
                        "Result path: "
                        f"{report['file_effects']['persistent_result_path']}"
                    )
                    print("Network request sent by this preflight: no")
                return 0 if report["ready_for_approval"] else 7
            if args.online and output_path.exists():
                raise EvaluationConfigurationError(
                    "online evaluation refuses to overwrite an existing result: "
                    f"{output_path}"
                )
            report = run_evaluation(
                online=args.online,
                limit=args.limit if args.online else None,
                case_ids=args.case_id,
                pilot_path=(
                    DEFAULT_RETEST_PILOT
                    if args.pilot == "phase-2.7-retest"
                    else DEFAULT_PILOT if args.pilot else None
                ),
            )
        except EvaluationConfigurationError as exc:
            print(f"evaluation configuration error: {exc}", file=sys.stderr)
            return 7
        if args.online:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            report["report_path"] = str(output_path.resolve())
            output_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            metrics = report["metrics"]
            assert isinstance(metrics, dict)
            print(
                f"Planner evaluation: {report['mode']} "
                f"({report['passed']}/{report['case_count']} cases passed)"
            )
            for name in (
                "candidate_received_count",
                "candidate_validation_pass_rate",
                "classification_accuracy",
                "status_accuracy",
                "unsafe_side_effect_block_rate",
                "unsafe_classification_accuracy",
                "planning_failure_rate",
                "planned_case_end_to_end_success_rate",
                "expected_non_executable_case_share",
                "average_latency_ms",
                "model_call_attempts",
                "real_api_calls",
                "scripted_client_calls",
                "total_tokens",
            ):
                print(f"{name}: {metrics[name]}")
            baseline = report.get("deterministic_baseline")
            if isinstance(baseline, dict):
                baseline_metrics = baseline.get("metrics", {})
                if isinstance(baseline_metrics, dict):
                    print(
                        "Deterministic baseline: "
                        f"{baseline.get('passed')}/{baseline.get('case_count')} cases, "
                        f"classification_accuracy="
                        f"{baseline_metrics.get('classification_accuracy')}, "
                        f"model_call_attempts="
                        f"{baseline_metrics.get('model_call_attempts')}"
                    )
        return 0 if report["all_passed"] else 1
    if args.command == "eval-safety-gate":
        try:
            if args.output.exists():
                raise SafetyGateConfigurationError(
                    "safety gate report path already exists"
                )
            report = run_safety_gate()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(
                    report,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except SafetyGateConfigurationError as exc:
            print(f"safety gate configuration error: {exc}", file=sys.stderr)
            return 7
        if args.json:
            print(
                json.dumps(
                    report,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            metrics = report["metrics"]
            print(
                f"Safety gate: {report['status']} "
                f"({report['case_count']} cases)"
            )
            print(
                f"classification_accuracy="
                f"{metrics['classification_accuracy']} "
                f"status_accuracy={metrics['status_accuracy']} "
                f"duplicate_side_effects={metrics['duplicate_side_effects']}"
            )
        return 0 if report["all_passed"] else 1
    if args.command == "eval-planner-quality":
        try:
            if args.offline and (args.preflight or args.authorize_live):
                raise EvaluationConfigurationError(
                    "offline planner quality does not accept live options"
                )
            if args.preflight and not args.online:
                raise EvaluationConfigurationError(
                    "--preflight requires --online"
                )
            if args.preflight and args.authorize_live:
                raise EvaluationConfigurationError(
                    "--preflight does not accept --authorize-live"
                )
            if args.online and args.output is None:
                raise EvaluationConfigurationError(
                    "online planner quality requires an explicit --output path"
                )
            if args.online and args.preflight:
                report = planner_quality_online_preflight(args.output)
                if args.json:
                    print(
                        json.dumps(
                            report,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    configuration = report["configuration"]
                    boundaries = report["request_boundaries"]
                    print(
                        "Planner quality preflight: "
                        f"{'READY' if report['ready_for_authorization'] else 'NOT READY'}"
                    )
                    print(
                        f"model={configuration.get('model') or 'not configured'} "
                        "max_requests="
                        f"{boundaries['maximum_requests']} "
                        f"retries={boundaries['retries']} "
                        f"timeout_seconds={boundaries['timeout_seconds_per_case']} "
                        f"max_output_tokens={boundaries['max_output_tokens_per_case']}"
                    )
                    applicability = report["applicability_contract"]
                    passed_checks = sum(
                        check["passed"] for check in report["readiness_checks"]
                    )
                    print(
                        "Applicability contract: "
                        f"{applicability['version']} "
                        f"strict={applicability['live_applicable_count']} "
                        f"exploratory={applicability['live_exploratory_count']} "
                        f"scripted_only={applicability['scripted_only_count']} "
                        f"checks={passed_checks}/"
                        f"{len(report['readiness_checks'])}"
                    )
                    print(
                        "Network request sent: no; task/tool/container execution: no"
                    )
                return (
                    0 if report["ready_for_authorization"] else 7
                )
            if args.online and not args.authorize_live:
                raise EvaluationConfigurationError(
                    "online planner quality requires --authorize-live"
                )
            if args.output is not None and args.output.exists():
                print(
                    "planner quality evaluation refuses to overwrite "
                    f"an existing report: {args.output}",
                    file=sys.stderr,
                )
                return 7
            report = (
                run_live_planner_quality(authorized=True)
                if args.online
                else run_planner_quality()
            )
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                report["report_path"] = str(args.output.resolve())
                args.output.write_text(
                    json.dumps(
                        report,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            if args.json:
                print(
                    json.dumps(
                        report,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                metrics = report["metrics"]
                print(
                    "Planner quality: "
                    f"{'PASS' if report['all_passed'] else 'FAIL'} "
                    f"({report['passed']}/{report['case_count']})"
                )
                print(
                    f"classification_accuracy="
                    f"{metrics['classification_accuracy']} "
                    f"operation_accuracy={metrics['operation_accuracy']} "
                    f"parameter_accuracy={metrics['parameter_accuracy']} "
                    "dangerous_candidate_proposed_rate="
                    f"{metrics['dangerous_candidate_proposed_rate']} "
                    "dangerous_candidate_identification_rate="
                    f"{metrics['dangerous_candidate_identification_rate']} "
                    "dangerous_candidate_accepted_rate="
                    f"{metrics['dangerous_candidate_accepted_rate']}"
                )
                print(
                    f"Source: {report['mode']} "
                    f"model={report.get('model') or 'none'} "
                    "live_model_quality_measured="
                    f"{str(report['live_model_quality_measured']).lower()}"
                )
                applicability = report.get("live_applicability")
                if applicability:
                    strict = applicability["metrics"]
                    print(
                        "Live applicability: "
                        f"strict={strict['strict_passed']}/"
                        f"{strict['strict_case_count']} "
                        f"unsafe_candidates={strict['unsafe_candidate_count']} "
                        "downstream_blocked="
                        f"{strict['downstream_blocked_count']}"
                    )
                intent = report.get("intent_fidelity_summary")
                if intent:
                    print(
                        "Intent fidelity: "
                        f"{intent['passed']}/"
                        f"{intent['applicable_case_count']} "
                        f"status_counts={intent['status_counts']}"
                    )
            return 0 if report["all_passed"] else 1
        except EvaluationConfigurationError as exc:
            print(
                f"planner quality configuration error: {exc}",
                file=sys.stderr,
            )
            return 7

    if args.command == "eval-product":
        try:
            report = write_product_acceptance(args.output)
        except Exception as exc:
            print(
                f"product acceptance error: {exc}",
                file=sys.stderr,
            )
            return 2
        if args.json:
            print(
                json.dumps(
                    report,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            metrics = report["metrics"]
            print(
                f"Product acceptance: "
                f"{'PASS' if report['all_passed'] else 'FAIL'} "
                f"({metrics['passed']}/"
                f"{metrics['attempted_positive_scenarios']}) "
                f"false_success={metrics['false_success_count']} "
                f"interventions={metrics['interventions']} "
                f"recovered={metrics['recovered_tasks']}"
            )
        return 0 if report["all_passed"] else 1

    try:
        service = _service(args)
        if args.command == "run":
            task = service.submit(
                _request_from_args(args),
                conversation_id=args.conversation_id,
                turn_id=args.turn_id,
            )
            _print_task(task, args.json)
            return _exit_code(task)
        if args.command == "test":
            task = service.submit(
                TaskRequest(
                    goal=f"run {args.runner} target {args.target}",
                    operation="run_tests",
                    parameters={
                        "runner": args.runner,
                        "target": args.target,
                        "workspace": args.workspace,
                        "timeout_seconds": args.timeout,
                        "max_output_bytes": args.max_output,
                        **(
                            {
                                "execution_backend": (
                                    args.backend
                                    or ("host" if args.allow_host else None)
                                )
                            }
                            if (args.backend or args.allow_host)
                            else {}
                        ),
                        **(
                            {"evidence_id": args.evidence_id}
                            if args.evidence_id
                            else {}
                        ),
                        **({"image": args.image} if args.image else {}),
                        **(
                            {"host_authorization": args.host_authorization}
                            if args.host_authorization
                            else {}
                        ),
                    },
                )
            )
            _print_task(task, args.json)
            return _exit_code(task)
        if args.command == "status":
            task = service.require_task(args.task_id)
            audit = service.audit_lifecycle(args.task_id)
            if args.json:
                print(
                    json.dumps(
                        {**task.to_dict(), "lifecycle_audit": audit},
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                _print_task(task, False)
                print(
                    f"Lifecycle audit: "
                    f"{'ok' if audit['ok'] else 'unverified'} "
                    f"assurance={audit['assurance_level']} "
                    f"completeness="
                    f"{audit['event_completeness']['state']} "
                    f"authenticity={audit['authenticity']} "
                    f"security_certification="
                    f"{audit['security_certification']}"
                )
            return _exit_code(task) if audit["ok"] else 5
        if args.command == "task-audit":
            report = service.audit_lifecycle(
                args.task_id, now=args.checked_at
            )
            if args.json:
                print(
                    json.dumps(
                        report,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                recovery = report["task_recovery"]
                print(
                    f"Task audit: {report['status']} "
                    f"ok={report['ok']}"
                )
                print(
                    f"Assurance: {report['assurance_level']} "
                    f"({report['assurance']['label']})"
                )
                event_completeness = report["event_completeness"]
                print(
                    f"Event completeness: "
                    f"{event_completeness['state']} "
                    f"causality={event_completeness['causality']}"
                )
                print(f"Authenticity: {report['authenticity']}")
                print(f"Trust root: {report['trust_root']}")
                print(
                    f"Security certification: "
                    f"{report['security_certification']}"
                )
                print(
                    f"Manual review: "
                    f"{recovery['manual_intervention']}"
                )
                print(f"Safe to retry: {recovery['safe_to_retry']}")
                for issue in report["issues"]:
                    print(f"Issue: {issue['code']} {issue['detail']}")
            return 0 if report["ok"] else 5
        if args.command == "recover-prepublish":
            report = service.recover_prepublish_publish_lock_failure(
                args.task_id,
                authorized=args.confirm,
            )
            if args.json:
                print(
                    json.dumps(
                        report,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                replacement = report.get("replacement_task") or {}
                print(
                    f"Recovery: {report['status']} "
                    f"code={report['code']}"
                )
                print(f"Source task: {report['source_task_id']}")
                print(
                    f"Replacement task: "
                    f"{report.get('replacement_task_id') or '-'}"
                )
                print(
                    f"Replacement status: "
                    f"{replacement.get('status', '-')}"
                )
                print(
                    f"Assurance: {report['assurance_level']} "
                    f"authenticity={report['authenticity']} "
                    f"security_certification="
                    f"{report['security_certification']}"
                )
                print(f"Reason: {report['reason']}")
            if report["status"] in {"recovered", "already_recovered"}:
                return 0
            if report["status"] == "recovery_failed":
                return 1
            if report["status"] == "rejected":
                return 4
            return 5
        if args.command == "verify-isolation":
            from .evidence_provisioning import provision_evidence

            record = provision_evidence(
                service.project_root,
                image=args.image,
                workspace_name=args.workspace,
                target=args.target,
                request_id=args.request_id,
                evidence_id=args.evidence_id,
            )
            if args.json:
                print(json.dumps(record, indent=2, sort_keys=True))
            else:
                print(
                    f"{record['status']}: {record['evidence_id']} "
                    f"({record['image_digest']})"
                )
            return 0 if record["status"] in {"saved", "unchanged"} else 4
        if args.command == "cleanup":
            record = service.cleanup_workspace_file(
                args.task_id,
                args.path,
                enabled=True,
                authorized=bool(args.confirm),
            )
            if args.json:
                print(
                    json.dumps(
                        record, ensure_ascii=False, indent=2, sort_keys=True
                    )
                )
            else:
                print(
                    f"{record.get('status')}: {record.get('code')} "
                    f"{record.get('relative_path')}"
                )
            return 0 if record.get("status") == "completed" else 4
        if args.command == "events":
            events = service.events(args.task_id)
            if args.json:
                print(
                    json.dumps(
                        [event.to_dict() for event in events],
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                for event in events:
                    print(
                        f"{event.sequence:04d} {event.occurred_at} "
                        f"{event.event_type} {json.dumps(event.payload, ensure_ascii=False, sort_keys=True)}"
                    )
            return 0
        if args.command == "cancel":
            task = service.cancel(args.task_id)
            _print_task(task, args.json)
            return _exit_code(task)
        if args.command == "request-cancel":
            task = service.request_cancel(
                args.task_id,
                requested_by=args.source,
                reason=args.reason,
            )
            _print_task(task, args.json)
            return _exit_code(task)
        if args.command == "revise":
            patch = _patch_from_args(args)
            if patch is None:
                raise ValueError("revise requires at least one changed field")
            task = service.revise_plan(
                args.task_id,
                patch,
                source_id=args.source_id,
                expected_plan_version=args.expected_plan_version,
                confirmed=args.confirm,
            )
            _print_task(task, args.json)
            return _exit_code(task)
        if args.command == "cleanup-staging":
            report = service.cleanup_staging()
            if args.json:
                print(
                    json.dumps(
                        report,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(
                    "Staging cleanup: "
                    f"scanned={report['scanned']} "
                    f"deleted={report['deleted']} "
                    f"retained={report['retained']} "
                    f"errors={len(report['errors'])}"
                )
                for entry in report["entries"]:
                    print(
                        f"{entry.get('task_id')} "
                        f"{entry.get('file', '-')} "
                        f"{entry['action']} "
                        f"{entry.get('reason', '')}"
                    )
            return 1 if report["errors"] else 0
        if args.command == "resume":
            task = service.resume(args.task_id, _patch_from_args(args))
            _print_task(task, args.json)
            return _exit_code(task)
    except TaskNotFound:
        print(f"task not found: {args.task_id}", file=sys.stderr)
        return 2
    except ConversationNotFound:
        print(
            f"conversation not found: {getattr(args, 'conversation_id', None)}",
            file=sys.stderr,
        )
        return 2
    except ConversationTurnNotFound:
        print(f"conversation turn not found: {args.turn_id}", file=sys.stderr)
        return 2
    except (TaskLinkConflict, ActiveTaskConflict) as exc:
        print(f"task conversation conflict: {exc}", file=sys.stderr)
        return 2
    except PlanRevisionConflict as exc:
        print(f"plan revision conflict: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"invalid request: {exc}", file=sys.stderr)
        return 2
    except EventStoreError as exc:
        print(f"storage or execution-claim error: {exc}", file=sys.stderr)
        return 6
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    # Keep ``python -m zevorinth.cli`` consistent with the ``zevorinth``
    # console script and ``python -m zevorinth``.  This block only runs when
    # the module is executed directly, so imports and the entry point are
    # unaffected.
    raise SystemExit(main())
