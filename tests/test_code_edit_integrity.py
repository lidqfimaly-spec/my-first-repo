"""P0-2 v3: the instrument must be checkable by someone who does not trust it.

The v3 review found two defects that a green test suite could not see:

1. the identity pinned the evaluator but not the validator/applier it measures
   through, so those could change while every number's label stayed the same;
2. the freeze was self-referential, so a rewritten digest function could keep
   the internal tests green while the numbers went to 114%.

These tests cover the repairs. They deliberately do not "fix" the self-reference
— it cannot be fixed from inside; they pin the external check that replaces it.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth import code_edit_baseline as baseline  # noqa: E402

ANCHOR_SCRIPT = PROJECT_ROOT / "scripts" / "anchor_code_edit_evidence.py"
MATRIX_SCRIPT = PROJECT_ROOT / "scripts" / "code_edit_mutation_matrix.py"
REPLIES_PATH = (
    baseline.DATASET_PATH.parent / "code_edit_baseline_v1_replies.json"
)


def _load_anchor_module():
    spec = importlib.util.spec_from_file_location("anchor_evidence", ANCHOR_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scripted_replies(dataset: baseline.Dataset) -> dict[str, str]:
    payload = json.loads(REPLIES_PATH.read_text("utf-8"))["replies"]
    by_id = {case["id"]: case for case in dataset.cases}
    filled: dict[str, str] = {}
    for case_id, reply in payload.items():
        candidate = dict(reply)
        if candidate.get("expected_sha256") == "__FROM_CONTEXT__":
            content = by_id[case_id]["files"][candidate["path"]]
            candidate["expected_sha256"] = hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()
        filled[case_id] = json.dumps(candidate, ensure_ascii=False)
    return filled


def _offline_record() -> dict:
    dataset = baseline.load_dataset()
    with tempfile.TemporaryDirectory() as temp_dir:
        return baseline.run_baseline(
            dataset,
            client=None,
            project_root=Path(temp_dir),
            scripted_replies=_scripted_replies(dataset),
            run_label="integrity-test",
        )


class DependencyIdentityTests(unittest.TestCase):
    def test_the_closure_covers_the_three_execution_edges(self) -> None:
        identity = baseline.dependency_identity()
        covered = identity["covered_symbols"]
        self.assertIn(
            "zevorinth.proposal_candidates::validate_proposal_candidate", covered
        )
        self.assertIn("zevorinth.workspace_edit::apply_text_edit", covered)
        # consume() is covered through the class source; assert the class is
        # there rather than pretending the method is a module-level symbol.
        self.assertIn(
            "zevorinth.proposal_candidates::ProposalSessionIssuer", covered
        )
        for helper in ("_validate_code_body", "validate_edit_precondition"):
            self.assertTrue(
                any(name.endswith(f"::{helper}") for name in covered),
                f"{helper} should be reachable and therefore covered",
            )

    def test_the_dependency_digest_is_frozen(self) -> None:
        self.assertEqual(
            baseline.dependency_identity()["sha256"],
            baseline.FROZEN_DEPENDENCY_SHA256,
        )

    def test_a_changed_call_graph_fails_closed(self) -> None:
        """A dependency that appears or disappears must not be measured silently."""
        declared = baseline.FROZEN_DEPENDENCY_SYMBOLS
        with mock.patch.object(
            baseline, "FROZEN_DEPENDENCY_SYMBOLS", declared[:-1]
        ):
            with self.assertRaises(baseline.BaselineError) as caught:
                baseline.dependency_identity()
        self.assertEqual(caught.exception.code, "dependency_closure_drift")

    def test_the_closure_is_read_from_the_live_code_not_a_list(self) -> None:
        """The frozen list has to equal what the call graph produces right now."""
        computed = [
            f"{module}::{name}" for module, name in baseline._dependency_closure()
        ]
        self.assertEqual(computed, list(baseline.FROZEN_DEPENDENCY_SYMBOLS))


class SourceTreeIdentityTests(unittest.TestCase):
    def test_the_tree_digest_covers_the_dependency_modules(self) -> None:
        identity = baseline.source_tree_identity()
        self.assertGreaterEqual(identity["files"], 50)
        self.assertGreater(identity["bytes"], 0)
        self.assertEqual(len(identity["sha256"]), 64)
        # Two calls in one process must agree: an identity that changes between
        # calls is not an identity.
        self.assertEqual(identity["sha256"], baseline.source_tree_identity()["sha256"])

    def test_the_record_carries_the_tree_it_ran_from(self) -> None:
        record = _offline_record()
        tree = record["freeze"]["source_tree"]
        self.assertEqual(tree["sha256"], baseline.source_tree_identity()["sha256"])


class AnchorPayloadTests(unittest.TestCase):
    def test_an_honest_record_passes_the_independent_check(self) -> None:
        record = _offline_record()
        anchor = _load_anchor_module()
        checks = anchor.check_record(record)
        names = [name for name, _, _ in checks]
        # The five identity/seal checks come first; V3.1 appends the metric
        # invariants (I1–I9) and the three-way tally agreement after them.
        self.assertEqual(
            names[:5],
            ["评测器摘要", "依赖摘要", "源码树摘要", "锚点载荷摘要", "结论封印"],
        )
        self.assertTrue(
            any(name.startswith("metric_denominator_exceeded:") for name in names),
            names,
        )
        self.assertIn("metric_tally_mismatch", names)
        for name, ok, detail in checks:
            self.assertTrue(ok, f"{name}: {detail}")

    def test_editing_a_metric_breaks_the_result_seal(self) -> None:
        record = _offline_record()
        anchor = _load_anchor_module()
        forged = copy.deepcopy(record)
        forged["metrics"]["correct_modification"]["numerator"] = 0
        results = {name: ok for name, ok, _ in anchor.check_record(forged)}
        self.assertFalse(results["结论封印"])
        # The identity itself is untouched: that is exactly why a seal is
        # needed in addition to an identity.
        self.assertTrue(results["评测器摘要"])

    def test_swapping_the_dataset_under_a_record_is_caught(self) -> None:
        record = _offline_record()
        anchor = _load_anchor_module()
        forged = copy.deepcopy(record)
        forged["freeze"]["dataset"]["sha256"] = "0" * 64
        results = {name: ok for name, ok, _ in anchor.check_record(forged)}
        self.assertFalse(results["锚点载荷摘要"])

    def test_the_payload_pins_the_metric_contract(self) -> None:
        record = _offline_record()
        self.assertEqual(
            record["anchor_payload"]["metric_contract"],
            baseline.METRIC_CONTRACT_VERSION,
        )
        self.assertEqual(record["metric_contract"], baseline.METRIC_CONTRACT_VERSION)

    def test_the_anchor_store_refuses_to_live_inside_the_repository(self) -> None:
        anchor = _load_anchor_module()
        with self.assertRaises(SystemExit) as caught:
            anchor._store_dir(str(PROJECT_ROOT / "evals" / "anchors"))
        self.assertIn("outside the repository", str(caught.exception))


class GuardClosureTests(unittest.TestCase):
    def test_the_replies_are_frozen(self) -> None:
        digest = hashlib.sha256(REPLIES_PATH.read_bytes()).hexdigest()
        self.assertEqual(digest, baseline.FROZEN_REPLIES_SHA256)

    def test_the_scripted_replies_are_recorded_with_the_run(self) -> None:
        record = _offline_record()
        replies = record["freeze"]["replies"]
        self.assertEqual(replies["kind"], "scripted")
        self.assertEqual(replies["version"], baseline.REPLIES_VERSION)
        self.assertEqual(replies["count"], len(record["cases"]))

    def test_a_dataset_without_rejecting_probes_is_refused(self) -> None:
        payload = json.loads(baseline.DATASET_PATH.read_text("utf-8"))
        payload["precision_gate_probes"] = [
            probe
            for probe in payload["precision_gate_probes"]
            if probe["expected"] != "reject"
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "dataset.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with mock.patch.object(
                baseline, "FROZEN_DATASET_SHA256", digest
            ):
                with self.assertRaises(baseline.BaselineError) as caught:
                    baseline.load_dataset(path)
        self.assertEqual(
            caught.exception.code, "dataset_missing_rejecting_probe"
        )


class TheGapIsClosedTests(unittest.TestCase):
    def test_an_in_scope_edit_on_a_no_change_case_is_counted_as_wrong(self) -> None:
        record = _offline_record()
        scope = record["scope_audit"]
        self.assertEqual(
            scope["in_scope_unwanted"], ["no_change_formatted_boundary"]
        )
        metric = record["metrics"]["incorrect_modification"]
        self.assertEqual(
            metric["numerator"],
            len(scope["wrong_edits"]) + len(scope["in_scope_unwanted"]),
        )
        self.assertEqual(metric["denominator"], len(scope["modified"]))

    def test_the_report_names_the_gap_and_its_members(self) -> None:
        report = baseline.render_report(_offline_record())
        self.assertIn("G 集", report)
        self.assertIn("no_change_formatted_boundary", report)
        self.assertIn("两者不相交：是", report)

    def test_the_two_numerators_cannot_double_count(self) -> None:
        record = _offline_record()
        wrong = set(record["scope_audit"]["wrong_edits"])
        gap = set(record["scope_audit"]["in_scope_unwanted"])
        self.assertEqual(wrong & gap, set())


class ProviderEvidenceTests(unittest.TestCase):
    def test_credentials_never_reach_a_record(self) -> None:
        reply = baseline.ModelReply(
            text="{}",
            model="served-model",
            usage={"prompt_tokens": 10, "completion_tokens": 2},
            raw={
                "id": "resp-1",
                "model": "served-model",
                "finish_reason": "stop",
                "authorization": "Bearer sk-should-not-survive",
            },
        )
        evidence = baseline._provider_evidence(reply)
        self.assertEqual(
            evidence["fields"],
            {"id": "resp-1", "model": "served-model", "finish_reason": "stop"},
        )
        self.assertEqual(evidence["dropped_keys"], ["authorization"])
        self.assertNotIn("sk-should-not-survive", json.dumps(evidence))

    def test_a_scripted_reply_has_no_provider_evidence(self) -> None:
        record = _offline_record()
        for case in record["cases"]:
            self.assertIsNone(case["provider"])


class MutationMatrixTests(unittest.TestCase):
    """The twelve rows the v3 review approved, run as a check rather than read."""

    def test_every_row_behaves_as_promised(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "matrix.json"
            proc = subprocess.run(
                [sys.executable, str(MATRIX_SCRIPT), "--json", str(out)],
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
            )
            rows = json.loads(out.read_text()) if out.exists() else []
        self.assertEqual(proc.returncode, 0, proc.stdout[-1500:] + proc.stderr[-1500:])
        self.assertEqual(len(rows), 12)
        for row in rows:
            self.assertTrue(row["observed_ok"], f"{row['row']}: {row['detail']}")


if __name__ == "__main__":
    unittest.main()
