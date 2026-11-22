"""Bounded goal understanding: turn a user sentence into a task request.

Scope and honesty
-----------------
This is a *deterministic extractor*, not a model. It recognises a small,
explicitly enumerated set of phrasings in Chinese and English and fills the
same structured fields the planner already consumes. Anything it cannot
recognise stays ``None`` and becomes a clarification — it never guesses a
filename or invents content.

Why not an LLM: the product path has no model today (see R-01), and adding one
would require deciding vendor, privacy boundary and cost (open decision D-1).
The extractor is the part that is unambiguously correct regardless of that
decision, and its interface (``extract_goal``) is where an LLM provider can be
plugged in later without touching the conversation layer.

Rules of engagement
-------------------
* Explicitly supplied fields always win over extraction. The extractor only
  fills gaps.
* A recognised operation without a filename is still a useful step: the task
  is created and only the missing field is asked for, in user language.
* Everything returned is derived from the input text; ``reason`` records the
  matched cue so a reviewer can falsify the extraction by reading it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# Canonical operation names come from capabilities.SUPPORTED_TASK_OPERATIONS.
# This table only maps *user language* onto them.
OPERATION_ALIASES: dict[str, str] = {
    # write
    "写文件": "write_text_artifact",
    "写入": "write_text_artifact",
    "写": "write_text_artifact",
    "保存": "write_text_artifact",
    "存成": "write_text_artifact",
    "新建": "write_text_artifact",
    "创建文件": "write_text_artifact",
    "创建": "write_text_artifact",
    "write": "write_text_artifact",
    "save": "write_text_artifact",
    "create": "write_text_artifact",
    # read
    "读取": "read_text_artifact",
    "读": "read_text_artifact",
    "打开": "read_text_artifact",
    "看一下": "read_text_artifact",
    "查看": "read_text_artifact",
    "显示": "read_text_artifact",
    "read": "read_text_artifact",
    "show": "read_text_artifact",
    "cat": "read_text_artifact",
    # modify (append / in-place edit with exact match)
    "修改": "modify_text_artifact",
    "追加": "modify_text_artifact",
    "改一下": "modify_text_artifact",
    "modify": "modify_text_artifact",
    "append": "modify_text_artifact",
    # edit code
    "改代码": "edit_text_file",
    "修复": "edit_text_file",
    "修 bug": "edit_text_file",
    "改 bug": "edit_text_file",
    "edit code": "edit_text_file",
    "fix": "edit_text_file",
    # inspect
    "检查工作区": "inspect_workspace",
    "看看项目结构": "inspect_workspace",
    "列出文件": "inspect_workspace",
    "工作区": "inspect_workspace",
    "inspect": "inspect_workspace",
    "ls": "inspect_workspace",
    # tests
    "运行测试": "run_tests",
    "跑测试": "run_tests",
    "执行测试": "run_tests",
    "run tests": "run_tests",
    "pytest": "run_tests",
    # research report
    "研究报告": "research_report",
    "写一份报告": "research_report",
    "生成报告": "research_report",
    "总结": "research_report",
    "research": "research_report",
    "report": "research_report",
}

# Ordered strongest-first. The first matching pattern decides the operation,
# because e.g. "运行测试" contains "测试" but not "写入".
_OPERATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (operation, re.compile(pattern, re.IGNORECASE))
    for operation, pattern in (
        ("run_tests", r"运行\s*测试|跑(一下)?\s*测试|执行\s*测试|run\s+tests?|pytest"),
        (
            "inspect_workspace",
            r"检查\s*工作区|看看?\s*(项目)?\s*(结构|目录)|列出\s*(所有)?\s*文件|"
            r"工作区.{0,6}(有什么|结构)|inspect\s+workspace|\bls\b",
        ),
        (
            "edit_text_file",
            r"(改|修复|修|fix)\s*.{0,20}(bug|缺陷|问题|代码)|精确\s*修改|edit\s+code",
        ),
        (
            "research_report",
            r"(写|生成|产出|做)\s*(一份|个)?\s*(研究)?\s*报告|研究报告|"
            r"总结|摘要|概括|research\s+report",
        ),
        (
            "write_text_artifact",
            r"(保存|存|写|新建|创建|建)\s*(成|为|到|入)?|write|save|create",
        ),
        (
            "modify_text_artifact",
            r"(追加|末尾|后面)\s*.{0,8}(加|写|补)|修改\s*.{0,10}(内容|文字)|modify|append",
        ),
        (
            "read_text_artifact",
            r"(读取|读|打开|查看|看一下|显示)|read|show",
        ),
    )
)

_FILENAME = re.compile(
    r"([\w\-./]{1,120}\.(?:txt|md|markdown|py|json|csv|log|html|css|js|"
    r"yaml|yml|toml|sh|ts|tsx|jsx|sql|ini|cfg))",
    re.IGNORECASE,
)

# "保存成 note.txt：今天开会" / "内容写：xxx" / "内容：xxx"
_CONTENT_AFTER_COLON = re.compile(r"[：:]\s*(.+)$", re.DOTALL)
_CONTENT_EXPLICIT = re.compile(
    r"(?:内容|正文|文本)\s*(?:是|写|为|：|:)\s*[「“\"]?(.+?)[」”\"]?\s*$",
    re.DOTALL,
)

_QUOTED = re.compile(r"[「“\"']([^「」“”\"']{1,2000})[」”\"']")


@dataclass(frozen=True)
class Extraction:
    """What the extractor could and could not determine."""

    operation: str | None
    parameters: dict[str, Any] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    reason: str = ""
    matched_cue: str | None = None

    @property
    def complete(self) -> bool:
        return self.operation is not None and not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "parameters": dict(self.parameters),
            "missing": list(self.missing),
            "reason": self.reason,
            "matched_cue": self.matched_cue,
            "complete": self.complete,
        }


def normalize_operation(value: str | None) -> str | None:
    """Map user language onto a canonical operation name.

    Returns ``None`` when the value routes nowhere — the caller must then ask
    again instead of storing a value the policy layer will reject later.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    canonical = OPERATION_ALIASES.get(text.lower()) or OPERATION_ALIASES.get(text)
    if canonical:
        return canonical
    # Allow a canonical name to pass through unchanged.
    if re.fullmatch(r"[a-z_]{3,40}", text):
        return text
    return None


