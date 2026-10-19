"""Pre-execution authorization and clarification rules."""

from __future__ import annotations

from pathlib import Path

from .artifact_tool import (
    MAX_ARTIFACT_BYTES,
    ArtifactToolError,
    validate_artifact_name,
)
from .models import Plan, PolicyDecision, TaskRequest
from .planner import WRITE_TEXT_ARTIFACT


SUPPORTED_OPERATIONS = frozenset({WRITE_TEXT_ARTIFACT})


def missing_required_fields(request: TaskRequest) -> list[str]:
    missing: list[str] = []
    if request.goal is None or not request.goal.strip():
        missing.append("goal")
    if request.operation is None or not request.operation.strip():
        missing.append("operation")
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
