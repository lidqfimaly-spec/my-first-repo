from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import SimulatedArtifactWriter  # noqa: E402
from zevorinth.llm_planner import (  # noqa: E402
    CANDIDATE_JSON_SCHEMA,
    LLMPlanner,
    LLMPlannerConfig,
    ModelCallResult,
    OpenAIResponsesClient,
    StructuredOutputError,
    _extract_refusal,
)
from zevorinth.models import (  # noqa: E402
    ExecutionMode,
    PlannerMode,
    TaskRequest,
    TaskStatus,
    ToolResult,
)
from zevorinth.service import TaskService  # noqa: E402


def candidate(
    *,
    classification: str = "plan",
    operation: str | None = "write_text_artifact",
    artifact_path: str | None = "result.txt",
    content: str | None = "llm planned content\n",
    missing_fields: list[str] | None = None,
    questions: list[str] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "classification": classification,
        "operation": operation,
        "artifact_path": artifact_path,
        "content": content,
        "missing_fields": list(missing_fields or []),
        "clarification_questions": list(questions or []),
        "reason": reason,
    }


class FakeClient:
    def __init__(self, result: ModelCallResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def create_candidate(self, **_: Any) -> ModelCallResult:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FailingWriter:
    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(self) -> None:
        self.calls = 0

    def write(self, **_: Any) -> ToolResult:
        self.calls += 1
        raise OSError("controlled write failure")


def planner_with(result: ModelCallResult | Exception) -> tuple[LLMPlanner, FakeClient]:
    client = FakeClient(result)
    planner = LLMPlanner(
        config=LLMPlannerConfig(
            model="test-model",
            api_key_present=True,
            timeout_seconds=1,
            max_output_tokens=200,
        ),
        client=client,
    )
    return planner, client


class LLMPlannerTests(unittest.TestCase):
    def _run(
        self,
        result: ModelCallResult | Exception,
        *,
        writer: Any = None,
        request: TaskRequest | None = None,
    ) -> tuple[Any, Any, list[Any], Any]:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        planner, client = planner_with(result)
        service = TaskService(root, planner=planner, writer=writer)
        task = service.submit(
            request
            or TaskRequest(
                goal="Create a result file using natural language.",
                operation=None,
                parameters={},
            )
        )
        return task, service, service.events(task.task_id), client

    def test_valid_structured_plan_uses_existing_execution_core(self) -> None:
        task, _, events, client = self._run(
            ModelCallResult(candidate=candidate(), status="completed")
        )

        self.assertEqual(task.status, TaskStatus.SUCCEEDED)
        self.assertEqual(task.execution_mode, ExecutionMode.REAL)
        self.assertEqual(task.planner_mode, PlannerMode.LLM)
        self.assertFalse(task.plan.deterministic)
        self.assertEqual(task.plan.planner_name, "llm-candidate-v1")
        self.assertTrue(task.verification.passed)
        self.assertEqual(client.calls, 1)
        self.assertEqual(
            task.request.parameters,
            {
                "artifact_path": "result.txt",
                "content": "llm planned content\n",
            },
        )
        event_types = [event.event_type for event in events]
        self.assertIn("planning_started", event_types)
        self.assertIn("planning_completed", event_types)
        self.assertIn("tool_started", event_types)
        self.assertIn("verification_completed", event_types)

    def test_structured_schema_forbids_extra_capabilities(self) -> None:
        self.assertFalse(CANDIDATE_JSON_SCHEMA["additionalProperties"])
        self.assertEqual(
            set(CANDIDATE_JSON_SCHEMA["properties"]),
            set(CANDIDATE_JSON_SCHEMA["required"]),
        )

    def test_responses_refusal_content_is_detected(self) -> None:
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(type="refusal", refusal="cannot comply")
                    ],
                )
            ]
        )
        self.assertEqual(_extract_refusal(response), "cannot comply")

    def test_official_sdk_adapter_uses_bounded_no_tool_request(self) -> None:
        captured: dict[str, Any] = {}

        class FakeResponses:
            def create(self, **kwargs: Any) -> Any:
                captured["request"] = kwargs
                return SimpleNamespace(
                    output_text=json.dumps(candidate()),
                    output=[],
                    status="completed",
                    error=None,
                    incomplete_details=None,
                    usage=SimpleNamespace(
                        input_tokens=12,
                        output_tokens=8,
                        total_tokens=20,
                    ),
                    id="resp-test",
                )

        class FakeOpenAI:
            def __init__(self, **kwargs: Any) -> None:
                captured["client"] = kwargs
                self.responses = FakeResponses()

        fake_module = ModuleType("openai")
        fake_module.OpenAI = FakeOpenAI
        with patch.dict(sys.modules, {"openai": fake_module}), patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-only-key"},
            clear=False,
        ):
            client = OpenAIResponsesClient(
                model="configured-model",
                timeout_seconds=7,
            )
            result = client.create_candidate(
                goal="Create result.txt",
                instructions="bounded instructions",
                max_output_tokens=321,
            )

        self.assertEqual(captured["client"]["max_retries"], 0)
        self.assertEqual(captured["client"]["timeout"], 7)
        request = captured["request"]
        self.assertEqual(request["model"], "configured-model")
        self.assertEqual(request["max_output_tokens"], 321)
        self.assertFalse(request["store"])
        self.assertNotIn("tools", request)
        self.assertEqual(request["timeout"], 7)
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertEqual(result.total_tokens, 20)
        self.assertEqual(result.candidate["artifact_path"], "result.txt")

    def test_missing_configuration_never_calls_client(self) -> None:
        client = FakeClient(ModelCallResult(candidate=candidate()))
        for config in [
            LLMPlannerConfig(
                model="test-model",
                api_key_present=False,
            ),
            LLMPlannerConfig(
                model=None,
                api_key_present=True,
            ),
        ]:
            with self.subTest(config=config):
                planner = LLMPlanner(config=config, client=client)
                outcome = planner.plan_request(
                    TaskRequest(
                        goal="Create a file.",
                        operation=None,
                        parameters={},
                    )
                )
                self.assertEqual(outcome.error_code in {"missing_api_key", "missing_model"}, True)
        self.assertEqual(client.calls, 0)

    def test_missing_sdk_client_is_explicit_planning_failure(self) -> None:
        planner = LLMPlanner(
            config=LLMPlannerConfig(
                model="test-model",
                api_key_present=True,
            ),
            client=None,
        )
        outcome = planner.plan_request(
            TaskRequest(
                goal="Create a file.",
                operation=None,
                parameters={},
            )
        )
        self.assertEqual(outcome.error_code, "llm_sdk_unavailable")

    def test_llm_timeout_is_inside_effective_lease(self) -> None:
        planner, _ = planner_with(ModelCallResult(candidate=candidate()))
        self.assertGreater(planner.required_lease_seconds, planner.config.timeout_seconds)

    def test_invalid_schema_candidate_fails_without_tool(self) -> None:
        invalid = candidate()
        invalid["shell"] = "rm -rf /"
        task, _, events, _ = self._run(
            ModelCallResult(candidate=invalid, status="completed")
        )

        self.assertEqual(task.status, TaskStatus.PLANNING_FAILED)
        self.assertEqual(task.error.code, "invalid_candidate_fields")
        self.assertEqual(task.execution_mode, ExecutionMode.NOT_EXECUTED)
        self.assertFalse(any(event.event_type == "tool_started" for event in events))

    def test_refusal_incomplete_timeout_and_connection_are_safe_failures(self) -> None:
        cases = [
            (
                ModelCallResult(refusal="no", status="completed"),
                "model_refusal",
            ),
            (
                ModelCallResult(
                    status="incomplete",
                    incomplete_reason="max_output_tokens",
                ),
                "model_response_incomplete",
            ),
            (TimeoutError("timeout"), "model_timeout"),
            (ConnectionError("offline"), "model_connection_error"),
            (StructuredOutputError("bad JSON"), "invalid_structured_output"),
        ]
        for result, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                task, _, events, _ = self._run(result)
                self.assertEqual(task.status, TaskStatus.PLANNING_FAILED)
                self.assertEqual(task.error.code, expected_code)
                self.assertFalse(
                    any(event.event_type == "tool_started" for event in events)
                )

    def test_unsafe_path_and_operation_are_locally_rejected(self) -> None:
        outputs = [
            candidate(artifact_path="../escape.txt"),
            candidate(artifact_path="/tmp/escape.txt"),
            candidate(operation="shell", artifact_path="x.txt"),
        ]
        for output in outputs:
            with self.subTest(output=output):
                task, _, events, _ = self._run(
                    ModelCallResult(candidate=output, status="completed")
                )
                self.assertEqual(task.status, TaskStatus.BLOCKED)
                self.assertEqual(task.execution_mode, ExecutionMode.NOT_EXECUTED)
                self.assertFalse(
                    any(event.event_type == "tool_started" for event in events)
                )

    def test_clarification_is_persisted_without_tool(self) -> None:
        task, _, events, _ = self._run(
            ModelCallResult(
                candidate=candidate(
                    classification="clarification",
                    operation=None,
                    artifact_path=None,
                    content=None,
                    missing_fields=["artifact_path", "content"],
                    questions=["Which file?", "What content?"],
                ),
                status="completed",
            )
        )

        self.assertEqual(task.status, TaskStatus.AWAITING_CLARIFICATION)
        self.assertEqual(task.missing_fields, ["artifact_path", "content"])
        self.assertEqual(task.clarification_questions, ["Which file?", "What content?"])
        self.assertFalse(any(event.event_type == "tool_started" for event in events))

    def test_policy_still_runs_after_valid_llm_candidate(self) -> None:
        planner, _ = planner_with(
            ModelCallResult(candidate=candidate(), status="completed")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root, planner=planner)
            request = TaskRequest(
                goal="Create result.txt.",
                operation=None,
                parameters={},
            )
            queued = service.enqueue(request)
            task_dir = root / "artifacts" / queued.task_id
            task_dir.mkdir(parents=True)
            (task_dir / "result.txt").write_text("already exists")

            blocked = service.execute_task(queued.task_id)
            self.assertEqual(blocked.status, TaskStatus.BLOCKED)
            self.assertEqual(blocked.error.code, "artifact_exists")
            self.assertEqual((task_dir / "result.txt").read_text(), "already exists")

    def test_execution_failure_remains_execution_failure(self) -> None:
        writer = FailingWriter()
        task, _, events, _ = self._run(
            ModelCallResult(candidate=candidate(), status="completed"),
            writer=writer,
        )

        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.execution_mode, ExecutionMode.REAL)
        self.assertEqual(writer.calls, 1)
        self.assertIn("tool_failed", [event.event_type for event in events])

    def test_simulated_writer_remains_simulated(self) -> None:
        task, _, _, _ = self._run(
            ModelCallResult(candidate=candidate(), status="completed"),
            writer=SimulatedArtifactWriter(),
        )

        self.assertEqual(task.status, TaskStatus.SIMULATED)
        self.assertEqual(task.execution_mode, ExecutionMode.SIMULATED)
        self.assertIsNone(task.artifact)
        self.assertIsNone(task.verification)

    def test_malformed_candidate_and_empty_content_fail_safely(self) -> None:
        for bad_candidate in [
            [],
            candidate(content=""),
            {
                "classification": "clarification",
                "operation": None,
                "artifact_path": None,
                "content": None,
                "missing_fields": ["unknown"],
                "clarification_questions": ["?"],
                "reason": None,
            },
        ]:
            with self.subTest(candidate=bad_candidate):
                task, _, events, _ = self._run(
                    ModelCallResult(candidate=bad_candidate, status="completed")
                )
                self.assertEqual(task.status, TaskStatus.PLANNING_FAILED)
                self.assertFalse(
                    any(event.event_type == "tool_started" for event in events)
                )


if __name__ == "__main__":
    unittest.main()
