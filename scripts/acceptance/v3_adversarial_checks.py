"""Adversarial checks A / D: what the external flow refuses and what it accepts.

Everything runs on copies in /tmp; the repository is only read.
"""
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
STORE = Path("/tmp/zv3-adversarial-store")
RECORD = ROOT / "evals/results/live-deepseek-chat-v3-a.json"


def load_anchor_module():
    spec = importlib.util.spec_from_file_location(
        "zv_anchor_probe", ROOT / "scripts/anchor_code_edit_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seal(anchor_mod, record):
    """Exactly what an attacker with source access does: recompute both seals."""
    record["anchor_payload"]["result_sha256"] = anchor_mod.recompute_result_seal(record)
    record["anchor_payload"]["combined_sha256"] = anchor_mod.recompute_combined(record)
    return record


def run(args, cwd=ROOT):
    proc = subprocess.run([PY, *args], cwd=str(cwd), capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    anchor_mod = load_anchor_module()
    findings = []
    if STORE.exists():
        shutil.rmtree(STORE)
    STORE.mkdir(parents=True)

    # ---------------------------------------------------------------- D1
    section("检查 D-1：外部流程是否接受 分子 > 分母 的结论（4/3 = 133.3%）")
    base = json.loads(RECORD.read_text())
    forged = json.loads(RECORD.read_text())
    forged["run_id"] = "forged-D1-4of3"
    forged["metrics"]["correct_modification"] = {
        "numerator": 4, "denominator": 3, "ratio": 4 / 3,
    }
    seal(anchor_mod, forged)
    path = Path("/tmp/zv3-forged-4of3.json")
    path.write_text(json.dumps(forged, ensure_ascii=False, indent=2))
    code, out = run(["scripts/anchor_code_edit_evidence.py", "record", str(path),
                     "--store", str(STORE)])
    print(f"伪造：correct_modification = 4/3 = {4/3:.1%}")
    print(out.strip())
    print(f"--> rc={code}   {'ACCEPTED（拒绝能力缺失）' if code == 0 else 'REFUSED'}")
    if code == 0:
        findings.append("D1: 外部流程接受 4/3=133.3% 的结论并完成封存")

    # ---------------------------------------------------------------- D1b
    section("检查 D-2：外部流程是否接受 8/7 的候选接受率（上一轮的原始形态）")
    forged2 = json.loads(RECORD.read_text())
    forged2["run_id"] = "forged-D2-8of7"
    forged2["metrics"]["candidate_acceptance"] = {
        "numerator": 8, "denominator": 7, "ratio": 8 / 7,
    }
    seal(anchor_mod, forged2)
    path2 = Path("/tmp/zv3-forged-8of7.json")
    path2.write_text(json.dumps(forged2, ensure_ascii=False, indent=2))
    code2, out2 = run(["scripts/anchor_code_edit_evidence.py", "record", str(path2),
                       "--store", str(STORE)])
    print(f"伪造：candidate_acceptance = 8/7 = {8/7:.1%}")
    print(out2.strip())
    print(f"--> rc={code2}   {'ACCEPTED（拒绝能力缺失）' if code2 == 0 else 'REFUSED'}")
    if code2 == 0:
        findings.append("D2: 外部流程接受 8/7=114.3% 的结论并完成封存")

    # ---------------------------------------------------------------- D3
    section("检查 D-3：把覆盖符号从 48 缩到 1，摘要自洽是否仍被接受")
    forged3 = json.loads(RECORD.read_text())
    forged3["run_id"] = "forged-D3-narrow-coverage"
    one = [forged3["evaluator_coverage"][0]]
    forged3["evaluator_coverage"] = one
    forged3["evaluator_sha256"] = anchor_mod.recompute_evaluator(one)["sha256"]
    seal(anchor_mod, forged3)
    path3 = Path("/tmp/zv3-forged-narrow.json")
    path3.write_text(json.dumps(forged3, ensure_ascii=False, indent=2))
    code3, out3 = run(["scripts/anchor_code_edit_evidence.py", "record", str(path3),
                       "--store", str(STORE)])
    print(f"覆盖符号：48 -> {len(one)}（{one[0]}），摘要按单符号重算")
    print(out3.strip())
    print(f"--> rc={code3}   {'ACCEPTED（覆盖可被收窄）' if code3 == 0 else 'REFUSED'}")
    if code3 == 0:
        findings.append("D3: 覆盖符号列表来自记录本身，48 可被收窄到 1 仍被封存")

    # ---------------------------------------------------------------- A1
    section("检查 A-1：数据集被替换后，外部复算是否仍然通过")
    code0, out0 = run(["scripts/anchor_code_edit_evidence.py", "record", str(RECORD),
                       "--store", str(STORE)])
    print(f"先在临时 store 里为原始记录建锚点：rc={code0}")
    work = Path("/tmp/zv3-acceptance-kept/dataset-swap")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    for part in ("src", "scripts"):
        shutil.copytree(ROOT / part, work / part)
    dataset = work / "src/zevorinth/evals/code_edit_baseline_v1.json"
    before = hashlib.sha256(dataset.read_bytes()).hexdigest()
    payload = json.loads(dataset.read_text())
    payload["cases"][0]["request"] = payload["cases"][0].get("request", "") + "（被审计者替换过）"
    dataset.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    after = hashlib.sha256(dataset.read_bytes()).hexdigest()
    print(f"数据集 sha256：{before[:16]}… -> {after[:16]}…（记录里的 dataset.sha256 未改）")
    code4, out4 = run(["scripts/anchor_code_edit_evidence.py", "verify",
                       str(RECORD), "--store", str(STORE)], cwd=work)
    print(out4.strip().splitlines()[-1] if out4.strip() else "")
    verified = "VERIFIED" in out4 and "NOT VERIFIED" not in out4
    print(f"--> rc={code4}   {'仍然 VERIFIED（数据集摘要未被复算）' if verified else '已拒绝'}")
    if verified:
        findings.append("A1: 数据集内容被替换后 verify 仍返回 VERIFIED（未复算 dataset/prompt 摘要）")

    # ---------------------------------------------------------------- A2
    section("检查 A-2：锚点文件被篡改后，链审计是否转红")
    store2 = Path("/tmp/zv3-tamper-store")
    if store2.exists():
        shutil.rmtree(store2)
    store2.mkdir(parents=True)
    run(["scripts/anchor_code_edit_evidence.py", "record", str(RECORD), "--store", str(store2)])
    record_b = ROOT / "evals/results/live-deepseek-chat-v3-b.json"
    run(["scripts/anchor_code_edit_evidence.py", "record", str(record_b), "--store", str(store2)])
    files = sorted(store2.glob("*.json"))
    entry = json.loads(files[0].read_text())
    entry["payload"]["metrics"]["correct_modification"]["numerator"] = 99
    files[0].write_text(json.dumps(entry, ensure_ascii=False, indent=2))
    code5, out5 = run(["scripts/anchor_code_edit_evidence.py", "audit", "--store", str(store2)])
    print(out5.strip())
    print(f"--> rc={code5}   {'转红' if code5 != 0 else '未转红（链审计失效）'}")
    if code5 == 0:
        findings.append("A2: 篡改 0001 的内容后，链审计仍然 intact")

    # ---------------------------------------------------------------- A3
    section("检查 A-3：锚点仓库放在仓库内是否被拒绝")
    code6, out6 = run(["scripts/anchor_code_edit_evidence.py", "record", str(RECORD),
                       "--store", str(ROOT / "evals/anchors")])
    print(out6.strip()[-200:])
    print(f"--> rc={code6}   {'已拒绝' if code6 != 0 else '未拒绝'}")

    print("\n" + "=" * 72)
    print("发现：")
    for item in findings:
        print(" -", item)
    print(f"共 {len(findings)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
