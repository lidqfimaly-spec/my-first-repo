"""Deterministic semantic, format, and code-point text precision gates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import PlanningClassification, PlanningOutcome, TaskRequest
from .planner import WRITE_TEXT_ARTIFACT


ACTIVE_EXACT_TEXT_CONTRACT_VERSION = "planner-exact-text-v1"
EXACT_TEXT_CONTRACT = (
    Path(__file__).with_name("evals") / "planner_exact_text_v1.json"
)
PRECISION_MODES = frozenset(
    {"semantic", "format_constrained", "exact_text"}
)
CONTENT_FORMATS = frozenset({"plain_text", "markdown", "json_object"})
CASE_CATEGORIES = frozenset(
    {"semantic", "format_constrained", "exact_text", "contract_error"}
)
PLANNING_CLASSIFICATIONS = frozenset(
    {"planned", "clarification", "unsupported", "failed"}
)
_MISSING = object()


@dataclass(frozen=True, slots=True)
class ResolvedTextPrecision:
    mode: str
    mode_source: str
    expected_text: str | None = None
    content_format: str | None = None

    def model_visible_parameters(self) -> dict[str, str]:
        visible = {"precision_mode": self.mode}
        if self.expected_text is not None:
            visible["expected_text"] = self.expected_text
        if self.content_format is not None:
            visible["content_format"] = self.content_format
        return visible


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"exact text contract duplicate key: {key}")
        payload[key] = value
    return payload


def load_exact_text_contract(
    path: Path | None = None,
) -> dict[str, Any]:
    contract_path = path or EXACT_TEXT_CONTRACT
    raw = contract_path.read_bytes()
    payload = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )
    if not isinstance(payload, dict):
        raise ValueError("exact text contract has invalid shape")
    if payload.get("version") != ACTIVE_EXACT_TEXT_CONTRACT_VERSION:
        raise ValueError("active exact text contract version mismatch")
    if payload.get("retroactively_applicable") is not False:
        raise ValueError("exact text contract must not be retroactive")
    modes = payload.get("modes")
    if not isinstance(modes, Mapping) or set(modes) != PRECISION_MODES:
        raise ValueError("exact text mode set mismatch")
    for mode, details in modes.items():
        if not isinstance(details, Mapping):
            raise ValueError(f"exact text mode is invalid: {mode}")
        if details.get("normalizes_text") is not False:
            raise ValueError(f"exact text mode normalizes text: {mode}")
    formats = payload.get("content_formats")
    if not isinstance(formats, Mapping) or set(formats) != CONTENT_FORMATS:
        raise ValueError("exact text content format set mismatch")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) < 10:
        raise ValueError("exact text contract needs at least ten cases")
    _validate_cases(cases)
    return {
        **payload,
        "contract_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _validate_cases(cases: list[Any]) -> None:
    ids: list[str] = []
    for case in cases:
        if not isinstance(case, Mapping):
            raise ValueError("exact text case must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("exact text case is missing id")
        ids.append(case_id)
        if case.get("category") not in CASE_CATEGORIES:
            raise ValueError(
                f"exact text case {case_id} has an invalid category"
            )
        if not isinstance(case.get("goal"), str) or not case["goal"]:
            raise ValueError(
                f"exact text case {case_id} is missing goal"
            )
        trusted = case.get("trusted_parameters")
        if not isinstance(trusted, Mapping):
            raise ValueError(
                f"exact text case {case_id} lacks trusted parameters"
            )
        if not isinstance(case.get("scripted_output"), Mapping):
            raise ValueError(
                f"exact text case {case_id} lacks scripted output"
            )
        if case.get("expected_classification") not in PLANNING_CLASSIFICATIONS:
            raise ValueError(
                f"exact text case {case_id} has an invalid classification"
            )
        if not isinstance(case.get("must_not_plan"), bool):
            raise ValueError(
                f"exact text case {case_id} lacks must_not_plan"
            )
        expected_error = case.get("expected_error_code")
        if expected_error is not None and not isinstance(expected_error, str):
            raise ValueError(
                f"exact text case {case_id} has an invalid error code"
            )
    if len(ids) != len(set(ids)):
        raise ValueError("exact text case IDs must be unique")


def resolve_text_precision(
    request: TaskRequest,
    *,
    contract: Mapping[str, Any] | None = None,
) -> ResolvedTextPrecision:
    """Resolve only explicit trusted precision metadata.

    Natural-language wording is not used to infer a mode here. The LLM goal is
    untrusted planner input, so punctuation or character-level preferences
    require explicit trusted metadata. Quoted-content intent fidelity remains
    a separate deterministic check for the existing contract.
    """
    if contract is None:
        load_exact_text_contract()
    parameters = request.parameters
    explicit_mode = parameters.get("precision_mode", _MISSING)
    expected_present = "expected_text" in parameters
    content_format_present = "content_format" in parameters
    if explicit_mode is not _MISSING:
        if (
            not isinstance(explicit_mode, str)
            or explicit_mode not in PRECISION_MODES
        ):
            raise ValueError("precision_mode is not defined")
        mode = explicit_mode
        mode_source = "trusted_precision_mode"
    elif expected_present and content_format_present:
        raise ValueError(
            "precision mode is ambiguous when expected_text and "
            "content_format are both present"
        )
    elif expected_present:
        mode = "exact_text"
        mode_source = "trusted_expected_text"
    elif content_format_present:
        mode = "format_constrained"
        mode_source = "trusted_content_format"
    else:
        return ResolvedTextPrecision(
            mode="semantic",
            mode_source="default_semantic",
        )

    expected_text: str | None = None
    if mode == "exact_text":
        if not expected_present:
            raise ValueError("exact_text requires expected_text")
        expected_text = parameters.get("expected_text")
        if not isinstance(expected_text, str):
            raise ValueError("expected_text must be a string")
    elif expected_present:
        raise ValueError(
            f"{mode} cannot be combined with expected_text"
        )

    content_format: str | None = None
    if mode == "format_constrained":
        if not content_format_present:
            raise ValueError(
                "format_constrained requires a content_format"
            )
        content_format = parameters.get("content_format")
        if content_format not in CONTENT_FORMATS:
            raise ValueError("content_format is not defined")
    elif content_format_present:
        raise ValueError(
            f"{mode} cannot be combined with content_format"
        )

    return ResolvedTextPrecision(
        mode=mode,
        mode_source=mode_source,
        expected_text=expected_text,
        content_format=content_format,
    )


def apply_text_precision(
    request: TaskRequest,
    outcome: PlanningOutcome,
    *,
    contract: Mapping[str, Any] | None = None,
    resolved: ResolvedTextPrecision | None = None,
) -> PlanningOutcome:
    loaded = contract if contract is not None else load_exact_text_contract()
    if (
        outcome.classification is not PlanningClassification.PLANNED
        or outcome.request is None
        or outcome.request.operation != WRITE_TEXT_ARTIFACT
    ):
        return outcome
    resolution = resolved or resolve_text_precision(
        request,
        contract=loaded,
    )
    content = outcome.request.parameters.get("content")
    metadata = {
        **dict(outcome.metadata),
        **_precision_metadata(loaded, resolution),
    }
    if not isinstance(content, str):
        return _failed(
            "text_precision_contract_error",
            "planned write content is not text",
            metadata,
        )
    if resolution.mode == "semantic":
        metadata["text_precision"].update(result="semantic")
        outcome.metadata = metadata
        return outcome
    if resolution.mode == "exact_text":
        expected = resolution.expected_text or ""
        comparison = _comparison_diagnostics(expected, content)
        metadata["text_precision"].update(
            comparison,
            result=(
                "exact_match"
                if content == expected
                else "exact_mismatch"
            ),
        )
        if content != expected:
            return _failed(
                "text_precision_mismatch",
                "planned content is not an exact code-point match",
                metadata,
            )
        outcome.metadata = metadata
        return outcome

    assert resolution.content_format is not None
    format_valid = _format_valid(resolution.content_format, content)
    metadata["text_precision"].update(
        content_format=resolution.content_format,
        result=(
            "format_match"
            if format_valid
            else "format_mismatch"
        ),
        observed_sha256=hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest(),
    )
    if not format_valid:
        return _failed(
            "text_format_mismatch",
            f"planned content does not satisfy {resolution.content_format}",
            metadata,
        )
    outcome.metadata = metadata
    return outcome


def _precision_metadata(
    contract: Mapping[str, Any],
    resolution: ResolvedTextPrecision,
) -> dict[str, Any]:
    return {
        "text_precision_gate": "passed",
        "text_precision": {
            "contract_version": contract["version"],
            "contract_sha256": contract["contract_sha256"],
            "mode": resolution.mode,
            "mode_source": resolution.mode_source,
            "normalized_before_compare": False,
        },
    }


def _comparison_diagnostics(expected: str, observed: str) -> dict[str, Any]:
    return {
        "expected_sha256": hashlib.sha256(
            expected.encode("utf-8")
        ).hexdigest(),
        "observed_sha256": hashlib.sha256(
            observed.encode("utf-8")
        ).hexdigest(),
        "expected_length": len(expected),
        "observed_length": len(observed),
        "expected_utf8_bytes": len(expected.encode("utf-8")),
        "observed_utf8_bytes": len(observed.encode("utf-8")),
    }


def _format_valid(content_format: str, content: str) -> bool:
    if content_format == "plain_text":
        return bool(content)
    if content_format == "markdown":
        stripped = content.lstrip()
        return bool(
            stripped.startswith("# ")
            or stripped.startswith("## ")
            or stripped.startswith("### ")
        )
    if content_format == "json_object":
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return False
        return isinstance(parsed, dict)
    return False


def _failed(
    code: str,
    message: str,
    metadata: Mapping[str, Any],
) -> PlanningOutcome:
    return PlanningOutcome(
        classification=PlanningClassification.FAILED,
        error_code=code,
        error_message=message,
        metadata={
            **dict(metadata),
            "text_precision_gate": "blocked",
        },
    )
