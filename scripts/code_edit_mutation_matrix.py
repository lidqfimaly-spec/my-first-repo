#!/usr/bin/env python
"""The v3 reverse-mutation matrix, executable.

The v3 review approved a fixed set of mutations and their expected outcomes.
A table in a document is a claim; this script is the check. Every row copies
``src`` / ``scripts`` (and ``tests`` when a check needs pytest) into a
throw-away directory under ``/tmp``, applies one minimal patch, and compares
what happened with what was promised.

Nothing under the repository is modified: the repository is read once per row
into a fresh copy.

    python scripts/code_edit_mutation_matrix.py            # run every row
    python scripts/code_edit_mutation_matrix.py --only validator
    python scripts/code_edit_mutation_matrix.py --json out.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
BASELINE = "src/zevorinth/code_edit_baseline.py"
VALIDATOR = "src/zevorinth/proposal_candidates.py"
APPLIER = "src/zevorinth/workspace_edit.py"
DATASET = "src/zevorinth/evals/code_edit_baseline_v1.json"


class Workdir:
    """One fresh copy of the repository's measurement tree."""

    def __init__(self, name: str, *, with_tests: bool = False) -> None:
        self.name = name
        self.path = Path(tempfile.mkdtemp(prefix=f"zv-matrix-{name}-"))
        for part in ("src", "scripts"):
            shutil.copytree(ROOT / part, self.path / part)
        if with_tests:
            shutil.copytree(ROOT / "tests", self.path / "tests")

    def patch(self, relative: str, old: str, new: str) -> None:
        target = self.path / relative
        text = target.read_text()
        if text.count(old) != 1:
            raise AssertionError(
                f"{self.name}: anchor occurs {text.count(old)} times in "
                f"{relative}"
            )
        target.write_text(text.replace(old, new))

    def write_json(self, relative: str, payload: Any) -> None:
        (self.path / relative).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )

    def run(self, *argv: str, script: str | None = None) -> subprocess.CompletedProcess:
        command = [PYTHON]
        if script:
            command.append(str(self.path / script))
        command.extend(argv)
        return subprocess.run(
            command, cwd=str(self.path), capture_output=True, text=True
        )

    def offline(self, tag: str) -> tuple[int, str, dict[str, Any] | None]:
        out = self.path / f"{tag}.json"
        proc = self.run(
            "--offline", "--out", str(out), script="scripts/run_code_edit_baseline.py"
        )
        record = json.loads(out.read_text()) if out.exists() else None
        return proc.returncode, proc.stdout + proc.stderr, record

    def anchor(self, record_path: Path, store: Path, mode: str = "record"):
        return self.run(
            mode,
            str(record_path),
            "--store",
            str(store),
            script="scripts/anchor_code_edit_evidence.py",
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _digest(record: dict[str, Any] | None, *path: str) -> str | None:
    node: Any = record
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) else None


def _refreeze_dataset(work: Workdir) -> None:
    """A maintainer editing the dataset would also move the frozen digest.

    Without this the change would be stopped one layer too early (by the
    digest guard) and the probe guard under test would never be reached.
    """
    raw = (work.path / DATASET).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    text = (work.path / BASELINE).read_text()
    text = re.sub(
        r'FROZEN_DATASET_SHA256 = \(\s*"[0-9a-f]{64}"\s*\)',
        f'FROZEN_DATASET_SHA256 = (\n    "{digest}"\n)',
        text,
        count=1,
    )
    (work.path / BASELINE).write_text(text)


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------


def row_dataset_content(work: Workdir) -> tuple[bool, str]:
    payload = json.loads((work.path / DATASET).read_text())
    payload["cases"][0]["goal"] += "（未升版本地改了一个字）"
    work.write_json(DATASET, payload)
    code, output, _ = work.offline("d")
    ok = code != 0 and "dataset_digest_mismatch" in output
    return ok, f"rc={code} · {'拒绝加载' if ok else '仍然跑完'}"


def row_prompt_content(work: Workdir) -> tuple[bool, str]:
    work.patch(
        BASELINE,
        'SYSTEM_PROMPT = """',
        'SYSTEM_PROMPT = """ ',
    )
    code, output, _ = work.offline("p")
    ok = code != 0 and "prompt_digest_mismatch" in output
    return ok, f"rc={code} · {'拒绝运行' if ok else '仍然跑完'}"


