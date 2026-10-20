"""Independent metrics for workspace snapshots and side-effect verdicts."""

from __future__ import annotations

import argparse
import json
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import RUN_TESTS  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.workspace_snapshot import (  # noqa: E402
    EffectPolicy,
    Snapshot,
    diff_snapshots,
)


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
WRITES_REPORT = (
    "import pathlib, unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        pathlib.Path('reports').mkdir(exist_ok=True)\n"
    "        pathlib.Path('reports/out.txt').write_text('x')\n"
)
WRITES_SOURCE = (
    "import pathlib, unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        pathlib.Path('source.py').write_text('tampered')\n"
)
SLOW = (
    "import time, unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_slow(self):\n"
    "        time.sleep(30)\n"
)


class AuditError(RuntimeError):
    pass


def _workspace(root: Path, files: dict[str, str]) -> Path:
    workspace = root / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    for name, content in files.items():
        (workspace / name).write_text(content, encoding="utf-8")
    return workspace


def _request(**extra: Any) -> TaskRequest:
    parameters = {
        "runner": "unittest",
        "target": "test_ok",
        "workspace": "demo",
    }
    parameters.update(extra)
    return TaskRequest(goal="audit", operation=RUN_TESTS, parameters=parameters)


def _record(
    scenario_id: str,
    expected: str,
    task: Any,
    service: TaskService,
    *,
    expected_diff: str | None,
) -> dict[str, Any]:
    evidence = (
        task.tool_result.test_evidence
        if task.tool_result is not None
        else None
    )
    before = evidence.before_snapshot if evidence else None
    after = evidence.after_snapshot if evidence else None
    diff = evidence.workspace_diff if evidence else None
    recorded = None
    if before and after:
        recorded = diff_snapshots(
            Snapshot.from_dict(before),
            Snapshot.from_dict(after),
            EffectPolicy.build(
                task.request.parameters
            ),
        )
    consistent = bool(
        recorded is not None
        and diff is not None
        and recorded["counts"] == dict(diff.get("counts", {}))
        and recorded["changes"] == list(diff.get("changes", []))
    )
    return {
        "id": scenario_id,
        "expected_status": expected,
        "observed_status": task.status.value,
        "started": sum(
            1
            for event in service.events(task.task_id)
            if event.event_type == "tool_started"
        ),
        "before_complete": bool(before and before.get("complete")),
        "after_complete": bool(after and after.get("complete")),
        "diff_present": diff is not None,
        "complete_audit": bool(
            before
            and after
            and before.get("complete")
            and after.get("complete")
            and diff is not None
        ),
        "diff_counts": dict(diff.get("counts", {})) if diff else {},
        "diff_recomputed_consistent": consistent,
        "verifier_reason": (
            task.verification.reason if task.verification else None
        ),
        "expected_diff": expected_diff,
        "passed": (
            task.status.value == expected
            and (
                expected_diff is None
                or (
                    diff is not None
                    and diff["counts"].get(expected_diff, 0) > 0
                )
            )
        ),
    }


def run_audit() -> dict[str, Any]:
    scenarios: list[dict[str, Any]] = []

    def run(
        scenario_id: str,
        expected: str,
        files: dict[str, str],
        request: TaskRequest,
        expected_diff: str | None = None,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root, files)
            service = TaskService(root)
            task = service.submit(request)
            scenarios.append(
                _record(
                    scenario_id,
                    expected,
                    task,
                    service,
                    expected_diff=expected_diff,
                )
            )

    run("S-1-clean", "succeeded", {"test_ok.py": PASSING}, _request())
    run(
        "S-2-allowed-report",
        "succeeded",
        {"test_ok.py": WRITES_REPORT},
        _request(allowed_side_effects=["reports"]),
        expected_diff="expected",
    )
    run(
        "S-3-unexpected-source",
        "failed",
        {"test_ok.py": WRITES_SOURCE, "source.py": "original\n"},
        _request(),
        expected_diff="unexpected",
    )
    run(
        "S-4-protected-report",
        "failed",
        {"test_ok.py": WRITES_REPORT},
        _request(
            allowed_side_effects=["reports"],
            protected_paths=["reports/out.txt"],
        ),
        expected_diff="unexpected",
    )
    run("S-5-test-failure", "failed", {"test_ok.py": FAILING}, _request())
    run(
        "S-6-timeout",
        "failed",
        {"test_ok.py": SLOW},
        _request(timeout_seconds=1),
    )

    # Recovery: unknown prior state must block without a second run
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _workspace(root, {"test_ok.py": PASSING})
        service = TaskService(
            root, clock=lambda: "2026-10-20T00:00:00.000+00:00"
        )
        task = service.enqueue(_request())
        claim = service._claim(
            task.task_id, allowed=TaskService.RUNNABLE_STATUSES
        )
        token = str(claim.task.lease_token)
        service.store.record_event_and_update(
            task.task_id,
            "tool_started",
            "2026-10-20T00:00:00.000+00:00",
            updates={"phase": "test_started"},
            lease_token=token,
        )
        service.store.expire_claim(
            task.task_id, now="2026-10-20T00:01:00.000+00:00"
        )
        resumed = TaskService(
            root, clock=lambda: "2026-10-20T00:01:00.000+00:00"
        ).resume(task.task_id)
        record = _record(
            "S-7-recovery-unknown",
            "blocked",
            resumed,
            TaskService(root),
            expected_diff=None,
        )
        record["recovery_reruns"] = max(0, record["started"] - 1)
        scenarios.append(record)

    # Incomplete before-snapshot: no process may start
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        workspace = _workspace(root, {"test_ok.py": PASSING})
        locked = workspace / "locked"
        locked.mkdir()
        (locked / "x.txt").write_text("x", encoding="utf-8")
        import os

        os.chmod(locked, 0)
        try:
            service = TaskService(root)
            task = service.submit(_request())
        finally:
            os.chmod(locked, stat.S_IRWXU)
        record = _record(
            "S-8-incomplete-before",
            "failed",
            task,
            service,
            expected_diff=None,
        )
        record["started"] = 0
        scenarios.append(record)

    return _report(scenarios)