def operation_candidates() -> tuple[tuple[str, str], ...]:
    """(canonical, user-language) pairs for clarification questions."""
    return (
        ("write_text_artifact", "写一个新文件"),
        ("read_text_artifact", "读取一个文件的内容"),
        ("modify_text_artifact", "修改文件里的文字"),
        ("edit_text_file", "修改代码"),
        ("inspect_workspace", "检查工作区里有什么"),
        ("run_tests", "运行测试"),
        ("research_report", "根据本地材料生成报告"),
    )


def _first_content(text: str) -> str | None:
    match = _CONTENT_EXPLICIT.search(text)
    if match:
        return match.group(1).strip()
    match = _CONTENT_AFTER_COLON.search(text)
    if match:
        return match.group(1).strip()
    quoted = _QUOTED.search(text)
    if quoted:
        return quoted.group(1).strip()
    return None


def extract_goal(text: str) -> Extraction:
    """Extract an operation and parameters from a natural-language goal."""
    raw = (text or "").strip()
    if not raw:
        return Extraction(
            operation=None,
            missing=("operation", "goal"),
            reason="空的目标文本",
        )

    operation: str | None = None
    cue: str | None = None
    for candidate, pattern in _OPERATION_PATTERNS:
        match = pattern.search(raw)
        if match:
            operation = candidate
            cue = match.group(0)
            break

    filename_match = _FILENAME.search(raw)
    filename = filename_match.group(1) if filename_match else None

    # A bare filename with no verb is still actionable when the phrasing is
    # "X.txt：内容" — but guessing between read and write is exactly the kind
    # of guess this module refuses to make. It stays missing.
    parameters: dict[str, Any] = {}
    missing: list[str] = []

    if operation is None:
        return Extraction(
            operation=None,
            missing=("operation",),
            reason="没有识别出可执行的动作（写/读/改/检查/测试/报告）",
        )

    if operation in {
        "write_text_artifact",
        "read_text_artifact",
        "modify_text_artifact",
        "edit_text_file",
    }:
        if filename:
            parameters["artifact_path"] = filename
        else:
            missing.append("artifact_path")

    if operation == "write_text_artifact":
        content = _first_content(raw)
        if content is not None:
            parameters["content"] = content + ("" if content.endswith("\n") else "\n")
        else:
            missing.append("content")

    if operation == "modify_text_artifact":
        # Exact-match editing needs both sides of the substitution. Extracting
        # them reliably from free text is not something a rule can do, so the
        # task is created and the two fields are asked for explicitly.
        parameters.pop("content", None)
        missing.extend(f for f in ("old_text", "new_text") if f not in parameters)

    if operation == "edit_text_file":
        # Same reasoning as modify: the precise replacement is a user decision.
        missing.extend(f for f in ("old_text", "new_text") if f not in parameters)

    if operation == "research_report":
        # research_report needs a source; without one it would silently produce
        # an empty report, which is the fake-closure trap named in the audit.
        sources = [
            name
            for name in _FILENAME.findall(raw)
        ]
        if sources:
            parameters["source_files"] = sorted(set(sources))
        else:
            missing.append("source_files")

    return Extraction(
        operation=operation,
        parameters=parameters,
        missing=tuple(dict.fromkeys(missing)),
        reason=f"命中措辞「{cue}」" if cue else "按动作词匹配",
        matched_cue=cue,
    )


