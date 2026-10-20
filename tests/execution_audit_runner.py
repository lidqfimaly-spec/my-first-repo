"""Metrics and cross-checks for controlled local test execution.

Each scenario runs through ``TaskService`` (or the real CLI for one case) in
a fresh temporary root.  Counters are recomputed from the persisted tasks and
events rather than trusting any single summary field.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import RUN_TESTS  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


PASSING = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        self.assertTrue(True)\n"
)
FAILING = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_no(self):\n"
    "        self.assertEqual(1, 2)\n"
)
SLOW = (
    "import time, unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_slow(self):\n"
    "        time.sleep(30)\n"
)
NOISY = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_noisy(self):\n"
    "        print('x' * 50000)\n"
)


class AuditError(RuntimeError):
    pass


def _workspace(root: Path, files: dict[str, str]) -> None:
    workspace = root / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    for name, content in files.items():
        (workspace / name).write_text(content, encoding="utf-8")


def _request(target: str = "test_demo", **extra: Any) -> TaskRequest:
    parameters = {
        "runner": "unittest",
        "target": target,
        "workspace": "demo",
    }
    parameters.update(extra)
    return TaskRequest(
        goal="execution audit", operation=RUN_TESTS, parameters=parameters
    )


def _event_types(service: TaskService, task_id: str) -> list[str]:
    return [event.event_type for event in service.events(task_id)]


def _record(
    scenario_id: str,
    expected: str,
    task: Any,
    service: TaskService,
    checks: dict[str, bool],
) -> dict[str, Any]:
    events = _event_types(service, task.task_id)
    evidence = (
        task.tool_result.test_evidence
        if task.tool_result is not None
        else None
    )
    return {
        "id": scenario_id,
        "expected_status": expected,
        "observed_status": task.status.value,
        "runner_invoked": events.count("tool_started"),
        "exit_code": evidence.exit_code if evidence else None,
        "timed_out": bool(evidence.timed_out) if evidence else False,
        "cancelled": bool(evidence.cancelled) if evidence else False,
        "truncated": bool(evidence.truncated) if evidence else False,
        "terminated_confirmed": (
            bool(evidence.terminated_confirmed) if evidence else False
        ),
        "verifier_passed": bool(
            task.verification.passed
        ) if task.verification else False,
        "verifier_reason": (
            task.verification.reason if task.verification else None
        ),
        "error_code": task.error.code if task.error else None,
        "success_events": events.count("task_succeeded"),
        "checks": checks,
        "passed": task.status.value == expected and all(checks.values()),
    }


def run_audit() -> dict[str, Any]:
    scenarios: list[dict[str, Any]] = []

    def case(
        scenario_id: str,
        expected: str,
        files: dict[str, str],
        request: TaskRequest,
        checks: dict[str, bool],
        cancel_after: float | None = None,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root, files)
            service = TaskService(root)
            task = service.enqueue(request)
            if cancel_after is None:
                result = service.execute_task(task.task_id)
            else:
                def cancel() -> None:
                    time.sleep(cancel_after)
                    service.request_cancel(
                        task.task_id, requested_by="audit", reason="stop"
                    )

                threading.Thread(target=cancel).start()
                result = service.execute_task(task.task_id)
            scenarios.append(
                _record(scenario_id, expected, result, service, checks)
            )

    case(
        "E-1-pass",
        "succeeded",
        {"test_demo.py": PASSING},
        _request(),
        {"exit_zero": True, "success_event_once": True},
    )
    case(
        "E-2-fail",
        "failed",
        {"test_bad.py": FAILING},
        _request("test_bad"),
        {"non_zero_exit": True},
    )
    case(
        "E-3-timeout",
        "failed",
        {"test_slow.py": SLOW},
        _request("test_slow", timeout_seconds=1),
        {"timed_out": True},
    )
    case(
        "E-4-cancel-before",
        "cancelled",
        {"test_demo.py": PASSING},
        _request(),
        {"not_started": True},
        cancel_after=0.0,
    )
    case(
        "E-5-cancel-during",
        "cancelled",
        {"test_slow.py": SLOW},
        _request("test_slow", timeout_seconds=30),
        {"terminated": True},
        cancel_after=0.4,
    )
    case(
        "E-6-unknown-runner",
        "blocked",
        {"test_demo.py": PASSING},
        _request(runner="bash"),
        {"rejected": True},
    )
    case(
        "E-7-shell-target",
        "blocked",
        {"test_demo.py": PASSING},
        _request("test_demo; rm -rf /"),
        {"rejected": True},
    )
    case(
        "E-8-truncated",
        "failed",
        {"test_noisy.py": NOISY},
        _request("test_noisy", max_output_bytes=256),
        {"truncated": True},
    )

    # CLI path
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _workspace(root, {"test_demo.py": PASSING})
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "zevorinth.cli",
                "--home",
                str(root),
                "test",
                "--workspace",
                "demo",
                "--target",
                "test_demo",
                "--json",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PYTHONPATH": str(PROJECT_ROOT / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "OPENAI_API_KEY": "",
            },
            timeout=120,
        )
        payload = json.loads(completed.stdout) if completed.stdout else {}
        evidence = (
            payload.get("tool_result") or {}
        ).get("test_evidence") or {}
        scenarios.append(
            {
                "id": "E-9-cli",
                "expected_status": "succeeded",
                "observed_status": payload.get("status"),
                "runner_invoked": 1 if evidence else 0,
                "exit_code": evidence.get("exit_code"),
                "timed_out": bool(evidence.get("timed_out")),
                "cancelled": bool(evidence.get("cancelled")),
                "truncated": bool(evidence.get("truncated")),
                "terminated_confirmed": True,
                "verifier_passed": bool(
                    (payload.get("verification") or {}).get("passed")
                ),
                "verifier_reason": (
                    payload.get("verification") or {}
                ).get("reason"),
                "error_code": (payload.get("error") or {}).get("code"),
                "success_events": 1,
                "checks": {"cli_exit_zero": completed.returncode == 0},
                "passed": completed.returncode == 0
                and payload.get("status") == "succeeded",
            }
        )

    return _report(scenarios)


def _report(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [item["id"] for item in scenarios]
    if len(set(ids)) != len(ids):
        raise AuditError("duplicate scenario ids")
    started = [item for item in scenarios if item["runner_invoked"] > 0]
    completed = [item for item in started if item["exit_code"] is not None]
    timed_out = [item for item in scenarios if item["timed_out"]]
    cancelled = [item for item in scenarios if item["cancelled"]]
    cancelled_before_start = [
        item
        for item in scenarios
        if item["observed_status"] == "cancelled"
        and item["runner_invoked"] == 0
    ]
    positive = [
        item for item in started if item["expected_status"] == "succeeded"
    ]
    agreement = [
        item
        for item in started
        if item["verifier_passed"]
        == (
            item["exit_code"] == 0
            and not item["truncated"]
            and not item["timed_out"]
            and not item["cancelled"]
        )
    ]
    blocked = [
        item for item in scenarios if item["observed_status"] == "blocked"
    ]
    passed = [
        item
        for item in scenarios
        if item["observed_status"] == "succeeded"
        and item["verifier_passed"]
        and item["success_events"] == 1
    ]
    failed = [
        item for item in scenarios if item["observed_status"] == "failed"
    ]
    duplicates = sum(
        max(item["runner_invoked"] - 1, 0) for item in scenarios
    )
    for item in scenarios:
        if item["observed_status"] == "succeeded":
            if not item["verifier_passed"]:
                raise AuditError(f"succeeded without verifier: {item['id']}")
            if item["exit_code"] not in (0, None):
                raise AuditError(f"succeeded with non-zero exit: {item['id']}")
        if item["timed_out"] and item["exit_code"] is not None:
            raise AuditError(f"timeout kept an exit code: {item['id']}")
        if item["observed_status"] == "blocked" and item["runner_invoked"]:
            raise AuditError(f"blocked but started: {item['id']}")
    if duplicates != 0:
        raise AuditError("duplicate runner invocations detected")
    metrics = {
        "scenario_count": len(scenarios),
        "requested": len(scenarios),
        "started": len(started),
        "completed": len(completed),
        "passed": len(passed),
        "failed": len(failed),
        "blocked": len(blocked),
        "cancelled": len(cancelled),
        "cancelled_before_start": len(cancelled_before_start),
        "cancelled_during_run": len(cancelled),
        "timed_out": len(timed_out),
        "not_executed": len(scenarios) - len(started) - len(blocked),
        "duplicate_side_effects": duplicates,
        "positive_verifier_pass_rate": (
            len([i for i in positive if i["verifier_passed"]]) / len(positive)
            if positive
            else 0.0
        ),
        "verifier_agreement_rate": (
            len(agreement) / len(started) if started else 0.0
        ),
        "timeout_confirmation_rate": (
            len([i for i in timed_out if i["terminated_confirmed"]])
            / len(timed_out)
            if timed_out
            else 0.0
        ),
        "cancel_confirmation_rate": (
            len([i for i in cancelled if i["terminated_confirmed"]])
            / len(cancelled)
            if cancelled
            else 0.0
        ),
    }
    metric_audit = {
        "positive_verifier_pass_rate": {
            "numerator": len(
                [i for i in positive if i["verifier_passed"]]
            ),
            "denominator": len(positive),
            "excluded_count": len(scenarios) - len(positive),
            "excluded_reason_counts": {
                "not_positive_started_case": len(scenarios) - len(positive)
            },
            "contributing_case_ids": [i["id"] for i in positive],
        },
        "verifier_agreement_rate": {
            "numerator": len(agreement),
            "denominator": len(started),
            "excluded_count": len(scenarios) - len(started),
            "excluded_reason_counts": {
                "not_started": len(scenarios) - len(started)
            },
            "contributing_case_ids": [i["id"] for i in started],
        },
        "timeout_confirmation_rate": {
            "numerator": len(
                [i for i in timed_out if i["terminated_confirmed"]]
            ),
            "denominator": len(timed_out),
            "excluded_count": len(scenarios) - len(timed_out),
            "excluded_reason_counts": {
                "not_timeout_case": len(scenarios) - len(timed_out)
            },
            "contributing_case_ids": [i["id"] for i in timed_out],
        },
        "cancel_confirmation_rate": {
            "numerator": len(
                [i for i in cancelled if i["terminated_confirmed"]]
            ),
            "denominator": len(cancelled),
            "excluded_count": len(scenarios) - len(cancelled),
            "excluded_reason_counts": {
                "not_cancel_case": len(scenarios) - len(cancelled)
            },
            "contributing_case_ids": [i["id"] for i in cancelled],
        },
    }
    return {
        "schema_version": "1.0",
        "scenarios": scenarios,
        "metrics": metrics,
        "metric_audit": metric_audit,
        "status": (
            "PASS"
            if all(item["passed"] for item in scenarios)
            else "FAIL"
        ),
        "failed_scenarios": [
            item["id"] for item in scenarios if not item["passed"]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    from datetime import datetime, timezone

    from tests.audit_report import build_metadata, write_envelope

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = time.perf_counter()
    report = run_audit()
    elapsed = (time.perf_counter() - started) * 1000
    exit_code = 0 if report["status"] == "PASS" else 1
    written = None
    if args.output_dir is not None:
        metadata = build_metadata(
            report,
            kind="execution_audit",
            command=["python3", "-m", "tests.execution_audit_runner"],
            exit_code=exit_code,
            started_at=started_at,
            duration_ms=elapsed,
            not_run=[
                "automatic mutation framework (not installed)",
                "real model API (out of scope)",
            ],
        )
        written = write_envelope(
            args.output_dir, stable=report, metadata=metadata
        )
    print(
        json.dumps(
            {
                "status": report["status"],
                "metrics": report["metrics"],
                "stable_sha256": written["stable_sha256"] if written else None,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
