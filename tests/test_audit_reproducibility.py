from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.audit_report import (  # noqa: E402
    ReportSerializationError,
    build_metadata,
    canonical_json,
    compare_stable,
    nondeterministic_fields,
    stable_digest,
    stable_projection,
    write_envelope,
)
from tests.e2e_acceptance_runner import run_acceptance  # noqa: E402
from zevorinth.safety_gate import run_safety_gate  # noqa: E402


class DeterministicSerializationTests(unittest.TestCase):
    def test_keys_are_sorted_and_whitespace_is_fixed(self) -> None:
        first = canonical_json({"b": 1, "a": [2, 3]})
        second = canonical_json({"a": [2, 3], "b": 1})
        self.assertEqual(first, second)
        self.assertEqual(first, '{"a":[2,3],"b":1}')

    def test_non_finite_numbers_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ReportSerializationError):
                canonical_json({"value": value})

    def test_floats_are_not_rounded(self) -> None:
        payload = canonical_json({"value": 0.6666666666666666})
        self.assertIn("0.6666666666666666", payload)

    def test_mixed_key_types_are_rejected(self) -> None:
        with self.assertRaises(ReportSerializationError):
            canonical_json({1: "a", "b": 2})

    def test_digest_is_order_independent_for_objects(self) -> None:
        self.assertEqual(
            stable_digest({"a": 1, "b": 2}),
            stable_digest({"b": 2, "a": 1}),
        )

    def test_array_order_changes_the_digest(self) -> None:
        self.assertNotEqual(
            stable_digest([1, 2]), stable_digest([2, 1])
        )


class StableProjectionTests(unittest.TestCase):
    def test_gate_projection_drops_timing_and_environment(self) -> None:
        report = {
            "metrics": {
                "total_duration_ms": 12.5,
                "case_duration_ms": {"a": 1.0},
                "classification_accuracy": 1.0,
            },
            "python_version": "3.14.6",
            "git_head": "abc",
            "sdk_status": {"installed": False},
            "cases": [{"id": "a", "duration_ms": 1.0, "passed": True}],
        }
        stable = stable_projection(report, "safety_gate")
        self.assertEqual(
            stable["metrics"], {"classification_accuracy": 1.0}
        )
        self.assertEqual(stable["cases"], [{"id": "a", "passed": True}])
        for key in ("python_version", "git_head", "sdk_status"):
            self.assertNotIn(key, stable)

    def test_acceptance_projection_drops_generated_at(self) -> None:
        report = {"generated_at": "2026-10-20T00:00:00+08:00", "metrics": {}}
        stable = stable_projection(report, "acceptance")
        self.assertEqual(stable, {"metrics": {}})

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(ReportSerializationError):
            stable_projection({}, "unknown")


class ReproducibilityTests(unittest.TestCase):
    def test_raw_reports_still_carry_volatile_fields(self) -> None:
        acceptance = run_acceptance()
        gate = run_safety_gate()
        self.assertIn("generated_at", acceptance)
        self.assertIn("total_duration_ms", gate["metrics"])
        self.assertIn("duration_ms", gate["cases"][0])
        self.assertIn(
            "artifact.path", nondeterministic_fields("acceptance")
        )

    def test_acceptance_stable_results_repeat(self) -> None:
        first = stable_projection(run_acceptance(), "acceptance")
        second = stable_projection(run_acceptance(), "acceptance")
        result = compare_stable(first, second)
        self.assertEqual(result["status"], "PASS", result)
        self.assertTrue(result["identical"])

    def test_safety_gate_stable_results_repeat(self) -> None:
        first = stable_projection(run_safety_gate(), "safety_gate")
        second = stable_projection(run_safety_gate(), "safety_gate")
        result = compare_stable(first, second)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["differing_top_level_keys"], [])

    def test_controlled_change_alters_the_stable_digest(self) -> None:
        stable = stable_projection(run_acceptance(), "acceptance")
        changed = copy.deepcopy(stable)
        changed["metrics"]["passed"] = changed["metrics"]["passed"] - 1
        self.assertNotEqual(stable_digest(stable), stable_digest(changed))
        result = compare_stable(stable, changed)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("metrics", result["differing_top_level_keys"])

    def test_case_order_participates_in_the_digest(self) -> None:
        stable = stable_projection(run_acceptance(), "acceptance")
        reversed_cases = copy.deepcopy(stable)
        reversed_cases["scenarios"] = list(
            reversed(reversed_cases["scenarios"])
        )
        self.assertNotEqual(
            stable_digest(stable), stable_digest(reversed_cases)
        )

    def test_metadata_points_at_the_stable_digest(self) -> None:
        stable = stable_projection(run_acceptance(), "acceptance")
        metadata = build_metadata(
            stable,
            kind="acceptance",
            command=["python", "-m", "tests.e2e_acceptance_runner"],
            exit_code=0,
            started_at="2026-10-20T00:00:00+00:00",
            duration_ms=12.3456,
            not_run=["mutation framework"],
        )
        self.assertEqual(metadata["stable_sha256"], stable_digest(stable))
        self.assertNotIn("generated_at", stable)
        self.assertIn("generated_at_utc", metadata)
        self.assertIn("environment", metadata)
        self.assertEqual(metadata["not_run"], ["mutation framework"])

    def test_envelope_files_are_written_with_one_trailing_newline(self) -> None:
        stable = stable_projection(run_acceptance(), "acceptance")
        metadata = build_metadata(
            stable,
            kind="acceptance",
            command=["run"],
            exit_code=0,
            started_at="2026-10-20T00:00:00+00:00",
            duration_ms=1.0,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            written = write_envelope(
                Path(temp_dir), stable=stable, metadata=metadata
            )
            stable_bytes = Path(written["stable_path"]).read_bytes()
            digest_text = (
                Path(written["stable_path"]).parent / "stable.sha256"
            ).read_text()
        self.assertTrue(stable_bytes.endswith(b"\n"))
        self.assertEqual(stable_bytes.count(b"\n"), 1)
        self.assertEqual(
            written["stable_sha256"],
            __import__("hashlib").sha256(stable_bytes).hexdigest(),
        )
        self.assertEqual(
            digest_text.strip(),
            written["stable_sha256"],
        )
        self.assertEqual(
            json.loads(stable_bytes.decode("utf-8")), stable
        )


if __name__ == "__main__":
    unittest.main()
