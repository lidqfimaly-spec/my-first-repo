"""Validation and data-flow helpers for bounded multi-step task plans."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .models import JsonObject, Plan, PlanStep
from .research_workflow import RESEARCH_REPORT_TOOL
from .workspace_edit import EDIT_TEXT_FILE_TOOL
from .workspace_inspection import INSPECT_WORKSPACE_TOOL


MULTI_STEP_OPERATION = "execute_plan"
MAX_PLAN_STEPS = 8
MAX_STEP_ATTEMPTS = 3
SUPPORTED_STEP_OPERATIONS = frozenset(
    {
        "write_text_artifact",
        "read_text_artifact",
        "modify_text_artifact",
        "run_tests",
        INSPECT_WORKSPACE_TOOL,
        EDIT_TEXT_FILE_TOOL,
        RESEARCH_REPORT_TOOL,
    }
)
FAILURE_POLICIES = frozenset({"fail_fast", "continue"})
_STEP_REFERENCE = re.compile(
    r"^\$\{(?P<step>[A-Za-z0-9_-]+)\.(?P<field>[A-Za-z0-9_.-]+)\}$"
)


APPROVAL_DIGEST_VERSION = "step-approval-v2"


def step_approval_digest(step: PlanStep, plan_id: str) -> str:
    """Digest of *exactly* what a human is approving for one step.

    Approval used to key on ``step_id`` alone, so a replan could keep the id and
    change the parameters — the approved step and the executed step differed
    silently (R-08). Binding the digest makes approval a statement about
    content, not about a name.

    ``plan_id`` is included so an approval cannot survive being carried into a
    different plan: the digest is a statement about "this step, in this plan".
    ``plan_version`` is deliberately *not* included. A replan bumps the version
    and keeps unchanged steps; binding the version would re-ask a human about
    content they already saw and did not change. Content binding is the
    stronger promise, and it is the one this gate keeps.
    """
    payload = {
        "version": APPROVAL_DIGEST_VERSION,
        "plan_id": plan_id,
        "step_id": step.step_id,
        "tool_name": step.tool_name,
        "parameters": step.parameters,
        "expected_result": step.expected_result,
        "depends_on": sorted(step.depends_on),
        "max_attempts": step.max_attempts,
        "retry_on_not_executed": step.retry_on_not_executed,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def step_is_approved(step: PlanStep, task) -> bool:
    """True only when this exact content, in this plan, was approved."""
    if task.plan is None:
        return False
    if step.step_id not in task.approved_step_ids:
        return False
    recorded = (task.approved_step_digests or {}).get(step.step_id)
    if not isinstance(recorded, str) or not recorded:
        return False
    try:
        expected = step_approval_digest(step, task.plan.plan_id)
    except (TypeError, ValueError):
        # An un-digestable step must never inherit an approval.
        return False
    return recorded == expected


class MultiStepPlanError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_plan_graph(
    plan: Plan,
    *,
    completed_step_ids: Sequence[str] = (),
) -> list[PlanStep]:
    """Validate a bounded graph and return its deterministic execution order."""
    if plan.failure_policy not in FAILURE_POLICIES:
        raise MultiStepPlanError(
            "invalid_failure_policy",
            "plan failure_policy must be fail_fast or continue",
        )
    if not plan.steps:
        raise MultiStepPlanError("empty_plan", "plan must contain at least one step")
    if len(plan.steps) > MAX_PLAN_STEPS:
        raise MultiStepPlanError(
            "plan_step_limit_exceeded",
            f"plan exceeds the {MAX_PLAN_STEPS} step limit",
        )
    by_id: dict[str, PlanStep] = {}
    completed = set(completed_step_ids)
    for step in plan.steps:
        if not step.step_id or step.step_id in by_id:
            raise MultiStepPlanError(
                "duplicate_step_id",
                "plan step IDs must be non-empty and unique",
            )
        if step.tool_name not in SUPPORTED_STEP_OPERATIONS:
            raise MultiStepPlanError(
                "unsupported_step_tool",
                f"unsupported step tool: {step.tool_name!r}",
            )
        if step.max_attempts < 1 or step.max_attempts > MAX_STEP_ATTEMPTS:
            raise MultiStepPlanError(
                "invalid_step_attempts",
                f"step max_attempts must be between 1 and {MAX_STEP_ATTEMPTS}",
            )
        if step.retry_on_not_executed and step.max_attempts < 2:
            raise MultiStepPlanError(
                "invalid_retry_policy",
                "retry_on_not_executed requires max_attempts greater than one",
            )
        if not isinstance(step.parameters, Mapping):
            raise MultiStepPlanError(
                "invalid_step_parameters",
                f"step {step.step_id!r} parameters must be an object",
            )
        by_id[step.step_id] = step

    for step in plan.steps:
        for dependency in step.depends_on:
            if dependency == step.step_id:
                raise MultiStepPlanError(
                    "self_dependency",
                    f"step {step.step_id!r} cannot depend on itself",
                )
            if dependency not in by_id and dependency not in completed:
                raise MultiStepPlanError(
                    "unknown_dependency",
                    f"step {step.step_id!r} depends on unknown step {dependency!r}",
                )
        _validate_references(step, by_id, completed)

    indegree = {
        step.step_id: len(
            [
                dependency
                for dependency in step.depends_on
                if dependency in by_id
            ]
        )
        for step in plan.steps
    }
    dependents: dict[str, list[str]] = {step.step_id: [] for step in plan.steps}
    for step in plan.steps:
        for dependency in step.depends_on:
            if dependency in by_id:
                dependents[dependency].append(step.step_id)
    ready = [
        step.step_id for step in plan.steps if indegree[step.step_id] == 0
    ]
    ordered: list[PlanStep] = []
    while ready:
        step_id = ready.pop(0)
        ordered.append(by_id[step_id])
        for dependent in dependents[step_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if len(ordered) != len(plan.steps):
        raise MultiStepPlanError(
            "cyclic_plan",
            "plan dependencies must be acyclic",
        )
    return ordered


def _validate_references(
    step: PlanStep,
    by_id: Mapping[str, PlanStep],
    completed: set[str],
) -> None:
    for reference in _iter_references(step.parameters):
        referrer, field = reference
        if referrer not in by_id and referrer not in completed:
            raise MultiStepPlanError(
                "unknown_step_reference",
                f"step {step.step_id!r} references unknown step {referrer!r}",
            )
        if referrer not in step.depends_on:
            raise MultiStepPlanError(
                "undeclared_step_reference",
                f"step {step.step_id!r} must declare {referrer!r} in depends_on",
            )
        if not field:
            raise MultiStepPlanError(
                "invalid_step_reference",
                "step references must include a non-empty field",
            )


def _iter_references(value: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        match = _STEP_REFERENCE.fullmatch(value)
        if match:
            found.append((match.group("step"), match.group("field")))
    elif isinstance(value, Mapping):
        if set(value) == {"$from_step", "field"}:
            step_id = value.get("$from_step")
            field = value.get("field")
            if isinstance(step_id, str) and isinstance(field, str):
                found.append((step_id, field))
        else:
            for item in value.values():
                found.extend(_iter_references(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_iter_references(item))
    return found


def resolve_step_parameters(
    parameters: Mapping[str, Any],
    outputs_by_step: Mapping[str, Mapping[str, Any]],
) -> JsonObject:
    """Resolve explicit prior-step references without evaluating expressions."""
    return _resolve_value(dict(parameters), outputs_by_step)


def _resolve_value(
    value: Any,
    outputs_by_step: Mapping[str, Mapping[str, Any]],
) -> Any:
    if isinstance(value, str):
        match = _STEP_REFERENCE.fullmatch(value)
        if match:
            return _lookup_output(
                outputs_by_step,
                match.group("step"),
                match.group("field"),
            )
        return value
    if isinstance(value, Mapping):
        if set(value) == {"$from_step", "field"}:
            return _lookup_output(
                outputs_by_step,
                str(value["$from_step"]),
                str(value["field"]),
            )
        return {
            str(key): _resolve_value(item, outputs_by_step)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_value(item, outputs_by_step) for item in value]
    return value


def _lookup_output(
    outputs_by_step: Mapping[str, Mapping[str, Any]],
    step_id: str,
    field: str,
) -> Any:
    output = outputs_by_step.get(step_id)
    if output is None:
        raise MultiStepPlanError(
            "step_output_unavailable",
            f"step {step_id!r} has no completed output",
        )
    current: Any = output
    for component in field.split("."):
        if not isinstance(current, Mapping) or component not in current:
            raise MultiStepPlanError(
                "step_output_field_missing",
                f"step {step_id!r} output has no field {field!r}",
            )
        current = current[component]
    return current
