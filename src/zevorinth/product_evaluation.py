"""Reproducible local acceptance for the V1 product task classes."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from .artifact_tool import RealArtifactWriter
from .capabilities import CAPABILITY_REGISTRY
from .host_authorization import HostGrantStore
from .models import ExecutionMode, Task, TaskRequest, TaskStatus
from .planner import (
    EDIT_TEXT_FILE,
    MODIFY_TEXT_ARTIFACT,
    READ_TEXT_ARTIFACT,
    RESEARCH_REPORT,
    WRITE_TEXT_ARTIFACT,
)
from .service import TaskService


PRODUCT_ACCEPTANCE_SCHEMA = "zevorinth-product-acceptance-v1"
PRODUCT_ACCEPTANCE_MODE = "REAL_LOCAL_TEMP_ISOLATED"


class ProductAcceptanceError(RuntimeError):
    pass


class ChildRewriteGuardWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self, artifacts_root: Path, forbidden_task_id: str) -> None:
        self.inner = RealArtifactWriter(artifacts_root)
        self.forbidden_task_id = forbidden_task_id
        self.task_ids: list[str] = []

    def write(self, *, task_id: str, **kwargs: Any) -> Any:
        if task_id == self.forbidden_task_id:
            raise AssertionError("child side effect must not be repeated")
        self.task_ids.append(task_id)
        return self.inner.write(task_id=task_id, **kwargs)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_path(root: Path, task: Task) -> Path | None:
    if task.artifact is None:
        return None
    return root / "artifacts" / task.task_id / task.artifact.relative_path


def _artifact_verified(root: Path, task: Task) -> bool:
    if task.verification is None or not task.verification.passed:
        return False
    path = _artifact_path(root, task)
    if task.artifact is None:
        return True
    return bool(
        path is not None
        and path.is_file()
        and _sha256(path) == task.artifact.sha256
    )


def _multi_step_verified(root: Path, task: Task) -> bool:
    if task.verification is None or not task.verification.passed:
        return False
    if not task.step_results or any(
        item.status != "succeeded" for item in task.step_results
    ):
        return False
    for item in task.step_results:
        if item.artifact is None:
            continue
        if item.child_task_id is None:
            return False
        path = (
            root
            / "artifacts"
            / item.child_task_id
            / item.artifact.relative_path
        )
        if not path.is_file() or _sha256(path) != item.artifact.sha256:
            return False
    return True


def _record(
    *,
    scenario_id: str,
    task_class: str,
    title: str,
    expected_status: str,
    task: Task,
    checks: Mapping[str, bool],
    independent_verification: bool,
    intervention_count: int = 0,
    recovered: bool = False,
    negative_control: bool = False,
    details: Mapping[str, Any] | None = None,
    started: float,
) -> dict[str, Any]:
    observed = task.status.value
    false_success = observed == TaskStatus.SUCCEEDED.value and not (
        independent_verification
    )
    passed = (
        observed == expected_status
        and all(bool(value) for value in checks.values())
        and not false_success
    )
    return {
        "id": scenario_id,
        "task_class": task_class,
        "title": title,
        "expected_status": expected_status,
        "observed_status": observed,
        "task_id": task.task_id,
        "passed": passed,
        "checks": {key: bool(value) for key, value in checks.items()},
        "independent_verification": independent_verification,
        "false_success": false_success,
        "intervention_count": intervention_count,
        "recovered": recovered,
        "negative_control": negative_control,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        "details": dict(details or {}),
    }


def _metrics(scenarios: list[dict[str, Any]], elapsed_ms: float) -> dict[str, Any]:
    attempted = [item for item in scenarios if not item["negative_control"]]
    passed = [item for item in attempted if item["passed"]]
    by_class: dict[str, dict[str, int]] = {}
    for item in scenarios:
        bucket = by_class.setdefault(
            item["task_class"],
            {"total": 0, "passed": 0, "failed": 0},
        )
        bucket["total"] += 1
        if item["passed"]:
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1
    return {
        "total_scenarios": len(scenarios),
        "attempted_positive_scenarios": len(attempted),
        "passed": len(passed),
        "failed": len(attempted) - len(passed),
        "negative_controls": sum(
            bool(item["negative_control"]) for item in scenarios
        ),
        "negative_controls_passed": sum(
            bool(item["negative_control"] and item["passed"])
            for item in scenarios
        ),
        "success_rate": (
            len(passed) / len(attempted) if attempted else 0.0
        ),
        "false_success_count": sum(
            bool(item["false_success"]) for item in scenarios
        ),
        "interventions": sum(
            int(item["intervention_count"]) for item in scenarios
        ),
        "recovered_tasks": sum(bool(item["recovered"]) for item in scenarios),
        "elapsed_ms": round(elapsed_ms, 3),
        "by_task_class": by_class,
    }


def _run_file_task(root: Path) -> dict[str, Any]:
    started = time.monotonic()
    service = TaskService(root)
    created = service.submit(
        TaskRequest(
            goal="create a verified source report",
            operation=WRITE_TEXT_ARTIFACT,
            parameters={
                "artifact_path": "source.txt",
                "content": "initial report\n",
            },
        )
    )
    source_path = _artifact_path(root, created)
    read = service.submit(
        TaskRequest(
            goal="read the verified source report",
            operation=READ_TEXT_ARTIFACT,
            parameters={
                "source_task_id": created.task_id,
                "artifact_path": "source.txt",
                "expected_sha256": created.artifact.sha256,
            },
        )
    )
    modified = service.submit(
        TaskRequest(
            goal="append a verified conclusion",
            operation=MODIFY_TEXT_ARTIFACT,
            parameters={
                "source_task_id": created.task_id,
                "artifact_path": "source.txt",
                "output_path": "final.txt",
                "content": "verified conclusion\n",
                "mode": "append",
                "expected_sha256": created.artifact.sha256,
            },
        )
    )
    modified_path = _artifact_path(root, modified)
    read_evidence = (
        read.tool_result.read_evidence
        if read.tool_result is not None
        else None
    )
    checks = {
        "create_verified": _artifact_verified(root, created),
        "read_verified": read.status is TaskStatus.SUCCEEDED
        and read.verification is not None
        and read.verification.passed,
        "read_content_matches": bool(
            read_evidence is not None
            and read_evidence.content == "initial report\n"
        ),
        "modify_verified": _artifact_verified(root, modified),
        "modified_content_matches": bool(
            modified_path is not None
            and modified_path.is_file()
            and modified_path.read_text(encoding="utf-8")
            == "initial report\nverified conclusion\n"
        ),
        "source_not_overwritten": bool(
            source_path is not None
            and source_path.is_file()
            and source_path.read_text(encoding="utf-8")
            == "initial report\n"
        ),
        "lineage_matches": bool(
            modified.tool_result is not None
            and modified.tool_result.lineage is not None
            and modified.tool_result.lineage.get("source_task_id")
            == created.task_id
        ),
    }
    return _record(
        scenario_id="P-FILE-01",
        task_class="file",
        title="create, read, and version an independently verified report",
        expected_status=TaskStatus.SUCCEEDED.value,
        task=modified,
        checks=checks,
        independent_verification=all(
            checks[key]
            for key in ("create_verified", "read_verified", "modify_verified")
        ),
        started=started,
    )


def _make_code_workspace(root: Path) -> Path:
    workspace = root / "workspaces" / "acceptance-code"
    workspace.mkdir(parents=True)
    (workspace / "sample.py").write_text(
        "VALUE = 1\n\ndef value():\n    return VALUE\n",
        encoding="utf-8",
    )
    (workspace / "test_sample.py").write_text(
        "import unittest\nimport sample\n\n"
        "class SampleTests(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        self.assertEqual(sample.value(), 2)\n",
        encoding="utf-8",
    )
    return workspace


def _run_code_tasks(root: Path) -> list[dict[str, Any]]:
    started = time.monotonic()
    workspace = _make_code_workspace(root)
    grant_store = HostGrantStore(root / "host-grants")
    service = TaskService(root, host_grant_store=grant_store)
    # Host execution is authorized by a real single-use grant bound to this
    # workspace and target; the evaluation never uses a standing token.
    grant_token = grant_store.issue(
        workspace="acceptance-code",
        target="test_sample",
        issuer="product-evaluation",
    )["token"]
    waiting = service.submit_code_task(
        "inspect, approve, edit, and test the sample module",
        workspace="acceptance-code",
        path="sample.py",
        old_text="VALUE = 1",
        new_text="VALUE = 2",
        test_target="test_sample",
        test_backend="host",
        host_authorization=grant_token,
    )
    approval_observed = waiting.status is TaskStatus.AWAITING_APPROVAL
    completed = service.approve_step(waiting.task_id, "edit", approved=True)
    step_statuses = [item.status for item in completed.step_results]
    checks = {
        "approval_required_before_edit": approval_observed,
        "all_steps_succeeded": step_statuses
        == ["succeeded", "succeeded", "succeeded"],
        "edit_content_matches": (
            workspace / "sample.py"
        ).read_text(encoding="utf-8")
        == "VALUE = 2\n\ndef value():\n    return VALUE\n",
        "test_exit_zero": bool(
            completed.step_results
            and completed.step_results[-1].output.get("test_exit_code") == 0
        ),
        "aggregate_verified": _artifact_verified(root, completed),
    }
    positive = _record(
        scenario_id="P-CODE-01",
        task_class="code",
        title="inspect, approve, edit, and test a registered workspace",
        expected_status=TaskStatus.SUCCEEDED.value,
        task=completed,
        checks=checks,
        independent_verification=all(checks.values()),
        intervention_count=1,
        details={"step_statuses": step_statuses},
        started=started,
    )

    negative_started = time.monotonic()
    source = (workspace / "sample.py").read_text(encoding="utf-8")
    unauthorized = service.submit(
        TaskRequest(
            goal="attempt an unauthorized edit",
            operation=EDIT_TEXT_FILE,
            parameters={
                "workspace": "acceptance-code",
                "path": "sample.py",
                "expected_sha256": hashlib.sha256(
                    source.encode("utf-8")
                ).hexdigest(),
                "old_text": "VALUE = 2",
                "new_text": "VALUE = 3",
                "authorization": "not-authorized",
            },
        )
    )
    negative = _record(
        scenario_id="P-CODE-NEG-01",
        task_class="code",
        title="unauthorized code edit fails before side effect",
        expected_status=TaskStatus.BLOCKED.value,
        task=unauthorized,
        checks={
            "blocked_code": unauthorized.error is not None
            and unauthorized.error.code == "edit_not_authorized",
            "source_unchanged": (
                workspace / "sample.py"
            ).read_text(encoding="utf-8")
            == source,
            "no_tool_started": all(
                event.event_type != "tool_started"
                for event in service.events(unauthorized.task_id)
            ),
        },
        independent_verification=True,
        negative_control=True,
        started=negative_started,
    )
    return [positive, negative]


def _make_research_workspace(root: Path) -> Path:
    workspace = root / "workspaces" / "acceptance-research"
    workspace.mkdir(parents=True)
    (workspace / "source-a.txt").write_text(
        "Independent verification reads the result from disk.\n",
        encoding="utf-8",
    )
    (workspace / "source-b.txt").write_text(
        "Local provenance does not prove source authenticity.\n",
        encoding="utf-8",
    )
    return workspace


def _research_parameters() -> dict[str, Any]:
    return {
        "workspace": "acceptance-research",
        "sources": ["source-a.txt", "source-b.txt"],
        "title": "Local evidence acceptance",
        "output_path": "research.md",
        "findings": [
            {
                "statement": "Verification is performed by reading the result.",
                "confidence": "supported",
                "citations": [
                    {
                        "source_path": "source-a.txt",
                        "quote": "Independent verification reads the result from disk.",
                    }
                ],
            },
            {
                "statement": "Local provenance has an explicit authenticity limit.",
                "confidence": "uncertain",
                "citations": [
                    {
                        "source_path": "source-b.txt",
                        "quote": "does not prove source authenticity",
                    }
                ],
            },
        ],
    }


def _run_research_tasks(root: Path) -> list[dict[str, Any]]:
    started = time.monotonic()
    _make_research_workspace(root)
    service = TaskService(root)
    task = service.submit(
        TaskRequest(
            goal="produce a source-preserving local research report",
            operation=RESEARCH_REPORT,
            parameters=_research_parameters(),
        )
    )
    report_path = _artifact_path(root, task)
    report = (
        report_path.read_text(encoding="utf-8")
        if report_path is not None and report_path.is_file()
        else ""
    )
    evidence = (
        task.tool_result.research_evidence
        if task.tool_result is not None
        else None
    )
    checks = {
        "report_verified": _artifact_verified(root, task),
        "sources_hashed": bool(
            evidence
            and len(evidence.get("sources", [])) == 2
            and all(item.get("sha256") for item in evidence["sources"])
        ),
        "citations_present": "Independent verification reads" in report
        and "does not prove source authenticity" in report,
        "uncertainty_visible": "UNCERTAIN" in report,
        "network_disabled": "Network access: disabled" in report,
    }
    positive = _record(
        scenario_id="P-RESEARCH-01",
        task_class="research",
        title="produce a traceable local research report",
        expected_status=TaskStatus.SUCCEEDED.value,
        task=task,
        checks=checks,
        independent_verification=all(checks.values()),
        started=started,
    )

    negative_started = time.monotonic()
    parameters = _research_parameters()
    parameters["findings"][0]["citations"][0]["quote"] = (
        "a quote that does not exist"
    )
    invalid = service.submit(
        TaskRequest(
            goal="reject a fabricated citation",
            operation=RESEARCH_REPORT,
            parameters=parameters,
        )
    )
    negative = _record(
        scenario_id="P-RESEARCH-NEG-01",
        task_class="research",
        title="fabricated research citation fails before report creation",
        expected_status=TaskStatus.BLOCKED.value,
        task=invalid,
        checks={
            "blocked_code": invalid.error is not None
            and invalid.error.code == "research_citation_not_found",
            "no_report_created": not (
                root
                / "artifacts"
                / invalid.task_id
                / "research.md"
            ).exists(),
            "no_tool_started": all(
                event.event_type != "tool_started"
                for event in service.events(invalid.task_id)
            ),
        },
        independent_verification=True,
        negative_control=True,
        started=negative_started,
    )
    return [positive, negative]


def _write_step(
    step_id: str,
    artifact_path: str,
    content: object,
    *,
    depends_on: list[str] | None = None,
    requires_approval: bool = False,
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "tool_name": WRITE_TEXT_ARTIFACT,
        "expected_result": f"create {artifact_path}",
        "parameters": {
            "artifact_path": artifact_path,
            "content": content,
        },
        "depends_on": list(depends_on or []),
        "requires_approval": requires_approval,
    }


def _run_complex_tasks(root: Path) -> list[dict[str, Any]]:
    service = TaskService(root)
    records: list[dict[str, Any]] = []

    started = time.monotonic()
    data_flow = service.submit_plan(
        "create a source and dependent copy",
        [
            _write_step("source", "flow-source.txt", "flow value"),
            _write_step(
                "copy",
                "flow-copy.txt",
                "${source.content}",
                depends_on=["source"],
            ),
        ],
    )
    tail = (
        root
        / "artifacts"
        / str(data_flow.step_results[-1].child_task_id)
        / "flow-copy.txt"
        if data_flow.step_results
        and data_flow.step_results[-1].child_task_id
        else None
    )
    data_flow_verified = _multi_step_verified(root, data_flow)
    records.append(
        _record(
            scenario_id="P-COMPLEX-01",
            task_class="complex",
            title="execute dependent steps with explicit data flow",
            expected_status=TaskStatus.SUCCEEDED.value,
            task=data_flow,
            checks={
                "two_steps_succeeded": [
                    item.status for item in data_flow.step_results
                ]
                == ["succeeded", "succeeded"],
                "data_flow_written": bool(
                    tail is not None
                    and tail.is_file()
                    and tail.read_text(encoding="utf-8") == "flow value"
                ),
                "aggregate_verified": data_flow_verified,
            },
            independent_verification=data_flow_verified,
            started=started,
        )
    )

    started = time.monotonic()
    partial = service.submit_plan(
        "continue independent work after a blocked step",
        [
            _write_step("good", "partial-good.txt", "kept"),
            _write_step("bad", "../escape.txt", "blocked"),
            _write_step("independent", "partial-second.txt", "also kept"),
        ],
        failure_policy="continue",
    )
    records.append(
        _record(
            scenario_id="P-COMPLEX-02",
            task_class="complex",
            title="report partial success without masking failed work",
            expected_status=TaskStatus.PARTIAL.value,
            task=partial,
            checks={
                "step_statuses_truthful": [
                    item.status for item in partial.step_results
                ]
                == ["succeeded", "blocked", "succeeded"],
                "verification_not_passed": (
                    partial.verification is not None
                    and not partial.verification.passed
                ),
                "error_is_partial": partial.error is not None
                and partial.error.code == "multi_step_partial",
            },
            independent_verification=True,
            started=started,
        )
    )

    started = time.monotonic()
    approval = service.submit_plan(
        "approve one high-impact step",
        [
            _write_step(
                "approval",
                "approved.txt",
                "approved",
                requires_approval=True,
            )
        ],
    )
    approved = service.approve_step(
        approval.task_id,
        "approval",
        approved=True,
    )
    records.append(
        _record(
            scenario_id="P-COMPLEX-03",
            task_class="complex",
            title="pause for approval and continue the exact pending step",
            expected_status=TaskStatus.SUCCEEDED.value,
            task=approved,
            checks={
                "paused_for_approval": (
                    approval.status is TaskStatus.AWAITING_APPROVAL
                ),
                "approval_cleared": approved.pending_approval_step_id is None,
                "approved_step_succeeded": bool(
                    approved.step_results
                    and approved.step_results[0].status == "succeeded"
                ),
            },
            independent_verification=_multi_step_verified(root, approved),
            intervention_count=1,
            started=started,
        )
    )

    recovery_root = root / "recovery"
    started = time.monotonic()
    base_time = "2026-10-19T00:00:00.000+00:00"
    future_time = "2026-10-19T00:02:01.000+00:00"
    original = TaskService(recovery_root, clock=lambda: base_time)

    def interrupt(stage: str, _: Task) -> None:
        if stage == "after_write_before_verification":
            raise RuntimeError("acceptance interruption after write")

    original.fault_hook = interrupt
    parent = original.enqueue(
        TaskRequest(
            goal="recover a multi-step child after interruption",
            operation="execute_plan",
            parameters={
                "steps": [
                    _write_step("recover", "recovered.txt", "recovered")
                ],
                "failure_policy": "fail_fast",
            },
        )
    )
    interruption_observed = False
    try:
        original.execute_task(parent.task_id)
    except RuntimeError as exc:
        if str(exc) != "acceptance interruption after write":
            raise
        interruption_observed = True
    interrupted_parent = original.require_task(parent.task_id)
    interrupted_child_id = (
        interrupted_parent.step_results[0].child_task_id
        if interrupted_parent.step_results
        else None
    )
    if interrupted_child_id is None:
        raise ProductAcceptanceError(
            "interrupted multi-step child identity is missing"
        )
    writer = ChildRewriteGuardWriter(
        recovery_root / "artifacts",
        interrupted_child_id,
    )
    restarted = TaskService(
        recovery_root,
        writer=writer,
        clock=lambda: future_time,
    )
    recovered = restarted.resume(parent.task_id)
    recovered_path = None
    if recovered.step_results:
        child_id = recovered.step_results[0].child_task_id
        if child_id:
            recovered_path = (
                recovery_root
                / "artifacts"
                / child_id
                / "recovered.txt"
            )
    records.append(
        _record(
            scenario_id="P-COMPLEX-04",
            task_class="complex",
            title="resume an interrupted child without repeating its side effect",
            expected_status=TaskStatus.SUCCEEDED.value,
            task=recovered,
            checks={
                "interruption_observed": interruption_observed,
                "child_writer_not_recalled": writer.task_ids == [
                    parent.task_id
                ],
                "recovered_content_matches": bool(
                    recovered_path is not None
                    and recovered_path.is_file()
                    and recovered_path.read_text(encoding="utf-8")
                    == "recovered"
                ),
                "aggregate_verified": _multi_step_verified(
                    recovery_root,
                    recovered,
                ),
            },
            independent_verification=_multi_step_verified(
                recovery_root,
                recovered,
            ),
            recovered=True,
            started=started,
        )
    )
    return records


def run_product_acceptance() -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix="zevorinth-product-acceptance-"
    ) as temp:
        root = Path(temp)
        scenarios: list[dict[str, Any]] = [
            _run_file_task(root / "file")
        ]
        scenarios.extend(_run_code_tasks(root / "code"))
        scenarios.extend(_run_research_tasks(root / "research"))
        scenarios.extend(_run_complex_tasks(root / "complex"))
    elapsed_ms = (time.monotonic() - started) * 1000
    metrics = _metrics(scenarios, elapsed_ms)
    return {
        "schema_version": PRODUCT_ACCEPTANCE_SCHEMA,
        "mode": PRODUCT_ACCEPTANCE_MODE,
        "acceptance_scope": "ENGINE_SLICES",
        "v1_user_journey_complete": False,
        "product_stage": CAPABILITY_REGISTRY["product_stage"],
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "isolation": {
            "temporary_home_and_workspaces": True,
            "network_requests": 0,
            "live_model_requests": 0,
            "container_requests": 0,
            "production_workspace_writes": 0,
        },
        "metrics": metrics,
        "all_passed": (
            metrics["failed"] == 0
            and metrics["false_success_count"] == 0
            and metrics["negative_controls_passed"]
            == metrics["negative_controls"]
        ),
        "scenarios": scenarios,
        "limitations": [
            "This acceptance runs on the current host only.",
            "Code tests use the explicitly authorized host backend.",
            "Research is local-source only and proves citation presence, not source truth.",
            "The scored scenarios call the trusted TaskService API directly; the control-center user journey is incomplete.",
            "Code patch analysis and research synthesis are caller-authored in this slice.",
            "Container, remote-host, multi-user, and live-model quality are not covered.",
            "A passing run is evidence for this temporary execution, not production trust.",
        ],
    }


def product_acceptance_markdown(report: Mapping[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# Zevorinth V1 Engine-Slice Acceptance",
        "",
        f"- Engine slice status: `{'PASS' if report['all_passed'] else 'FAIL'}`",
        f"- Acceptance scope: `{report['acceptance_scope']}`",
        f"- V1 user journey complete: `{str(report['v1_user_journey_complete']).lower()}`",
        f"- Mode: `{report['mode']}`",
        f"- Product stage: `{report['product_stage']}`",
        f"- Passed: `{metrics['passed']}/{metrics['attempted_positive_scenarios']}`",
        f"- Negative controls: `{metrics['negative_controls_passed']}/{metrics['negative_controls']}`",
        f"- False successes: `{metrics['false_success_count']}`",
        f"- Interventions: `{metrics['interventions']}`",
        f"- Recovered tasks: `{metrics['recovered_tasks']}`",
        f"- Elapsed: `{metrics['elapsed_ms']} ms`",
        "",
        "## Scenario Results",
        "",
        "| ID | Class | Expected | Observed | Passed | Interventions |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for scenario in report["scenarios"]:
        lines.append(
            f"| {scenario['id']} | {scenario['task_class']} | "
            f"{scenario['expected_status']} | {scenario['observed_status']} | "
            f"{str(scenario['passed']).lower()} | "
            f"{scenario['intervention_count']} |"
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_product_acceptance(
    output: Path,
    *,
    report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    report = dict(report or run_product_acceptance())
    output = output.resolve()
    if output.exists():
        raise ProductAcceptanceError(
            "output already exists; refusing to overwrite acceptance evidence"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown = output.with_suffix(".md")
    if markdown.exists():
        output.unlink()
        raise ProductAcceptanceError(
            "markdown output already exists; refusing to overwrite evidence"
        )
    markdown.write_text(product_acceptance_markdown(report), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = write_product_acceptance(args.output)
    except ProductAcceptanceError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            f"Product acceptance: "
            f"{'PASS' if report['all_passed'] else 'FAIL'} "
            f"({report['metrics']['passed']}/"
            f"{report['metrics']['attempted_positive_scenarios']})"
        )
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
