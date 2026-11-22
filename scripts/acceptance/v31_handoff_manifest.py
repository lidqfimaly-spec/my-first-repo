#!/usr/bin/env python3
"""V3.1 交付清单：给独立验收一个字节级 diff 基准。

这个清单**不是证据，也不是结论**。它只做一件事：把「验收开始时的被评对象」
逐文件钉成 sha256，好让验收方在自己的工作前后各算一次，任何字节变化都会显形。

为什么需要它：V3 独立验收时是用一段临时脚本算的 381 个文件清单，脚本没留下来，
所以那个「零改动」结论只能靠报告自述。这次把清单本身做成可重跑的工具。

用法::

    python scripts/acceptance/v31_handoff_manifest.py capture   # 写 evals/acceptance/v31-handoff.json
    python scripts/acceptance/v31_handoff_manifest.py check     # 与清单比对，有漂移则非零退出
    python scripts/acceptance/v31_handoff_manifest.py check --json   # 机器可读

边界（必须如实理解）：

1. 清单由**实施方**生成。它能证明「验收期间被评对象没变」，不能证明「实施方交付的
   就是评审批准的东西」——后者只能靠人工比对 diff。
2. 清单不覆盖 `evals/results/` 之外的新增运行产物：验收方自己跑出的新结果会以
   `added` 出现，那是预期的，不是漂移。`modified` / `removed` 才是。
3. 清单不构成不可篡改信任根。它可以被一起改掉；它只是让「顺手改一下」变得刺眼。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_SCHEMA = "zevorinth-v31-handoff-manifest-v1"
DEFAULT_MANIFEST = ROOT / "evals" / "acceptance" / "v31-handoff.json"

# 仓库内被纳入钉定的范围。刻意分开写，方便验收方逐项质疑。
REPO_GLOBS: tuple[str, ...] = (
    "src/zevorinth/**/*",
    "scripts/**/*",
    "tests/**/*",
    "docs/p0-2-*.md",
    "evals/results/*.json",
)

# 仓库外的锚点存储：冻结证据本身，也要能被发现被换掉。
STORE_DIR = Path.home() / ".zevorinth" / "evidence-anchors" / "code-edit"

SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collect() -> dict[str, dict[str, object]]:
    """按相对标签收集 {label: {sha256, bytes}}。"""
    files: dict[str, dict[str, object]] = {}
    for pattern in REPO_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            if not path.is_file():
                continue
            if SKIP_DIR_NAMES & set(path.parts):
                continue
            if path.suffix in SKIP_SUFFIXES:
                continue
            label = path.relative_to(ROOT).as_posix()
            files[label] = {"sha256": _sha256(path), "bytes": path.stat().st_size}

    for path in sorted(STORE_DIR.glob("*.json")) if STORE_DIR.is_dir() else []:
        if not path.is_file():
            continue
        label = f"~/.zevorinth/evidence-anchors/code-edit/{path.name}"
        files[label] = {"sha256": _sha256(path), "bytes": path.stat().st_size}
    return files


def _frozen_identities() -> dict[str, object]:
    """记录冻结身份，方便与锚点载荷交叉核对（只读，不参与漂移判定）。"""
    sys.path.insert(0, str(ROOT / "src"))
    from zevorinth import code_edit_baseline as baseline

    evaluator = baseline.evaluator_identity()
    dependencies = baseline.dependency_identity()
    tree = baseline.source_tree_identity()
    return {
        "evaluator_sha256": evaluator["sha256"],
        "evaluator_symbols": evaluator["covered_symbols"],
        "evaluator_symbol_count": len(evaluator["covered_symbols"]),
        "evaluator_version": evaluator["version"],
        "frozen_evaluator_sha256_matches": evaluator["sha256"] == baseline.FROZEN_EVALUATOR_SHA256,
        "dependency_sha256": dependencies["sha256"],
        "dependency_symbols": dependencies["covered_symbols"],
        "dependency_symbol_count": len(dependencies["covered_symbols"]),
        "source_tree_sha256": tree["sha256"],
        "source_tree_files": tree["files"],
        "source_tree_bytes": tree["bytes"],
        "metric_contract_version": baseline.METRIC_CONTRACT_VERSION,
        "anchor_schema": baseline.ANCHOR_SCHEMA,
    }


def _combined(files: dict[str, dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for label in sorted(files):
        digest.update(label.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(files[label]["sha256"]).encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


def build() -> dict[str, object]:
    files = _collect()
    return {
        "schema": MANIFEST_SCHEMA,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": list(REPO_GLOBS),
        "file_count": len(files),
        "bytes": sum(int(meta["bytes"]) for meta in files.values()),
        "combined_sha256": _combined(files),
        "frozen_identities": _frozen_identities(),
        "files": files,
    }


def cmd_capture(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser() if args.out else DEFAULT_MANIFEST
    manifest = build()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"captured {manifest['file_count']} files / {manifest['bytes']} B -> {out}")
    print(f"combined_sha256 = {manifest['combined_sha256']}")
    identities = manifest["frozen_identities"]
    print(
        "frozen: evaluator {} ({} symbols, frozen={}) · deps {} ({} symbols) · tree {} "
        "({} files)".format(
            str(identities["evaluator_sha256"])[:16],
            identities["evaluator_symbol_count"],
            identities["frozen_evaluator_sha256_matches"],
            str(identities["dependency_sha256"])[:16],
            identities["dependency_symbol_count"],
            str(identities["source_tree_sha256"])[:16],
            identities["source_tree_files"],
        )
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.manifest).expanduser() if args.manifest else DEFAULT_MANIFEST
    if not path.is_file():
        print(f"REFUSED: manifest not found: {path}", file=sys.stderr)
        print("先跑 `capture`，或由交付方提供清单。", file=sys.stderr)
        return 2
    expected = json.loads(path.read_text(encoding="utf-8"))
    if expected.get("schema") != MANIFEST_SCHEMA:
        print(f"REFUSED: unexpected manifest schema: {expected.get('schema')}", file=sys.stderr)
        return 2

    recorded: dict[str, dict[str, object]] = expected.get("files") or {}
    current = _collect()

    modified = sorted(
        label
        for label in recorded.keys() & current.keys()
        if recorded[label]["sha256"] != current[label]["sha256"]
    )
    removed = sorted(recorded.keys() - current.keys())
    added = sorted(current.keys() - recorded.keys())

    if args.json:
        print(
            json.dumps(
                {
                    "manifest": str(path),
                    "captured_at": expected.get("captured_at"),
                    "modified": modified,
                    "removed": removed,
                    "added": added,
                    "drift": bool(modified or removed),
                    "recorded_count": len(recorded),
                    "current_count": len(current),
                    "current_combined_sha256": _combined(current),
                },
                indent=2,
            )
        )
        return 0 if not (modified or removed) else 1

    print(f"manifest      : {path}（captured_at {expected.get('captured_at')}）")
    print(f"recorded      : {len(recorded)} files / {expected.get('bytes')} B")
    print(f"current       : {len(current)} files / {sum(int(m['bytes']) for m in current.values())} B")
    print(f"combined      : recorded {str(expected.get('combined_sha256'))[:32]}…")
    print(f"                current  {_combined(current)[:32]}…")

    for label in modified:
        print(f"MODIFIED  {label}")
        print(f"          {str(recorded[label]['sha256'])[:32]}… -> {str(current[label]['sha256'])[:32]}…")
    for label in removed:
        print(f"REMOVED   {label}")
    for label in added:
        print(f"ADDED     {label}（验收期新增运行产物属预期；其它新增须解释）")

    if modified or removed:
        print(
            "\nRESULT: DRIFT（{} 改 / {} 删 / {} 增）".format(len(modified), len(removed), len(added))
        )
        print("被评对象在验收期间发生了字节变化。必须先解释，再谈结论。")
        return 1

    print(f"\nRESULT: NO_DRIFT（0 改 / 0 删 / {len(added)} 增）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="钉定当前被评对象，写出清单")
    capture.add_argument("--out", help=f"输出路径（默认 {DEFAULT_MANIFEST}）")
    capture.set_defaults(func=cmd_capture)

    check = sub.add_parser("check", help="与清单比对，有改/删则非零退出")
    check.add_argument("--manifest", help=f"清单路径（默认 {DEFAULT_MANIFEST}）")
    check.add_argument("--json", action="store_true", help="输出机器可读结果")
    check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
