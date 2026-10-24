"""Reproducible offline evidence for the local Task Control Center."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "task-control-center-evidence-v1"
REVIEW_SCHEMA_VERSION = "task-control-center-independent-review-v1"
REVIEW_RESULT_SCHEMA_VERSION = "task-control-center-review-result-v1"
REVIEW_SCOPE = "task-control-center-v1"
BLOCKED_TEST_TERMS = ("container", "docker", "live", "online")
BLOCKED_TEST_MODULES = (
    "test_evidence_provisioning",
    "tests.test_evidence_provisioning",
)
FROZEN_EVIDENCE = {
    "artifacts/audits/phase-3.90/stable-summary.json":
        "ac3abacb5645c710e58f0a2ded32777b7b85960ae65e2938c1d0d183ecc2f1ff",
    "artifacts/audits/phase-3.91/stable-summary.json":
        "0360e53253b82ae4a60979b9c4e71e612c54967cb7b01f5425c49fcb0879b89d",
    "artifacts/audits/phase-3.92/stable-summary.json":
        "131883b01ec6b2d1480a95de46cf4559cb6433b0db9b2439d06e028565327dd7",
}
REQUIRED_CONTROL_TESTS = (
    "test_workbench_control.ControlReceiptClassificationTests."
        "test_failure_plus_side_effect_is_inconsistent",
    "test_workbench_control.ControlReceiptClassificationTests."
        "test_receipt_states_are_fail_closed_and_shared",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_active_external_claim_fails_clarification_closed",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_control_reservation_and_claim_roll_back_together",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_cancel_and_revision_race_has_one_side_effect",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_cancel_completion_loss_reconciles_from_task_event",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_cancel_rechecks_claim_generation_atomically",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_clarification_claim_rejects_racing_plan_revision",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_completed_receipt_keeps_historical_version_and_error",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_competing_control_instances_only_one_records_completion",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_competing_revision_instances_have_one_winner",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_confirmed_clarification_action_executes_and_rereads_state",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_lost_completion_is_reported_for_manual_review",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_llm_planner_queued_task_can_still_be_cancelled",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_preclaim_failure_is_idempotent_across_instances",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_interrupted_task_is_not_advertised_as_revisable",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_task_creation_is_real_idempotent_and_reloadable",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_concurrent_identical_task_creation_has_one_created_response",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_task_creation_with_omitted_parameters_requires_clarification",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_task_note_is_auditable_and_does_not_change_status",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_in_progress_receipt_is_not_treated_as_unknown_or_retried",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_replay_and_status_share_conflicting_receipt_state",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_released_or_expired_claim_is_not_reported_in_progress",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_request_status_flags_conflicting_receipt_evidence",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_revision_cannot_overwrite_active_clarification_claim",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_revision_completion_loss_reconciles_from_plan_revision",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_revision_rejects_invalid_changes_and_reused_request_content",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_revision_reconciliation_finds_evidence_after_many_revisions",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_same_request_id_concurrent_conflict_preserves_exact_content",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_same_request_id_concurrent_duplicate_has_one_effect",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_service_start_and_cancel_guard_stale_plan_versions",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_shared_service_keeps_cli_cancel_and_revision_semantics",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_start_and_cancel_race_has_one_terminal_effect",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_start_and_revision_race_respects_expected_plan_version",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_start_completion_loss_reconciles_from_claim_terminal",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_start_queued_task_executes_once_and_links_claim_receipt",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_start_reconciliation_requires_same_attempt_id",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_unexpected_postclaim_error_requires_manual_reconciliation",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_preclaimed_validation_failure_releases_lease",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_expired_claim_preview_and_submit_require_recovery",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_released_claim_restores_only_eligible_operations",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_claim_race_atomic_reservation_rejects_expired_lease",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_note_race_after_reservation_cannot_mutate_claimed_task",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_revision_race_after_reservation_cannot_clear_expired_lease",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_cancel_race_after_reservation_reports_lease_recovery",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_naive_lease_timestamp_has_consistent_preview_and_submit",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_missing_expiry_lease_atomic_paths_use_active_conflict",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_execution_claim_rejects_missing_expiry_lease",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_test_task_clarification_cannot_bypass_start_boundary",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_control_action_requires_token_origin_confirmation_and_version",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_request_status_requires_control_session",
    "test_workbench_control.WorkbenchControlHTTPTests."
        "test_read_only_api_remains_405_and_workbench_has_no_control_session",
    "test_workbench.WorkbenchHTTPTests."
        "test_mutation_methods_and_unknown_routes_are_rejected",
)
REQUIRED_CODE_REVIEW_SCOPE = frozenset(
    {
        "four_control_actions",
        "task_creation_and_notes",
        "idempotency_concurrency",
        "request_event_consistency",
        "failure_reconciliation",
        "security_boundaries",
    }
)
REQUIRED_ARCHITECTURE_REVIEW_SCOPE = frozenset(
    {
        "frontend_backend_separation",
        "state_transitions",
        "request_attempt_terminal_linkage",
        "reconciliation",
        "extension_constraints",
    }
)
CODE_REVIEW_STATUSES = frozenset(
    {"CLEAR", "REQUEST_CHANGES", "UNAVAILABLE"}
)
ARCHITECTURE_REVIEW_STATUSES = frozenset(
    {"CLEAR", "WATCH", "BLOCK", "UNAVAILABLE"}
)
REVIEW_CONCLUSION_LINE = "Independent review conclusion:"


class EvidenceError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise EvidenceError(f"required file is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid JSON evidence: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceError(f"JSON evidence must be an object: {path}")
    return payload


def _relevant_code_files(root: Path) -> Iterable[Path]:
    roots = (
        root / "src" / "zevorinth",
        root / "tests",
    )
    for base in roots:
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if any(part in {"__pycache__", ".idea"} for part in path.parts):
                continue
            if path.suffix not in {
                ".py",
                ".js",
                ".css",
                ".html",
                ".json",
            }:
                continue
            yield path
    for path in (
        root / "README.md",
        root / "DESIGN.md",
        root / "pyproject.toml",
        *sorted((root / "docs").glob("*.md")),
    ):
        if path.is_file() and ".idea" not in path.parts:
            yield path


def code_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _relevant_code_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
    return digest.hexdigest()


def changed_relevant_files(root: Path) -> list[str]:
    tracked = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "HEAD",
            "--",
            ".",
            ":(exclude).idea",
            ":(exclude)artifacts",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            ".",
            ":(exclude).idea",
            ":(exclude)artifacts",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    relevant = {
        path.relative_to(root).as_posix()
        for path in _relevant_code_files(root)
    }
    return sorted(
        (set(tracked) | set(untracked)) & relevant
    )


def git_state(root: Path) -> dict[str, Any]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        [
            "git",
            "status",
            "--short",
            "--untracked-files=all",
            "--",
            ".",
            ":(exclude).idea",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {
        "head": head,
        "worktree_clean": not bool(status.strip()),
        "status_sha256": sha256_bytes(status.encode("utf-8")),
    }


def build_candidate_snapshot(root: Path) -> dict[str, Any]:
    root = root.resolve()
    state = git_state(root)
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "schema_version": "task-control-center-candidate-snapshot-v1",
        "repository": str(root),
        "branch": branch,
        "head": state["head"],
        "worktree_clean": state["worktree_clean"],
        "status_sha256": state["status_sha256"],
        "code_fingerprint": code_fingerprint(root),
        "files": [
            {
                "path": relative,
                "sha256": sha256_file(root / relative),
            }
            for relative in changed_relevant_files(root)
        ],
        "excluded": [
            ".idea/**",
            ".env*",
            "secret configuration",
            "runtime temporary files",
        ],
    }


def write_candidate_snapshot(root: Path, output_path: Path) -> None:
    root = root.resolve()
    output_path = output_path.resolve()
    try:
        output_path.relative_to(_audit_output_root(root))
    except ValueError as exc:
        raise EvidenceError(
            "candidate snapshot output must stay under "
            "artifacts/audits/task-control-center-v1"
        ) from exc
    if (
        output_path.suffix != ".json"
        or not output_path.name.startswith("final-candidate-snapshot")
    ):
        raise EvidenceError(
            "candidate snapshot must use a final-candidate-snapshot*.json name"
        )
    if output_path.exists():
        raise EvidenceError(
            f"candidate snapshot output already exists: {output_path}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=output_path.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(canonical_json(build_candidate_snapshot(root)))
    os.replace(temporary, output_path)


def _flatten_tests(suite: unittest.TestSuite | unittest.TestCase) -> list[unittest.TestCase]:
    if isinstance(suite, unittest.TestCase):
        return [suite]
    tests: list[unittest.TestCase] = []
    for item in suite:
        tests.extend(_flatten_tests(item))
    return tests


def is_blocked_test_id(test_id: str) -> bool:
    lowered = test_id.lower()
    return any(term in lowered for term in BLOCKED_TEST_TERMS) or any(
        test_id.startswith(module) for module in BLOCKED_TEST_MODULES
    )


def discover_tests(root: Path) -> tuple[list[unittest.TestCase], list[unittest.TestCase]]:
    loader = unittest.TestLoader()
    suite = loader.discover(
        str(root / "tests"),
        top_level_dir=str(root / "tests"),
    )
    all_tests = _flatten_tests(suite)
    selected = [
        test
        for test in all_tests
        if not is_blocked_test_id(test.id())
    ]
    filtered = [test for test in all_tests if test not in selected]
    return selected, filtered


def _run_tests(
    root: Path,
    *,
    result_path: Path,
    log_path: Path,
) -> int:
    for path in (root, root / "src"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    before = code_fingerprint(root)
    selected, filtered = discover_tests(root)
    output = _CaptureStream()
    result = unittest.TextTestRunner(stream=output, verbosity=1).run(
        unittest.TestSuite(selected)
    )
    after = code_fingerprint(root)
    counts = {
        "discovered": len(selected) + len(filtered),
        "selected": len(selected),
        "filtered": len(filtered),
        "passed": (
            result.testsRun
            - len(result.failures)
            - len(result.errors)
            - len(result.skipped)
            - len(result.expectedFailures)
            - len(result.unexpectedSuccesses)
        ),
        "failed": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "python": sys.executable,
        "code_fingerprint_before": before,
        "code_fingerprint_after": after,
        "blocked_terms": list(BLOCKED_TEST_TERMS),
        "counts": counts,
        "selected_tests": sorted(test.id() for test in selected),
        "filtered_tests": sorted(test.id() for test in filtered),
        "skipped_tests": [
            {"test": test.id(), "reason": reason}
            for test, reason in result.skipped
        ],
        "successful": result.wasSuccessful(),
    }
    result_path.write_text(canonical_json(payload), encoding="utf-8")
    normalized_log = re.sub(
        r"Ran (\d+) tests in [0-9.]+s",
        r"Ran \1 tests in <duration>s",
        output.value,
    )
    log_path.write_text(normalized_log, encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


class _CaptureStream:
    def __init__(self) -> None:
        self._parts: list[str] = []

    def write(self, value: str) -> int:
        self._parts.append(value)
        return len(value)

    def flush(self) -> None:
        return None

    @property
    def value(self) -> str:
        return "".join(self._parts)


def _filtered_mutation_cases(root: Path, output_path: Path) -> dict[str, int]:
    source = read_json(root / "tests" / "mutation_cases.json")
    cases = source.get("cases")
    if not isinstance(cases, list):
        raise EvidenceError("mutation_cases.json has no cases array")
    selected = []
    filtered = []
    for case in cases:
        tests = case.get("tests") if isinstance(case, dict) else None
        if not isinstance(tests, list):
            raise EvidenceError("mutation case has invalid tests field")
        test_text = " ".join(str(test) for test in tests)
        blocked = any(
            term in test_text.lower()
            for term in BLOCKED_TEST_TERMS
        ) or any(module in test_text for module in BLOCKED_TEST_MODULES)
        (filtered if blocked else selected).append(case)
    output_path.write_text(
        canonical_json({"version": source.get("version"), "cases": selected}),
        encoding="utf-8",
    )
    return {
        "declared": len(cases),
        "selected": len(selected),
        "filtered": len(filtered),
    }


def run_evidence(
    root: Path,
    output_dir: Path,
    review_manifest_path: Path,
) -> int:
    root = root.resolve()
    output_dir = _validate_output_path(
        root,
        output_dir,
        directory=True,
    )
    if output_dir.exists() and any(output_dir.iterdir()):
        raise EvidenceError(
            f"evidence output directory is not empty: {output_dir}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = output_dir / "run-metadata.json"
    if metadata_path.exists():
        raise EvidenceError(
            f"output directory already contains evidence: {metadata_path}"
        )
    previous_source = (
        root
        / "artifacts"
        / "audits"
        / "task-control-center-v1"
        / "stable-summary.json"
    )
    previous_path = output_dir / "previous-summary.json"
    previous_path.write_bytes(previous_source.read_bytes())
    code_before = code_fingerprint(root)
    git_before = git_state(root)
    review_manifest = _evidence_file(
        root,
        evidence_path(review_manifest_path, root),
        "review manifest",
    )
    validate_review_manifest(
        root,
        review_manifest,
        expected_code_fingerprint=code_before,
    )
    script = Path(__file__).resolve()
    test_evidence: dict[str, Any] = {}
    system_python = Path(shutil.which("python3") or "/usr/bin/python3")
    venv_python = root / ".venv" / "bin" / "python"
    if system_python.absolute() == venv_python.absolute():
        raise EvidenceError(
            "system and virtual-environment Python must be distinct"
        )
    for name, python in (
        ("system_python", system_python),
        ("venv_python", venv_python),
    ):
        if not python.is_file():
            raise EvidenceError(f"{name} interpreter is missing: {python}")
        result_path = output_dir / f"{name}-result.json"
        log_path = output_dir / f"{name}.log"
        completed = subprocess.run(
            [
                str(python),
                str(script),
                "_run-tests",
                "--root",
                str(root),
                "--result",
                str(result_path),
                "--log",
                str(log_path),
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise EvidenceError(
                f"{name} tests failed with exit {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        result = read_json(result_path)
        if not result.get("successful"):
            raise EvidenceError(f"{name} tests did not report success")
        test_evidence[name] = {
            "result": str(result_path.relative_to(root)),
            "result_sha256": sha256_file(result_path),
            "log": str(log_path.relative_to(root)),
            "log_sha256": sha256_file(log_path),
        }
    cases_path = output_dir / "mutation-cases.json"
    mutation_filters = _filtered_mutation_cases(root, cases_path)
    mutations_path = output_dir / "mutations.json"
    mutation_run = subprocess.run(
        [
            str(root / ".venv" / "bin" / "python"),
            str(root / "tests" / "mutation_runner.py"),
            "--cases",
            str(cases_path),
            "--output",
            str(mutations_path),
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if mutation_run.returncode != 0:
        raise EvidenceError(
            f"mutation testing failed: {mutation_run.stderr.strip()}"
        )
    code_after = code_fingerprint(root)
    if code_before != code_after:
        raise EvidenceError("code changed while evidence was being generated")
    git_after = git_state(root)
    if git_before["head"] != git_after["head"]:
        raise EvidenceError("Git HEAD changed while evidence was being generated")
    frozen = {}
    for relative, expected in FROZEN_EVIDENCE.items():
        path = root / relative
        observed = sha256_file(path)
        if observed != expected:
            raise EvidenceError(f"frozen evidence mismatch: {relative}")
        frozen[relative] = {
            "expected_sha256": expected,
            "observed_sha256": observed,
        }
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "code": {
            **git_before,
            "code_fingerprint": code_before,
        },
        "tests": test_evidence,
        "mutation": {
            "source": "tests/mutation_cases.json",
            "source_sha256": sha256_file(root / "tests" / "mutation_cases.json"),
            "cases": str(cases_path.relative_to(root)),
            "cases_sha256": sha256_file(cases_path),
            "report": str(mutations_path.relative_to(root)),
            "report_sha256": sha256_file(mutations_path),
            "filters": mutation_filters,
        },
        "review": {
            "path": evidence_path(review_manifest, root),
            "sha256": sha256_file(review_manifest),
        },
        "previous_summary": {
            "path": str(previous_path.relative_to(root)),
            "sha256": sha256_file(previous_path),
        },
        "frozen_evidence": frozen,
    }
    metadata_path.write_text(canonical_json(metadata), encoding="utf-8")
    print(metadata_path)
    return 0


def _validate_counts(counts: Mapping[str, Any], label: str) -> None:
    required = (
        "discovered",
        "selected",
        "filtered",
        "passed",
        "failed",
        "errors",
        "skipped",
        "expected_failures",
        "unexpected_successes",
    )
    for field in required:
        if not isinstance(counts.get(field), int) or counts[field] < 0:
            raise EvidenceError(f"{label}.{field} must be a non-negative integer")
    accounted = (
        counts["passed"]
        + counts["failed"]
        + counts["errors"]
        + counts["skipped"]
        + counts["expected_failures"]
        + counts["unexpected_successes"]
    )
    if counts["discovered"] != counts["selected"] + counts["filtered"]:
        raise EvidenceError(f"{label} discovered/selected/filtered mismatch")
    if counts["selected"] != accounted:
        raise EvidenceError(f"{label} selected/test-result count mismatch")


def _validate_hash(path: Path, expected: str, label: str) -> None:
    if not path.is_file():
        raise EvidenceError(f"{label} missing: {path}")
    observed = sha256_file(path)
    if observed != expected:
        raise EvidenceError(
            f"{label} SHA mismatch: expected {expected}, observed {observed}"
        )


def _evidence_file(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{label} path must be a non-empty string")
    relative = Path(value)
    if relative.is_absolute():
        raise EvidenceError(f"{label} path must be relative to the repository")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise EvidenceError(f"{label} path escapes the repository") from exc
    relative_parts = path.relative_to(root.resolve()).parts
    if ".idea" in relative_parts or any(
        part.startswith(".env") for part in relative_parts
    ):
        raise EvidenceError(
            f"{label} path points to a protected file: {path}"
        )
    if not path.is_file():
        raise EvidenceError(f"{label} missing: {path}")
    return path


def _report_statuses(report_text: str, label: str) -> set[str]:
    statuses: set[str] = set()
    for line in report_text.splitlines():
        if not line.lower().startswith(REVIEW_CONCLUSION_LINE.lower()):
            continue
        value = line.split(":", 1)[1].strip().upper().replace(" ", "_")
        if value:
            statuses.add(value)
    if not statuses:
        raise EvidenceError(f"{label} has no conclusion declaration")
    return statuses


def _audit_output_root(root: Path) -> Path:
    return (
        root.resolve()
        / "artifacts"
        / "audits"
        / "task-control-center-v1"
    )


def _validate_output_path(root: Path, value: Path, *, directory: bool) -> Path:
    resolved = value.resolve()
    audit_root = _audit_output_root(root)
    try:
        resolved.relative_to(audit_root)
    except ValueError as exc:
        raise EvidenceError(
            "evidence output must stay under "
            "artifacts/audits/task-control-center-v1"
        ) from exc
    if not directory and resolved.suffix != ".json":
        raise EvidenceError("summary output must be a JSON file")
    if not directory and resolved.name not in {
        "stable-summary.json",
        "reproduced-summary.json",
    }:
        raise EvidenceError(
            "summary output must be stable-summary.json or "
            "reproduced-summary.json"
        )
    return resolved


def _normalize_review_status(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise EvidenceError(f"{label}.status must be a string")
    status = value.strip().upper().replace(" ", "_")
    if not status:
        raise EvidenceError(f"{label}.status must not be empty")
    return status


def validate_review_manifest(
    root: Path,
    review_manifest_path: Path,
    *,
    expected_code_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Validate both independent review lanes and preserve their risk state."""
    root = root.resolve()
    manifest_path = review_manifest_path.resolve()
    try:
        manifest_path.relative_to(root)
    except ValueError as exc:
        raise EvidenceError(
            "review manifest must be inside the repository"
        ) from exc
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise EvidenceError("unexpected independent review manifest schema")
    if manifest.get("scope") != REVIEW_SCOPE:
        raise EvidenceError("independent review manifest scope mismatch")
    repository = manifest.get("repository")
    if not isinstance(repository, str):
        raise EvidenceError("review manifest has no repository")
    try:
        repository_path = Path(repository).resolve()
    except (OSError, RuntimeError) as exc:
        raise EvidenceError("review manifest repository is invalid") from exc
    if repository_path != root:
        raise EvidenceError("review manifest repository does not match root")
    current_git = git_state(root)
    current_fingerprint = (
        expected_code_fingerprint
        if expected_code_fingerprint is not None
        else code_fingerprint(root)
    )
    if manifest.get("head") != current_git["head"]:
        raise EvidenceError("review manifest HEAD does not match repository")
    if manifest.get("code_fingerprint") != current_fingerprint:
        raise EvidenceError(
            "review manifest code fingerprint does not match repository"
        )

    lanes = (
        (
            "code_review",
            "independent_code_review",
            CODE_REVIEW_STATUSES,
            REQUIRED_CODE_REVIEW_SCOPE,
        ),
        (
            "architecture_review",
            "independent_architecture_review",
            ARCHITECTURE_REVIEW_STATUSES,
            REQUIRED_ARCHITECTURE_REVIEW_SCOPE,
        ),
    )
    validated: dict[str, Any] = {}
    for key, output_key, allowed_statuses, required_scope in lanes:
        lane = manifest.get(key)
        if not isinstance(lane, dict):
            raise EvidenceError(f"review manifest has no {key}")
        status = _normalize_review_status(lane.get("status"), key)
        if status not in allowed_statuses:
            raise EvidenceError(f"{key} has unsupported status: {status}")
        completed = lane.get("completed")
        files_reviewed = lane.get("files_reviewed")
        if not isinstance(files_reviewed, int) or files_reviewed < 0:
            raise EvidenceError(
                f"{key}.files_reviewed must be a non-negative integer"
            )
        lane_summary: dict[str, Any] = {
            "status": status,
            "completed": status != "UNAVAILABLE",
            "files_reviewed": files_reviewed,
        }
        if completed is not (status != "UNAVAILABLE"):
            raise EvidenceError(
                f"{key}.completed disagrees with review status"
            )
        conclusion = _normalize_review_status(
            lane.get("conclusion"),
            f"{key}.conclusion",
        )
        if conclusion != status:
            raise EvidenceError(
                f"{key}.conclusion does not match review status"
            )
        if status == "UNAVAILABLE":
            if files_reviewed != 0:
                raise EvidenceError(
                    f"{key} unavailable status must report zero files"
                )
            reason = lane.get("reason")
            if (
                not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 1000
            ):
                raise EvidenceError(
                    f"{key} unavailable status requires a bounded reason"
                )
            lane_summary["reason"] = reason
            validated[output_key] = status
            validated[key] = lane_summary
            continue

        if files_reviewed <= 0:
            raise EvidenceError(
                f"{key} cannot complete without reviewing files"
            )
        reviewed_files = lane.get("reviewed_files")
        if (
            not isinstance(reviewed_files, list)
            or len(reviewed_files) != files_reviewed
        ):
            raise EvidenceError(
                f"{key}.reviewed_files must match files_reviewed"
            )
        reviewed_paths: set[str] = set()
        for item in reviewed_files:
            if not isinstance(item, dict):
                raise EvidenceError(
                    f"{key}.reviewed_files entries must be objects"
                )
            relative = item.get("path")
            if not isinstance(relative, str) or relative in reviewed_paths:
                raise EvidenceError(
                    f"{key}.reviewed_files paths must be unique strings"
                )
            if ".idea" in Path(relative).parts:
                raise EvidenceError(
                    f"{key}.reviewed_files cannot include .idea"
                )
            reviewed_path = _evidence_file(
                root,
                relative,
                f"{key}.reviewed_files",
            )
            _validate_hash(
                reviewed_path,
                str(item.get("sha256")),
                f"{key}.reviewed_files[{relative}]",
            )
            reviewed_paths.add(relative)
        changed_paths = set(changed_relevant_files(root))
        missing_changed = changed_paths - reviewed_paths
        if missing_changed:
            raise EvidenceError(
                f"{key} omitted changed files: {sorted(missing_changed)}"
            )
        extra_changed = reviewed_paths - changed_paths
        if extra_changed:
            raise EvidenceError(
                f"{key} included files outside the changed candidate: "
                f"{sorted(extra_changed)}"
            )
        report_path = _evidence_file(root, lane.get("report"), f"{key}.report")
        if not report_path.read_text(encoding="utf-8").strip():
            raise EvidenceError(f"{key} report is empty")
        _validate_hash(
            report_path,
            str(lane.get("report_sha256")),
            f"{key} report",
        )
        report_text = report_path.read_text(encoding="utf-8")
        if current_git["head"] not in report_text:
            raise EvidenceError(
                f"{key} report does not identify the reviewed HEAD"
            )
        if current_fingerprint not in report_text:
            raise EvidenceError(
                f"{key} report does not identify the reviewed fingerprint"
            )
        report_statuses = _report_statuses(
            report_text,
            f"{key} report",
        )
        accepted_report_statuses = (
            {"PASS", "CLEAR", "REQUEST_CHANGES", "UNAVAILABLE"}
            if key == "code_review"
            else {
                "PASS",
                "CLEAR",
                "WATCH",
                "BLOCK",
                "BLOCKED",
                "UNAVAILABLE",
            }
        )
        unsupported = report_statuses - accepted_report_statuses
        if unsupported:
            raise EvidenceError(
                f"{key} report has unsupported conclusion: "
                f"{sorted(unsupported)}"
            )
        normalized_report_statuses = {
            "CLEAR" if item == "PASS" else item
            for item in report_statuses
        }
        normalized_report_statuses = {
            "BLOCK" if item == "BLOCKED" else item
            for item in normalized_report_statuses
        }
        if normalized_report_statuses != {status}:
            raise EvidenceError(
                f"{key} report conclusion disagrees with machine result"
            )
        result_path = _evidence_file(root, lane.get("result"), f"{key}.result")
        _validate_hash(
            result_path,
            str(lane.get("result_sha256")),
            f"{key} result",
        )
        result = read_json(result_path)
        if result.get("schema_version") != REVIEW_RESULT_SCHEMA_VERSION:
            raise EvidenceError(f"{key} result has an unexpected schema")
        if result.get("lane") != key:
            raise EvidenceError(f"{key} result lane mismatch")
        result_status = _normalize_review_status(
            result.get("status"),
            f"{key}.result",
        )
        result_conclusion = _normalize_review_status(
            result.get("conclusion"),
            f"{key}.result.conclusion",
        )
        if result_status != status or result_conclusion != conclusion:
            raise EvidenceError(
                f"{key} manifest and machine-readable result disagree"
            )
        if result.get("files_reviewed") != files_reviewed:
            raise EvidenceError(
                f"{key} manifest and result file counts disagree"
            )
        if result.get("reviewed_files") != reviewed_files:
            raise EvidenceError(
                f"{key} manifest and result reviewed files disagree"
            )
        if result.get("target_repository") != lane.get(
            "target_repository"
        ):
            raise EvidenceError(
                f"{key} result target repository disagrees with manifest"
            )
        if result.get("target_head") != lane.get("target_head"):
            raise EvidenceError(
                f"{key} result target HEAD disagrees with manifest"
            )
        if result.get("target_code_fingerprint") != lane.get(
            "target_code_fingerprint"
        ):
            raise EvidenceError(
                f"{key} result code fingerprint disagrees with manifest"
            )
        if result.get("report") != lane.get("report"):
            raise EvidenceError(f"{key} result report path mismatch")
        if result.get("report_sha256") != lane.get("report_sha256"):
            raise EvidenceError(f"{key} result report hash mismatch")
        if result.get("covered_scope") != lane.get("covered_scope"):
            raise EvidenceError(f"{key} result covered scope mismatch")
        for field in (
            "target_repository",
            "target_head",
            "target_code_fingerprint",
        ):
            if not isinstance(lane.get(field), str):
                raise EvidenceError(f"{key} has no {field}")
        try:
            target_repository = Path(lane["target_repository"]).resolve()
        except (OSError, RuntimeError) as exc:
            raise EvidenceError(
                f"{key}.target_repository is invalid"
            ) from exc
        if target_repository != root:
            raise EvidenceError(
                f"{key} did not review the target repository"
            )
        if lane.get("target_head") != current_git["head"]:
            raise EvidenceError(f"{key} reviewed a different HEAD")
        if lane.get("target_code_fingerprint") != current_fingerprint:
            raise EvidenceError(
                f"{key} reviewed a different code fingerprint"
            )
        covered_scope = lane.get("covered_scope")
        if not isinstance(covered_scope, list) or not all(
            isinstance(item, str) for item in covered_scope
        ):
            raise EvidenceError(f"{key}.covered_scope must be a string array")
        missing_scope = required_scope - set(covered_scope)
        if missing_scope:
            raise EvidenceError(
                f"{key} omitted required review scope: "
                f"{sorted(missing_scope)}"
            )
        lane_summary.update(
            {
                "report": evidence_path(report_path, root),
                "report_sha256": sha256_file(report_path),
                "result": evidence_path(result_path, root),
                "result_sha256": sha256_file(result_path),
                "target_head": current_git["head"],
                "target_code_fingerprint": current_fingerprint,
                "covered_scope": sorted(covered_scope),
                "uncovered_scope": list(lane.get("uncovered_scope") or []),
            }
        )
        validated[output_key] = status
        validated[key] = lane_summary

    code_status = validated["independent_code_review"]
    architecture_status = validated["independent_architecture_review"]
    merge_ready = (
        code_status == "CLEAR" and architecture_status == "CLEAR"
    )
    blocking_reasons = []
    if code_status != "CLEAR":
        blocking_reasons.append(f"code_review:{code_status}")
    if architecture_status != "CLEAR":
        blocking_reasons.append(
            f"architecture_review:{architecture_status}"
        )
    if (
        code_status == "REQUEST_CHANGES"
        or architecture_status in {"WATCH", "BLOCK"}
    ):
        candidate_acceptance = "INDEPENDENT_REVIEW_BLOCKED"
    elif "UNAVAILABLE" in {code_status, architecture_status}:
        candidate_acceptance = "INDEPENDENT_REVIEW_PENDING"
    elif not merge_ready:
        candidate_acceptance = "INDEPENDENT_REVIEW_BLOCKED"
    else:
        candidate_acceptance = "INDEPENDENT_REVIEW_CLEAR"
    validated.update(
        {
            "path": evidence_path(manifest_path, root),
            "sha256": sha256_file(manifest_path),
            "merge_ready": merge_ready,
            "blocking_reasons": blocking_reasons,
            "candidate_acceptance": candidate_acceptance,
        }
    )
    return validated


