"""What a human is actually approving, projected for a review form (F-11).

The control centre used to offer ``approve_step`` as a bare checkbox next to a
step id. That is a consent ritual, not consent: the person clicking it could
not see the tool, the parameters, or what the step would touch. This module
projects exactly the facts needed to say yes or no, and nothing invented.

Two properties are load-bearing:

* **Read-only.** Every observation here opens files for reading or hashes
  them; nothing is created, locked, or repaired.
* **Fail-open on description, fail-closed on consent.** If a fact cannot be
  established (workspace missing, unreadable file), the projection says
  ``unknown`` with a reason. It never guesses, and it never turns an unknown
  into an implicit "looks fine".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .multi_step import _iter_references, step_approval_digest
from .models import PlanStep, Task


APPROVAL_FORM_VERSION = "approval-form-v1"
MAX_PARAMETER_TEXT = 4000
MAX_OBSERVED_FILE_BYTES = 4 * 1024 * 1024

TOOL_LABELS = {
    "write_text_artifact": "写一个新文件",
    "modify_text_artifact": "改写一个已有文件",
    "read_text_artifact": "读取一个文件的内容",
    "run_tests": "在指定的工作区跑测试",
    "inspect_workspace": "查看工作区里的文件",
    "edit_text_file": "直接改写工作区里的源码文件",
    "research_report": "整理一份调研报告并写成文件",
}

SIDE_EFFECTS = {
    "write_text_artifact": "creates_artifact",
    "research_report": "creates_artifact",
    "modify_text_artifact": "modifies_artifact",
    "edit_text_file": "modifies_source_file",
    "run_tests": "runs_code",
    "read_text_artifact": "reads_only",
    "inspect_workspace": "reads_only",
}

SIDE_EFFECT_SENTENCES = {
    "creates_artifact": "会新建一个文件，不会改动已有文件。",
    "modifies_artifact": "会改写一个已有文件的内容。",
    "modifies_source_file": "会直接改写工作区里的源码文件。",
    "runs_code": "会在隔离的工作区里运行代码，不写产物文件。",
    "reads_only": "只读，不产生任何写入。",
}

_TOOL_UNKNOWN = "这个工具没有中文说明"
_SIDE_EFFECT_UNKNOWN = "说不清这步会动什么，请先不要批准。"


def _bounded(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _parameter_value(
    value: Any,
    redact: Callable[[str], str],
) -> tuple[str, bool]:
    """Return (display text, whether it is a deferred prior-step reference).

    A deferred parameter is not a value yet: it resolves from an earlier
    step's real output at execution time. Saying so is the difference between
    "you approved this text" and "you approved whatever step 1 produces".
    """
    deferred = bool(_iter_references(value))
    if isinstance(value, str):
        return _bounded(redact(value), MAX_PARAMETER_TEXT), deferred
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return "<无法展示的值>", deferred
    return _bounded(redact(encoded), MAX_PARAMETER_TEXT), deferred


def _parameter_rows(
    parameters: Mapping[str, Any],
    redact: Callable[[str], str],
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    deferred: list[str] = []
    for name in sorted(parameters):
        text, is_deferred = _parameter_value(parameters[name], redact)
        rows.append(
            {
                "name": name,
                "value": text,
                "deferred": is_deferred,
            }
        )
        if is_deferred:
            deferred.append(name)
    return rows, deferred


def _sha256_file(path: Path) -> str | None:
    """Digest a regular file without following symlinks out of the tree."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_OBSERVED_FILE_BYTES:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _unknown_target(path: str, kind: str, reason: str) -> dict[str, Any]:
    return {
        "path": path,
        "kind": kind,
        "state": "unknown",
        "sentence": f"现在没法确认这个文件的状态（{reason}），批准前请自行核对。",
        "current_sha256": None,
        "expected_sha256": None,
    }


def _artifact_write_target(
    task: Task,
    project_root: Path,
    artifact_path: Any,
) -> dict[str, Any] | None:
    if not isinstance(artifact_path, str) or not artifact_path:
        return None
    target = project_root / "artifacts" / task.task_id / artifact_path
    if target.exists():
        return {
            "path": artifact_path,
            "kind": "artifact",
            "state": "occupied",
            "sentence": (
                "这个名字的产物文件已经存在，而写入不会覆盖已有文件——"
                "照现在的参数，这步会失败。"
            ),
            "current_sha256": _sha256_file(target),
            "expected_sha256": None,
        }
    return {
        "path": artifact_path,
        "kind": "artifact",
        "state": "absent",
        "sentence": "这步会新建这个文件，现在它还不存在。",
        "current_sha256": None,
        "expected_sha256": None,
    }


