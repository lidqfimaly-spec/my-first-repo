"""Compare two safety-gate reports without letting metrics drift silently.

The comparison refuses to guess: a report that predates the auditable
metric contract has no numerator/denominator per metric, so it is marked
``NOT_COMPARABLE`` instead of being back-filled.

Usage:

    python3 -m tests.eval_compare baseline.json candidate.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


REQUIRED_REPORT_FIELDS = (
    "gate_version",
    "dataset_version",
    "metric_definition_version",
    "cases",
    "metrics",
    "metric_audit",
    "status",
)
REQUIRED_METRIC_FIELDS = (
    "numerator",
    "denominator",
    "excluded_count",
    "contributing_case_ids",
    "value",
)
ACCEPTANCE_REQUIRED_FIELDS = (
    "metric_definition_version",
    "scenarios",
    "metrics",
    "metric_audit",
)
HIGHER_IS_BETTER = frozenset(
    {
        "classification_accuracy",
        "status_accuracy",
        "candidate_validation_pass_rate",
        "normal_plan_success_rate",
        "safety_block_pass_rate",
    }
)
LOWER_IS_BETTER = frozenset({"duplicate_side_effects"})


def _missing_fields(report: Mapping[str, Any]) -> list[str]:
    missing = [field for field in REQUIRED_REPORT_FIELDS if field not in report]
    audit = report.get("metric_audit")
    if isinstance(audit, Mapping):
        for name, metric in audit.items():
            if not isinstance(metric, Mapping):
                missing.append(f"metric_audit.{name}")
                continue
            for field in REQUIRED_METRIC_FIELDS:
                if field not in metric:
                    missing.append(f"metric_audit.{name}.{field}")
    return missing


def _case_state(report: Mapping[str, Any]) -> dict[str, bool]:
    return {
        str(case["id"]): bool(case.get("passed"))
        for case in report.get("cases", [])
    }


def compare_reports(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    baseline_missing = _missing_fields(baseline)
    candidate_missing = _missing_fields(candidate)
    if baseline_missing or candidate_missing:
        return {
            "status": "NOT_COMPARABLE",
            "comparable": False,
            "reasons": [
                *[f"baseline_missing:{name}" for name in baseline_missing],
                *[f"candidate_missing:{name}" for name in candidate_missing],
            ],
            "regressions": [],
        }
    if (
        baseline["metric_definition_version"]
        != candidate["metric_definition_version"]
    ):
        return {
            "status": "NOT_COMPARABLE",
            "comparable": False,
            "reasons": [
                "metric_definition_version_changed:"
                f"{baseline['metric_definition_version']}"
                f"->{candidate['metric_definition_version']}"
            ],
            "regressions": [],
        }

    baseline_cases = _case_state(baseline)
    candidate_cases = _case_state(candidate)
    removed = sorted(set(baseline_cases) - set(candidate_cases))
    added = sorted(set(candidate_cases) - set(baseline_cases))
    regressions: list[str] = []
    for case_id in removed:
        regressions.append(
            f"case_removed:{case_id}"
            + (":was_passing" if baseline_cases[case_id] else ":was_failing")
        )
    for case_id in sorted(set(baseline_cases) & set(candidate_cases)):
        if baseline_cases[case_id] and not candidate_cases[case_id]:
            regressions.append(f"case_regressed:{case_id}")
    for case_id in added:
        if not candidate_cases[case_id]:
            regressions.append(f"new_case_failing:{case_id}")

    baseline_audit = baseline["metric_audit"]
    candidate_audit = candidate["metric_audit"]
    denominator_changes: dict[str, dict[str, int]] = {}
    value_changes: dict[str, dict[str, float]] = {}
    for name in sorted(set(baseline_audit) | set(candidate_audit)):
        before = baseline_audit.get(name)
        after = candidate_audit.get(name)
        if before is None or after is None:
            regressions.append(f"metric_missing:{name}")
            continue
        before_den = int(before["denominator"])
        after_den = int(after["denominator"])
        if before_den != after_den:
            denominator_changes[name] = {
                "baseline": before_den,
                "candidate": after_den,
            }
            if after_den < before_den:
                regressions.append(f"denominator_shrunk:{name}")
        before_value = float(before["value"])
        after_value = float(after["value"])
        if before_value != after_value:
            value_changes[name] = {
                "baseline": before_value,
                "candidate": after_value,
            }
        if name in HIGHER_IS_BETTER and after_value < before_value:
            regressions.append(f"metric_regressed:{name}")
        if name in LOWER_IS_BETTER and after_value > before_value:
            regressions.append(f"metric_regressed:{name}")

    baseline_skipped = int(baseline["metrics"].get("skipped_count", 0))
    candidate_skipped = int(candidate["metrics"].get("skipped_count", 0))
    baseline_unexecuted = int(baseline["metrics"].get("unexecuted_count", 0))
    candidate_unexecuted = int(candidate["metrics"].get("unexecuted_count", 0))
    if candidate_skipped > baseline_skipped:
        regressions.append("skipped_count_increased")
    if candidate_unexecuted > baseline_unexecuted:
        regressions.append("unexecuted_count_increased")

    return {
        "status": "PASS" if not regressions else "FAIL",
        "comparable": True,
        "reasons": [],
        "dataset_version_changed": (
            baseline["dataset_version"] != candidate["dataset_version"]
        ),
        "baseline_case_count": len(baseline_cases),
        "candidate_case_count": len(candidate_cases),
        "removed_cases": removed,
        "added_cases": added,
        "denominator_changes": denominator_changes,
        "value_changes": value_changes,
        "skipped_count": [baseline_skipped, candidate_skipped],
        "unexecuted_count": [baseline_unexecuted, candidate_unexecuted],
        "regressions": regressions,
    }


def detect_report_kind(report: Mapping[str, Any]) -> str:
    if "scenarios" in report:
        return "acceptance"
    if "cases" in report:
        return "safety_gate"
    return "unknown"


def compare_acceptance_reports(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare two E2E acceptance reports under the same rules."""
    missing = [
        field
        for report, label in ((baseline, "baseline"), (candidate, "candidate"))
        for field in ACCEPTANCE_REQUIRED_FIELDS
        if field not in report
    ]
    if missing:
        return {
            "status": "NOT_COMPARABLE",
            "comparable": False,
            "kind": "acceptance",
            "reasons": [f"missing:{name}" for name in missing],
            "regressions": [],
        }
    if (
        baseline["metric_definition_version"]
        != candidate["metric_definition_version"]
    ):
        return {
            "status": "NOT_COMPARABLE",
            "comparable": False,
            "kind": "acceptance",
            "reasons": [
                "metric_definition_version_changed:"
                f"{baseline['metric_definition_version']}"
                f"->{candidate['metric_definition_version']}"
            ],
            "regressions": [],
        }
    before = {
        str(item["id"]): bool(item.get("passed"))
        for item in baseline["scenarios"]
    }
    after = {
        str(item["id"]): bool(item.get("passed"))
        for item in candidate["scenarios"]
    }
    regressions: list[str] = []
    removed = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    for scenario_id in removed:
        regressions.append(f"scenario_removed:{scenario_id}")
    for scenario_id in sorted(set(before) & set(after)):
        if before[scenario_id] and not after[scenario_id]:
            regressions.append(f"scenario_regressed:{scenario_id}")
    for scenario_id in added:
        if not after[scenario_id]:
            regressions.append(f"new_scenario_failing:{scenario_id}")
    denominator_changes: dict[str, dict[str, int]] = {}
    for name, detail in candidate["metric_audit"].items():
        previous = baseline["metric_audit"].get(name)
        if not isinstance(previous, dict):
            regressions.append(f"metric_missing_in_baseline:{name}")
            continue
        if previous.get("denominator") != detail.get("denominator"):
            denominator_changes[name] = {
                "baseline": previous.get("denominator"),
                "candidate": detail.get("denominator"),
            }
            if (detail.get("denominator") or 0) < (previous.get("denominator") or 0):
                regressions.append(f"denominator_shrunk:{name}")
    return {
        "status": "PASS" if not regressions else "FAIL",
        "comparable": True,
        "kind": "acceptance",
        "reasons": [],
        "removed_scenarios": removed,
        "added_scenarios": added,
        "denominator_changes": denominator_changes,
        "regressions": regressions,
    }


def _load(path: Path) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    return json.loads(payload.decode("utf-8")), hashlib.sha256(
        payload
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--kind",
        choices=("auto", "gate", "acceptance"),
        default="auto",
    )
    args = parser.parse_args()
    baseline, baseline_sha = _load(args.baseline)
    candidate, candidate_sha = _load(args.candidate)
    kind = args.kind
    if kind == "auto":
        kind = detect_report_kind(baseline)
        if detect_report_kind(candidate) != kind:
            kind = "unknown"
    if kind == "acceptance":
        result = compare_acceptance_reports(baseline, candidate)
    elif kind in {"gate", "safety_gate"}:
        if kind == "safety_gate":
            kind = "gate"
        result = compare_reports(baseline, candidate)
    else:
        result = {
            "status": "NOT_COMPARABLE",
            "comparable": False,
            "reasons": ["unknown_or_mismatched_report_kind"],
            "regressions": [],
        }
    result["kind"] = kind
    result["baseline"] = {
        "path": str(args.baseline),
        "sha256": baseline_sha,
    }
    result["candidate"] = {
        "path": str(args.candidate),
        "sha256": candidate_sha,
    }
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
