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
    DEEPSEEK_CANDIDATE_JSON_SCHEMA,
    LLMProvider,
    LLMPlanner,
    LLMPlannerConfig,
    LLMConfigurationError,
    ModelCallResult,
    OpenAIResponsesClient,
    StructuredOutputError,
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
            provider=LLMProvider.OPENAI,
            model="test-model",
            api_key_present=True,
            api_key_env="OPENAI_API_KEY",
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
                goal="Create result.txt with content llm planned content.",
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
        self.assertEqual(
            DEEPSEEK_CANDIDATE_JSON_SCHEMA["required"],
            ["classification"],
        )

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
        ), patch(
            "zevorinth.llm_planner.openai_sdk_status",
            return_value={
                "installed": True,
                "version": "3.35.1",
                "minimum_version": "3.35.1",
                "compatible": True,
                "module_path": "/fake/openai/__init__.py",
            },
        ):
            config = LLMPlannerConfig(
                provider=LLMProvider.OPENAI,
                model="configured-model",
                api_key_present=True,
                api_key_env="OPENAI_API_KEY",
                timeout_seconds=7,
            )
            client = OpenAIResponsesClient(config)
            result = client.create_candidate(
                goal="Create result.txt",
                instructions="bounded instructions",
                max_output_tokens=321,
                trusted_context={"workspace": "demo"},
            )

        self.assertEqual(captured["client"]["max_retries"], 0)
        self.assertEqual(captured["client"]["timeout"], 7)
        self.assertNotIn("base_url", captured["client"])
        request = captured["request"]
        self.assertEqual(request["model"], "configured-model")
        self.assertEqual(request["max_output_tokens"], 321)
        self.assertFalse(request["store"])
        self.assertNotIn("tools", request)
        self.assertEqual(request["timeout"], 7)
        model_input = json.loads(request["input"])
        self.assertEqual(model_input["user_goal"], "Create result.txt")
        self.assertEqual(
            model_input["trusted_parameters"],
            {"workspace": "demo"},
        )
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertEqual(result.total_tokens, 20)
        self.assertEqual(result.candidate["artifact_path"], "result.txt")

    def test_deepseek_environment_is_explicit_and_secret_is_not_reported(self) -> None:
        secret = "deepseek-test-secret-value"
        with patch.dict(
            os.environ,
            {
                "ZEVORINTH_LLM_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": secret,
                "OPENAI_API_KEY": "must-not-be-used",
                "ZEVORINTH_MODEL": "deepseek-flash",
            },
            clear=False,
        ):
            config = LLMPlannerConfig.from_environment()
            report = config.public_report()

        self.assertEqual(config.provider, LLMProvider.DEEPSEEK)
        self.assertEqual(config.api_key_env, "DEEPSEEK_API_KEY")
        self.assertTrue(config.api_key_present)
        self.assertEqual(config.base_url, "https://api.deepseek.com")
        self.assertEqual(config.model, "deepseek-flash")
        self.assertNotIn(secret, json.dumps(report))

    def test_deepseek_adapter_uses_own_key_and_base_url(self) -> None:
        captured: dict[str, Any] = {}

        class FakeResponses:
            def create(self, **kwargs: Any) -> Any:
                captured["request"] = kwargs
                return SimpleNamespace(
                    output_text=json.dumps(
                        candidate(artifact_path="deepseek.txt")
                    ),
                    output=[],
                    status="completed",
                    error=None,
                    incomplete_details=None,
                    usage=None,
                    id="deepseek-test",
                )

        class FakeOpenAI:
            def __init__(self, **kwargs: Any) -> None:
                captured["client"] = kwargs
                self.responses = FakeResponses()

        fake_module = ModuleType("openai")
        fake_module.OpenAI = FakeOpenAI
        config = LLMPlannerConfig(
            provider=LLMProvider.DEEPSEEK,
            model="deepseek-flash",
            api_key_present=True,
            api_key_env="DEEPSEEK_API_KEY",
            base_url="https://api.deepseek.com",
            timeout_seconds=8,
        )
        with patch.dict(sys.modules, {"openai": fake_module}), patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "deepseek-test-key",
                "OPENAI_API_KEY": "openai-test-key",
            },
            clear=False,
        ), patch(
            "zevorinth.llm_planner.openai_sdk_status",
            return_value={
                "installed": True,
                "version": "3.35.1",
                "minimum_version": "3.35.1",
                "compatible": True,
                "module_path": "/fake/openai/__init__.py",
            },
        ):
            client = OpenAIResponsesClient(config)
            result = client.create_candidate(
                goal="Create deepseek.txt",
                instructions="bounded",
                max_output_tokens=222,
            )

        self.assertEqual(
            captured["client"]["base_url"],
            "https://api.deepseek.com",
        )
        self.assertEqual(captured["client"]["api_key"], "deepseek-test-key")
        self.assertEqual(captured["client"]["max_retries"], 0)
        request = captured["request"]
        self.assertNotIn("store", request)
        self.assertNotIn("tools", request)
        self.assertNotIn("strict", request["text"]["format"])
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        self.assertEqual(
            request["text"]["format"]["schema"]["required"],
            ["classification"],
        )
        self.assertEqual(request["max_output_tokens"], 222)
        self.assertEqual(result.candidate["artifact_path"], "deepseek.txt")

    def test_provider_keys_do_not_cross(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ZEVORINTH_LLM_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": "",
                "OPENAI_API_KEY": "openai-only",
                "ZEVORINTH_MODEL": "deepseek-flash",
            },
            clear=False,
        ):
            deepseek_config = LLMPlannerConfig.from_environment()
        self.assertFalse(deepseek_config.api_key_present)
        self.assertEqual(
            deepseek_config.validation_error()[0],
            "missing_api_key",
        )

        with patch.dict(
            os.environ,
            {
                "ZEVORINTH_LLM_PROVIDER": "openai",
                "DEEPSEEK_API_KEY": "deepseek-only",
                "OPENAI_API_KEY": "",
                "ZEVORINTH_MODEL": "configured-model",
            },
            clear=False,
        ):
            openai_config = LLMPlannerConfig.from_environment()
        self.assertFalse(openai_config.api_key_present)
        self.assertEqual(openai_config.api_key_env, "OPENAI_API_KEY")

    def test_secret_in_client_exception_is_not_echoed(self) -> None:
        secret = "deepseek-secret-must-not-escape"
        planner, _ = planner_with(RuntimeError(secret))
        outcome = planner.plan_request(
            TaskRequest(
                goal="Create a file.",
                operation=None,
                parameters={},
            )
        )
        serialized = json.dumps(outcome.to_dict(), ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertEqual(outcome.error_code, "model_api_error")

    def test_unknown_provider_fails_configuration_validation(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ZEVORINTH_LLM_PROVIDER": "unknown",
                "ZEVORINTH_MODEL": "some-model",
            },
            clear=False,
        ):
            config = LLMPlannerConfig.from_environment()
        self.assertEqual(config.provider, None)
        self.assertEqual(config.validation_error()[0], "unsupported_provider")

    def test_clarification_does_not_require_unrelated_file_fields(self) -> None:
        task, _, events, _ = self._run(
            ModelCallResult(
                candidate={
                    "classification": "clarification",
                    "missing_fields": ["artifact_path"],
                    "clarification_questions": ["What file name should I use?"],
                },
                status="completed",
                text_present=True,
            )
        )

        self.assertEqual(task.status, TaskStatus.AWAITING_CLARIFICATION)
        self.assertEqual(task.missing_fields, ["artifact_path"])
        self.assertFalse(any(event.event_type == "tool_started" for event in events))

    def test_category_specific_missing_and_unknown_fields_are_diagnosed(self) -> None:
        cases = [
            (
                {"classification": "plan", "operation": "write_text_artifact"},
                "missing_candidate_fields",
                "candidate_missing_fields",
            ),
            (
                {
                    **candidate(),
                    "hidden_instruction": "sensitive-value",
                },
                "unknown_candidate_fields",
                "candidate_unknown_fields",
            ),
        ]
        for candidate_value, error_code, diagnostic_key in cases:
            with self.subTest(error_code=error_code):
                task, _, events, _ = self._run(
                    ModelCallResult(
                        candidate=candidate_value,
                        status="completed",
                        text_present=True,
                    )
                )
                self.assertEqual(task.status, TaskStatus.PLANNING_FAILED)
                self.assertEqual(task.error.code, error_code)
                self.assertIn(diagnostic_key, task.planning_metadata)
                self.assertNotIn(
                    "sensitive-value",
                    json.dumps(task.planning_metadata, ensure_ascii=False),
                )
                self.assertFalse(
                    any(event.event_type == "tool_started" for event in events)
                )

    def test_empty_invalid_json_and_invalid_status_have_safe_diagnostics(self) -> None:
        cases = [
            (
                ModelCallResult(
                    candidate=None,
                    status="completed",
                    text_present=False,
                    output_item_types=["message"],
                    content_part_types=[],
                ),
                "missing_structured_output",
            ),
            (
                ModelCallResult(
                    candidate=None,
                    status="completed",
                    text_present=True,
                    text_length=12,
                    json_error_type="JSONDecodeError",
                    output_item_types=["message"],
                    content_part_types=["output_text"],
                ),
                "invalid_structured_output",
            ),
            (
                ModelCallResult(
                    candidate=candidate(),
                    status="unexpected",
                ),
                "invalid_response_status",
            ),
            (
                ModelCallResult(
                    candidate=candidate(),
                    status="completed",
                    http_status=500,
                ),
                "model_response_failed",
            ),
        ]
        for result, error_code in cases:
            with self.subTest(error_code=error_code):
                task, _, events, _ = self._run(result)
                self.assertEqual(task.status, TaskStatus.PLANNING_FAILED)
                self.assertEqual(task.error.code, error_code)
                self.assertEqual(task.execution_mode, ExecutionMode.NOT_EXECUTED)
                self.assertFalse(
                    any(event.event_type == "tool_started" for event in events)
                )

    def test_api_error_keeps_only_whitelisted_diagnostics(self) -> None:
        class StatusError(RuntimeError):
            status_code = 429

        secret = "provider-secret-in-error"
        planner, _ = planner_with(StatusError(secret))
        outcome = planner.plan_request(
            TaskRequest(
                goal="Create a file.",
                operation=None,
                parameters={},
            )
        )
        serialized = json.dumps(outcome.to_dict(), ensure_ascii=False)
        self.assertEqual(outcome.metadata["http_status"], 429)
        self.assertNotIn(secret, serialized)

    def test_incompatible_sdk_version_is_rejected_before_client_creation(self) -> None:
        fake_module = ModuleType("openai")
        fake_module.OpenAI = object
        with patch.dict(sys.modules, {"openai": fake_module}), patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-only-key"},
            clear=False,
        ), patch(
            "zevorinth.llm_planner.openai_sdk_status",
            return_value={
                "installed": True,
                "version": "2.45.0",
                "minimum_version": "3.35.1",
                "compatible": False,
                "module_path": "/fake/openai/__init__.py",
            },
        ):
            with self.assertRaises(LLMConfigurationError):
                OpenAIResponsesClient(
                    LLMPlannerConfig(
                        provider=LLMProvider.OPENAI,
                        model="configured-model",
                        api_key_present=True,
                        api_key_env="OPENAI_API_KEY",
                        timeout_seconds=5,
                    )
                )

    def test_missing_configuration_never_calls_client(self) -> None:
        client = FakeClient(ModelCallResult(candidate=candidate()))
        for config in [
            LLMPlannerConfig(
                provider=LLMProvider.OPENAI,
                model="test-model",
                api_key_present=False,
                api_key_env="OPENAI_API_KEY",
            ),
            LLMPlannerConfig(
                provider=LLMProvider.OPENAI,
                model=None,
                api_key_present=True,
                api_key_env="OPENAI_API_KEY",
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
                provider=LLMProvider.OPENAI,
                model="test-model",
                api_key_present=True,
                api_key_env="OPENAI_API_KEY",
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
        self.assertEqual(task.error.code, "unknown_candidate_fields")
        self.assertEqual(task.execution_mode, ExecutionMode.NOT_EXECUTED)
        self.assertFalse(any(event.event_type == "tool_started" for event in events))

    def test_refusal_incomplete_timeout_and_connection_are_safe_failures(self) -> None:
        cases = [
            (
                ModelCallResult(refusal_present=True, status="completed"),
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
                goal="Create result.txt with content llm planned content.",
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
