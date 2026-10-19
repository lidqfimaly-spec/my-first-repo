"""Command-line interface for the bounded Phase 1 task runtime."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .artifact_tool import SimulatedArtifactWriter
from .event_store import EventStoreError, TaskNotFound
from .models import Task, TaskRequest, TaskStatus
from .planner import WRITE_TEXT_ARTIFACT
from .service import TaskService


STATUS_EXIT_CODES = {
    TaskStatus.SUCCEEDED: 0,
    TaskStatus.FAILED: 1,
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
    return {
        "schema_version": 2,
        "project": "Zevorinth",
        "version": __version__,
        "phase": 1,
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
            "model_provider": False,
            "controlled_tool_execution": True,
            "result_verification": True,
            "durable_event_store": True,
            "resume_interrupted_task": True,
            "sqlite_execution_leases": True,
        },
        "task_types": [WRITE_TEXT_ARTIFACT],
        "deployment_scope": {
            "single_host_shared_sqlite": True,
            "distributed_execution": False,
        },
        "notice": (
            "Phase 1.5 supports one deterministic artifact-writing task with "
            "single-host SQLite execution leases. Natural-language planning, "
            "LLM calls, and distributed execution are not implemented."
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
    print(f"Notice: {report['notice']}")


def _service(args: argparse.Namespace) -> TaskService:
    writer = SimulatedArtifactWriter() if getattr(args, "simulate", False) else None
    return TaskService(
        _project_root(args.home),
        writer=writer,
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
    ]
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

    run = subparsers.add_parser(
        "run",
        help="submit and execute one structured artifact task",
    )
    run.add_argument("--goal", default=None)
    run.add_argument("--operation", default=WRITE_TEXT_ARTIFACT)
    run.add_argument("--artifact-path", default=None)
    run.add_argument("--content", default=None)
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

    try:
        service = _service(args)
        if args.command == "run":
            task = service.submit(_request_from_args(args))
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
    except ValueError as exc:
        print(f"invalid request: {exc}", file=sys.stderr)
        return 2
    except EventStoreError as exc:
        print(f"storage or execution-claim error: {exc}", file=sys.stderr)
        return 6
    raise AssertionError(f"unhandled command: {args.command}")