def _pinned_digest_target(
    path: str,
    *,
    kind: str,
    absolute: Path | None,
    expected_sha256: Any,
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    expected = expected_sha256 if isinstance(expected_sha256, str) else None
    if absolute is None:
        return _unknown_target(path, kind, unavailable_reason or "路径未登记")
    current = _sha256_file(absolute)
    if current is None:
        if not absolute.exists():
            return {
                "path": path,
                "kind": kind,
                "state": "missing",
                "sentence": "计划里要改的这个文件现在不存在，这步会失败。",
                "current_sha256": None,
                "expected_sha256": expected,
            }
        return _unknown_target(path, kind, "文件读不了或不是普通文件")
    if expected is None:
        return _unknown_target(path, kind, "计划里没有记录预期摘要")
    if current == expected:
        return {
            "path": path,
            "kind": kind,
            "state": "present_matching",
            "sentence": "文件现在的内容和计划时记录的摘要一致，改动前提成立。",
            "current_sha256": current,
            "expected_sha256": expected,
        }
    return {
        "path": path,
        "kind": kind,
        "state": "present_changed",
        "sentence": (
            "文件现在的内容和计划时记录的摘要不一致——"
            "计划是在旧内容上做的，照现在这样执行会被拒绝。"
        ),
        "current_sha256": current,
        "expected_sha256": expected,
    }


def _target_for_step(
    task: Task,
    step: PlanStep,
    project_root: Path,
) -> dict[str, Any] | None:
    parameters = step.parameters
    if step.tool_name == "write_text_artifact":
        return _artifact_write_target(
            task, project_root, parameters.get("artifact_path")
        )
    if step.tool_name == "research_report":
        return _artifact_write_target(
            task, project_root, parameters.get("output_path")
        )
    if step.tool_name == "modify_text_artifact":
        source_task_id = parameters.get("source_task_id")
        artifact_path = parameters.get("artifact_path")
        if not isinstance(source_task_id, str) or not isinstance(
            artifact_path, str
        ):
            return None
        absolute = (
            project_root / "artifacts" / source_task_id / artifact_path
        )
        return _pinned_digest_target(
            artifact_path,
            kind="source_artifact",
            absolute=absolute,
            expected_sha256=parameters.get("expected_sha256"),
        )
    if step.tool_name == "edit_text_file":
        workspace = parameters.get("workspace")
        relative = parameters.get("path")
        if not isinstance(workspace, str) or not isinstance(relative, str):
            return None
        base = project_root / "workspaces" / workspace
        reason: str | None = None
        absolute: Path | None = base / relative
        if base.is_symlink() or not base.is_dir():
            absolute = None
            reason = "工作区没有登记"
        else:
            try:
                resolved = absolute.resolve()
                root = base.resolve()
                if root not in resolved.parents:
                    absolute = None
                    reason = "路径跑出了工作区"
            except OSError:
                absolute = None
                reason = "路径无法解析"
        return _pinned_digest_target(
            f"{workspace}/{relative}",
            kind="workspace_file",
            absolute=absolute,
            expected_sha256=parameters.get("expected_sha256"),
            unavailable_reason=reason,
        )
    return None


def _approval_state(task: Task, step: PlanStep) -> dict[str, Any]:
    plan_id = task.plan.plan_id if task.plan is not None else None
    digest = (
        step_approval_digest(step, plan_id) if plan_id is not None else None
    )
    recorded = (task.approved_step_digests or {}).get(step.step_id)
    if step.step_id not in task.approved_step_ids:
        state = "approval_required"
        label = "这步需要你确认后才会执行。"
    elif recorded != digest:
        state = "approval_content_changed"
        label = (
            "这步之前批准过，但内容已经变了——旧批准不覆盖新内容，"
            "需要你重新确认。"
        )
    else:
        state = "approved_matching"
        label = "这步的当前内容和已批准的内容一致。"
    return {
        "required": True,
        "state": state,
        "state_label": label,
        "digest": digest,
        "approved_digest": recorded,
    }


def build_approval_form(
    task: Task,
    *,
    project_root: Path,
    redact: Callable[[str], str] | None = None,
) -> dict[str, Any] | None:
    """Project the step awaiting approval, or ``None`` when none is pending."""
    step_id = task.pending_approval_step_id
    if not step_id or task.plan is None:
        return None
    step = next(
        (item for item in task.plan.steps if item.step_id == step_id),
        None,
    )
    if step is None:
        return None
    redactor = redact or (lambda value: value)
    side_effect = SIDE_EFFECTS.get(step.tool_name, "unknown")
    rows, deferred = _parameter_rows(step.parameters, redactor)
    return {
        "contract_version": APPROVAL_FORM_VERSION,
        "task_id": task.task_id,
        "plan_version": task.plan_version,
        "step": {
            "step_id": step.step_id,
            "tool_name": step.tool_name,
            "tool_label": TOOL_LABELS.get(step.tool_name, _TOOL_UNKNOWN),
            "expected_result": _bounded(
                redactor(step.expected_result or ""), 600
            ),
            "depends_on": sorted(step.depends_on),
            "side_effect": side_effect,
            "side_effect_sentence": SIDE_EFFECT_SENTENCES.get(
                side_effect, _SIDE_EFFECT_UNKNOWN
            ),
            "parameters": rows,
            "parameters_truncated": any(
                len(row["value"]) >= MAX_PARAMETER_TEXT for row in rows
            ),
            "deferred_parameters": deferred,
        },
        "target": _target_for_step(task, step, Path(project_root)),
        "approval": _approval_state(task, step),
        "read_only": True,
    }