def row_evaluator_logic(work: Workdir) -> tuple[bool, str]:
    # The anchor moved when V3.1 renamed ``_ratio`` to ``_metric``: the row is
    # about "changing the calculator turns the freeze test red", and that is
    # still the same edit, one call deeper.
    work.patch(
        BASELINE,
        '        "candidate_acceptance": _metric(\n'
        '            "candidate_acceptance", len(accepted), len(edit_candidates)\n'
        '        ),',
        '        "candidate_acceptance": _metric(\n'
        '            "candidate_acceptance", len(accepted) + 1, len(edit_candidates)\n'
        '        ),',
    )
    proc = work.run(
        "-m", "pytest", "tests/test_code_edit_baseline.py", "-q", "-k", "frozen"
    )
    ok = proc.returncode != 0
    tail = (proc.stdout.strip().splitlines() or ["?"])[-1]
    return ok, f"冻结测试 {'转红' if ok else '仍绿'}（{tail}）"


def _dependency_changed(work: Workdir, tag: str, baseline: str) -> tuple[bool, str]:
    code, output, record = work.offline(tag)
    if record is None:
        return False, f"rc={code} 且没有记录：{output.strip().splitlines()[-1:] }"
    digest = _digest(record, "freeze", "dependencies", "sha256")
    ok = digest != baseline
    return ok, f"依赖摘要 {str(digest)[:16]}… {'≠' if ok else '='} 基线"


def row_validator(work: Workdir, baseline: str) -> tuple[bool, str]:
    work.patch(
        VALIDATOR,
        '    """Validate a provider candidate without executing or mutating anything."""\n',
        '    """Validate a provider candidate without executing or mutating anything."""\n'
        '    raise ProposalCandidateError("forced_reject", "matrix row 4")\n',
    )
    return _dependency_changed(work, "v", baseline)


def row_applier(work: Workdir, baseline: str) -> tuple[bool, str]:
    work.patch(
        APPLIER,
        "def apply_text_edit(",
        "def apply_text_edit(  # matrix row 5\n",
    )
    return _dependency_changed(work, "a", baseline)


def row_authorization(work: Workdir, baseline: str) -> tuple[bool, str]:
    work.patch(
        VALIDATOR,
        "    def consume(",
        "    def consume(  # matrix row 6\n",
    )
    return _dependency_changed(work, "c", baseline)


def row_stale_revision(work: Workdir, baseline_record: dict[str, Any], store: Path):
    record = work.path / "r.json"
    code, output, data = work.offline("r")
    if data is None:
        return False, "基线记录没产生"
    shutil.copy(record, work.path / "r.json")
    anchored = work.anchor(record, store)
    if anchored.returncode != 0:
        return False, f"锚点写入失败：{anchored.stdout[-160:]}"
    before = _digest(data, "freeze", "source_tree", "sha256")
    # A package file that is in neither the evaluator nor the dependency
    # closure: the identity labels cannot move, but the revision did.
    revision_file = work.path / "src/zevorinth/__init__.py"
    revision_file.write_text(
        revision_file.read_text()
        + "\n# matrix row 7: revision moved, identity label kept\n"
    )
    verified = work.anchor(record, store, mode="verify")
    ok = verified.returncode != 0 and "源码树摘要" in verified.stdout
    return ok, (
        f"源码树 {str(before)[:12]}… 变动后校验 "
        f"{'拒绝' if ok else '仍通过'}"
    )


def row_tampered_identity(work: Workdir, store: Path) -> tuple[bool, str]:
    work.patch(
        BASELINE,
        '"sha256": _identity_digest(sources),',
        '"sha256": FROZEN_EVALUATOR_SHA256,',
    )
    work.patch(
        BASELINE,
        '    """The six metrics, each with its numerator and denominator."""',
        '    """The six metrics, each with its numerator and denominator."""\n'
        "    # matrix row 8: measurement path changed behind a lying digest",
    )
    code, output, data = work.offline("t")
    if data is None:
        return False, f"离线运行失败 rc={code}"
    proc = work.anchor(work.path / "t.json", store)
    ok = proc.returncode != 0 and "评测器摘要" in proc.stdout
    return ok, (
        f"记录仍报 {_digest(data, 'evaluator_sha256')[:12]}… · "
        f"外部复算{'不一致（拒绝锚定）' if ok else '也认同（漏检）'}"
    )


def _drop_probes(work: Workdir, keep: Callable[[dict], bool]) -> None:
    payload = json.loads((work.path / DATASET).read_text())
    payload["precision_gate_probes"] = [
        probe for probe in payload["precision_gate_probes"] if keep(probe)
    ]
    work.write_json(DATASET, payload)
    _refreeze_dataset(work)


def row_no_accept_probe(work: Workdir) -> tuple[bool, str]:
    _drop_probes(work, lambda p: p["expected"] != "accept")
    code, output, _ = work.offline("na")
    ok = code != 0 and "dataset_missing_control_probe" in output
    return ok, f"rc={code} · {'拒绝加载' if ok else '仍然加载'}"


def row_no_reject_probe(work: Workdir) -> tuple[bool, str]:
    _drop_probes(work, lambda p: p["expected"] != "reject")
    code, output, _ = work.offline("nr")
    ok = code != 0 and "dataset_missing_rejecting_probe" in output
    return ok, f"rc={code} · {'拒绝加载' if ok else '仍然加载'}"


