#!/usr/bin/env python
"""V3.1 metric-invariant matrix: 23 rows, each with a declared outcome.

Every row starts from the same offline record, applies exactly one change, and
is then pushed through the *external* anchor flow. A row passes only when the
observed rejection is the one promised — "it refused somehow" is not a result.

Rows 1-17 exercise the invariants (math, version dispatch, set and tallies).
Rows 18-21 exercise refusal atomicity: the store must be byte-identical after
a refused write. Rows 22a/22b are positive samples: an over-strict guard that
cannot express ``N/A`` would fail them.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
ANCHOR = ROOT / "scripts" / "anchor_code_edit_evidence.py"
RUNNER = ROOT / "scripts" / "run_code_edit_baseline.py"


def load_anchor():
    spec = importlib.util.spec_from_file_location("zv_anchor", ANCHOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


A = load_anchor()


def offline_record(work: Path) -> dict:
    out = work / "base.json"
    subprocess.run(
        [PY, str(RUNNER), "--offline", "--out", str(out)],
        cwd=str(ROOT), capture_output=True, text=True, check=True,
    )
    return json.loads(out.read_text())


def resign(record: dict) -> dict:
    """What an attacker with source access does: recompute both seals."""
    record["anchor_payload"]["result_sha256"] = A.recompute_result_seal(record)
    record["anchor_payload"]["combined_sha256"] = A.recompute_combined(record)
    return record


def run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(ANCHOR), *argv], cwd=str(ROOT), capture_output=True, text=True
    )


class Row:
    def __init__(self, base: dict, work: Path) -> None:
        self.base = base
        self.work = work
        self.store = Path(tempfile.mkdtemp(prefix="zv31-store-"))

    def record(self, mutate, name: str, *, reseal: bool = True) -> dict:
        rec = json.loads(json.dumps(self.base))
        rec["run_id"] = name
        mutate(rec)
        if reseal:
            resign(rec)
        path = self.work / f"{name}.json"
        path.write_text(json.dumps(rec, ensure_ascii=False))
        return {"rec": rec, "path": path}

    def anchor(self, path: Path) -> subprocess.CompletedProcess:
        return run("record", str(path), "--store", str(self.store))

    def verify(self, path: Path, *, full: bool = False) -> subprocess.CompletedProcess:
        args = ["verify", str(path), "--store", str(self.store)]
        if full:
            args.append("--full")
        return run(*args)

    def files(self) -> list[Path]:
        return sorted(self.store.glob("*"))

    def fingerprint(self) -> str:
        return A.store_fingerprint(self.store)

    def close(self) -> None:
        shutil.rmtree(self.store, ignore_errors=True)


def expect(proc: subprocess.CompletedProcess, code: str, *, rc: int | None = None):
    text = proc.stdout + proc.stderr
    ok_code = code in text
    ok_rc = (proc.returncode != 0) if rc is None else (proc.returncode == rc)
    return ok_code and ok_rc, f"rc={proc.returncode} · {'命中 ' + code if ok_code else '未命中 ' + code}"


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="zv31-matrix-"))
    base = offline_record(work)
    rows: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        rows.append((name, ok, detail))

    # ---------------------------------------------------------------- math
    def row(name, mutate, code):
        ctx = Row(base, work)
        try:
            forged = ctx.record(mutate, name)
            proc = ctx.anchor(forged["path"])
            ok, detail = expect(proc, code)
            add(name, ok and not ctx.files(), detail + f" · store={len(ctx.files())} 文件")
        finally:
            ctx.close()

    row(
        "01-4of3",
        lambda r: r["metrics"]["correct_modification"].update(
            numerator=4, denominator=3, ratio=4 / 3
        ),
        "metric_denominator_exceeded",
    )
    row(
        "02-8of7",
        lambda r: r["metrics"]["candidate_acceptance"].update(
            numerator=8, denominator=7, ratio=8 / 7
        ),
        "metric_denominator_exceeded",
    )
    row(
        "03-empty-as-zero",
        lambda r: r["metrics"]["gate_control_acceptance"].update(
            numerator=0, denominator=0, ratio=0.0, status="OK"
        ),
        "metric_empty_population_misreported",
    )
    row(
        "04-empty-without-status",
        lambda r: r["metrics"]["gate_control_acceptance"].pop("status", None),
        "metric_contract_field_missing",
    )
    row(
        "05-negative",
        lambda r: r["metrics"]["unrelated_modification"].update(
            numerator=-1, ratio=-0.125
        ),
        "metric_value_invalid",
    )
    row(
        "06-num-eq-den-plus-one",
        lambda r: r["metrics"]["incorrect_modification"].update(
            numerator=9, denominator=8, ratio=1.125
        ),
        "metric_denominator_exceeded",
    )
    row(
        "07-ratio-inconsistent",
        lambda r: r["metrics"]["correct_modification"].update(ratio=0.9),
        "metric_ratio_inconsistent",
    )

    # ------------------------------------------------------------- version
    def as_v2(rec):
        rec["metric_contract"] = "code-edit-metric-contract-v2"

    row("09-forged-v2-label", as_v2, "metric_contract_field_mismatch")

    def v3_missing_status(rec):
        for entry in rec["metrics"].values():
            entry.pop("status", None)

    row("10-forged-v3-without-status", v3_missing_status, "metric_contract_field_missing")
    row(
        "11-contract-missing",
        lambda r: r.pop("metric_contract", None),
        "metric_contract_missing",
    )
    row(
        "12-contract-unknown",
        lambda r: r.update(metric_contract="code-edit-metric-contract-v99"),
        "metric_contract_version_unknown",
    )

    def tampered_tallies(rec):
        rec["tallies"]["modified"] = rec["tallies"]["modified"][:-1]

    row("13-tallies-drift", tampered_tallies, "metric_tally_mismatch")

    def drop_metric(rec):
        rec["metrics"].pop("gate_control_acceptance")

    row("14-v3-missing-metric", drop_metric, "metric_set_complete")
    row(
        "15-renamed-metric",
        lambda r: r["metrics"].update(
            acceptance=r["metrics"].pop("candidate_acceptance")
        ),
        "metric_set_complete",
    )

    def tamper_scope(rec):
        rec["scope_audit"]["in_scope_unwanted"] = ["fix_guard_normal"]

    row("16-scope-audit-drift", tamper_scope, "metric_tally_mismatch:scope_audit")

    # ------------------------------------------------ refusal atomicity
    ctx = Row(base, work)
    try:
        forged = ctx.record(
            lambda r: r["metrics"]["candidate_acceptance"].update(
                numerator=8, denominator=7, ratio=8 / 7
            ),
            "18-refusal",
        )
        before = ctx.fingerprint()
        first = ctx.anchor(forged["path"])
        second = ctx.anchor(forged["path"])
        after = ctx.fingerprint()
        add(
            "18-retry-twice",
            first.returncode != 0 and second.returncode != 0 and len(ctx.files()) == 0,
            f"rc={first.returncode}/{second.returncode} · store={len(ctx.files())} 文件",
        )
        add(
            "19-fingerprint-unchanged",
            before == after,
            f"{before[:16]}… -> {after[:16]}…",
        )
        leftovers = [p.name for p in ctx.store.glob(".*") if p.name not in (".", "..")]
        add("20-no-temp-leftover", not leftovers, f"残留={leftovers}")

        good = ctx.record(lambda r: None, "19-then-good", reseal=False)
        proc = ctx.anchor(good["path"])
        names = [p.name for p in ctx.files()]
        add(
            "21-legal-after-refusal",
            proc.returncode == 0 and any(n.startswith("0001-") for n in names),
            f"rc={proc.returncode} · {names}",
        )
    finally:
        ctx.close()

    # ------------------------------------------------ in-process: row 08/08b
    # 08: the same case counted twice reaches the numerator. The generation
    # boundary refuses because the table and the metrics disagree.
    from zevorinth import code_edit_baseline as B  # noqa: E402

    tallies = json.loads(json.dumps(base["tallies"]))
    doubled = dict(base["metrics"]["candidate_acceptance"])
    doubled["numerator"] = 9
    metrics = json.loads(json.dumps(base["metrics"]))
    metrics["candidate_acceptance"] = doubled
    try:
        B.assert_metric_contract(metrics, tallies)
        add("08-duplicate-sample", False, "未拒绝")
    except B.BaselineError as exc:
        add("08-duplicate-sample", True, f"生成边界 {exc.code}")

    # 08b: the internal calculator replaced by a constant. The second
    # derivation refuses, so no record is written.
    patched = work / "constant-metric"
    shutil.copytree(ROOT / "src", patched / "src")
    shutil.copytree(ROOT / "scripts", patched / "scripts")
    target = patched / "src/zevorinth/code_edit_baseline.py"
    text = target.read_text()
    marker = 'def _metric(name: str, numerator: int, denominator: int) -> dict[str, Any]:'
    assert text.count(marker) == 1
    text = text.replace(
        marker,
        marker + '\n    return {"numerator": 1, "denominator": 1, "ratio": 1.0, "status": "OK"}',
        1,
    )
    target.write_text(text)
    proc = subprocess.run(
        [PY, str(patched / "scripts/run_code_edit_baseline.py"), "--offline",
         "--out", str(patched / "out.json")],
        cwd=str(patched), capture_output=True, text=True,
    )
    refused = proc.returncode != 0 and not (patched / "out.json").exists()
    add(
        "08b-constant-calculator",
        refused,
        f"rc={proc.returncode} · 记录{'未' if refused else ''}写入",
    )

    # ------------------------------------------------------- positive samples
    ctx = Row(base, work)
    try:
        good = ctx.record(lambda r: None, "22a-normal", reseal=False)
        proc = ctx.anchor(good["path"])
        add("22a-normal-record", proc.returncode == 0, f"rc={proc.returncode}")
    finally:
        ctx.close()

    ctx = Row(base, work)
    try:
        def empty_population(rec):
            rec["tallies"]["gate_probes"].update(controls=0, controls_accepted=0)
            rec["metrics"]["gate_control_acceptance"] = {
                "numerator": 0, "denominator": 0, "ratio": None, "status": "N/A",
            }

        naive = ctx.record(empty_population, "22b-empty")
        proc = ctx.anchor(naive["path"])
        text = proc.stdout + proc.stderr
        add(
            "22b-empty-population",
            proc.returncode == 0 and "FAIL" not in text,
            f"rc={proc.returncode} · {'空分母以 N/A 通过' if 'N/A' in text else '未见 N/A'}",
        )
    finally:
        ctx.close()

    # ---------------------------------------------------------------- report
    print()
    for name, ok, detail in sorted(rows):
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    bad = [name for name, ok, _ in rows if not ok]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} rows behaved as promised")
    if bad:
        print("failed: " + ", ".join(bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
