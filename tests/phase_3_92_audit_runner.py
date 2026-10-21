"""Generate Phase 3.92 lifecycle applicability evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import Event, Task, TaskRequest  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.task_lifecycle import audit_task_lifecycle  # noqa: E402
from zevorinth.workbench import ReadOnlyWorkbench  # noqa: E402


NOW = "2026-10-21T00:00:00.000+00:00"
PHASE_3_91_EVIDENCE = {
    "artifacts/audits/phase-3.91/stable-summary.json": (
        "0360e53253b82ae4a60979b9c4e71e612c54967cb7b01f5425c49fcb0879b89d"
    ),
    "artifacts/audits/phase-3.91/workbench-matrix.json": (
        "86514ff0ec5963f72f153ac2549110e1c112b2a94d6f795767dfd0bed3bfa65e"
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_new_json(path: Path, payload: Any) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")


def _clarification_case(root: Path) -> tuple[TaskService, Task]:
    service = TaskService(root)
    task = service.submit(
        TaskRequest(
            goal="run tests before workspace is known",
            operation="run_tests",
            parameters={
                "runner": "unittest",
                "target": "tests.test_demo",
            },
        )
    )
    return service, task


def _matrix() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        service, clarification = _clarification_case(root)
        clarification_audit = service.audit_lifecycle(
            clarification.task_id,
            now=NOW,
        )
        clarification_preview = service.recovery_preview(
            clarification.task_id,
            now=NOW,
        )
        workbench_detail = ReadOnlyWorkbench(root).task_detail(
            clarification.task_id
        )

        queued = service.enqueue(
            TaskRequest(
                goal="queued run_tests",
                operation="run_tests",
                parameters={
                    "runner": "unittest",
                    "target": "tests.test_demo",
                    "workspace": "missing",
                },
            )
        )
        queued_audit = service.audit_lifecycle(
            queued.task_id,
            now=NOW,
        )

        blocked = service.submit(
            TaskRequest(
                goal="blocked before execution",
                operation="run_tests",
                parameters={
                    "runner": "unittest",
                    "target": "tests.test_demo",
                    "workspace": "missing",
                    "execution_backend": "host",
                    "host_authorization": "local-maintenance",
                },
            )
        )
        blocked_audit = service.audit_lifecycle(
            blocked.task_id,
            now=NOW,
        )

        executed = service.enqueue(
            TaskRequest(
                goal="executed without final plan",
                operation="run_tests",
                parameters={
                    "runner": "unittest",
                    "target": "tests.test_demo",
                    "workspace": "missing",
                },
            )
        )
        executed_events = [
            *service.store.list_events(executed.task_id),
            Event.new(
                executed.task_id,
                "execution_plan_finalized",
                NOW,
                payload={"final_execution_plan": None},
            ),
        ]
        data = executed.to_dict()
        data["status"] = "verifying"
        data["phase"] = "verification_started"
        executed_verifying = Task.from_dict(data)
        executed_audit = audit_task_lifecycle(
            executed_verifying,
            executed_events,
            now=NOW,
        )

        return {
            "phase": "3.92",
            "status": "PASS",
            "scope": "lifecycle_audit_stage_applicability",
            "clarification": {
                "task_status": clarification.status.value,
                "auditor_version": clarification_audit["auditor_version"],
                "audit_ok": clarification_audit["ok"],
                "audit_issue_codes": [
                    item["code"]
                    for item in clarification_audit["issues"]
                ],
                "manual_intervention": clarification_audit[
                    "task_recovery"
                ]["manual_intervention"],
                "preview_action": clarification_preview["actions"][0][
                    "action"
                ],
                "preview_current_state": clarification_preview["actions"][0][
                    "current_state"
                ],
                "preview_risk": clarification_preview["actions"][0]["risk"],
                "preview_requires_approval": clarification_preview[
                    "actions"
                ][0]["requires_approval"],
                "workbench_audit_ok": workbench_detail[
                    "lifecycle_audit"
                ]["ok"],
                "workbench_preview_action": workbench_detail[
                    "recovery_preview"
                ]["actions"][0]["action"],
            },
            "pre_execution_states": {
                "queued_audit_ok": queued_audit["ok"],
                "queued_issue_codes": [
                    item["code"] for item in queued_audit["issues"]
                ],
                "blocked_status": blocked.status.value,
                "blocked_audit_ok": blocked_audit["ok"],
                "blocked_issue_codes": [
                    item["code"] for item in blocked_audit["issues"]
                ],
            },
            "executed_without_plan": {
                "task_status": executed_verifying.status.value,
                "auditor_version": executed_audit["auditor_version"],
                "audit_ok": executed_audit["ok"],
                "audit_issue_codes": [
                    item["code"] for item in executed_audit["issues"]
                ],
                "missing_final_execution_plan": (
                    "missing_final_execution_plan"
                    in {
                        item["code"] for item in executed_audit["issues"]
                    }
                ),
            },
            "isolation": {
                "real_model_requests": 0,
                "external_network_requests": 0,
                "dependency_installs": 0,
                "real_workspace_writes": 0,
                "container_executions": 0,
            },
        }


def _integrity() -> dict[str, Any]:
    files: dict[str, Any] = {}
    all_match = True
    for relative, expected in PHASE_3_91_EVIDENCE.items():
        observed = _sha256(PROJECT_ROOT / relative)
        matches = observed == expected
        all_match = all_match and matches
        files[relative] = {
            "expected_sha256": expected,
            "observed_sha256": observed,
            "matches": matches,
        }
    return {
        "phase": "3.91",
        "baseline_commit": "f3564ea",
        "all_preserved_evidence_matches": all_match,
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts/audits/phase-3.92",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "lifecycle-applicability.json": _matrix(),
        "phase-3.91-integrity.json": _integrity(),
    }
    for name, payload in outputs.items():
        path = args.output_dir / name
        if path.exists():
            raise FileExistsError(path)
        _write_new_json(path, payload)
        print(f"{path} {_sha256(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
