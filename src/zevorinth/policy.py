"""Pre-execution authorization and clarification rules."""

from __future__ import annotations

from pathlib import Path

from .artifact_tool import (
    MAX_ARTIFACT_BYTES,
    ArtifactToolError,
    validate_artifact_name,
)
from .capabilities import SUPPORTED_TASK_OPERATIONS
from .models import Plan, PolicyDecision, TaskRequest
from .planner import WRITE_TEXT_ARTIFACT
from .local_test_runner import (
    RUN_TESTS_TOOL,
    TestExecutionError,
    resolve_workspace,
    validate_parameters,
)


SUPPORTED_OPERATIONS = SUPPORTED_TASK_OPERATIONS

#: Exact token a caller must present to use the non-isolated host backend.
HOST_AUTHORIZATION_TOKEN = "local-maintenance"


def missing_required_fields(request: TaskRequest) -> list[str]:
    missing: list[str] = []
    if request.goal is None or not request.goal.strip():
        missing.append("goal")
    if request.operation is None or not request.operation.strip():
        missing.append("operation")
    if request.operation == RUN_TESTS_TOOL:
        for field in ("runner", "target", "workspace"):
            if request.parameters.get(field) is None:
                missing.append(field)
        return missing
    if request.parameters.get("artifact_path") is None:
        missing.append("artifact_path")
    if request.parameters.get("content") is None:
        missing.append("content")
    return missing


class ArtifactPolicy:
    def evaluate(
        self,
        *,
        request: TaskRequest,
        plan: Plan,
        task_id: str,
        artifacts_root: Path,
    ) -> PolicyDecision:
        if request.operation not in SUPPORTED_OPERATIONS:
            return PolicyDecision(
                allowed=False,
                code="unsupported_operation",
                reason=f"operation is not supported: {request.operation!r}",
            )
        if len(plan.steps) != 1:
            return PolicyDecision(
                allowed=False,
                code="unbounded_plan",
                reason="Phase 1 permits exactly one plan step",
            )
        step = plan.steps[0]
        if request.operation == RUN_TESTS_TOOL:
            return self._evaluate_tests(
                request=request,
                step_tool=step.tool_name,
                artifacts_root=artifacts_root,
            )
        if step.tool_name != WRITE_TEXT_ARTIFACT:
            return PolicyDecision(
                allowed=False,
                code="unsupported_tool",
                reason=f"tool is not allowed: {step.tool_name!r}",
            )

        raw_path = request.parameters.get("artifact_path")
        try:
            artifact_name = validate_artifact_name(raw_path)
        except ArtifactToolError as exc:
            return PolicyDecision(False, exc.code, exc.message)

        content = request.parameters.get("content")
        if not isinstance(content, str):
            return PolicyDecision(
                False, "invalid_content", "content must be a string"
            )
        if not content:
            return PolicyDecision(False, "empty_content", "content must not be empty")
        try:
            content_bytes = content.encode("utf-8")
        except UnicodeEncodeError:
            return PolicyDecision(
                False, "invalid_content", "content is not valid UTF-8 text"
            )
        if len(content_bytes) > MAX_ARTIFACT_BYTES:
            return PolicyDecision(
                False,
                "content_too_large",
                f"content exceeds the {MAX_ARTIFACT_BYTES} byte limit",
            )

        root = artifacts_root.absolute()
        if root.is_symlink():
            return PolicyDecision(
                False,
                "unsafe_artifacts_root",
                "artifacts root must not be a symlink",
            )
        root = root.resolve()
        task_dir = root / task_id
        if task_dir.exists() or task_dir.is_symlink():
            try:
                resolved_task_dir = task_dir.resolve(strict=True)
            except OSError:
                return PolicyDecision(
                    False,
                    "unsafe_artifact_directory",
                    "task artifact directory cannot be resolved safely",
                )
            if (
                resolved_task_dir.parent != root
                or task_dir.is_symlink()
                or not resolved_task_dir.is_dir()
            ):
                return PolicyDecision(
                    False,
                    "unsafe_artifact_directory",
                    "task artifact path is not a safe directory inside artifacts root",
                )
            target = resolved_task_dir / artifact_name
            if target.exists() or target.is_symlink():
                return PolicyDecision(
                    False,
                    "artifact_exists",
                    "refusing to overwrite an existing artifact",
                )

        return PolicyDecision(
            allowed=True,
            code="allowed",
            reason="single-step artifact creation is authorized",
        )

    def _evaluate_tests(
        self,
        *,
        request: TaskRequest,
        step_tool: str,
        artifacts_root: Path,
    ) -> PolicyDecision:
        if step_tool != RUN_TESTS_TOOL:
            return PolicyDecision(
                allowed=False,
                code="unsupported_tool",
                reason=f"tool is not allowed: {step_tool!r}",
            )
        try:
            parameters = validate_parameters(dict(request.parameters))
            if parameters.get("execution_backend") == "host" and (
                request.parameters.get("host_authorization")
                != HOST_AUTHORIZATION_TOKEN
            ):
                return PolicyDecision(
                    False,
                    "host_execution_not_authorized",
                    "host execution requires explicit local-maintenance "
                    "authorization",
                )
            workspace = resolve_workspace(
                artifacts_root.parent, parameters["workspace"]
            )
        except TestExecutionError as exc:
            return PolicyDecision(False, exc.code, exc.message)
        top = parameters["target"].split(".")[0]
        module = workspace / f"{top}.py"
        package = workspace / top
        if not module.is_file() and not package.is_dir():
            return PolicyDecision(
                allowed=False,
                code="test_target_missing",
                reason=(
                    f"test target {parameters['target']!r} is not present in "
                    f"workspace {parameters['workspace']!r}"
                ),
            )
        for candidate in (module, package):
            if candidate.exists():
                resolved = candidate.resolve()
                if workspace not in resolved.parents and resolved != workspace:
                    return PolicyDecision(
                        allowed=False,
                        code="unsafe_test_target",
                        reason=(
                            "test target resolves outside the approved "
                            "workspace"
                        ),
                    )
        return PolicyDecision(
            allowed=True,
            code="allowed",
            reason=(
                f"bounded {parameters['runner']} target "
                f"{parameters['target']} in {parameters['workspace']}"
            ),
        )
