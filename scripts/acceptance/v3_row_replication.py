"""Independent replication of matrix rows 9 / 10 / 12 in a kept copy.

The matrix script deletes its workdirs, so "the mutation really happened"
cannot be inspected afterwards. This repeats the three most critical rows in
/tmp/zv3-acceptance-kept and leaves every copy on disk.
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
BASELINE = "src/zevorinth/code_edit_baseline.py"
DATASET = "src/zevorinth/evals/code_edit_baseline_v1.json"
KEEP = Path("/tmp/zv3-acceptance-kept")
BASELINE_DATASET_DIGEST = "835f2bb0610ca85a538779f15c74b50e177644ac3fee6931a8eff7165cb4193e"
BASELINE_EVALUATOR_DIGEST = "0c9bc436ff42436328ccdda904a3f32906d46275be95dabd7d490ef85d1681c3"


def fresh(name: str) -> Path:
    work = KEEP / name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    for part in ("src", "scripts"):
        shutil.copytree(ROOT / part, work / part)
    return work


def refreeze(work: Path) -> str:
    digest = hashlib.sha256((work / DATASET).read_bytes()).hexdigest()
    text = (work / BASELINE).read_text()
    new, n = re.subn(
        r'FROZEN_DATASET_SHA256 = \(\s*"[0-9a-f]{64}"\s*\)',
        f'FROZEN_DATASET_SHA256 = (\n    "{digest}"\n)',
        text,
        count=1,
    )
    assert n == 1, "frozen digest anchor not found"
    (work / BASELINE).write_text(new)
    return digest


def run(work: Path, tag: str):
    out = work / f"{tag}.json"
    proc = subprocess.run(
        [PY, str(work / "scripts/run_code_edit_baseline.py"), "--offline", "--out", str(out)],
        cwd=str(work), capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def probes(work: Path):
    return json.loads((work / DATASET).read_text())["precision_gate_probes"]


def report(title, work, code, output, extra):
    print(f"\n=== {title} ===")
    print(f"  workdir              : {work}")
    print(f"  变异后数据集 sha256   : {hashlib.sha256((work / DATASET).read_bytes()).hexdigest()[:16]}…")
    frozen = re.search(r'FROZEN_DATASET_SHA256 = \(\s*"([0-9a-f]{64})"',
                       (work / BASELINE).read_text()).group(1)
    print(f"  冻结常量（副本内）    : {frozen[:16]}…")
    print(f"  探针 expected 分布    : {[p['expected'] for p in probes(work)]}")
    print(f"  {extra}")
    err = [ln for ln in output.splitlines() if "refused" in ln.lower() or "dataset_" in ln]
    print(f"  rc={code} · {err[:2]}")


def main() -> int:
    KEEP.mkdir(parents=True, exist_ok=True)

    w1 = fresh("row09-no-accept-probe")
    payload = json.loads((w1 / DATASET).read_text())
    payload["precision_gate_probes"] = [
        p for p in payload["precision_gate_probes"] if p["expected"] != "accept"
    ]
    (w1 / DATASET).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    d1 = refreeze(w1)
    code1, out1 = run(w1, "na")
    report("行9 删除全部 accept 探针（已再冻结）", w1, code1, out1,
           f"refreeze -> {d1[:16]}… （基线 {BASELINE_DATASET_DIGEST[:16]}…）")
    ok9 = code1 != 0 and "dataset_missing_control_probe" in out1

    w2 = fresh("row10-no-reject-probe")
    payload = json.loads((w2 / DATASET).read_text())
    payload["precision_gate_probes"] = [
        p for p in payload["precision_gate_probes"] if p["expected"] != "reject"
    ]
    (w2 / DATASET).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    d2 = refreeze(w2)
    code2, out2 = run(w2, "nr")
    report("行10 删除全部 reject 探针（已再冻结）", w2, code2, out2, f"refreeze -> {d2[:16]}…")
    ok10 = code2 != 0 and "dataset_missing_rejecting_probe" in out2

    w3 = fresh("row12-aux-field")
    path = w3 / BASELINE
    text = path.read_text()
    old = 'VARIANTS = ("normal", "boundary", "adversarial")'
    assert text.count(old) == 1, text.count(old)
    path.write_text(text.replace(old, 'VARIANTS = ("boundary", "normal", "adversarial")'))
    proc = subprocess.run(
        [PY, "-c",
         "import sys; sys.path.insert(0,'src');"
         "from zevorinth import code_edit_baseline as m;"
         "print(m.evaluator_identity()['sha256'])"],
        cwd=str(w3), capture_output=True, text=True,
    )
    got = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr.strip()[-300:]
    print("\n=== 行12 修改辅助分类字段（VARIANTS 顺序）===")
    print(f"  workdir            : {w3}")
    print(f"  变异后评测器摘要    : {got[:16]}…")
    print(f"  基线评测器摘要      : {BASELINE_EVALUATOR_DIGEST[:16]}…")
    ok12 = proc.returncode == 0 and got != BASELINE_EVALUATOR_DIGEST

    print(f"\n行9={ok9} 行10={ok10} 行12={ok12}")
    return 0 if (ok9 and ok10 and ok12) else 1


if __name__ == "__main__":
    sys.exit(main())
