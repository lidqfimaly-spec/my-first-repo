"""Validate a coverage.py JSON report against safety-module thresholds."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


THRESHOLDS = {
    "artifact_tool.py": (70.0, 60.0),
    "conversation_service.py": (60.0, 55.0),
    "event_store.py": (80.0, 65.0),
    "local_test_runner.py": (70.0, 60.0),
    "policy.py": (70.0, 65.0),
    "safety_gate.py": (75.0, 60.0),
    "service.py": (80.0, 70.0),
    "verifier.py": (70.0, 70.0),
    "workspace_snapshot.py": (75.0, 65.0),
}


def evaluate_coverage(report: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = {}
    failures: list[str] = []
    files = report.get("files")
    if not isinstance(files, dict):
        raise ValueError("coverage report has no files object")
    meta = report.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("coverage report has no meta object")
    # A report produced without ``--branch`` still contains ``summary``
    # numbers, so an unset branch flag would otherwise let every module
    # clear its branch threshold with an empty denominator.
    if meta.get("branch_coverage") is not True:
        raise ValueError("coverage report was produced without --branch")
    for module_name, (min_lines, min_branches) in THRESHOLDS.items():
        matches = [
            value
            for filename, value in files.items()
            if Path(filename).name == module_name
        ]
        if len(matches) != 1:
            failures.append(f"{module_name}:missing_or_ambiguous")
            modules[module_name] = {"status": "missing"}
            continue
        summary = matches[0]["summary"]
        statements = int(summary["num_statements"])
        branches = int(summary["num_branches"])
        line_percent = (
            100.0 * int(summary["covered_lines"]) / statements
            if statements
            else 0.0
        )
        branch_percent = (
            100.0 * int(summary["covered_branches"]) / branches
            if branches
            else 100.0
        )
        passed = (
            branches > 0
            and line_percent >= min_lines
            and branch_percent >= min_branches
        )
        if branches <= 0:
            failures.append(f"{module_name}:no_branch_data")
        if not passed:
            failures.append(module_name)
        modules[module_name] = {
            "covered_lines": int(summary["covered_lines"]),
            "num_statements": statements,
            "line_percent": line_percent,
            "minimum_line_percent": min_lines,
            "covered_branches": int(summary["covered_branches"]),
            "num_branches": branches,
            "branch_percent": branch_percent,
            "minimum_branch_percent": min_branches,
            "status": "PASS" if passed else "FAIL",
        }
    return {
        "status": "PASS" if not failures else "FAIL",
        "modules": modules,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    payload = args.coverage_json.read_bytes()
    report = json.loads(payload.decode("utf-8"))
    result = evaluate_coverage(report)
    result["coverage_report_sha256"] = hashlib.sha256(payload).hexdigest()
    result["coverage_report_path"] = str(args.coverage_json)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