def build_summary(
    root: Path,
    metadata_path: Path,
    *,
    replacement_reason: str,
) -> dict[str, Any]:
    root = root.resolve()
    metadata = read_json(metadata_path)
    run_dir = metadata_path.resolve().parent
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("unexpected run metadata schema")
    code = metadata.get("code")
    if not isinstance(code, dict):
        raise EvidenceError("run metadata has no code object")
    current_code = code_fingerprint(root)
    if code.get("code_fingerprint") != current_code:
        raise EvidenceError(
            "test evidence does not match the current code fingerprint"
        )
    current_git = git_state(root)
    for field in ("head", "worktree_clean", "status_sha256"):
        if code.get(field) != current_git[field]:
            raise EvidenceError(
                f"test evidence does not match current Git state: {field}"
            )
    tests = metadata.get("tests")
    if not isinstance(tests, dict):
        raise EvidenceError("run metadata has no tests object")
    test_summary: dict[str, Any] = {}
    interpreter_paths: set[str] = set()
    for name in ("system_python", "venv_python"):
        evidence = tests.get(name)
        if not isinstance(evidence, dict):
            raise EvidenceError(f"missing test evidence: {name}")
        result_path = _evidence_file(
            root,
            evidence.get("result"),
            f"{name} result",
        )
        log_path = _evidence_file(
            root,
            evidence.get("log"),
            f"{name} log",
        )
        _validate_hash(
            result_path,
            str(evidence.get("result_sha256")),
            f"{name} result",
        )
        _validate_hash(
            log_path,
            str(evidence.get("log_sha256")),
            f"{name} log",
        )
        result = read_json(result_path)
        interpreter = result.get("python")
        if not isinstance(interpreter, str) or not interpreter:
            raise EvidenceError(f"{name} result has no interpreter path")
        interpreter_paths.add(str(Path(interpreter).absolute()))
        if not result.get("successful"):
            raise EvidenceError(f"{name} result is not successful")
        if (
            result.get("code_fingerprint_before") != current_code
            or result.get("code_fingerprint_after") != current_code
        ):
            raise EvidenceError(
                f"{name} result does not match the current code fingerprint"
            )
        counts = result.get("counts")
        if not isinstance(counts, dict):
            raise EvidenceError(f"{name} result has no counts")
        _validate_counts(counts, name)
        selected = result.get("selected_tests")
        filtered = result.get("filtered_tests")
        if not isinstance(selected, list) or not isinstance(filtered, list):
            raise EvidenceError(f"{name} test ID lists are missing")
        if (
            len(selected) != counts["selected"]
            or len(filtered) != counts["filtered"]
            or len(set(selected)) != len(selected)
            or len(set(filtered)) != len(filtered)
        ):
            raise EvidenceError(f"{name} test ID/count inconsistency")
        selected_ids = set(selected)
        missing = [
            required
            for required in REQUIRED_CONTROL_TESTS
            if required not in selected_ids
        ]
        if missing:
            raise EvidenceError(
                f"{name} did not execute required control tests: {missing}"
            )
        if any(
            is_blocked_test_id(str(test))
            for test in selected
        ):
            raise EvidenceError(f"{name} selected a blocked test")
        test_summary[name] = {
            **counts,
            "python": interpreter,
            "result": summary_evidence_path(result_path, root, run_dir),
            "result_sha256": evidence["result_sha256"],
            "log": summary_evidence_path(log_path, root, run_dir),
            "log_sha256": evidence["log_sha256"],
        }
    if len(interpreter_paths) != 2:
        raise EvidenceError(
            "system and virtual-environment test runs used the same interpreter"
        )
    mutation = metadata.get("mutation")
    if not isinstance(mutation, dict):
        raise EvidenceError("run metadata has no mutation object")
    source_path = _evidence_file(
        root,
        mutation.get("source"),
        "mutation source",
    )
    _validate_hash(
        source_path,
        str(mutation.get("source_sha256")),
        "mutation source",
    )
    cases_path = _evidence_file(
        root,
        mutation.get("cases"),
        "mutation cases",
    )
    report_path = _evidence_file(
        root,
        mutation.get("report"),
        "mutation report",
    )
    _validate_hash(
        cases_path,
        str(mutation.get("cases_sha256")),
        "mutation cases",
    )
    _validate_hash(
        report_path,
        str(mutation.get("report_sha256")),
        "mutation report",
    )
    cases = read_json(cases_path)
    report = read_json(report_path)
    declared = len(cases.get("cases", []))
    excluded = int(mutation.get("filters", {}).get("filtered", -1))
    if declared + excluded != int(
        mutation.get("filters", {}).get("declared", -1)
    ):
        raise EvidenceError("mutation input/filter counts mismatch")
    if report.get("case_count") != declared:
        raise EvidenceError("mutation report/input case count mismatch")
    counts = report.get("counts")
    if not isinstance(counts, dict):
        raise EvidenceError("mutation report has no counts")
    accounted = sum(
        int(counts.get(field, -1))
        for field in ("killed", "survived", "invalid", "error")
    )
    if accounted != declared:
        raise EvidenceError("mutation result count mismatch")
    if report.get("status") != "PASS" or any(
        int(counts.get(field, 0)) != 0
        for field in ("survived", "invalid", "error")
    ):
        raise EvidenceError("mutation evidence is not PASS")
    evidence_scopes = report.get("evidence_scope_counts")
    if not isinstance(evidence_scopes, dict):
        raise EvidenceError("mutation report has no evidence scope counts")
    if any(
        not isinstance(evidence_scopes.get(field), int)
        or evidence_scopes[field] < 0
        for field in ("in_process", "static_source_check", "unverified")
    ):
        raise EvidenceError("mutation evidence scope counts are invalid")
    if evidence_scopes["unverified"] != 0:
        raise EvidenceError("mutation evidence contains unverified kills")
    if sum(
        evidence_scopes[field]
        for field in ("in_process", "static_source_check", "unverified")
    ) != declared:
        raise EvidenceError("mutation evidence scope count mismatch")
    review = metadata.get("review")
    if not isinstance(review, dict):
        raise EvidenceError("run metadata has no review evidence")
    review_path = _evidence_file(root, review.get("path"), "review")
    _validate_hash(review_path, str(review.get("sha256")), "review")
    review_summary = validate_review_manifest(
        root,
        review_path,
        expected_code_fingerprint=current_code,
    )
    previous_record = metadata.get("previous_summary")
    if not isinstance(previous_record, dict):
        raise EvidenceError("run metadata has no previous summary snapshot")
    previous_summary = _evidence_file(
        root,
        previous_record.get("path"),
        "previous summary",
    )
    _validate_hash(
        previous_summary,
        str(previous_record.get("sha256")),
        "previous summary",
    )
    frozen = metadata.get("frozen_evidence")
    if not isinstance(frozen, dict):
        raise EvidenceError("run metadata has no frozen evidence")
    frozen_summary = {}
    for relative, expected in FROZEN_EVIDENCE.items():
        observed = sha256_file(root / relative)
        if observed != expected:
            raise EvidenceError(f"frozen evidence mismatch: {relative}")
        evidence = frozen.get(relative)
        if not isinstance(evidence, dict):
            raise EvidenceError(f"frozen evidence missing from run: {relative}")
        if evidence.get("observed_sha256") != observed:
            raise EvidenceError(f"frozen evidence run record mismatch: {relative}")
        frozen_summary[relative] = {
            "expected_sha256": expected,
            "observed_sha256": observed,
        }
    previous_sha = sha256_file(previous_summary)
    previous = read_json(previous_summary)
    incidents = previous.get("constraint_incidents")
    if not isinstance(incidents, list) or not incidents:
        raise EvidenceError(
            "previous summary does not contain historical constraint_incidents"
        )
    history = previous.get("evidence_history")
    if not isinstance(history, list):
        history = []
    if isinstance(previous.get("superseded_summary"), dict):
        prior = previous["superseded_summary"].get("previous_sha256")
        if prior not in {
            item.get("previous_sha256")
            for item in history
            if isinstance(item, dict)
        }:
            history = [
                *history,
                {
                    "previous_sha256": prior,
                    "replacement_reason": previous[
                        "superseded_summary"
                    ].get("replacement_reason"),
                },
            ]
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "task-control-center-v1",
        "status": "PASS_EVIDENCE_INTEGRITY",
        "validation_mode": "offline_noncontainer",
        "code": {
            "head": current_git["head"],
            "worktree_clean": current_git["worktree_clean"],
            "status_sha256": current_git["status_sha256"],
            "code_fingerprint": current_code,
        },
        "tests": {
            **test_summary,
            "selection": {
                "blocked_terms": list(BLOCKED_TEST_TERMS),
                "blocked_modules": list(BLOCKED_TEST_MODULES),
            },
        },
        "mutations": {
            "declared": int(mutation["filters"]["declared"]),
            "executed": declared,
            "filtered": excluded,
            "killed": int(counts["killed"]),
            "survived": int(counts["survived"]),
            "invalid": int(counts["invalid"]),
            "error": int(counts["error"]),
            "evidence_scopes": evidence_scopes,
            "cases": summary_evidence_path(cases_path, root, run_dir),
            "cases_sha256": sha256_file(cases_path),
            "report": summary_evidence_path(report_path, root, run_dir),
            "report_sha256": sha256_file(report_path),
        },
        "special_review": {
            **review_summary,
        },
        "review_status": {
            "independent_code_review":
                review_summary["independent_code_review"],
            "independent_architecture_review":
                review_summary["independent_architecture_review"],
            "merge_ready": review_summary["merge_ready"],
            "blocking_reasons": review_summary["blocking_reasons"],
        },
        "constraint_incidents": incidents,
        "evidence_history": history,
        "constraint_scope": {
            "runtime_side_effect_observation": "NOT_INSTRUMENTED",
            "selection_basis": (
                "test IDs are filtered for container, Docker, live and online; "
                "runtime egress, container and model counters are not asserted"
            ),
            "container_tests_executed_this_run": None,
            "docker_backed_tests_executed_this_run": None,
            "live_tests_executed_this_run": None,
            "online_tests_executed_this_run": None,
            "real_model_requests": None,
            "external_network_requests": None,
            "dependency_installs": None,
        },
        "frozen_evidence": frozen_summary,
        "superseded_summary": {
            "previous_sha256": previous_sha,
            "replacement_reason": replacement_reason,
        },
        "candidate_acceptance": (
            review_summary["candidate_acceptance"]
        ),
    }


