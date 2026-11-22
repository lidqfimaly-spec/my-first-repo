"""V3.1: the metric layer refuses impossible values, and says why.

Three layers are tested separately because they fail independently:

* the generator (``_metric`` / ``assert_metric_contract``) — must not produce
  the value;
* the seal boundary (``anchor.check_record``) — must not accept the record,
  and must leave the store untouched;
* the audit boundary (``anchor.audit``) — must mark an already-stored entry.

The point of the suite is not "4/3 is rejected" — it is that rejecting it does
not depend on which layer happens to be running.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from zevorinth import code_edit_baseline as baseline  # noqa: E402


def _load_anchor():
    path = ROOT / "scripts" / "anchor_code_edit_evidence.py"
    spec = importlib.util.spec_from_file_location("zv_anchor_v31", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ANCHOR = _load_anchor()
PY = sys.executable
_RECORD_CACHE: dict[str, dict] = {}


def _offline_record() -> dict:
    if "record" in _RECORD_CACHE:
        return json.loads(json.dumps(_RECORD_CACHE["record"]))
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "run.json"
        subprocess.run(
            [
                PY,
                str(ROOT / "scripts/run_code_edit_baseline.py"),
                "--offline",
                "--out",
                str(out),
            ],
            cwd=str(ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        record = json.loads(out.read_text())
    _RECORD_CACHE["record"] = record
    return json.loads(json.dumps(record))


def _forge(mutate, run_id: str = "forged") -> dict:
    record = _offline_record()
    record["run_id"] = run_id
    mutate(record)
    record["anchor_payload"]["result_sha256"] = ANCHOR.recompute_result_seal(record)
    record["anchor_payload"]["combined_sha256"] = ANCHOR.recompute_combined(record)
    return record


def _codes(record) -> list[str]:
    return [name for name, ok, _ in ANCHOR.check_record(record) if not ok]


class MetricCreationTests(unittest.TestCase):
    def test_an_empty_population_is_na_not_zero(self) -> None:
        value = baseline._metric("gate_control_acceptance", 0, 0)
        self.assertIsNone(value["ratio"])
        self.assertEqual(value["status"], "N/A")
        self.assertEqual(
            baseline._metric("gate_control_acceptance", 1, 1)["status"], "OK"
        )

    def test_numerator_above_denominator_is_refused_at_creation(self) -> None:
        for numerator, denominator in ((4, 3), (8, 7), (9, 8)):
            with self.subTest(value=f"{numerator}/{denominator}"):
                with self.assertRaises(baseline.BaselineError) as caught:
                    baseline._metric("candidate_acceptance", numerator, denominator)
                self.assertEqual(caught.exception.code, "metric_denominator_exceeded")

    def test_negative_and_non_integer_counts_are_refused(self) -> None:
        for numerator, denominator in ((-1, 2020), (1, -2), (1.0, 2), (True, 2)):
            with self.subTest(value=f"{numerator}/{denominator}"):
                with self.assertRaises(baseline.BaselineError) as caught:
                    baseline._metric("candidate_acceptance", numerator, denominator)
                self.assertEqual(caught.exception.code, "metric_value_invalid")

    def test_a_name_outside_the_contract_is_refused(self) -> None:
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline._metric("acceptance", 1, 1)
        self.assertEqual(caught.exception.code, "metric_unknown_name")


class SecondDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = _offline_record()
        self.metrics = self.record["metrics"]
        self.tallies = self.record["tallies"]

    def test_the_honest_record_satisfies_its_own_table(self) -> None:
        baseline.assert_metric_contract(self.metrics, self.tallies)

    def test_a_constant_calculator_is_caught_by_the_table(self) -> None:
        constant = {
            name: {"numerator": 1, "denominator": 1, "ratio": 1.0, "status": "OK"}
            for name in baseline.METRIC_NAMES
        }
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.assert_metric_contract(constant, self.tallies)
        self.assertEqual(caught.exception.code, "metric_population_mismatch")

    def test_a_dropped_metric_is_refused(self) -> None:
        metrics = dict(self.metrics)
        metrics.pop("gate_control_acceptance")
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.assert_metric_contract(metrics, self.tallies)
        self.assertEqual(caught.exception.code, "metric_set_incomplete")

    def test_the_gap_cannot_be_merged_into_wrong_edits(self) -> None:
        tallies = json.loads(json.dumps(self.tallies))
        tallies["wrong_edits"] = tallies["in_scope_unwanted"]
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.assert_metric_contract(self.metrics, tallies)
        self.assertEqual(caught.exception.code, "metric_tally_mismatch")


class AnchorInvariantTests(unittest.TestCase):
    """The seal boundary refuses what the generator refuses — and more."""

    def test_the_honest_record_has_no_failing_check(self) -> None:
        self.assertEqual(_codes(_offline_record()), [])

    def test_four_of_three_never_reaches_the_store(self) -> None:
        record = _forge(
            lambda r: r["metrics"]["correct_modification"].update(
                numerator=4, denominator=3, ratio=4 / 3
            )
        )
        self.assertIn("metric_denominator_exceeded:correct_modification", _codes(record))

    def test_eight_of_seven_never_reaches_the_store(self) -> None:
        record = _forge(
            lambda r: r["metrics"]["candidate_acceptance"].update(
                numerator=8, denominator=7, ratio=8 / 7
            )
        )
        self.assertIn("metric_denominator_exceeded:candidate_acceptance", _codes(record))

    def test_an_empty_population_reported_as_zero_is_refused(self) -> None:
        record = _forge(
            lambda r: r["metrics"]["gate_control_acceptance"].update(
                numerator=0, denominator=0, ratio=0.0, status="OK"
            )
        )
        self.assertIn(
            "metric_empty_population_misreported:gate_control_acceptance",
            _codes(record),
        )

    def test_claiming_v2_does_not_escape_the_invariants(self) -> None:
        record = _forge(
            lambda r: r.update(metric_contract="code-edit-metric-contract-v2")
        )
        self.assertIn("metric_contract_field_mismatch", _codes(record))

    def test_claiming_v2_without_the_table_does_not_escape_either(self) -> None:
        def strip(record):
            record["metric_contract"] = "code-edit-metric-contract-v2"
            record.pop("case_table", None)
            record.pop("tallies", None)

        self.assertIn("metric_evidence_missing", _codes(_forge(strip)))

    def test_an_unknown_contract_is_refused_not_guessed(self) -> None:
        record = _forge(
            lambda r: r.update(metric_contract="code-edit-metric-contract-v99")
        )
        self.assertIn("metric_contract_version_unknown", _codes(record))

    def test_the_arithmetic_survives_the_version_field_contract(self) -> None:
        # A v2-shaped record (no status, no table) is still checked for 4/3.
        def as_legacy(record):
            record["metric_contract"] = "code-edit-metric-contract-v2"
            record.pop("case_table", None)
            record.pop("tallies", None)
            for entry in record["metrics"].values():
                entry.pop("status", None)
            record["metrics"]["correct_modification"].update(
                numerator=4, denominator=3, ratio=4 / 3
            )

        codes = _codes(_forge(as_legacy))
        self.assertIn("metric_denominator_exceeded:correct_modification", codes)


class FullCasesTests(unittest.TestCase):
    """``full_cases`` goes to the disk; it must fail closed, never downgrade."""

    def test_the_honest_record_passes_full_verification(self) -> None:
        for name, ok, detail in ANCHOR.full_cases_checks(_offline_record()):
            self.assertTrue(ok, f"{name}: {detail}")

    def test_a_missing_dataset_is_a_failure_not_a_downgrade(self) -> None:
        def point_elsewhere(record):
            # Both copies move, so the claim stays consistent and the absence
            # itself is what has to be reported.
            record["dataset"]["path"] = "no-such-dataset.json"
            record["anchor_payload"]["dataset"]["file"] = "no-such-dataset.json"

        checks = ANCHOR.full_cases_checks(_forge(point_elsewhere))
        self.assertEqual([name for name, ok, _ in checks if not ok], ["dataset_missing"])

    def test_a_dataset_digest_that_does_not_match_disk_is_refused(self) -> None:
        def claim_matches_a_different_file(record):
            # Both copies move together, so the claim is internally consistent
            # and the *disk* comparison is what has to catch it.
            record["dataset"]["sha256"] = "0" * 64
            record["anchor_payload"]["dataset"]["sha256"] = "0" * 64

        record = _forge(claim_matches_a_different_file)
        failing = [name for name, ok, _ in ANCHOR.full_cases_checks(record) if not ok]
        self.assertEqual(failing, ["dataset_disk_mismatch"])

    def test_retargeting_the_top_level_dataset_claim_is_refused(self) -> None:
        # The top-level block is what `--full` reads, and it is NOT inside
        # result_sha256. Left unchecked it could point at another dataset while
        # the seal and the chain both stayed valid.
        record = _forge(lambda r: r["dataset"].update(sha256="1" * 64))
        self.assertIn("dataset_claim_consistency", _codes(record))
        self.assertEqual(
            [name for name, ok, _ in ANCHOR.full_cases_checks(record) if not ok],
            ["dataset_claim_consistency"],
        )

    def test_retargeting_the_top_level_prompt_claim_is_refused(self) -> None:
        record = _forge(lambda r: r.update(prompt_sha256="2" * 64))
        self.assertIn("prompt_claim_consistency", _codes(record))

    def test_a_replaced_dataset_file_is_refused_even_when_the_claim_matches_it(
        self,
    ) -> None:
        """Swap the dataset on disk *and* the digest beside it; still refused.

        This is the "new dataset impersonating the old version" path: the claim
        is consistent, so the digests agree — and the file is still not the one
        the anchor pinned. Only the pinned copy can catch it.
        """
        import hashlib

        honest = _offline_record()
        source = ROOT / "src" / "zevorinth" / "evals" / honest["dataset"]["path"]
        payload = json.loads(source.read_text("utf-8"))
        payload["cases"][0]["expected"]["kind"] = "refuse"  # a different dataset

        with tempfile.TemporaryDirectory() as tmp:
            swapped = Path(tmp) / "swapped.json"
            swapped.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
            digest = hashlib.sha256(swapped.read_bytes()).hexdigest()
            record = _forge(
                lambda r: (
                    r["dataset"].update(sha256=digest),
                    r["anchor_payload"]["dataset"].update(sha256=digest),
                )
            )
            original = ANCHOR._dataset_path
            ANCHOR._dataset_path = lambda _record: swapped
            try:
                failing = [name for name, ok, _ in ANCHOR.full_cases_checks(record) if not ok]
            finally:
                ANCHOR._dataset_path = original
        # The digests now agree, so the only thing left to notice is content.
        self.assertIn("dataset_expected_kind_mismatch", failing)

    def test_an_action_that_contradicts_the_model_reply_is_refused(self) -> None:
        def lie_about_action(record):
            record["cases"][0]["action"] = "refuse"
            record["case_table"][0]["action"] = "refuse"

        failing = [
            name
            for name, ok, _ in ANCHOR.full_cases_checks(_forge(lie_about_action))
            if not ok
        ]
        self.assertIn("reply_action_mismatch", failing)

    def test_a_table_that_contradicts_the_cases_is_refused(self) -> None:
        def lie_in_table(record):
            record["case_table"][0]["passed"] = False

        failing = [
            name
            for name, ok, _ in ANCHOR.full_cases_checks(_forge(lie_in_table))
            if not ok
        ]
        self.assertIn("case_table_vs_cases", failing)


class RefusalLeavesNoTraceTests(unittest.TestCase):
    def test_a_refused_record_does_not_touch_the_store(self) -> None:
        record = _forge(
            lambda r: r["metrics"]["candidate_acceptance"].update(
                numerator=8, denominator=7, ratio=8 / 7
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            path = Path(tmp) / "record.json"
            path.write_text(json.dumps(record, ensure_ascii=False))
            before = ANCHOR.store_fingerprint(store)
            first = subprocess.run(
                [PY, str(ROOT / "scripts/anchor_code_edit_evidence.py"),
                 "record", str(path), "--store", str(store)],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            second = subprocess.run(
                [PY, str(ROOT / "scripts/anchor_code_edit_evidence.py"),
                 "record", str(path), "--store", str(store)],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            after = ANCHOR.store_fingerprint(store)
            self.assertNotEqual(first.returncode, 0)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(before, after)
            self.assertFalse(store.exists(), "a refused write must not create the store")

    def test_a_legal_record_still_anchors_after_a_refusal(self) -> None:
        good = _offline_record()
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            path = Path(tmp) / "good.json"
            path.write_text(json.dumps(good, ensure_ascii=False))
            proc = subprocess.run(
                [PY, str(ROOT / "scripts/anchor_code_edit_evidence.py"),
                 "record", str(path), "--store", str(store)],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            entries = sorted(p.name for p in store.glob("*.json"))
            self.assertEqual(len(entries), 1)
            self.assertTrue(entries[0].startswith("0001-"))


class AuditBoundaryTests(unittest.TestCase):
    def test_an_impossible_value_already_in_the_store_is_marked(self) -> None:
        good = _offline_record()
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            path = Path(tmp) / "good.json"
            path.write_text(json.dumps(good, ensure_ascii=False))
            subprocess.run(
                [PY, str(ROOT / "scripts/anchor_code_edit_evidence.py"),
                 "record", str(path), "--store", str(store)],
                cwd=str(ROOT), check=True, capture_output=True, text=True,
            )
            entry = next(store.glob("0001-*.json"))
            payload = json.loads(entry.read_text())
            payload["payload"]["metrics"]["correct_modification"].update(
                numerator=4, denominator=3, ratio=4 / 3
            )
            entry.write_text(json.dumps(payload, ensure_ascii=False))
            proc = subprocess.run(
                [PY, str(ROOT / "scripts/anchor_code_edit_evidence.py"),
                 "audit", "--store", str(store)],
                cwd=str(ROOT), capture_output=True, text=True,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("metric_denominator_exceeded", proc.stdout)
            self.assertIn("payload_with_case_table", proc.stdout)


if __name__ == "__main__":
    unittest.main()
