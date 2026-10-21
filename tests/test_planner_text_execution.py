"""Phase 3.90: text precision through write and independent readback."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import RealArtifactWriter  # noqa: E402
from zevorinth.evaluation import ScriptedPlannerClient  # noqa: E402
from zevorinth.llm_planner import LLMPlanner, LLMPlannerConfig  # noqa: E402
from zevorinth.models import (  # noqa: E402
    ExecutionMode,
    TaskRequest,
    TaskStatus,
    ToolResult,
)
from zevorinth.planner_text_execution_runner import (  # noqa: E402
    load_text_execution_contract,
    run_text_execution_regression,
)
from zevorinth.service import TaskService  # noqa: E402


def _planner(case: dict[str, Any]) -> LLMPlanner:
    return LLMPlanner(
        config=LLMPlannerConfig(
            model="offline-text-execution",
            api_key_present=True,
        ),
        client=ScriptedPlannerClient(case),
    )


class PostWriteTamperingWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self, artifacts_root: Path, replacement: bytes) -> None:
        self._delegate = RealArtifactWriter(artifacts_root)
        self._replacement = replacement
        self.calls = 0

    def write(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        result = self._delegate.write(**kwargs)
        target = (
            self._delegate.artifacts_root
            / kwargs["task_id"]
            / kwargs["artifact_path"]
        )
        target.write_bytes(self._replacement)
        return result


class PlannerTextExecutionTests(unittest.TestCase):
    def test_execution_contract_binds_active_precision_contract(self) -> None:
        contract = load_text_execution_contract()
        self.assertEqual(contract["version"], "planner-text-execution-v1")
        self.assertFalse(contract["retroactively_applicable"])
        self.assertEqual(
            contract["precision_contract"]["version"],
            "planner-exact-text-v1",
        )
        self.assertEqual(
            contract["precision_contract"]["sha256"],
            "cf22a6adb91b03a03830c89c887a962e3d5835b7e7a2baa140e32eae15b8e8ec",
        )
        self.assertEqual(
            {case["category"] for case in contract["cases"]},
            {
                "exact_text",
                "semantic",
                "format_constrained",
                "precondition",
            },
        )

    def test_execution_contract_rejects_duplicate_or_precision_drift(
        self,
    ) -> None:
        contract = load_text_execution_contract()
        payload = dict(contract)
        payload.pop("contract_sha256")
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            duplicate = directory / "duplicate.json"
            duplicate.write_text(
                '{"version":"one","version":"two"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate key"):
                load_text_execution_contract(duplicate)

            mismatch = directory / "mismatch.json"
            mismatch_payload = json.loads(json.dumps(payload))
            mismatch_payload["precision_contract"] = {
                "version": "planner-exact-text-v1",
                "sha256": "0" * 64,
            }
            mismatch.write_text(
                json.dumps(mismatch_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "precision contract"):
                load_text_execution_contract(mismatch)

    def test_end_to_end_matrix_is_repeatable_and_really_writes(
        self,
    ) -> None:
        first = run_text_execution_regression()
        second = run_text_execution_regression()
        self.assertEqual(first, second)
        self.assertTrue(first["all_passed"], first["cases"])
        self.assertEqual(first["metrics"]["case_count"], 10)
        self.assertEqual(first["metrics"]["succeeded_task_count"], 6)
        self.assertEqual(first["metrics"]["planning_failed_task_count"], 4)
        self.assertEqual(first["metrics"]["verified_artifact_count"], 6)
        self.assertEqual(first["metrics"]["artifact_write_count"], 6)
        self.assertEqual(first["metrics"]["rejected_artifact_count"], 4)
        self.assertEqual(first["metrics"]["tool_executions"], 6)
        self.assertEqual(first["metrics"]["real_api_calls"], 0)
        self.assertEqual(first["metrics"]["network_requests"], 0)
        self.assertEqual(first["metrics"]["container_executions"], 0)
        self.assertEqual(
            first["metrics"]["non_temporary_workspace_side_effects"],
            0,
        )

    def test_exact_unicode_whitespace_and_bom_are_verified_from_bytes(
        self,
    ) -> None:
        report = run_text_execution_regression()
        cases = {case["case_id"]: case for case in report["cases"]}
        unicode_case = cases["exact_unicode_multiline"]
        self.assertEqual(unicode_case["observed_task_status"], "succeeded")
        self.assertTrue(unicode_case["verification_passed"])
        self.assertEqual(
            unicode_case["artifact_byte_count"],
            len("任务完成\ncafé\n".encode("utf-8")),
        )
        self.assertTrue(unicode_case["bytes_match_expected_utf8"])
        self.assertEqual(
            unicode_case["observed_artifact_sha256"],
            hashlib.sha256("任务完成\ncafé\n".encode("utf-8")).hexdigest(),
        )
        whitespace_case = cases["exact_whitespace_preserved"]
        self.assertTrue(whitespace_case["bytes_match_expected_utf8"])
        bom_case = cases["exact_utf8_bom_preserved"]
        self.assertEqual(
            bom_case["artifact_prefix_hex"],
            "efbbbf68656c6c6f",
        )
        self.assertTrue(bom_case["bytes_match_expected_utf8"])

    def test_success_and_rejection_event_chains_remain_separate(self) -> None:
        report = run_text_execution_regression()
        cases = {case["case_id"]: case for case in report["cases"]}
        success = cases["format_json_object_executed"]
        self.assertEqual(success["event_types"][-1], "task_succeeded")
        self.assertEqual(
            success["event_types"].count("verification_completed"),
            1,
        )
        rejected = cases["exact_punctuation_mismatch_rejected"]
        self.assertEqual(rejected["observed_task_status"], "planning_failed")
        self.assertEqual(
            rejected["observed_precision_result"],
            "exact_mismatch",
        )
        self.assertEqual(
            rejected["observed_precision_gate"],
            "blocked",
        )
        self.assertEqual(
            rejected["observed_intent_status"],
            "planned_faithful",
        )
        self.assertFalse(rejected["plan_created"])
        self.assertEqual(rejected["artifact_file_count"], 0)
        self.assertNotIn("task_planned", rejected["event_types"])
        self.assertNotIn("tool_started", rejected["event_types"])
        self.assertNotIn("verification_completed", rejected["event_types"])

    def test_post_write_content_tamper_fails_independent_verification(
        self,
    ) -> None:
        case = {
            "id": "exact_tamper",
            "goal": "创建 exact.txt，内容按可信规范提供。",
            "trusted_parameters": {
                "precision_mode": "exact_text",
                "expected_text": "exact\n",
            },
            "scripted_output": {
                "classification": "plan",
                "operation": "write_text_artifact",
                "artifact_path": "exact.txt",
                "content": "exact\n",
                "missing_fields": [],
                "clarification_questions": [],
                "reason": None,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            writer = PostWriteTamperingWriter(
                root / "artifacts",
                "tampered\n".encode("utf-8"),
            )
            service = TaskService(
                root,
                planner=_planner(case),
                writer=writer,
            )
            task = service.submit(
                TaskRequest(
                    goal=case["goal"],
                    operation=None,
                    parameters=dict(case["trusted_parameters"]),
                )
            )
            events = service.events(task.task_id)
            artifact = (
                root / "artifacts" / task.task_id / "exact.txt"
            ).read_bytes()

        self.assertEqual(writer.calls, 1)
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.error.code, "verification_failed")
        self.assertIsNotNone(task.verification)
        self.assertFalse(task.verification.passed)
        self.assertEqual(
            task.verification.reason,
            "artifact_content_mismatch",
        )
        self.assertEqual(artifact, b"tampered\n")
        event_types = [event.event_type for event in events]
        self.assertIn("tool_succeeded", event_types)
        self.assertIn("verification_completed", event_types)
        self.assertNotIn("task_succeeded", event_types)

    def test_candidate_cannot_supply_its_own_expected_text(self) -> None:
        case = {
            "id": "candidate_expected_text_forgery",
            "goal": "创建 forged.txt，内容按可信规范提供。",
            "trusted_parameters": {},
            "scripted_output": {
                "classification": "plan",
                "operation": "write_text_artifact",
                "artifact_path": "forged.txt",
                "content": "wrong",
                "expected_text": "wrong",
                "missing_fields": [],
                "clarification_questions": [],
                "reason": None,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, planner=_planner(case))
            task = service.submit(
                TaskRequest(
                    goal=case["goal"],
                    operation=None,
                    parameters={},
                )
            )
            events = service.events(task.task_id)
            artifact_exists = (
                root / "artifacts" / task.task_id / "forged.txt"
            ).exists()

        self.assertEqual(task.status, TaskStatus.PLANNING_FAILED)
        self.assertEqual(task.error.code, "unknown_candidate_fields")
        self.assertIsNone(task.plan)
        self.assertFalse(artifact_exists)
        event_types = [event.event_type for event in events]
        self.assertNotIn("task_planned", event_types)
        self.assertNotIn("tool_started", event_types)


if __name__ == "__main__":
    unittest.main()