def evidence_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path.resolve())


def summary_evidence_path(path: Path, root: Path, run_dir: Path) -> str:
    try:
        relative = path.resolve().relative_to(run_dir.resolve())
    except ValueError:
        return evidence_path(path, root)
    return f"<run>/{relative.as_posix()}"


def write_summary(
    root: Path,
    metadata_path: Path,
    output_path: Path,
    *,
    replacement_reason: str,
) -> None:
    root = root.resolve()
    metadata_path = metadata_path.resolve()
    output_path = _validate_output_path(
        root,
        output_path,
        directory=False,
    )
    metadata = read_json(metadata_path)
    previous_record = metadata.get("previous_summary")
    if not isinstance(previous_record, dict):
        raise EvidenceError("run metadata has no previous summary snapshot")
    previous_summary = _evidence_file(
        root,
        previous_record.get("path"),
        "previous summary",
    )
    review_record = metadata.get("review")
    if not isinstance(review_record, dict):
        raise EvidenceError("run metadata has no review evidence")
    review_manifest = _evidence_file(
        root,
        review_record.get("path"),
        "review manifest",
    )
    protected = {
        metadata_path,
        previous_summary,
        review_manifest,
        root / "tests" / "mutation_cases.json",
        *(root / relative for relative in FROZEN_EVIDENCE),
    }
    review_data = read_json(review_manifest)
    for key in ("code_review", "architecture_review"):
        lane = review_data.get(key)
        if not isinstance(lane, dict):
            continue
        for field in ("report", "result"):
            if isinstance(lane.get(field), str):
                protected.add(_evidence_file(root, lane[field], key))
    for relative in (
        "system_python-result.json",
        "venv_python-result.json",
        "system_python.log",
        "venv_python.log",
        "mutation-cases.json",
        "mutations.json",
    ):
        protected.add(metadata_path.parent / relative)
    if output_path in protected:
        raise EvidenceError("refusing to overwrite an evidence input")
    summary = build_summary(
        root,
        metadata_path,
        replacement_reason=replacement_reason,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=output_path.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(canonical_json(summary))
    os.replace(temporary, output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    run_parser.add_argument("--output-dir", type=Path, required=True)
    run_parser.add_argument("--review-manifest", type=Path, required=True)
    tests_parser = subparsers.add_parser("_run-tests")
    tests_parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    tests_parser.add_argument("--result", type=Path, required=True)
    tests_parser.add_argument("--log", type=Path, required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    snapshot_parser.add_argument("--output", type=Path, required=True)
    summary_parser = subparsers.add_parser("summarize")
    summary_parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    summary_parser.add_argument("--run-metadata", type=Path, required=True)
    summary_parser.add_argument("--output", type=Path, required=True)
    summary_parser.add_argument("--replace-reason", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_evidence(
                args.root,
                args.output_dir,
                args.review_manifest,
            )
        if args.command == "_run-tests":
            return _run_tests(
                args.root.resolve(),
                result_path=args.result.resolve(),
                log_path=args.log.resolve(),
            )
        if args.command == "snapshot":
            write_candidate_snapshot(
                args.root.resolve(),
                args.output.resolve(),
            )
            print(args.output.resolve())
            return 0
        write_summary(
            args.root,
            args.run_metadata,
            args.output,
            replacement_reason=args.replace_reason,
        )
        print(args.output.resolve())
        return 0
    except EvidenceError as exc:
        print(f"evidence error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
