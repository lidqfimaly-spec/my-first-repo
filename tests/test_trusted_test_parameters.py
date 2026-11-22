"""Phase 3.45: lock trusted run_tests parameter propagation and failure names."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.artifact_tool import ArtifactToolError  # noqa: E402
from zevorinth.evidence_provisioning import ProvisioningError  # noqa: E402
from zevorinth.llm_planner import (  # noqa: E402
    LLMPlanner,
    LLMPlannerConfig,
    LLMProvider,
    ModelCallResult,
)
from zevorinth.local_test_runner import (  # noqa: E402
    TestExecutionError,
    validate_parameters,
)
from zevorinth.models import (  # noqa: E402
    EventType,
    ExecutionMode,
    TaskRequest,
    TaskStatus,
)
from zevorinth.planning_validation import PlanningCandidateValidator  # noqa: E402
from zevorinth.planner import build_test_plan  # noqa: E402
from zevorinth.service import (  # noqa: E402
    TaskService,
    _test_execution_plan_mismatch,
)
from tests.host_grant_support import authorized_service  # noqa: E402


#: A well-formed but never-issued token; shape tests must not need a store.
_TOKEN = "a" * 32 + "." + "b" * 64


def _base(**overrides):
    values = {
        "runner": "unittest",
        "target": "test_ok",
        "workspace": "demo",
    }
    values.update(overrides)
    return values


def _candidate(**overrides):
    values = {
        "classification": "plan",
        "operation": "run_tests",
        "artifact_path": None,
        "content": None,
        "test_target": "test_ok",
        "missing_fields": [],
        "clarification_questions": [],
        "reason": None,
    }
    values.update(overrides)
    return values


class ParameterNormalizationTests(unittest.TestCase):
    def test_auto_provision_is_preserved(self) -> None:
        normalized = validate_parameters(_base(auto_provision_isolation=True))
        self.assertIs(normalized["auto_provision_isolation"], True)

    def test_auto_provision_defaults_to_false(self) -> None:
        normalized = validate_parameters(_base())
        self.assertIs(normalized["auto_provision_isolation"], False)

    def test_auto_provision_rejects_non_boolean(self) -> None:
        for value in (1, 0, "true", None):
            with self.subTest(value=value):
                with self.assertRaises(TestExecutionError) as raised:
                    validate_parameters(
                        _base(auto_provision_isolation=value)
                    )
                self.assertEqual(
                    raised.exception.code,
                    "invalid_auto_provision_isolation",
                )

    def test_auto_provision_requires_container_backend(self) -> None:
        with self.assertRaises(TestExecutionError) as raised:
            validate_parameters(
                _base(
                    execution_backend="host",
                    auto_provision_isolation=True,
                )
            )
        self.assertEqual(
            raised.exception.code,
            "invalid_auto_provision_isolation",
        )

    def test_trusted_fields_are_preserved(self) -> None:
        normalized = validate_parameters(
            _base(
                evidence_id="auto-test",
                image="python:3.13-alpine",
                config_digest="a" * 64,
                command_digest="b" * 64,
                host_authorization=_TOKEN,
                required_artifacts=["report.json", "./sub/out.xml"],
                disk_limit_bytes=2 * 1024 * 1024,
                workspace_budget_bytes=4096,
                auto_provision_isolation=True,
            )
        )
        self.assertEqual(normalized["evidence_id"], "auto-test")
        self.assertEqual(normalized["image"], "python:3.13-alpine")
        self.assertEqual(normalized["config_digest"], "a" * 64)
        self.assertEqual(normalized["command_digest"], "b" * 64)
        self.assertEqual(normalized["host_authorization"], _TOKEN)
        self.assertEqual(
            normalized["required_artifacts"],
            ["report.json", "sub/out.xml"],
        )
        self.assertEqual(normalized["disk_limit_bytes"], 2 * 1024 * 1024)
        self.assertEqual(normalized["workspace_budget_bytes"], 4096)
        self.assertIs(normalized["auto_provision_isolation"], True)

    def test_invalid_evidence_id_is_rejected(self) -> None:
        for value in ("../escape", "", "a" * 129, "bad$id"):
            with self.subTest(value=value):
                with self.assertRaises(TestExecutionError) as raised:
                    validate_parameters(_base(evidence_id=value))
                self.assertEqual(raised.exception.code, "invalid_evidence_id")

    def test_unpinned_or_unsafe_image_is_rejected(self) -> None:
        for value in ("python", "bad image", "/etc/passwd", "foo;rm"):
            with self.subTest(value=value):
                with self.assertRaises(TestExecutionError) as raised:
                    validate_parameters(_base(image=value))
                self.assertEqual(raised.exception.code, "invalid_image")

    def test_invalid_digest_is_rejected(self) -> None:
        for field in ("config_digest", "command_digest"):
            for value in ("xyz", "A" * 64, "a" * 63):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(TestExecutionError) as raised:
                        validate_parameters(_base(**{field: value}))
                    self.assertEqual(
                        raised.exception.code, f"invalid_{field}"
                    )

    def test_invalid_required_artifact_is_rejected(self) -> None:
        for value in ("report.json", ["../escape"], ["/abs"], [""], [42]):
            with self.subTest(value=value):
                with self.assertRaises(TestExecutionError) as raised:
                    validate_parameters(
                        _base(required_artifacts=value)
                    )
                self.assertEqual(
                    raised.exception.code,
                    "invalid_required_artifacts",
                )

    def test_invalid_disk_limit_is_rejected(self) -> None:
        for value in (0, 1023, True, "1MiB"):
            with self.subTest(value=value):
                with self.assertRaises(TestExecutionError) as raised:
                    validate_parameters(
                        _base(disk_limit_bytes=value)
                    )
                self.assertEqual(raised.exception.code, "invalid_disk_limit")

    def test_invalid_workspace_budget_is_rejected(self) -> None:
        for value in (-1, True, "10"):
            with self.subTest(value=value):
                with self.assertRaises(TestExecutionError) as raised:
                    validate_parameters(
                        _base(workspace_budget_bytes=value)
                    )
                self.assertEqual(
                    raised.exception.code,
                    "invalid_workspace_budget",
                )


class LlmPlanPropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = PlanningCandidateValidator()

    def _request(self, **overrides):
        parameters = {
            "workspace": "demo",
            "auto_provision_isolation": True,
        }
        parameters.update(overrides)
        return TaskRequest(
            goal="run test_ok",
            operation=None,
            parameters=parameters,
        )

    def test_auto_provision_survives_plan_build(self) -> None:
        result = self.validator.validate(
            _candidate(), base_request=self._request()
        )
        self.assertEqual(result.classification.value, "planned")
        self.assertIs(
            result.plan.steps[0].parameters["auto_provision_isolation"],
            True,
        )

    def test_trusted_fields_survive_plan_build(self) -> None:
        request = self._request(
            evidence_id="auto-test",
            image="python:3.13-alpine",
            config_digest="a" * 64,
            command_digest="b" * 64,
            host_authorization=_TOKEN,
            required_artifacts=["report.json"],
            disk_limit_bytes=2 * 1024 * 1024,
            workspace_budget_bytes=4096,
        )
        result = self.validator.validate(
            _candidate(), base_request=request
        )
        self.assertEqual(result.classification.value, "planned")
        parameters = result.plan.steps[0].parameters
        for field in (
            "evidence_id",
            "image",
            "config_digest",
            "command_digest",
            "host_authorization",
            "required_artifacts",
            "disk_limit_bytes",
            "workspace_budget_bytes",
            "auto_provision_isolation",
        ):
            self.assertEqual(
                parameters[field],
                request.parameters[field],
                field,
            )

    def test_post_plan_check_detects_field_loss(self) -> None:
        def lossy_plan(request, *, planner_name):
            plan = build_test_plan(request, planner_name=planner_name)
            plan.steps[0].parameters["auto_provision_isolation"] = False
            return plan

        with patch(
            "zevorinth.planning_validation.build_test_plan",
            side_effect=lossy_plan,
        ):
            result = self.validator.validate(
                _candidate(), base_request=self._request()
            )

        self.assertEqual(result.classification.value, "failed")
        self.assertEqual(result.error_code, "trusted_parameter_loss")
        self.assertIsNone(result.plan)


class FakeCandidateClient:
    def __init__(self, candidate: dict) -> None:
        self.candidate = candidate
        self.calls = 0

    def create_candidate(
        self,
        *,
        goal: str,
        instructions: str,
        max_output_tokens: int,
        trusted_context: dict | None = None,
    ) -> ModelCallResult:
        del goal, instructions, max_output_tokens, trusted_context
        self.calls += 1
        return ModelCallResult(
            candidate=self.candidate,
            status="completed",
            text_present=True,
            output_bytes=128,
        )


def _planner() -> LLMPlanner:
    return LLMPlanner(
        config=LLMPlannerConfig(
            provider=LLMProvider.DEEPSEEK,
            model="deepseek-flash",
            api_key_present=True,
            api_key_env="DEEPSEEK_API_KEY",
        ),
        client=FakeCandidateClient(_candidate()),
    )


def _workspace(test: unittest.TestCase) -> tuple[Path, Path]:
    temp_dir = tempfile.TemporaryDirectory()
    test.addCleanup(temp_dir.cleanup)
    root = Path(temp_dir.name)
    workspace = root / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    (workspace / "test_ok.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    return root, workspace


def _successful_raw() -> dict:
    return {
        "runner": "unittest",
        "target": "test_ok",
        "workspace": "demo",
        "exit_code": 0,
        "timed_out": False,
        "cancelled": False,
        "terminated_confirmed": True,
        "truncated": False,
        "stdout": b"OK\n",
        "stderr": b"",
        "stdout_bytes": 3,
        "stderr_bytes": 0,
        "stdout_sha256": hashlib.sha256(b"OK\n").hexdigest(),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "duration_ms": 1.0,
        "output_limit_bytes": 8192,
        "timeout_seconds": 10.0,
        "artifacts": [],
        "collection_status": "complete",
    }


class ExecutionWiringTests(unittest.TestCase):
    def test_auto_provision_reaches_container_and_verifier(self) -> None:
        root, workspace = _workspace(self)
        (workspace / "inside.txt").write_text("inside\n", encoding="utf-8")
        service = TaskService(root, planner=_planner())
        provision_calls: list[dict] = []
        container_calls: list[dict] = []
        host_calls: list[dict] = []

        def fake_provision(root_arg, **kwargs):
            provision_calls.append({"root": str(root_arg), **kwargs})
            workspace = Path(root_arg) / "workspaces" / kwargs["workspace_name"]
            (workspace / "probe-written.txt").write_text(
                "ok\n", encoding="utf-8"
            )
            return {"status": "verified", "evidence_id": kwargs["evidence_id"]}

        def fake_container(self, task, parameters, cancel_check=None):
            container_calls.append(dict(parameters))
            return _successful_raw()

        def host_fallback(**kwargs):
            host_calls.append(dict(kwargs))
            raise AssertionError("host fallback must not be used")

        service.test_runner = host_fallback
        # What is under test is the wiring (provision -> container -> verifier),
        # not whether this machine has a reachable container runtime: the live
        # probes are pinned so a stopped Docker daemon cannot turn a wiring
        # assertion into an environment report.
        with patch(
            "zevorinth.evidence_provisioning.provision_evidence",
            side_effect=fake_provision,
        ), patch.object(
            TaskService, "_run_container_test", fake_container
        ), patch(
            "zevorinth.evidence_store.EvidenceStore.verify_stored_evidence",
            return_value={"state": "verified", "reasons": []},
        ), patch(
            "zevorinth.isolation._live_runtime_identity",
            return_value="stub-runtime",
        ), patch(
            "zevorinth.isolation._live_image_digest",
            return_value="python@sha256:" + "a" * 64,
        ):
            task = service.submit(
                TaskRequest(
                    goal="run test_ok",
                    operation=None,
                    parameters={
                        "workspace": "demo",
                        "auto_provision_isolation": True,
                    },
                )
            )

        self.assertEqual(task.status, TaskStatus.SUCCEEDED)
        self.assertTrue(task.verification.passed)
        self.assertEqual(task.tool_result.tool_name, "run_tests")
        self.assertEqual(len(provision_calls), 1)
        self.assertEqual(provision_calls[0]["workspace_name"], "demo")
        self.assertEqual(provision_calls[0]["target"], "test_ok")
        self.assertEqual(
            provision_calls[0]["image"], "python:3.13-alpine"
        )
        self.assertEqual(len(container_calls), 1)
        self.assertEqual(
            container_calls[0]["evidence_id"],
            f"auto-{task.task_id}",
        )
        self.assertIs(container_calls[0]["auto_provision_isolation"], True)
        self.assertEqual(host_calls, [])
        snapshot = task.execution_plan_snapshot
        self.assertEqual(snapshot["kind"], "run_tests")
        self.assertEqual(snapshot["task_id"], task.task_id)
        self.assertEqual(
            snapshot["evidence_id"], f"auto-{task.task_id}"
        )
        self.assertEqual(
            snapshot["config_digest"],
            task.tool_result.test_evidence.config_digest,
        )
        self.assertEqual(
            snapshot["command_digest"],
            task.tool_result.test_evidence.command_digest,
        )
        artifacts = {
            item["path"]: item
            for item in snapshot["provisioning_artifacts"]
        }
        self.assertIn("probe-written.txt", artifacts)
        self.assertEqual(
            artifacts["probe-written.txt"]["ownership"],
            "provisioning",
        )
        self.assertEqual(
            artifacts["probe-written.txt"]["retention"],
            "workspace_baseline",
        )
        self.assertEqual(
            artifacts["probe-written.txt"]["cleanup"],
            "deferred_to_controlled_cleanup",
        )
        event_types = [
            event.event_type for event in service.events(task.task_id)
        ]
        self.assertLess(
            event_types.index(EventType.EXECUTION_PLAN_FINALIZED.value),
            event_types.index(EventType.TOOL_STARTED.value),
        )

    def test_provisioning_failure_stops_before_container(self) -> None:
        root, workspace = _workspace(self)
        (workspace / "inside.txt").write_text("inside\n", encoding="utf-8")
        service = TaskService(root, planner=_planner())
        container_calls: list[dict] = []

        def fake_provision(root_arg, **kwargs):
            del root_arg, kwargs
            raise ProvisioningError(
                "boundary_probe_failed", "network was reachable"
            )

        def fake_container(self, task, parameters, cancel_check=None):
            container_calls.append(dict(parameters))
            raise AssertionError("container must not start")

        with patch(
            "zevorinth.evidence_provisioning.provision_evidence",
            side_effect=fake_provision,
        ), patch.object(
            TaskService, "_run_container_test", fake_container
        ):
            task = service.submit(
                TaskRequest(
                    goal="run test_ok",
                    operation=None,
                    parameters={
                        "workspace": "demo",
                        "auto_provision_isolation": True,
                    },
                )
            )

        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.error.code, "provisioning_failed")
        self.assertIn("boundary_probe_failed", task.error.message)
        self.assertEqual(task.tool_result.tool_name, "run_tests")
        self.assertEqual(container_calls, [])
        self.assertEqual(task.verification, None)

    def test_execution_plan_persistence_failure_blocks_container(self) -> None:
        root, workspace = _workspace(self)
        (workspace / "inside.txt").write_text("inside\n", encoding="utf-8")
        service = TaskService(root, planner=_planner())
        container_calls: list[dict] = []

        def fake_provision(root_arg, **kwargs):
            workspace = Path(root_arg) / "workspaces" / kwargs["workspace_name"]
            (workspace / "probe-written.txt").write_text(
                "ok\n", encoding="utf-8"
            )
            return {"status": "verified", "evidence_id": kwargs["evidence_id"]}

        def fake_container(self, task, parameters, cancel_check=None):
            container_calls.append(dict(parameters))
            return _successful_raw()

        original_record = service.store.record_event_and_update

        def failing_record(task_id, event_type, *args, **kwargs):
            value = getattr(event_type, "value", event_type)
            if value == EventType.EXECUTION_PLAN_FINALIZED.value:
                raise RuntimeError("store unavailable")
            return original_record(task_id, event_type, *args, **kwargs)

        service.store.record_event_and_update = failing_record
        with patch(
            "zevorinth.evidence_provisioning.provision_evidence",
            side_effect=fake_provision,
        ), patch.object(
            TaskService, "_run_container_test", fake_container
        ):
            task = service.submit(
                TaskRequest(
                    goal="run test_ok",
                    operation=None,
                    parameters={
                        "workspace": "demo",
                        "auto_provision_isolation": True,
                    },
                )
            )

        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(
            task.error.code, "execution_plan_persistence_failed"
        )
        self.assertEqual(task.tool_result.tool_name, "run_tests")
        self.assertEqual(container_calls, [])

    def test_expired_or_invalidated_evidence_blocks_success(self) -> None:
        for state, code in (
            ("expired", "evidence_expired"),
            ("invalidated", "evidence_invalidated"),
        ):
            with self.subTest(state=state):
                root, workspace = _workspace(self)
                (workspace / "inside.txt").write_text(
                    "inside\n", encoding="utf-8"
                )
                service = TaskService(root, planner=_planner())

                def fake_provision(root_arg, **kwargs):
                    workspace = (
                        Path(root_arg)
                        / "workspaces"
                        / kwargs["workspace_name"]
                    )
                    (workspace / "probe-written.txt").write_text(
                        "ok\n", encoding="utf-8"
                    )
                    return {
                        "status": "verified",
                        "evidence_id": kwargs["evidence_id"],
                    }

                def fake_container(
                    self, task, parameters, cancel_check=None
                ):
                    return _successful_raw()

                with patch(
                    "zevorinth.evidence_provisioning.provision_evidence",
                    side_effect=fake_provision,
                ), patch.object(
                    TaskService, "_run_container_test", fake_container
                ), patch(
                    "zevorinth.evidence_store.EvidenceStore.verify_stored_evidence",
                    return_value={"state": state, "reasons": [code]},
                ), patch(
                    # The revalidation probe must not depend on whether a
                    # Docker daemon happens to be running on this machine.
                    "zevorinth.isolation._live_runtime_identity",
                    return_value="docker@test",
                ), patch(
                    "zevorinth.isolation._live_image_digest",
                    return_value="python@sha256:" + "a" * 64,
                ):
                    task = service.submit(
                        TaskRequest(
                            goal="run test_ok",
                            operation=None,
                            parameters={
                                "workspace": "demo",
                                "auto_provision_isolation": True,
                            },
                        )
                    )

                self.assertEqual(task.status, TaskStatus.FAILED)
                self.assertEqual(task.error.code, code)
                self.assertFalse(task.verification.passed)
                self.assertTrue(task.tool_result.success)
                event_types = [
                    event.event_type
                    for event in service.events(task.task_id)
                ]
                self.assertIn("tool_succeeded", event_types)
                self.assertNotIn("task_succeeded", event_types)

    def test_run_tests_failure_records_run_tests_tool_name(self) -> None:
        root, _ = _workspace(self)
        service, _, grant_token = authorized_service(
            root, workspace="demo", target="test_ok"
        )

        def failing_runner(**kwargs):
            del kwargs
            raise TestExecutionError("controlled_failure", "boom")

        service.test_runner = failing_runner
        task = service.submit(
            TaskRequest(
                goal="run test_ok",
                operation="run_tests",
                parameters={
                    "runner": "unittest",
                    "target": "test_ok",
                    "workspace": "demo",
                    "execution_backend": "host",
                    "host_authorization": grant_token,
                },
            )
        )

        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.error.code, "controlled_failure")
        self.assertEqual(task.tool_result.tool_name, "run_tests")

    def test_write_failure_records_write_tool_name(self) -> None:
        class FailingWriter:
            tool_name = "write_text_artifact"
            execution_mode = ExecutionMode.REAL

            def write(self, **kwargs):
                del kwargs
                raise ArtifactToolError(
                    "write_failed", "controlled write failure"
                )

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        service = TaskService(
            Path(temp_dir.name), writer=FailingWriter()
        )
        task = service.submit(
            TaskRequest(
                goal="write a file",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "result.txt",
                    "content": "hello\n",
                },
            )
        )

        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.error.code, "write_failed")
        self.assertEqual(task.tool_result.tool_name, "write_text_artifact")

    def test_missing_tool_context_uses_unknown_sentinel(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        service = TaskService(Path(temp_dir.name))
        queued = service.enqueue(
            TaskRequest(goal="placeholder", operation=None, parameters={})
        )
        claim = service._claim(
            queued.task_id, allowed=TaskService.RUNNABLE_STATUSES
        )
        self.assertTrue(claim.acquired)
        failed = service._finish_tool_failure(
            claim.task,
            claim.task.lease_token,
            "unknown_context",
            "no tool context supplied",
            service.clock(),
        )
        self.assertEqual(failed.tool_result.tool_name, "unknown_tool")

    def test_final_plan_mismatch_helper_detects_each_identity(self) -> None:
        task = SimpleNamespace(
            task_id="task-1",
            attempt_id="attempt-1",
            claim_generation=2,
            plan_version=1,
            execution_plan_version=1,
            execution_plan_snapshot={
                "kind": "run_tests",
                "task_id": "task-1",
                "attempt_id": "attempt-1",
                "claim_generation": 2,
                "plan_version": 1,
                "tool_name": "run_tests",
                "runner": "unittest",
                "target": "test_ok",
                "workspace": "demo",
                "evidence_id": "auto-1",
                "config_digest": "a" * 64,
                "command_digest": "b" * 64,
            },
        )
        evidence = SimpleNamespace(
            runner="unittest",
            target="test_ok",
            workspace="demo",
            attempt_id="attempt-1",
            claim_generation=2,
            evidence_id="auto-1",
            config_digest="a" * 64,
            command_digest="b" * 64,
        )
        self.assertIsNone(
            _test_execution_plan_mismatch(task, evidence)
        )
        for field in (
            "evidence_id",
            "config_digest",
            "command_digest",
            "target",
        ):
            changed = SimpleNamespace(**vars(evidence))
            setattr(changed, field, "different")
            self.assertEqual(
                _test_execution_plan_mismatch(task, changed),
                "execution_plan_snapshot_mismatch",
                field,
            )


if __name__ == "__main__":
    unittest.main()