def row_in_scope_unwanted(work: Workdir, baseline_record: dict[str, Any]):
    code, output, data = work.offline("g")
    if data is None:
        return False, f"离线运行失败 rc={code}"
    scope = data["scope_audit"]
    counted = data["metrics"]["incorrect_modification"]["numerator"]
    ok = (
        scope["in_scope_unwanted"] == ["no_change_formatted_boundary"]
        and counted == len(scope["wrong_edits"]) + len(scope["in_scope_unwanted"])
        and scope["disjoint"]
    )
    return ok, (
        f"G={scope['in_scope_unwanted']} · 错误修改率分子 {counted}"
        f"（应改却改错 {len(scope['wrong_edits'])} + G {len(scope['in_scope_unwanted'])}）"
    )


def row_aux_field(work: Workdir, baseline: str) -> tuple[bool, str]:
    work.patch(
        BASELINE,
        "    @property\n    def passed(self) -> bool:\n        return self.failure_class is None",
        "    @property\n    def passed(self) -> bool:\n        return True",
    )
    code, output, record = work.offline("x")
    if record is None:
        return False, f"离线运行失败 rc={code}"
    digest = _digest(record, "evaluator_sha256")
    ok = digest != baseline
    return ok, f"评测器摘要 {str(digest)[:16]}… {'≠' if ok else '='} 基线"


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default=None)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    store = Path(tempfile.mkdtemp(prefix="zv-matrix-store-"))
    rows: list[dict[str, Any]] = []

    # One pristine run supplies the reference digests every other row compares
    # against; it is also the row-11 subject.
    base = Workdir("baseline")
    code, output, baseline_record = base.offline("base")
    if baseline_record is None:
        raise SystemExit(f"baseline run failed: {output[-400:]}")
    baseline_eval = _digest(baseline_record, "evaluator_sha256")
    baseline_dep = _digest(baseline_record, "freeze", "dependencies", "sha256")

    plans: list[tuple[str, str, Callable[[Workdir], tuple[bool, str]]]] = [
        ("dataset_content", "修改数据集内容但不升级身份 → FAIL CLOSED", row_dataset_content),
        ("prompt_content", "修改 Prompt 内容但不升级身份 → FAIL CLOSED", row_prompt_content),
        (
            "evaluator_logic",
            "修改评测器计算逻辑 → FAIL CLOSED",
            row_evaluator_logic,
        ),
        (
            "validator",
            "修改产品候选校验器 → 实验身份变化",
            lambda work: row_validator(work, baseline_dep),
        ),
        (
            "applier",
            "修改补丁应用器 → 实验身份变化",
            lambda work: row_applier(work, baseline_dep),
        ),
        (
            "authorization",
            "修改授权消费逻辑 → 实验身份变化",
            lambda work: row_authorization(work, baseline_dep),
        ),
        (
            "stale_revision",
            "修改修订但沿用旧身份 → 身份校验失败",
            lambda work: row_stale_revision(work, baseline_record, store / "revision"),
        ),
        (
            "tampered_identity",
            "篡改内部身份计算函数 → 外部锚点验证失败",
            lambda work: row_tampered_identity(work, store / "tamper"),
        ),
        ("no_accept_probe", "删除 accept 探针 → 加载失败", row_no_accept_probe),
        ("no_reject_probe", "删除全部 reject 探针 → 加载失败", row_no_reject_probe),
        (
            "in_scope_unwanted",
            "应不改用例做了范围内修改 → 错误修改率正确计入",
            lambda work: row_in_scope_unwanted(work, baseline_record),
        ),
        (
            "aux_field",
            "修改辅助分类字段 → 身份变化（完整性失败）",
            lambda work: row_aux_field(work, baseline_eval),
        ),
    ]

    for name, description, action in plans:
        if args.only and args.only != name:
            continue
        work = Workdir(name, with_tests=(name == "evaluator_logic"))
        try:
            ok, detail = action(work)
        except Exception as exc:  # noqa: BLE001 - a crashed row is a finding
            ok, detail = False, f"行本身出错：{type(exc).__name__}: {exc}"
        finally:
            work.cleanup()
        rows.append(
            {
                "row": name,
                "expectation": description,
                "observed_ok": ok,
                "detail": detail,
            }
        )
        print(f"{'PASS' if ok else 'FAIL'}  {name:20s} {description}")
        print(f"      {detail}")

    base.cleanup()
    shutil.rmtree(store, ignore_errors=True)

    failed = [row for row in rows if not row["observed_ok"]]
    print()
    print(f"{len(rows) - len(failed)}/{len(rows)} rows behaved as promised")
    if args.json:
        Path(args.json).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
        )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