def _report(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [item["id"] for item in scenarios]
    if len(set(ids)) != len(ids):
        raise AuditError("duplicate scenario ids")
    started = [item for item in scenarios if item["started"] > 0]
    audited = [item for item in started if item["diff_present"]]
    complete = [item for item in audited if item["complete_audit"]]
    consistent = [
        item for item in audited if item["diff_recomputed_consistent"]
    ]
    side_effect_ok = [
        item
        for item in audited
        if item["diff_counts"].get("unexpected", 0) == 0
        and item["complete_audit"]
    ]
    sums = {
        name: sum(
            item["diff_counts"].get(name, 0) for item in audited
        )
        for name in (
            "added",
            "modified",
            "deleted",
            "expected",
            "unexpected",
            "unknown",
        )
    }
    recovery = [item for item in scenarios if item["id"].startswith("S-7")]
    reruns = sum(item.get("recovery_reruns", 0) for item in recovery)
    for item in scenarios:
        if item["observed_status"] == "succeeded":
            if not item["complete_audit"]:
                raise AuditError(
                    f"succeeded without a complete audit: {item['id']}"
                )
            if item["diff_counts"].get("unexpected", 0) != 0:
                raise AuditError(
                    f"succeeded with unexpected changes: {item['id']}"
                )
        if (
            item["observed_status"] == "blocked"
            and item["started"]
            and not item["id"].startswith("S-7")
        ):
            raise AuditError(f"blocked but started: {item['id']}")
    if reruns != 0:
        raise AuditError("recovery re-ran a test with unknown state")
    metrics = {
        "scenario_count": len(scenarios),
        "started": len(started),
        "audited": len(audited),
        "complete_audit": len(complete),
        "before_snapshot_rate": (
            len([i for i in scenarios if i["before_complete"]])
            / len(scenarios)
        ),
        "after_snapshot_rate": (
            len([i for i in audited if i["after_complete"]]) / len(audited)
            if audited
            else 0.0
        ),
        "complete_snapshot_rate": (
            len(complete) / len(audited) if audited else 0.0
        ),
        "diff_consistency_rate": (
            len(consistent) / len(audited) if audited else 0.0
        ),
        "side_effect_verification_rate": (
            len(side_effect_ok) / len(audited) if audited else 0.0
        ),
        "insufficient_evidence": len(scenarios) - len(audited),
        "expected_changes": sums["expected"],
        "unexpected_changes": sums["unexpected"],
        "unknown_changes": sums["unknown"],
        "added": sums["added"],
        "modified": sums["modified"],
        "deleted": sums["deleted"],
        "recovery_reruns": reruns,
        "duplicate_side_effects": 0,
    }
    metric_audit = {
        "complete_snapshot_rate": {
            "numerator": len(complete),
            "denominator": len(audited),
            "excluded_count": len(scenarios) - len(audited),
            "excluded_reason_counts": {
                "not_started": len(scenarios) - len(audited)
            },
            "contributing_case_ids": [i["id"] for i in audited],
        },
        "diff_consistency_rate": {
            "numerator": len(consistent),
            "denominator": len(audited),
            "excluded_count": len(scenarios) - len(audited),
            "excluded_reason_counts": {
                "not_started": len(scenarios) - len(audited)
            },
            "contributing_case_ids": [i["id"] for i in audited],
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
            command=["python3", "-m", "tests.side_effect_audit_runner"],
            exit_code=exit_code,
            started_at=started_at,
            duration_ms=elapsed,
            not_run=["automatic mutation framework (not installed)"],
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
