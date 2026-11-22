#!/usr/bin/env python
"""Run the P0-2 code-edit quality baseline.

Two modes, and they are not interchangeable:

* ``--offline`` replays the scripted candidates in
  ``evals/code_edit_baseline_v1_replies.json``. It proves the *evaluator* works
  and gives CI a deterministic result. It says nothing about any model.
* ``--live`` asks a real provider. It records the model identifier the provider
  returned, not the one that was requested.

Neither mode grants anything. A baseline is evidence about edit quality; it is
not permission for a model to edit code in the product.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from zevorinth import code_edit_baseline as baseline  # noqa: E402


def _load_scripted_replies() -> dict[str, str]:
    import hashlib

    path = (
        baseline.DATASET_PATH.parent
        / "code_edit_baseline_v1_replies.json"
    )
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != baseline.FROZEN_REPLIES_SHA256:
        raise SystemExit(
            "scripted replies do not match FROZEN_REPLIES_SHA256: "
            f"{digest[:16]}… vs {baseline.FROZEN_REPLIES_SHA256[:16]}…"
        )
    payload = json.loads(raw.decode("utf-8"))
    return {
        case_id: json.dumps(reply, ensure_ascii=False)
        for case_id, reply in payload["replies"].items()
    }


def _fill_digests(
    dataset: baseline.Dataset, replies: dict[str, str]
) -> dict[str, str]:
    """Let scripted candidates bind to the digest the prompt would show."""
    import hashlib

    import json as _json

    filled: dict[str, str] = {}
    by_id = {case["id"]: case for case in dataset.cases}
    for case_id, raw in replies.items():
        case = by_id[case_id]
        candidate = _json.loads(raw)
        if candidate.get("expected_sha256") == "__FROM_CONTEXT__":
            name = candidate["path"]
            content = case["files"][name].encode("utf-8")
            candidate["expected_sha256"] = hashlib.sha256(content).hexdigest()
        filled[case_id] = _json.dumps(candidate, ensure_ascii=False)
    return filled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", default=os.environ.get("ZEVORINTH_MODEL"))
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "ZEVORINTH_BASELINE_BASE_URL", "https://api.deepseek.com"
        ),
    )
    parser.add_argument(
        "--api-key-env", default="DEEPSEEK_API_KEY"
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--out", default=None)
    parser.add_argument("--report", default=None)
    parser.add_argument("--notes", default=None)
    parser.add_argument(
        "--project-root",
        default=None,
        help="where case workspaces are materialised (default: temp dir)",
    )
    args = parser.parse_args(argv)
    if args.offline == args.live:
        parser.error("choose exactly one of --offline / --live")

    dataset = baseline.load_dataset()
    temporary: tempfile.TemporaryDirectory | None = None
    if args.project_root:
        project_root = Path(args.project_root)
        project_root.mkdir(parents=True, exist_ok=True)
    else:
        temporary = tempfile.TemporaryDirectory()
        project_root = Path(temporary.name)

    client = None
    replies = None
    label = "offline-scripted"
    if args.live:
        api_key = os.environ.get(args.api_key_env, "")
        if not api_key:
            print(
                f"refusing to run live without {args.api_key_env}",
                file=sys.stderr,
            )
            return 2
        if not args.model:
            print("refusing to run live without --model", file=sys.stderr)
            return 2
        label = f"live-{args.model}"
        client = baseline.ChatCompletionsClient(
            base_url=args.base_url,
            api_key=api_key,
            model=args.model,
            name="chat-completions",
            temperature=args.temperature,
            max_tokens=args.max_tokens,
        )
    else:
        replies = _fill_digests(dataset, _load_scripted_replies())

    run = baseline.run_baseline(
        dataset,
        client=client,
        project_root=project_root,
        scripted_replies=replies,
        notes=args.notes,
        run_label=label,
    )
    if temporary is not None:
        temporary.cleanup()

    out = (
        Path(args.out)
        if args.out
        else ROOT / "evals" / "results" / f"{label}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = baseline.render_report(run)
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nraw results: {out}")
    return 0 if not run["failures"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except baseline.BaselineError as exc:
        # The code is the machine-readable half of a refusal; a reviewer greps
        # for it, so it must not be buried inside a traceback's message.
        print(f"refused [{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(3) from None
