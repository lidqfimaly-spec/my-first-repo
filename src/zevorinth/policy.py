"""Pre-execution authorization and clarification rules."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID

from .host_authorization import HostAuthorizationError

from .artifact_tool import (
    MAX_ARTIFACT_BYTES,
    ArtifactToolError,
    validate_artifact_name,
)
from .capabilities import SUPPORTED_TASK_OPERATIONS
from .models import Plan, PolicyDecision, TaskRequest
from .planner import (
    CONTROLLED_SHELL,
    EDIT_TEXT_FILE,
    INSPECT_WORKSPACE,
    MODIFY_TEXT_ARTIFACT,
    READ_TEXT_ARTIFACT,
    RESEARCH_REPORT,
    WRITE_TEXT_ARTIFACT,
)
from .multi_step import MULTI_STEP_OPERATION
from .local_test_runner import (
    RUN_TESTS_TOOL,
    TestExecutionError,
    resolve_workspace,
    validate_parameters,
)
from .shell_tool import ShellToolError, validate_shell_parameters
from .workspace_edit import (
    WorkspaceEditError,
    validate_edit_parameters,
    validate_edit_precondition,
)
from .workspace_inspection import (
    WorkspaceInspectionError,
    validate_inspection_parameters,
)
from .research_workflow import (
    ResearchWorkflowError,
    validate_research_parameters,
)


SUPPORTED_OPERATIONS = SUPPORTED_TASK_OPERATIONS


def missing_required_fields(request: TaskRequest) -> list[str]:
    missing: list[str] = []
    if request.goal is None or not request.goal.strip():
        missing.append("goal")
    if request.operation is None or not request.operation.strip():
        missing.append("operation")
    if request.operation == MULTI_STEP_OPERATION:
        if not isinstance(request.parameters.get("steps"), list):
            missing.append("steps")
        return missing
    if request.operation == RUN_TESTS_TOOL:
        for field in ("runner", "target", "workspace"):
            if request.parameters.get(field) is None:
                missing.append(field)
        return missing
    if request.operation == READ_TEXT_ARTIFACT:
        for field in ("source_task_id", "artifact_path"):
            if request.parameters.get(field) is None:
                missing.append(field)
        return missing
    if request.operation == MODIFY_TEXT_ARTIFACT:
        for field in (
            "source_task_id",
            "artifact_path",
            "content",
            "expected_sha256",
        ):
            if request.parameters.get(field) is None:
                missing.append(field)
        return missing
    if request.operation == INSPECT_WORKSPACE:
        if request.parameters.get("workspace") is None:
            missing.append("workspace")
        return missing
    if request.operation == EDIT_TEXT_FILE:
        for field in (
            "workspace",
            "path",
            "expected_sha256",
            "old_text",
            "new_text",
            "authorization",
        ):
            if request.parameters.get(field) is None:
                missing.append(field)
        return missing
    if request.operation == RESEARCH_REPORT:
        for field in (
            "workspace",
            "sources",
            "title",
            "output_path",
            "findings",
        ):
            if request.parameters.get(field) is None:
                missing.append(field)
        return missing
    if request.parameters.get("artifact_path") is None:
        missing.append("artifact_path")
    if request.parameters.get("content") is None:
        missing.append("content")
    return missing


class ArtifactPolicy:
    def __init__(self, host_grant_store: Any = None) -> None:
        self._host_grant_store_override = host_grant_store

    def _host_grant_store(self) -> Any:
        if self._host_grant_store_override is not None:
            return self._host_grant_store_override
        from .host_authorization import HostGrantStore

        return HostGrantStore()

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
        if request.operation == READ_TEXT_ARTIFACT:
            return self._evaluate_read(
                request=request,
                step_tool=step.tool_name,
            )
        if request.operation == MODIFY_TEXT_ARTIFACT:
            return self._evaluate_modify(
                request=request,
                step_tool=step.tool_name,
                task_id=task_id,
                artifacts_root=artifacts_root,
            )
        if request.operation == INSPECT_WORKSPACE:
            return self._evaluate_workspace_inspection(
                request=request,
                step_tool=step.tool_name,
                artifacts_root=artifacts_root,
            )
        if request.operation == EDIT_TEXT_FILE:
            return self._evaluate_workspace_edit(
                request=request,
                step_tool=step.tool_name,
                artifacts_root=artifacts_root,
            )
        if request.operation == RESEARCH_REPORT:
            return self._evaluate_research_report(
                request=request,
                plan=plan,
                step_tool=step.tool_name,
                task_id=task_id,
                artifacts_root=artifacts_root,
            )
        if request.operation == CONTROLLED_SHELL:
            return self._evaluate_controlled_shell(
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

    @staticmethod
    def _canonical_uuid(value: object, field: str) -> str:
        if not isinstance(value, str):
            raise ArtifactToolError(
                f"invalid_{field}", f"{field} must be a canonical UUID string"
            )
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError) as exc:
            raise ArtifactToolError(
                f"invalid_{field}", f"{field} must be a canonical UUID string"
            ) from exc
        if str(parsed) != value:
            raise ArtifactToolError(
                f"invalid_{field}", f"{field} must be a canonical UUID string"
            )
        return value

    @staticmethod
    def _sha256(value: object, field: str) -> str:
        if not isinstance(value, str) or len(value) != 64:
            raise ArtifactToolError(
                f"invalid_{field}", f"{field} must be a SHA-256 hex digest"
            )
        try:
            int(value, 16)
        except ValueError as exc:
            raise ArtifactToolError(
                f"invalid_{field}", f"{field} must be a SHA-256 hex digest"
            ) from exc
        return value.lower()

    def _evaluate_read(
        self,
        *,
        request: TaskRequest,
        step_tool: str,
    ) -> PolicyDecision:
        if step_tool != READ_TEXT_ARTIFACT:
            return PolicyDecision(
                allowed=False,
                code="unsupported_tool",
                reason=f"tool is not allowed: {step_tool!r}",
            )
        try:
            self._canonical_uuid(
                request.parameters.get("source_task_id"), "source_task_id"
            )
            validate_artifact_name(request.parameters.get("artifact_path"))
            expected = request.parameters.get("expected_sha256")
            if expected is not None:
                self._sha256(expected, "expected_sha256")
        except ArtifactToolError as exc:
            return PolicyDecision(False, exc.code, exc.message)
        return PolicyDecision(
            allowed=True,
            code="allowed",
            reason="bounded read from verified source artifact",
        )

    def _evaluate_modify(
        self,
        *,
        request: TaskRequest,
        step_tool: str,
        task_id: str,
        artifacts_root: Path,
    ) -> PolicyDecision:
        if step_tool != MODIFY_TEXT_ARTIFACT:
            return PolicyDecision(
                allowed=False,
                code="unsupported_tool",
                reason=f"tool is not allowed: {step_tool!r}",
            )
        parameters = request.parameters
        try:
            self._canonical_uuid(
                parameters.get("source_task_id"), "source_task_id"
            )
            validate_artifact_name(parameters.get("artifact_path"))
            output_path = parameters.get(
                "output_path", parameters.get("artifact_path")
            )
            validate_artifact_name(output_path)
            self._sha256(parameters.get("expected_sha256"), "expected_sha256")
            mode = parameters.get("mode", "replace")
            if mode not in {"replace", "append"}:
                raise ArtifactToolError(
                    "invalid_modify_mode",
                    "mode must be replace or append",
                )
            content = parameters.get("content")
            if not isinstance(content, str):
                raise ArtifactToolError(
                    "invalid_content", "content must be a string"
                )
            if not content:
                raise ArtifactToolError(
                    "empty_content", "content must not be empty"
                )
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > MAX_ARTIFACT_BYTES:
                raise ArtifactToolError(
                    "content_too_large",
                    f"content exceeds the {MAX_ARTIFACT_BYTES} byte limit",
                )
        except UnicodeEncodeError:
            return PolicyDecision(
                False, "invalid_content", "content is not valid UTF-8 text"
            )
        except ArtifactToolError as exc:
            return PolicyDecision(False, exc.code, exc.message)

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
            target = resolved_task_dir / str(output_path)
            if target.exists() or target.is_symlink():
                return PolicyDecision(
                    False,
                    "artifact_exists",
                    "refusing to overwrite an existing artifact",
                )
        return PolicyDecision(
            allowed=True,
            code="allowed",
            reason="bounded new-version artifact modification is authorized",
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
            workspace = resolve_workspace(
                artifacts_root.parent, parameters["workspace"]
            )
        except TestExecutionError as exc:
            return PolicyDecision(False, exc.code, exc.message)
        top = parameters["target"].split(".")[0]
        module = workspace / f"{top}.py"
        package = workspace / top
        if not module.is_file() and not package.is_dir():
            # "Cannot look" is not "is not there": an unreadable workspace must
            # never be reported as a missing test target, or the user will go
            # hunting for a file that still exists.
            try:
                os.listdir(workspace)
            except OSError as exc:
                return PolicyDecision(
                    allowed=False,
                    code="workspace_unreadable",
                    reason=(
                        f"workspace {parameters['workspace']!r} could not be"
                        f" read: {type(exc).__name__}"
                    ),
                )
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
        # Host execution needs a usable grant.  This is a preview only: it
        # never consumes, so a task that is refused here keeps its grant for
        # the caller to retry with the corrected scope.  The authoritative,
        # atomic single-use check happens in ``run_unittest`` immediately
        # before the process starts.
        if parameters["execution_backend"] == "host":
            try:
                self._host_grant_store().verify(
                    parameters["host_authorization"],
                    workspace=parameters["workspace"],
                    target=parameters["target"],
                )
            except HostAuthorizationError as exc:
                return PolicyDecision(False, exc.code, exc.message)
        return PolicyDecision(
            allowed=True,
            code="allowed",
            reason=(
                f"bounded {parameters['runner']} target "
                f"{parameters['target']} in {parameters['workspace']}"
            ),
        )

    def _evaluate_workspace_inspection(
        self,
        *,
        request: TaskRequest,
        step_tool: str,
        artifacts_root: Path,
    ) -> PolicyDecision:
        if step_tool != INSPECT_WORKSPACE:
            return PolicyDecision(
                False,
                "unsupported_tool",
                f"tool is not allowed: {step_tool!r}",
            )
        try:
            values = validate_inspection_parameters(
                dict(request.parameters)
            )
            resolve_workspace(artifacts_root.parent, values["workspace"])
        except (WorkspaceInspectionError, TestExecutionError) as exc:
            return PolicyDecision(False, exc.code, exc.message)
        return PolicyDecision(
            True,
            "allowed",
            "bounded read-only workspace inspection is authorized",
        )

    def _evaluate_workspace_edit(
        self,
        *,
        request: TaskRequest,
        step_tool: str,
        artifacts_root: Path,
    ) -> PolicyDecision:
        if step_tool != EDIT_TEXT_FILE:
            return PolicyDecision(
                False,
                "unsupported_tool",
                f"tool is not allowed: {step_tool!r}",
            )
        try:
            values = validate_edit_parameters(dict(request.parameters))
            resolve_workspace(artifacts_root.parent, values["workspace"])
            validate_edit_precondition(
                artifacts_root.parent,
                dict(request.parameters),
            )
        except (WorkspaceEditError, TestExecutionError) as exc:
            return PolicyDecision(False, exc.code, exc.message)
        return PolicyDecision(
            True,
            "allowed",
            "authorized exact-text workspace edit is ready",
        )

    def _evaluate_research_report(
        self,
        *,
        request: TaskRequest,
        plan: Plan,
        step_tool: str,
        task_id: str,
        artifacts_root: Path,
    ) -> PolicyDecision:
        if step_tool != RESEARCH_REPORT:
            return PolicyDecision(
                False,
                "unsupported_tool",
                f"tool is not allowed: {step_tool!r}",
            )
        try:
            values = validate_research_parameters(
                artifacts_root.parent,
                dict(request.parameters),
            )
            output_path = validate_artifact_name(values["output_path"])
        except (ResearchWorkflowError, ArtifactToolError) as exc:
            return PolicyDecision(False, exc.code, exc.message)
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
            target = resolved_task_dir / output_path
            if target.exists() or target.is_symlink():
                return PolicyDecision(
                    False,
                    "artifact_exists",
                    "refusing to overwrite an existing artifact",
                )
        return PolicyDecision(
            True,
            "allowed",
            "local source-preserving research report is authorized",
        )

    def _evaluate_controlled_shell(
        self,
        *,
        request: TaskRequest,
        step_tool: str,
        artifacts_root: Path,
    ) -> PolicyDecision:
        if step_tool != CONTROLLED_SHELL:
            return PolicyDecision(
                False,
                "unsupported_tool",
                f"tool is not allowed: {step_tool!r}",
            )
        workspace_root = artifacts_root.parent
        try:
            validate_shell_parameters(
                dict(request.parameters),
                workspace_root=workspace_root,
            )
        except ShellToolError as exc:
            return PolicyDecision(False, exc.code, exc.message)
        return PolicyDecision(
            True,
            "allowed",
            "controlled shell command is whitelisted and within bounds",
        )
