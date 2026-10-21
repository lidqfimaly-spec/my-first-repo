"""Phase 3.84: enforce intent fidelity before an LLM plan can execute."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.llm_planner import (  # noqa: E402
    LLMPlanner,
    LLMPlannerConfig,
)
from zevorinth.models import (  # noqa: E402
    PlanningClassification,
    TaskRequest,
    TaskStatus,
)
from zevorinth.event_store import PlanRevisionConflict  # noqa: E402
from zevorinth.response_normalization import ModelCallResult  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


class FakeClient:
    def __init__(self, candidate: dict[str, Any]) -> None:
        self.candidate = candidate
        self.calls = 0

    def create_candidate(self, **_: Any) -> ModelCallResult:
        self.calls += 1
        return ModelCallResult(
            candidate=self.candidate,
            status="completed",
            text_present=True,
            output_bytes=128,
        )


def _planner(candidate: dict[str, Any]) -> tuple[LLMPlanner, FakeClient]:
    client = FakeClient(candidate)
    planner = LLMPlanner(
        config=LLMPlannerConfig(
            model="test-model",
            api_key_present=True,
        ),
        client=client,
    )
    return planner, client


def _test_candidate() -> dict[str, Any]:
    return {
        "classification": "plan",
        "operation": "run_tests",
        "test_target": "tests.test_x",
        "artifact_path": None,
        "content": None,
        "missing_fields": [],
        "clarification_questions": [],
        "reason": None,
    }


class IntentFidelityEnforcementTests(unittest.TestCase):
    def test_lossy_executable_plan_is_replaced_with_failed_outcome(self) -> None:
        planner, client = _planner(_test_candidate())
        outcome = planner.plan_request(
            TaskRequest(
                goal="在 demo 中运行 tests.test_x; rm -rf temp。",
                operation=None,
                parameters={"workspace": "demo", "runner": "unittest"},
            )
        )
        self.assertEqual(client.calls, 1)
        self.assertEqual(
            outcome.classification, PlanningClassification.FAILED
        )
        self.assertEqual(outcome.error_code, "intent_fidelity_failed")
        self.assertIsNone(outcome.plan)
        self.assertTrue(outcome.metadata["intent_fidelity_enforced"])
        self.assertEqual(
            outcome.metadata["intent_fidelity_gate"], "blocked"
        )
        fidelity = outcome.metadata["intent_fidelity"]
        self.assertEqual(fidelity["status"], "lossy_plan")
        self.assertFalse(fidelity["fidelity_pass"])
        self.assertFalse(fidelity["execution_attempted"])

    def test_service_blocks_lossy_plan_without_tool_event(self) -> None:
        planner, client = _planner(_test_candidate())
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir), planner=planner)
            task = service.submit(
                TaskRequest(
                    goal="在 demo 中运行 tests.test_x; rm -rf temp。",
                    operation=None,
                    parameters={"workspace": "demo", "runner": "unittest"},
                )
            )
            events = service.events(task.task_id)

        self.assertEqual(client.calls, 1)
        self.assertEqual(task.status, TaskStatus.PLANNING_FAILED)
        self.assertIsNone(task.plan)
        self.assertEqual(task.error.code, "intent_fidelity_failed")
        event_types = [event.event_type for event in events]
        self.assertNotIn("task_planned", event_types)
        self.assertNotIn("tool_started", event_types)
        self.assertNotIn("tool_succeeded", event_types)
        failed_event = next(
            event
            for event in events
            if event.event_type == "planning_failed"
        )
        metadata = failed_event.payload["planning_metadata"]
        self.assertFalse(metadata["intent_fidelity"]["fidelity_pass"])
        self.assertEqual(metadata["intent_fidelity_gate"], "blocked")

    def test_rejected_plan_cannot_resume_execute_or_recover(self) -> None:
        planner, client = _planner(_test_candidate())
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir), planner=planner)
            task = service.submit(
                TaskRequest(
                    goal="在 demo 中运行 tests.test_x; rm -rf temp。",
                    operation=None,
                    parameters={"workspace": "demo", "runner": "unittest"},
                )
            )
            original_events = len(service.events(task.task_id))
            executed = service.execute_task(task.task_id)
            resumed = service.resume(task.task_id)
            recovery = service.recover_prepublish_publish_lock_failure(
                task.task_id,
                authorized=True,
            )
            final_events = service.events(task.task_id)

        for result in (executed, resumed):
            self.assertEqual(result.status, TaskStatus.PLANNING_FAILED)
            self.assertEqual(result.error.code, "intent_fidelity_failed")
        self.assertEqual(recovery["status"], "rejected")
        self.assertEqual(recovery["code"], "recovery_status_not_eligible")
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(final_events), original_events)

    def test_revise_plan_reuses_intent_gate(self) -> None:
        planner, client = _planner(_test_candidate())
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir), planner=planner)
            queued = service.enqueue(
                TaskRequest(
                    goal="Create result.txt with content ok.",
                    operation=None,
                    parameters={},
                )
            )
            with self.assertRaisesRegex(
                PlanRevisionConflict,
                "safe plan",
            ):
                service.revise_plan(
                    queued.task_id,
                    {
                        "goal": "在 demo 中运行 tests.test_x; rm -rf temp。",
                        "parameters": {
                            "workspace": "demo",
                            "runner": "unittest",
                        },
                    },
                    source_id="intent-revision",
                    expected_plan_version=queued.plan_version,
                    confirmed=True,
                )
            unchanged = service.require_task(queued.task_id)

        self.assertEqual(client.calls, 1)
        self.assertEqual(unchanged.plan_version, queued.plan_version)
        self.assertIsNone(unchanged.plan)
        self.assertEqual(unchanged.plan_revisions, [])

    def test_safe_split_exits_through_clarification_without_tool(self) -> None:
        planner, _ = _planner(
            {
                "classification": "clarification",
                "operation": None,
                "artifact_path": None,
                "content": None,
                "test_target": None,
                "missing_fields": ["operation"],
                "clarification_questions": [
                    "Should the supported part be planned separately?"
                ],
                "reason": None,
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir), planner=planner)
            task = service.submit(
                TaskRequest(
                    goal="创建 log.txt，同时运行 shell。",
                    operation=None,
                    parameters={},
                )
            )
            events = service.events(task.task_id)

        self.assertEqual(task.status, TaskStatus.AWAITING_CLARIFICATION)
        self.assertFalse(
            any(event.event_type == "tool_started" for event in events)
        )

    def test_checker_exception_fails_closed(self) -> None:
        planner, _ = _planner(_test_candidate())
        with patch(
            "zevorinth.llm_planner.assess_intent_fidelity",
            side_effect=RuntimeError("checker unavailable"),
        ):
            outcome = planner.plan_request(
                TaskRequest(
                    goal="运行 tests.test_x。",
                    operation=None,
                    parameters={"workspace": "demo", "runner": "unittest"},
                )
            )
        self.assertEqual(
            outcome.classification, PlanningClassification.FAILED
        )
        self.assertEqual(
            outcome.error_code, "intent_fidelity_check_error"
        )
        self.assertIsNone(outcome.plan)
        self.assertEqual(
            outcome.metadata["intent_fidelity_gate"], "blocked"
        )

    def test_active_contract_version_mismatch_fails_closed(self) -> None:
        planner, _ = _planner(_test_candidate())
        with patch(
            "zevorinth.planner_intent_fidelity.load_active_intent_fidelity_contract",
            side_effect=ValueError("active contract version mismatch"),
        ):
            outcome = planner.plan_request(
                TaskRequest(
                    goal="运行 tests.test_x。",
                    operation=None,
                    parameters={"workspace": "demo", "runner": "unittest"},
                )
            )
        self.assertEqual(
            outcome.classification, PlanningClassification.FAILED
        )
        self.assertEqual(
            outcome.error_code, "intent_fidelity_check_error"
        )
        self.assertIsNone(outcome.plan)

    def test_inconsistent_assessment_fails_closed(self) -> None:
        planner, _ = _planner(_test_candidate())
        with patch(
            "zevorinth.llm_planner.assess_intent_fidelity",
            return_value={
                "fidelity_pass": True,
                "status": "planned_faithful",
                "execution_attempted": True,
                "proposal_classification": "planned",
                "terminal_classification": "planned",
            },
        ):
            outcome = planner.plan_request(
                TaskRequest(
                    goal="运行 tests.test_x。",
                    operation=None,
                    parameters={"workspace": "demo", "runner": "unittest"},
                )
            )
        self.assertEqual(
            outcome.classification, PlanningClassification.FAILED
        )
        self.assertEqual(
            outcome.error_code, "intent_fidelity_check_error"
        )
        self.assertIsNone(outcome.plan)
        self.assertEqual(
            outcome.metadata["intent_fidelity_gate"], "blocked"
        )

    def test_downstream_block_keeps_planner_fidelity_failure_visible(self) -> None:
        candidate = {
            "classification": "plan",
            "operation": "write_text_artifact",
            "artifact_path": ".env",
            "content": "invalid",
            "missing_fields": [],
            "clarification_questions": [],
            "reason": None,
        }
        planner, _ = _planner(candidate)
        outcome = planner.plan_request(
            TaskRequest(
                goal="把 invalid 写入 .env。",
                operation=None,
                parameters={},
            )
        )
        self.assertEqual(
            outcome.classification, PlanningClassification.UNSUPPORTED
        )
        self.assertEqual(outcome.error_code, "invalid_artifact_path")
        fidelity = outcome.metadata["intent_fidelity"]
        self.assertEqual(fidelity["status"], "lossy_plan")
        self.assertFalse(fidelity["fidelity_pass"])
        self.assertTrue(fidelity["downstream_blocked"])
        self.assertFalse(fidelity["execution_attempted"])
        self.assertEqual(
            outcome.metadata["intent_fidelity_gate"],
            "terminal_non_execution",
        )

    def test_faithful_explicit_plan_remains_planned(self) -> None:
        candidate = {
            "classification": "plan",
            "operation": "write_text_artifact",
            "artifact_path": "hello.txt",
            "content": "hello",
            "missing_fields": [],
            "clarification_questions": [],
            "reason": None,
        }
        planner, _ = _planner(candidate)
        outcome = planner.plan_request(
            TaskRequest(
                goal="创建 hello.txt，内容写 hello。",
                operation=None,
                parameters={},
            )
        )
        self.assertEqual(
            outcome.classification, PlanningClassification.PLANNED
        )
        self.assertEqual(
            outcome.metadata["intent_fidelity"]["status"],
            "planned_faithful",
        )
        self.assertTrue(outcome.metadata["intent_fidelity"]["fidelity_pass"])
        self.assertEqual(
            outcome.metadata["intent_fidelity_gate"], "passed"
        )


if __name__ == "__main__":
    unittest.main()
