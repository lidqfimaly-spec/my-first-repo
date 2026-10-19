"""Convert provider response objects into safe, internal diagnostics."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


MAX_RESPONSE_TEXT_BYTES = 96 * 1024
_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


@dataclass(slots=True)
class ModelCallResult:
    candidate: object | None = None
    refusal_present: bool = False
    status: str | None = None
    incomplete_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    response_id: str | None = None
    http_status: int | None = None
    response_error_present: bool = False
    output_item_types: list[str] = field(default_factory=list)
    content_part_types: list[str] = field(default_factory=list)
    text_present: bool = False
    text_length: int = 0
    output_bytes: int = 0
    json_error_type: str | None = None

    def diagnostics(self) -> dict[str, object]:
        return {
            "http_status": self.http_status,
            "response_status": self.status,
            "response_error_present": self.response_error_present,
            "incomplete_reason": self.incomplete_reason,
            "output_item_types": list(self.output_item_types),
            "content_part_types": list(self.content_part_types),
            "text_present": self.text_present,
            "text_length": self.text_length,
            "output_bytes": self.output_bytes,
            "json_error_type": self.json_error_type,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "response_id": self.response_id,
        }


def normalize_response_object(response: object) -> ModelCallResult:
    output_items = list(getattr(response, "output", []) or [])
    item_types: list[str] = []
    content_types: list[str] = []
    output_texts: list[str] = []
    refusal_present = False

    for item in output_items:
        item_types.append(_safe_type_name(getattr(item, "type", None)))
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            part_type = getattr(part, "type", None)
            content_types.append(_safe_type_name(part_type))
            if part_type == "output_text":
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    output_texts.append(text)
            elif part_type == "refusal":
                refusal_present = True

    text = "".join(output_texts)
    if not text:
        try:
            fallback = getattr(response, "output_text", None)
        except Exception:
            fallback = None
        if isinstance(fallback, str):
            text = fallback
    encoded = text.encode("utf-8")
    candidate: object | None = None
    json_error_type: str | None = None
    if text:
        if len(encoded) > MAX_RESPONSE_TEXT_BYTES:
            json_error_type = "response_too_large"
        else:
            try:
                candidate = json.loads(text)
            except json.JSONDecodeError:
                json_error_type = "JSONDecodeError"
            except Exception as exc:
                json_error_type = _safe_type_name(type(exc).__name__)

    usage = getattr(response, "usage", None)
    incomplete = getattr(response, "incomplete_details", None)
    request = getattr(response, "_request", None)
    http_response = getattr(request, "_response", None)
    raw_http_status = getattr(http_response, "status_code", None)
    http_status = raw_http_status if isinstance(raw_http_status, int) else None
    return ModelCallResult(
        candidate=candidate,
        refusal_present=refusal_present,
        status=_safe_optional_text(getattr(response, "status", None), 32),
        incomplete_reason=_safe_optional_text(
            getattr(incomplete, "reason", None),
            128,
        ),
        input_tokens=_optional_int(getattr(usage, "input_tokens", None)),
        output_tokens=_optional_int(getattr(usage, "output_tokens", None)),
        total_tokens=_optional_int(getattr(usage, "total_tokens", None)),
        response_id=_safe_optional_text(getattr(response, "id", None), 128),
        http_status=http_status,
        response_error_present=getattr(response, "error", None) is not None,
        output_item_types=_bounded_unique(item_types),
        content_part_types=_bounded_unique(content_types),
        text_present=bool(text),
        text_length=len(text),
        output_bytes=len(encoded),
        json_error_type=json_error_type,
    )


def _safe_type_name(value: object) -> str:
    if isinstance(value, str) and _SAFE_NAME.fullmatch(value):
        return value
    return "unknown"


def _safe_optional_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:limit]


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _bounded_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
        if len(result) >= 32:
            break
    return result