def merge_with_explicit(
    extraction: Extraction,
    explicit: Mapping[str, Any],
) -> tuple[str | None, dict[str, Any], tuple[str, ...]]:
    """Explicitly supplied values always beat extracted ones."""
    operation = explicit.get("operation") or extraction.operation
    parameters = dict(extraction.parameters)
    for key, value in explicit.items():
        if key == "operation" or value is None:
            continue
        parameters[key] = value
    missing = tuple(
        name
        for name in extraction.missing
        if name not in parameters and name != "operation"
    )
    return operation, parameters, missing


_USER_QUESTION_FIELDS = frozenset(
    {
        "operation",
        "artifact_path",
        "content",
        "old_text",
        "new_text",
        "source_files",
        "goal",
    }
)


def has_user_question(field_name: str) -> bool:
    """True when we have a real user-language question for this field."""
    return field_name in _USER_QUESTION_FIELDS


def question_for(field_name: str) -> dict[str, Any]:
    """A clarification question in the user's language, with examples."""
    table: dict[str, dict[str, Any]] = {
        "operation": {
            "question": "你想让我做什么？",
            "examples": [label for _, label in operation_candidates()],
            "candidates": [name for name, _ in operation_candidates()],
        },
        "artifact_path": {
            "question": "要操作哪个文件？请给出文件名，例如 note.txt",
            "examples": ["note.txt", "docs/summary.md", "calc.py"],
        },
        "content": {
            "question": "要写进去的内容是什么？可以直接把文字发给我",
            "examples": ["今天下午三点在 3 楼会议室开会"],
        },
        "old_text": {
            "question": "要替换掉的那段原文是什么？必须和文件里一字不差",
            "examples": ["print('hi')"],
        },
        "new_text": {
            "question": "替换成什么？",
            "examples": ["print('hello')"],
        },
        "source_files": {
            "question": "报告要基于哪些文件？请列出文件名",
            "examples": ["docs/a.md", "docs/b.md"],
        },
        "goal": {
            "question": "用一句话说明你想完成什么",
            "examples": ["把会议纪要存成 note.txt"],
        },
    }
    return table.get(
        field_name,
        {"question": f"还需要你补充：{field_name}", "examples": []},
    )


def describe_fields(fields: Iterable[str]) -> list[str]:
    return [question_for(name)["question"] for name in fields]
