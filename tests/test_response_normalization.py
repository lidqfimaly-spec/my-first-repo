from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.response_normalization import normalize_response_object  # noqa: E402


def response(
    *,
    text: str | None = None,
    status: str = "completed",
    incomplete_reason: str | None = None,
    content_type: str = "output_text",
    item_type: str = "message",
    output_text_fallback: str | None = None,
    http_status: int | None = 200,
) -> object:
    content = []
    if text is not None:
        content.append(SimpleNamespace(type=content_type, text=text, refusal=text))
    return SimpleNamespace(
        id="resp-test",
        status=status,
        output=[SimpleNamespace(type=item_type, content=content)],
        output_text=output_text_fallback if output_text_fallback is not None else "",
        incomplete_details=(
            SimpleNamespace(reason=incomplete_reason)
            if incomplete_reason
            else None
        ),
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
        ),
        error=None,
        _request=SimpleNamespace(
            _response=SimpleNamespace(status_code=http_status)
        ),
    )


class ResponseNormalizationTests(unittest.TestCase):
    def test_valid_structured_text_is_parsed(self) -> None:
        payload = {"classification": "plan", "content": "safe"}
        result = normalize_response_object(response(text=json.dumps(payload)))

        self.assertEqual(result.candidate, payload)
        self.assertTrue(result.text_present)
        self.assertEqual(result.output_item_types, ["message"])
        self.assertEqual(result.content_part_types, ["output_text"])
        self.assertEqual(result.http_status, 200)
        self.assertIsNone(result.json_error_type)
        self.assertFalse(hasattr(result, "raw_text"))

    def test_refusal_is_identified_without_returning_refusal_text(self) -> None:
        result = normalize_response_object(
            response(text="sensitive refusal text", content_type="refusal")
        )

        self.assertTrue(result.refusal_present)
        self.assertEqual(result.content_part_types, ["refusal"])
        self.assertFalse(hasattr(result, "refusal_text"))

    def test_incomplete_reason_and_http_status_are_kept(self) -> None:
        result = normalize_response_object(
            response(
                text='{"classification":',
                status="incomplete",
                incomplete_reason="max_output_tokens",
                http_status=400,
            )
        )

        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.incomplete_reason, "max_output_tokens")
        self.assertEqual(result.http_status, 400)
        self.assertEqual(result.json_error_type, "JSONDecodeError")

    def test_empty_and_unknown_output_types_are_safe(self) -> None:
        result = normalize_response_object(
            response(
                text=None,
                item_type="unexpected\nvalue",
                content_type="unexpected\npart",
                output_text_fallback=None,
            )
        )

        self.assertFalse(result.text_present)
        self.assertIsNone(result.candidate)
        self.assertEqual(result.output_item_types, ["unknown"])
        self.assertEqual(result.content_part_types, [])

    def test_invalid_json_records_error_type_not_text(self) -> None:
        secret_text = "not-json-with-sensitive-value"
        result = normalize_response_object(response(text=secret_text))
        diagnostics = json.dumps(result.diagnostics(), ensure_ascii=False)

        self.assertIsNone(result.candidate)
        self.assertEqual(result.json_error_type, "JSONDecodeError")
        self.assertNotIn(secret_text, diagnostics)

    def test_output_text_property_is_used_only_as_fallback(self) -> None:
        payload = {"classification": "unsupported"}
        result = normalize_response_object(
            response(
                text=None,
                output_text_fallback=json.dumps(payload),
            )
        )

        self.assertEqual(result.candidate, payload)
        self.assertTrue(result.text_present)


if __name__ == "__main__":
    unittest.main()
