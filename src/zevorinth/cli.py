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
from .conversation_service import ConversationService
from .event_store import (
    ActiveTaskConflict,
    ConversationNotFound,
    ConversationTurnNotFound,
    EventStoreError,
    MessageConflict,
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
from .service import TaskService


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
    TaskStatus.CANCELLED: 5,
}


def _project_root(override: Path | None = None) -> Path:
    return (override or Path.cwd()).resolve()


def doctor_report(project_root: Path | None = None) -> dict[str, object]:
    """Return a truthful, machine-readable snapshot of current capabilities."""
    root = _project_root(project_root)
    llm_config = LLMPlannerConfig.from_environment()
    sdk_status = openai_sdk_status()
    return {
        "schema_version": 3,
        "project": "Zevorinth",
        "version": __version__,
        "phase": 2,
        "hardening_phase": "1.5",
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
            "deterministic_planner": True,
            "model_provider": llm_config.validation_error() is None,
            "controlled_tool_execution": True,
            "result_verification": True,
            "durable_event_store": True,
            "resume_interrupted_task": True,
            "sqlite_execution_leases": True,
            "llm_planner_available": bool(sdk_status["compatible"]),
        },
        "task_types": [WRITE_TEXT_ARTIFACT],
        "llm_planner": {
            **llm_config.public_report(),
            "sdk": sdk_status,
        },
        "deployment_scope": {
            "single_host_shared_sqlite": True,
            "distributed_execution": False,
        },
        "notice": (
            "Phase 2 supports deterministic and explicitly selected LLM planning "
            "for one artifact-writing operation. LLM planning never grants tools "
            "or bypasses local validation, Policy, verification, or leases."
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
    print(f"Task types: {', '.join(report['task_types'])}")
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
    "status": 0,
    "already_processed": 0,
    "awaiting_clarification": 3,
    "needs_question_binding": 3,
    "ambiguous": 3,
    "requires_structured_update": 3,
    "requires_confirmation": 4,
    "active_task_conflict": 4,
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

    status = subparsers.add_parser("status", help="show a persisted task")
    status.add_argument("task_id")
    status.add_argument("--json", action="store_true")

    events = subparsers.add_parser("events", help="show ordered persisted events")
    events.add_argument("task_id")
    events.add_argument("--json", action="store_true")

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
        if args.command == "status":
            task = service.require_task(args.task_id)
            _print_task(task, args.json)
            return _exit_code(task)
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
    except ValueError as exc:
        print(f"invalid request: {exc}", file=sys.stderr)
        return 2
    except EventStoreError as exc:
        print(f"storage or execution-claim error: {exc}", file=sys.stderr)
        return 6
    raise AssertionError(f"unhandled command: {args.command}")
