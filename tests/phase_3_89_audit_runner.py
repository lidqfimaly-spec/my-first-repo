"""Generate Phase 3.89 offline evidence without overwriting history."""

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

from zevorinth.evaluation import IncrementingClock  # noqa: E402
from zevorinth.llm_planner import LLMPlanner, LLMPlannerConfig  # noqa: E402
from zevorinth.models import TaskRequest  # noqa: E402
from zevorinth.planner_exact_text_runner import (  # noqa: E402
    run_exact_text_regression,
)
from zevorinth.planner_quality import run_planner_quality  # noqa: E402
from zevorinth.planner_text_precision import (  # noqa: E402
    load_exact_text_contract,
)
from zevorinth.response_normalization import ModelCallResult  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


PHASE_3_88_HASHES = {
    "artifacts/evaluations/phase-3.88-quality-live.json": (
        "6a5c9b2d349c09a807877434988cedc89f169da83caa247d5852aa7d430ac028"
    ),
    "artifacts/audits/phase-3.88/stable-summary.json": (
        "9e4465e32a39104e04f33e92020b4de0ce14d4b8b72ab0d218e2e0aa4c944cd2"
    ),
    "src/zevorinth/evals/planner_intent_fidelity_v3.json": (
        "83663c21266cecd729e9d3fc120d7c98d8c89eb69aa4c77b11617b69c456ae75"
    ),
    "src/zevorinth/evals/live_evaluation_metric_contract_v2.json": (
        "1ec71b8ed77e7559e23f88e0ef9902ddc2c838b88ca2c3f0bde819ffe59325f2"
    ),
    "src/zevorinth/evals/planner_precision_v1.json": (
        "bc9206acc2736ee63ad9dd8a31b088907e2ef28d144656e67a8882c843ad336c"
    ),
}


class WrongContentClient:
    def create_candidate(
        self,
        *,
        goal: str,
        instructions: str,
        max_output_tokens: int,
        trusted_context: dict[str, Any] | None = None,
    ) -> ModelCallResult:
        del goal, instructions, max_output_tokens, trusted_context
        return ModelCallResult(
            candidate={
                "classification": "plan",
                "operation": "write_text_artifact",
                "artifact_path": "exact.txt",
                "content": "wrong",
                "missing_fields": [],
                "clarification_questions": [],
                "reason": None,
            },
            status="completed",
            text_present=True,
            output_bytes=128,
        )


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


def _core_regression() -> dict[str, Any]:
    planner = LLMPlanner(
        config=LLMPlannerConfig(
            model="offline-exact-text",
            api_key_present=True,
        ),
        client=WrongContentClient(),
        clock=IncrementingClock(),
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        service = TaskService(root, planner=planner)
        task = service.submit(
            TaskRequest(
                goal="创建 exact.txt，内容按可信规范提供。",
                operation=None,
                parameters={
                    "precision_mode": "exact_text",
                    "expected_text": "right",
                },
            )
        )
        events = service.events(task.task_id)
        artifact_exists = root.joinpath("exact.txt").exists()
    event_types = [event.event_type for event in events]
    failed_event = next(
        event for event in events if event.event_type == "planning_failed"
    )
    metadata = failed_event.payload["planning_metadata"]
    return {
        "phase": "3.89",
        "task_status": task.status.value,
        "error_code": task.error.code if task.error else None,
        "event_types": event_types,
        "plan_created": task.plan is not None,
        "artifact_created": artifact_exists,
        "task_planned_event": "task_planned" in event_types,
        "tool_started_event": "tool_started" in event_types,
        "tool_succeeded_event": "tool_succeeded" in event_types,
        "execution_attempted": False,
        "text_precision_gate": metadata.get("text_precision_gate"),
        "text_precision": metadata.get("text_precision"),
        "intent_fidelity_gate": metadata.get("intent_fidelity_gate"),
        "intent_fidelity": metadata.get("intent_fidelity"),
        "network_request_sent": False,
        "container_executed": False,
    }


def _history_integrity() -> dict[str, Any]:
    files: dict[str, Any] = {}
    all_match = True
    for relative, expected in PHASE_3_88_HASHES.items():
        observed = _sha256(PROJECT_ROOT / relative)
        matches = observed == expected
        all_match = all_match and matches
        files[relative] = {
            "expected_sha256": expected,
            "observed_sha256": observed,
            "matches": matches,
        }
    return {
        "phase": "3.88",
        "all_original_evidence_matches": all_match,
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts/audits/phase-3.89",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "exact-text-regression.json": run_exact_text_regression(),
        "offline-quality-report.json": run_planner_quality(),
        "core-regression.json": _core_regression(),
        "phase-3.88-integrity.json": _history_integrity(),
        "exact-text-contract.json": load_exact_text_contract(),
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
