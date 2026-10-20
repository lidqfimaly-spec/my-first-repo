"""Local validation of untrusted planner candidates."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .artifact_tool import ArtifactToolError, validate_artifact_name
from .models import (
    PlanningClassification,
    PlanningOutcome,
    TaskRequest,
)
from .local_test_runner import TARGET_PATTERN, TestExecutionError, validate_parameters
from .planner import (
    RUN_TESTS,
    WRITE_TEXT_ARTIFACT,
    PlanningError,
    build_artifact_plan,
    build_test_plan,
)


MAX_MODEL_RESPONSE_BYTES = 96 * 1024
MAX_CLARIFICATION_QUESTIONS = 5
ALLOWED_MISSING_FIELDS = frozenset(
    {"goal", "operation", "artifact_path", "content", "workspace", "test_target"}
)
CANDIDATE_FIELDS = frozenset(
    {
        "classification",
        "operation",
        "artifact_path",
        "content",
        "missing_fields",
        "clarification_questions",
        "reason",
        "test_target",
    }
)
_SAFE_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
REQUIRED_FIELDS_BY_CLASSIFICATION = {
    "plan": ("operation",),
    "clarification": ("missing_fields", "clarification_questions"),
    "unsupported": ("reason",),
}
_DEFAULTS = {
    "operation": None,
    "test_target": None,
    "artifact_path": None,
    "content": None,
    "missing_fields": [],
    "clarification_questions": [],
    "reason": None,
}


class PlanningCandidateValidator:
    """Business validation performed after schema parsing and before execution."""

    planner_name = "llm-candidate-v1"

    def validate(
        self,
        candidate: object,
        *,
        base_request: TaskRequest,
        metadata: Mapping[str, Any] | None = None,
    ) -> PlanningOutcome:
        base_metadata = {
            **dict(metadata or {}),
            "candidate_received": True,
        }
        if not isinstance(candidate, Mapping):
            return self._failed(
                "invalid_candidate",
                "planner candidate must be an object",
                base_metadata,
            )
        candidate_shape = _candidate_shape(candidate)
        diagnostic_metadata = {**base_metadata, **candidate_shape}
        try:
            unknown_fields = sorted(set(candidate) - CANDIDATE_FIELDS)
        except Exception:
            return self._failed(
                "invalid_candidate",
                "planner candidate fields are not readable",
                diagnostic_metadata,
            )
        if unknown_fields:
            return self._failed(
                "unknown_candidate_fields",
                "planner candidate contains fields outside the local contract",
                diagnostic_metadata,
                extra={"candidate_unknown_fields": _safe_field_names(unknown_fields)},
            )
        try:
            encoded = json.dumps(candidate, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            return self._failed(
                "invalid_candidate",
                "planner candidate is not JSON serializable",
                diagnostic_metadata,
            )
        if len(encoded) > MAX_MODEL_RESPONSE_BYTES:
            return self._failed(
                "candidate_too_large",
                "planner candidate exceeds the response size limit",
                {**diagnostic_metadata, "candidate_bytes": len(encoded)},
            )

        classification = candidate.get("classification")
        if classification not in REQUIRED_FIELDS_BY_CLASSIFICATION:
            return self._failed(
                "invalid_candidate_classification",
                "classification must be plan, clarification, or unsupported",
                {
                    **diagnostic_metadata,
                    "candidate_classification": "invalid",
                },
            )
        normalized = {**_DEFAULTS, **dict(candidate)}
        missing_fields = [
            field_name
            for field_name in REQUIRED_FIELDS_BY_CLASSIFICATION[classification]
            if field_name not in candidate
        ]
        if missing_fields:
            return self._failed(
                "missing_candidate_fields",
                "planner candidate is missing category-specific fields",
                {
                    **diagnostic_metadata,
                    "candidate_classification": classification,
                    "candidate_missing_fields": list(missing_fields),
                },
            )
        if classification == "clarification":
            return self._clarification(
                normalized,
                base_request,
                base_metadata,
                candidate_shape,
            )
        if classification == "unsupported":
            reason = normalized.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                return self._failed(
                    "invalid_unsupported_reason",
                    "unsupported candidate must contain a reason",
                    diagnostic_metadata,
                )
            return PlanningOutcome(
                classification=PlanningClassification.UNSUPPORTED,
                request=base_request,
                error_code="unsupported_request",
                error_message=reason.strip()[:1000],
                metadata={
                    **base_metadata,
                    "candidate_validation": "passed",
                },
            )
        return self._plan(
            normalized,
            base_request,
            base_metadata,
            candidate_shape,
        )

    def _plan(
        self,
        candidate: Mapping[str, Any],
        base_request: TaskRequest,
        metadata: dict[str, Any],
        candidate_shape: Mapping[str, Any],
    ) -> PlanningOutcome:
        failure_metadata = {**metadata, **candidate_shape}
        operation = candidate.get("operation")
        if operation == RUN_TESTS:
            return self._plan_test(
                candidate,
                base_request,
                metadata,
                candidate_shape,
            )
        if operation != WRITE_TEXT_ARTIFACT:
            return PlanningOutcome(
                classification=PlanningClassification.UNSUPPORTED,
                request=base_request,
                error_code="unsupported_operation",
                error_message="candidate operation is not locally allowed",
                metadata={
                    **failure_metadata,
                    "candidate_validation": "rejected",
                    "validation_error_code": "unsupported_operation",
                },
            )
        missing = [
            field
            for field in ("artifact_path", "content")
            if candidate.get(field) is None
        ]
        if missing:
            return self._failed(
                "missing_candidate_fields",
                "planner candidate is missing category-specific fields",
                {
                    **failure_metadata,
                    "candidate_classification": "plan",
                    "candidate_missing_fields": missing,
                },
            )
        try:
            artifact_path = validate_artifact_name(candidate.get("artifact_path"))
        except ArtifactToolError as exc:
            return PlanningOutcome(
                classification=PlanningClassification.UNSUPPORTED,
                request=base_request,
                error_code=exc.code,
                error_message=exc.message,
                metadata={
                    **failure_metadata,
                    "candidate_validation": "rejected",
                    "validation_error_code": exc.code,
                },
            )
        content = candidate.get("content")
        if not isinstance(content, str) or not content:
            return self._failed(
                "invalid_candidate_content",
                "candidate content must be a non-empty string",
                failure_metadata,
            )
        if not base_request.goal or not base_request.goal.strip():
            return self._clarification(
                {
                    "missing_fields": ["goal"],
                    "clarification_questions": ["请提供要执行的目标。"],
                },
                base_request,
                metadata,
                candidate_shape,
            )
        request = TaskRequest(
            goal=base_request.goal,
            operation=WRITE_TEXT_ARTIFACT,
            parameters={"artifact_path": artifact_path, "content": content},
        )
        try:
            plan = build_artifact_plan(
                request,
                planner_name=self.planner_name,
                deterministic=False,
            )
        except PlanningError as exc:
            return self._failed(
                exc.code,
                exc.message,
                {**metadata, "validation_error_code": exc.code},
            )
        return PlanningOutcome(
            classification=PlanningClassification.PLANNED,
            request=request,
            plan=plan,
            metadata={**metadata, "candidate_validation": "passed"},
        )

    def _plan_test(
        self,
        candidate: Mapping[str, Any],
        base_request: TaskRequest,
        metadata: dict[str, Any],
        candidate_shape: Mapping[str, Any],
    ) -> PlanningOutcome:
        """Accept only a bounded ``test_target`` from the model.

        Workspace, runner, image and evidence are taken from the trusted task
        request; the model cannot supply them.
        """
        target = candidate.get("test_target")
        if not isinstance(target, str) or not TARGET_PATTERN.fullmatch(target):
            return self._failed(
                "invalid_test_target",
                "test_target must be a dotted identifier",
                {
                    **metadata,
                    **candidate_shape,
                    "validation_error_code": "invalid_test_target",
                },
            )
        parameters = dict(base_request.parameters)
        if not parameters.get("workspace"):
            return self._clarification(
                {
                    "missing_fields": ["workspace"],
                    "clarification_questions": ["请提供已注册的工作区。"],
                },
                base_request,
                metadata,
                candidate_shape,
            )
        parameters["target"] = target
        parameters.setdefault("runner", "unittest")
        request = TaskRequest(
            goal=base_request.goal, operation=RUN_TESTS, parameters=parameters
        )
        try:
            normalized = validate_parameters(dict(request.parameters))
            plan = build_test_plan(
                TaskRequest(
                    goal=request.goal,
                    operation=RUN_TESTS,
                    parameters=normalized,
                ),
                planner_name=self.planner_name,
            )
        except (TestExecutionError, PlanningError) as exc:
            code = getattr(exc, "code", "invalid_test_plan")
            message = getattr(exc, "message", str(exc))
            return PlanningOutcome(
                classification=PlanningClassification.UNSUPPORTED,
                request=base_request,
                error_code=code,
                error_message=message,
                metadata={
                    **metadata,
                    **candidate_shape,
                    "candidate_validation": "rejected",
                    "validation_error_code": code,
                },
            )
        return PlanningOutcome(
            classification=PlanningClassification.PLANNED,
            request=request,
            plan=plan,
            metadata={**metadata, "candidate_validation": "passed"},
        )

    def _clarification(
        self,
        candidate: Mapping[str, Any],
        base_request: TaskRequest,
        metadata: dict[str, Any],
        candidate_shape: Mapping[str, Any],
    ) -> PlanningOutcome:
        failure_metadata = {**metadata, **candidate_shape}
        questions_raw = candidate.get("clarification_questions")
        missing_raw = candidate.get("missing_fields")
        if not isinstance(questions_raw, list) or not questions_raw:
            return self._failed(
                "invalid_clarification",
                "clarification requires at least one question",
                failure_metadata,
            )
        if len(questions_raw) > MAX_CLARIFICATION_QUESTIONS:
            return self._failed(
                "too_many_clarification_questions",
                "clarification question count exceeds the local limit",
                failure_metadata,
            )
        questions: list[str] = []
        for item in questions_raw:
            if not isinstance(item, str) or not item.strip() or len(item) > 500:
                return self._failed(
                    "invalid_clarification_question",
                    "clarification questions must be short non-empty strings",
                    failure_metadata,
                )
            questions.append(item.strip())
        if not isinstance(missing_raw, list):
            return self._failed(
                "invalid_missing_fields",
                "missing_fields must be an array",
                failure_metadata,
            )
        missing_fields: list[str] = []
        for item in missing_raw:
            if item not in ALLOWED_MISSING_FIELDS:
                return self._failed(
                    "invalid_missing_field",
                    "candidate requested an unknown clarification field",
                    failure_metadata,
                )
            if item not in missing_fields:
                missing_fields.append(item)
        return PlanningOutcome(
            classification=PlanningClassification.CLARIFICATION,
            request=base_request,
            missing_fields=missing_fields,
            clarification_questions=questions,
            metadata={
                **metadata,
                "candidate_validation": "passed",
            },
        )

    @staticmethod
    def _failed(
        code: str,
        message: str,
        metadata: dict[str, Any],
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> PlanningOutcome:
        return PlanningOutcome(
            classification=PlanningClassification.FAILED,
            error_code=code,
            error_message=message,
            metadata={
                **metadata,
                **dict(extra or {}),
                "candidate_validation": "rejected",
                "validation_error_code": code,
            },
        )


def _safe_field_names(names: list[object]) -> list[str]:
    safe: list[str] = []
    for raw in names[:20]:
        value = str(raw)
        safe.append(value if _SAFE_FIELD_NAME.fullmatch(value) else "<redacted>")
    return safe


def _candidate_shape(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return bounded, non-sensitive metadata about a candidate.

    Field names are sanitized and capped. Only the two safe enumerations are
    recorded as values; no free-form model content is copied into diagnostics.
    """
    try:
        keys = sorted(candidate, key=str)
        shape: dict[str, Any] = {
            "candidate_fields": _safe_field_names(keys),
            "candidate_field_count": len(keys),
        }
        classification = candidate.get("classification")
        if isinstance(classification, str):
            shape["candidate_classification"] = (
                classification
                if classification in REQUIRED_FIELDS_BY_CLASSIFICATION
                else "invalid"
            )
        operation = candidate.get("operation")
        if isinstance(operation, str):
            shape["candidate_operation"] = (
                operation
                if operation in {WRITE_TEXT_ARTIFACT, RUN_TESTS}
                else "invalid"
            )
        return shape
    except Exception:
        return {
            "candidate_fields": ["<unavailable>"],
            "candidate_field_count": -1,
        }
