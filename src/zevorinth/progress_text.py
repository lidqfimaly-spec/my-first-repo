"""Human-readable progress derived from a task's own event sequence (F-16).

Design rule: this module **reads only**. It never writes an event, never
transitions a task, and never infers a state the engine has not recorded.
Every string it produces must be traceable to a concrete event or to
``task.plan.steps`` being non-empty.

Why not a percentage: the engine has no notion of "total work remaining"
— a step can take 5 ms or 5 minutes, and a real percentage would be a
fabricated number. The honest equivalent is "step n of m + what is happening
right now", which is what ``text`` says.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# Tool/operation internal names -> user language. Kept in sync with
# capabilities.SUPPORTED_TASK_OPERATIONS; unknown names fall back to a
# generic phrase rather than leaking the raw identifier.
TOOL_LABELS: dict[str, str] = {
    "write_text_artifact": "写入文件",
    "read_text_artifact": "读取文件",
    "modify_text_artifact": "修改文件",
    "edit_text_file": "精确修改代码",
    "inspect_workspace": "检查工作区",
    "research_report": "生成研究报告",
    "run_tests": "运行测试",
    "controlled_shell": "执行受限命令",
    "execute_plan": "执行多步计划",
    "multi_step": "执行多步计划",
}

STATUS_LABELS: dict[str, str] = {
    "queued": "排队中",
    "running": "正在执行",
    "paused": "已暂停",
    "awaiting_approval": "等待你审批",
    "awaiting_clarification": "等你补充信息",
    "blocked": "被阻塞",
    "verifying": "正在核对结果",
    "succeeded": "已完成",
    "partial": "部分完成",
    "failed": "失败",
    "planning_failed": "没能生成计划",
    "interrupted": "已中断",
    "cancelled": "已取消",
    "simulated": "仅模拟（未真正执行）",
}

_TERMINAL = frozenset(
    {
        "succeeded",
        "partial",
        "failed",
        "planning_failed",
        "interrupted",
        "cancelled",
        "simulated",
    }
)

# Event -> one sentence. Only events that mean something to a user appear here;
# bookkeeping events (lease_renewed, staging_cleaned, ...) stay silent.
EVENT_TEXT: dict[str, str] = {
    "task_created": "已创建任务",
    "planning_started": "正在理解你的目标",
    "planning_completed": "已生成执行计划",
    "task_planned": "已生成执行计划",
    "planning_clarification": "需要你补充一些信息",
    "clarification_required": "需要你补充一些信息",
    "clarification_updated": "已记录你的补充",
    "planning_rejected": "这个目标不符合可执行范围",
    "planning_failed": "没能生成计划",
    "task_claimed": "开始执行",
    "policy_rejected": "在执行前被安全检查拦下",
    "step_started": "开始执行一个步骤",
    "step_completed": "一个步骤已完成",
    "step_failed": "一个步骤失败",
    "step_skipped": "一个步骤被跳过",
    "step_approval_required": "有一个步骤需要你审批",
    "step_approved": "已审批",
    "steps_replanned": "计划已调整",
    "verification_started": "正在独立核对结果",
    "verification_completed": "核对完成",
    "task_succeeded": "任务完成",
    "task_partial": "任务部分完成",
    "task_failed": "任务失败",
    "task_cancelled": "任务已取消",
    "task_interrupted": "任务被中断",
    "task_paused": "任务已暂停",
    "pause_requested": "已请求暂停",
    "cancel_requested": "已请求取消，会在下一个安全点停下",
    "task_resumed": "任务已恢复",
    "task_note_added": "已添加备注",
    "plan_revised": "计划已修改",
}


# Known error codes -> what the user should do. A policy rejection is reported
# by the engine as BLOCKED (not FAILED), because it is recoverable with a
# different input; the message therefore has to explain *what to change*.
ERROR_TEXT: dict[str, str] = {
    "invalid_artifact_path": "文件名不能是相对路径或隐藏文件名",
    "unsupported_operation": "这个操作当前不支持",
    "text_match_count": "要替换的那段文字在文件里不是恰好出现一次",
    "source_file_changed": "源文件在你提交之后被改动过，需要先重新读取",
    "missing_parameter": "还缺少一个必要参数",
    "artifact_exists": "目标文件已存在，请换一个名字",
    "unauthorized_edit": "这次修改没有授权，被拦下了",
    "plan_step_limit": "步骤太多，超出了单次任务上限",
    "workspace_pattern": "工作区路径不符合可访问范围",
}


def error_text(code: str | None) -> str | None:
    if not code:
        return None
    return ERROR_TEXT.get(code)


def tool_label(name: str | None) -> str:
    if not name:
        return "一个步骤"
    return TOOL_LABELS.get(name, "一个步骤")


def status_label(status: str | None) -> str:
    if not status:
        return "未知状态"
    return STATUS_LABELS.get(status, status)


def _event_field(event: Any, key: str) -> Any:
    if isinstance(event, Mapping):
        return event.get(key)
    return getattr(event, key, None)


def _payload(event: Any) -> Mapping[str, Any]:
    value = _event_field(event, "payload")
    return value if isinstance(value, Mapping) else {}


def describe_progress(
    task: Any,
    events: Iterable[Any],
    *,
    max_events: int = 400,
) -> dict[str, Any]:
    """Build a user-facing progress view for one task.

    ``task`` is a ``models.Task``; ``events`` is the ordered event list from
    ``EventStore.list_events``. Both are only read.
    """
    event_list = list(events)
    truncated = len(event_list) > max_events
    if truncated:
        event_list = event_list[-max_events:]

    status_value = getattr(task, "status", None)
    status_value = getattr(status_value, "value", status_value) or "unknown"

    # Step accounting comes from STEP_* events, which the engine emits per
    # plan step. Total comes from the persisted plan, not from counting events,
    # so a not-yet-started plan still shows "0 of 3".
    plan = getattr(task, "plan", None)
    plan_steps = list(getattr(plan, "steps", []) or []) if plan else []
    steps_total = len(plan_steps)

    started: list[str] = []
    completed: list[str] = []
    failed: list[str] = []
    current_label: str | None = None
    current_step: str | None = None
    tool_succeeded = 0
    tool_failed = 0

    for event in event_list:
        kind = _event_field(event, "event_type")
        payload = _payload(event)
        step_id = payload.get("step_id")
        if kind == "step_started":
            if isinstance(step_id, str):
                started.append(step_id)
            current_step = step_id if isinstance(step_id, str) else None
            current_label = tool_label(payload.get("tool_name"))
        elif kind == "step_completed":
            if isinstance(step_id, str) and step_id not in completed:
                completed.append(step_id)
        elif kind == "step_failed":
            if isinstance(step_id, str) and step_id not in failed:
                failed.append(step_id)
        elif kind == "tool_succeeded":
            tool_succeeded += 1
            current_step = None
            current_label = None
        elif kind == "tool_failed":
            tool_failed += 1
        elif kind in {"tool_started"}:
            current_label = tool_label(payload.get("tool_name"))
        elif kind in {
            "verification_started",
            "task_succeeded",
            "task_failed",
            "task_partial",
            "task_cancelled",
            "task_interrupted",
        }:
            current_step = None
            current_label = None

    # Single-step tasks never emit STEP_* events: the engine goes straight to
    # TOOL_STARTED / TOOL_SUCCEEDED. Accounting must therefore fall back to
    # tool-level events, otherwise every single-step task would forever read
    # "0 of 1" even after it succeeded.
    step_events_seen = bool(started or completed or failed)
    if not step_events_seen and steps_total:
        steps_done = min(steps_total, tool_succeeded)
        failed_steps = 1 if tool_failed and not tool_succeeded else 0
    else:
        steps_done = len(completed)
        failed_steps = len(failed)

    if steps_total and steps_done > steps_total:
        # Defensive: never report n > m. If this ever triggers, the event log
        # and the plan disagree and the caller should be able to see it.
        steps_done = steps_total

    last_event = event_list[-1] if event_list else None
    last_kind = _event_field(last_event, "event_type") if last_event else None
    last_text = EVENT_TEXT.get(str(last_kind), "") if last_kind else ""

    phase = getattr(task, "phase", None)
    cancel_requested = getattr(task, "cancel_requested_at", None) is not None

    # The headline sentence. Order matters: an in-flight step beats a stale
    # terminal-looking phrase, and a terminal status beats everything.
    error = getattr(task, "error", None)
    code = getattr(error, "code", None) if error is not None else None
    detail = error_text(code)

    if status_value in _TERMINAL:
        text = status_label(status_value)
        if code and status_value in {
            "failed",
            "planning_failed",
            "interrupted",
        }:
            text = f"{text}：{detail}" if detail else f"{text}（{code}）"
    elif status_value == "blocked":
        # Policy rejections land here, not in FAILED: the engine keeps the task
        # resumable with a corrected input.
        text = (
            f"被执行前的安全检查拦下：{detail}"
            if detail
            else f"被执行前的安全检查拦下（{code}）"
            if code
            else "被阻塞，需要调整后才能继续"
        )
    elif status_value == "awaiting_approval":
        text = "有一步需要你审批后才能继续"
    elif status_value == "awaiting_clarification":
        text = "需要你补充信息才能继续"
    elif status_value == "paused":
        text = "已暂停，恢复后从暂停点继续"
    elif current_label:
        if steps_total:
            text = (
                f"正在{current_label}"
                f"（第 {min(steps_done + 1, steps_total)}/{steps_total} 步）"
            )
        else:
            text = f"正在{current_label}"
    elif steps_total:
        text = f"已完成 {steps_done}/{steps_total} 步"
    else:
        text = status_label(status_value)

    if cancel_requested and status_value not in _TERMINAL:
        text = f"{text} · 已请求取消，将在下一个安全点停止"

    return {
        "task_id": getattr(task, "task_id", None),
        "status": status_value,
        "status_text": status_label(status_value),
        "phase": phase,
        "terminal": status_value in _TERMINAL,
        "cancel_requested": cancel_requested,
        "steps_total": steps_total,
        "steps_done": steps_done,
        "steps_failed": failed_steps,
        "current_step_id": current_step,
        "current_tool_label": current_label,
        "last_event": {
            "type": last_kind,
            "at": _event_field(last_event, "occurred_at") if last_event else None,
            "text": last_text,
        },
        "text": text,
        "events_truncated": truncated,
    }
