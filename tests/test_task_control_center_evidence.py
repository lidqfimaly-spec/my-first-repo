"""Tests for Task Control Center evidence generation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tests.task_control_center_evidence import (  # noqa: E402
    BLOCKED_TEST_TERMS,
    REQUIRED_ARCHITECTURE_REVIEW_SCOPE,
    REQUIRED_CODE_REVIEW_SCOPE,
    REQUIRED_CONTROL_TESTS,
    EvidenceError,
    _evidence_file,
    _validate_counts,
    build_summary,
    canonical_json,
    code_fingerprint,
    git_state,
    is_blocked_test_id,
    sha256_file,
    validate_review_manifest,
    write_candidate_snapshot,
    write_summary,
)


def _write_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(payload), encoding="utf-8")
    return sha256_file(path)


class EvidenceFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.frozen = {
            "artifacts/audits/phase-3.90/stable-summary.json": "",
            "artifacts/audits/phase-3.91/stable-summary.json": "",
            "artifacts/audits/phase-3.92/stable-summary.json": "",
        }
        self._init_repo()
        self._write_frozen()
        self.output_path = (
            root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "stable-summary.json"
        )
        self.run_dir = self.output_path.parent / "fixture-run"
        self.metadata_path = self.run_dir / "run-metadata.json"
        self._write_previous()
        self.previous_path = self.run_dir / "previous-summary.json"
        self.previous_path.parent.mkdir(parents=True, exist_ok=True)
        self.previous_path.write_bytes(self.output_path.read_bytes())
        self.metadata = self._build_metadata()
        _write_json(self.metadata_path, self.metadata)

    def _init_repo(self) -> None:
        (self.root / ".gitignore").write_text(
            "artifacts/*\n",
            encoding="utf-8",
        )
        (self.root / "src" / "zevorinth").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "src" / "zevorinth" / "example.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_example.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "evidence@example.invalid"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Evidence Test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "add", ".gitignore", "src", "tests"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-qm", "fixture"],
            cwd=self.root,
            check=True,
        )
        (self.root / "src" / "zevorinth" / "example.py").write_text(
            "VALUE = 3\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_example.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n"
            "    def test_also_ok(self):\n"
            "        self.assertEqual(1 + 1, 2)\n",
            encoding="utf-8",
        )

    def _write_frozen(self) -> None:
        for index, relative in enumerate(self.frozen, start=1):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"frozen-{index}\n", encoding="utf-8")
            self.frozen[relative] = sha256_file(path)

    def _write_previous(self) -> None:
        _write_json(
            self.output_path,
            {
                "status": "PASS",
                "constraint_incidents": [
                    "historical docker test invocation"
                ],
            },
        )

    def _write_review(self) -> None:
        state = git_state(self.root)
        fingerprint = code_fingerprint(self.root)
        _write_json(
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "independent-review"
            / "review-manifest.json",
            {
                "schema_version": "task-control-center-independent-review-v1",
                "scope": "task-control-center-v1",
                "repository": str(self.root.resolve()),
                "head": state["head"],
                "code_fingerprint": fingerprint,
                "code_review": {
                    "status": "UNAVAILABLE",
                    "completed": False,
                    "conclusion": "UNAVAILABLE",
                    "files_reviewed": 0,
                    "reason": "code review channel unavailable",
                },
                "architecture_review": {
                    "status": "UNAVAILABLE",
                    "completed": False,
                    "conclusion": "UNAVAILABLE",
                    "files_reviewed": 0,
                    "reason": "architecture review channel unavailable",
                },
            },
        )

    def complete_reviews(
        self,
        *,
        code_status: str = "CLEAR",
        architecture_status: str = "CLEAR",
    ) -> Path:
        review = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "independent-review"
            / "review-manifest.json"
        )
        payload = json.loads(review.read_text(encoding="utf-8"))
        state = git_state(self.root)
        fingerprint = code_fingerprint(self.root)
        for key, status, required_scope in (
            (
                "code_review",
                code_status,
                REQUIRED_CODE_REVIEW_SCOPE,
            ),
            (
                "architecture_review",
                architecture_status,
                REQUIRED_ARCHITECTURE_REVIEW_SCOPE,
            ),
        ):
            lane = payload[key]
            lane["status"] = status
            lane["completed"] = status != "UNAVAILABLE"
            lane["conclusion"] = status
            if status == "UNAVAILABLE":
                lane["files_reviewed"] = 0
                lane.pop("report", None)
                lane.pop("report_sha256", None)
                lane.pop("result", None)
                lane.pop("result_sha256", None)
                lane.pop("reviewed_files", None)
                continue
            reviewed_paths = (
                "src/zevorinth/example.py",
                "tests/test_example.py",
                "tests/mutation_cases.json",
            )
            reviewed_files = [
                {
                    "path": relative,
                    "sha256": sha256_file(self.root / relative),
                }
                for relative in reviewed_paths
            ]
            report = review.parent / f"{key}.md"
            result = review.parent / f"{key}.json"
            covered_scope = sorted(required_scope)
            report.write_text(
                "Independent review\n"
                f"HEAD: {state['head']}\n"
                f"Code fingerprint: {fingerprint}\n"
                f"Independent review conclusion: {status}\n",
                encoding="utf-8",
            )
            _write_json(
                result,
                {
                    "schema_version":
                        "task-control-center-review-result-v1",
                    "lane": key,
                    "status": status,
                    "conclusion": status,
                    "files_reviewed": len(reviewed_files),
                    "reviewed_files": reviewed_files,
                    "target_repository": str(self.root.resolve()),
                    "target_head": state["head"],
                    "target_code_fingerprint": fingerprint,
                    "covered_scope": covered_scope,
                    "uncovered_scope": [],
                    "report": str(report.relative_to(self.root)),
                    "report_sha256": sha256_file(report),
                },
            )
            lane.update(
                {
                    "files_reviewed": len(reviewed_files),
                    "reviewed_files": reviewed_files,
                    "report": str(report.relative_to(self.root)),
                    "report_sha256": sha256_file(report),
                    "result": str(result.relative_to(self.root)),
                    "result_sha256": sha256_file(result),
                    "target_repository": str(self.root.resolve()),
                    "target_head": state["head"],
                    "target_code_fingerprint": fingerprint,
                    "covered_scope": covered_scope,
                    "uncovered_scope": [],
                }
            )
        _write_json(review, payload)
        self.metadata["review"]["sha256"] = sha256_file(review)
        _write_json(self.metadata_path, self.metadata)
        return review

    def _test_result(
        self,
        name: str,
        *,
        successful: bool = True,
    ) -> tuple[Path, Path]:
        selected = list(REQUIRED_CONTROL_TESTS)
        filtered = [
            f"tests.test_container.ContainerTests.{term}"
            for term in BLOCKED_TEST_TERMS
        ]
        result_path = self.run_dir / f"{name}-result.json"
        log_path = self.run_dir / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"Ran {len(selected)} tests\nOK (skipped=1)\n",
            encoding="utf-8",
        )
        counts = {
            "discovered": len(selected) + len(filtered),
            "selected": len(selected),
            "filtered": len(filtered),
            "passed": len(selected) - 1,
            "failed": 0,
            "errors": 0,
            "skipped": 1,
            "expected_failures": 0,
            "unexpected_successes": 0,
        }
        _write_json(
            result_path,
            {
                "successful": successful,
                "python": f"/fixture/{name}/python",
                "code_fingerprint_before": code_fingerprint(self.root),
                "code_fingerprint_after": code_fingerprint(self.root),
                "counts": counts,
                "selected_tests": selected,
                "filtered_tests": filtered,
            },
        )
        return result_path, log_path

    def _build_metadata(self) -> dict:
        tests = {}
        for name in ("system_python", "venv_python"):
            result, log = self._test_result(name)
            tests[name] = {
                "result": str(result.relative_to(self.root)),
                "result_sha256": sha256_file(result),
                "log": str(log.relative_to(self.root)),
                "log_sha256": sha256_file(log),
            }
        source = self.root / "tests" / "mutation_cases.json"
        _write_json(
            source,
            {
                "version": "1",
                "cases": [
                    {"id": "control-a", "tests": ["test_control"]},
                    {"id": "control-b", "tests": ["test_control"]},
                ],
            },
        )
        self._write_review()
        cases_path = self.run_dir / "mutation-cases.json"
        cases_path.write_text(
            canonical_json(
                {
                    "version": "1",
                    "cases": [
                        {"id": "control-a", "tests": ["test_control"]},
                        {"id": "control-b", "tests": ["test_control"]},
                    ],
                }
            ),
            encoding="utf-8",
        )
        report_path = self.run_dir / "mutations.json"
        _write_json(
            report_path,
            {
                "status": "PASS",
                "case_count": 2,
                "counts": {
                    "killed": 2,
                    "survived": 0,
                    "invalid": 0,
                    "error": 0,
                },
                "evidence_scope_counts": {
                    "in_process": 2,
                    "static_source_check": 0,
                    "unverified": 0,
                },
            },
        )
        review = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "independent-review"
            / "review-manifest.json"
        )
        stable_fingerprint = code_fingerprint(self.root)
        for name in ("system_python", "venv_python"):
            result_path = self.root / tests[name]["result"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["code_fingerprint_before"] = stable_fingerprint
            result["code_fingerprint_after"] = stable_fingerprint
            _write_json(result_path, result)
            tests[name]["result_sha256"] = sha256_file(result_path)
        code = {
            **git_state(self.root),
            "code_fingerprint": code_fingerprint(self.root),
        }
        return {
            "schema_version": "task-control-center-evidence-v1",
            "code": code,
            "tests": tests,
            "mutation": {
                "source": str(source.relative_to(self.root)),
                "source_sha256": sha256_file(source),
                "cases": str(cases_path.relative_to(self.root)),
                "cases_sha256": sha256_file(cases_path),
                "report": str(report_path.relative_to(self.root)),
                "report_sha256": sha256_file(report_path),
                "filters": {
                    "declared": 3,
                    "selected": 2,
                    "filtered": 1,
                },
            },
            "review": {
                "path": str(review.relative_to(self.root)),
                "sha256": sha256_file(review),
            },
            "previous_summary": {
                "path": str(self.previous_path.relative_to(self.root)),
                "sha256": sha256_file(self.previous_path),
            },
            "frozen_evidence": {
                relative: {
                    "expected_sha256": digest,
                    "observed_sha256": digest,
                }
                for relative, digest in self.frozen.items()
            },
        }


class TaskControlCenterEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = EvidenceFixture(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _build(self) -> dict:
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ):
            return build_summary(
                self.root,
                self.fixture.metadata_path,
                replacement_reason="test replacement",
            )

    def test_valid_evidence_generates_deterministic_summary(self) -> None:
        first = self._build()
        second = self._build()
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(first["status"], "PASS_EVIDENCE_INTEGRITY")
        self.assertEqual(
            first["candidate_acceptance"],
            "INDEPENDENT_REVIEW_PENDING",
        )
        self.assertEqual(
            first["constraint_incidents"],
            ["historical docker test invocation"],
        )
        self.assertEqual(
            first["special_review"]["independent_architecture_review"],
            "UNAVAILABLE",
        )
        self.assertFalse(first["review_status"]["merge_ready"])
        audit_root = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
        )
        first_path = audit_root / "summary-a" / "stable-summary.json"
        second_path = audit_root / "summary-b" / "stable-summary.json"
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ):
            write_summary(
                self.root,
                self.fixture.metadata_path,
                first_path,
                replacement_reason="test replacement",
            )
            write_summary(
                self.root,
                self.fixture.metadata_path,
                second_path,
                replacement_reason="test replacement",
            )
        self.assertEqual(first_path.read_bytes(), second_path.read_bytes())

    def test_candidate_snapshot_binds_exact_changed_files(self) -> None:
        output = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "final-candidate-snapshot-12345678.json"
        )
        write_candidate_snapshot(self.root, output)
        snapshot = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["code_fingerprint"], code_fingerprint(self.root))
        self.assertEqual(
            snapshot["status_sha256"],
            git_state(self.root)["status_sha256"],
        )
        self.assertEqual(
            [item["path"] for item in snapshot["files"]],
            [
                "src/zevorinth/example.py",
                "tests/mutation_cases.json",
                "tests/test_example.py",
            ],
        )
        self.assertTrue(
            all(
                item["sha256"] == sha256_file(self.root / item["path"])
                for item in snapshot["files"]
            )
        )
        with self.assertRaises(EvidenceError):
            write_candidate_snapshot(self.root, output)

    def test_log_hash_changes_are_recorded_in_summary(self) -> None:
        first = self._build()
        log_path = (
            self.root
            / self.fixture.metadata["tests"]["system_python"]["log"]
        )
        log_path.write_text(
            "Ran 129 tests in 99.999s\nOK\n",
            encoding="utf-8",
        )
        self.fixture.metadata["tests"]["system_python"]["log_sha256"] = (
            sha256_file(log_path)
        )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        second = self._build()
        self.assertNotEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(
            second["tests"]["system_python"]["log_sha256"],
            self.fixture.metadata["tests"]["system_python"]["log_sha256"],
        )

    def test_missing_input_and_invalid_json_fail_closed(self) -> None:
        with self.assertRaises(EvidenceError):
            build_summary(
                self.root,
                self.root / "missing.json",
                replacement_reason="test",
            )
        self.fixture.metadata_path.write_text("{", encoding="utf-8")
        with self.assertRaises(EvidenceError):
            self._build()

    def test_test_count_contradiction_fails(self) -> None:
        with self.assertRaises(EvidenceError):
            _validate_counts(
                {
                    "discovered": 3,
                    "selected": 3,
                    "filtered": 1,
                    "passed": 2,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                    "expected_failures": 0,
                    "unexpected_successes": 0,
                },
                "test",
            )

    def test_container_backed_module_is_filtered_without_name_heuristics(self) -> None:
        self.assertTrue(
            is_blocked_test_id(
                "tests.test_evidence_provisioning.ProvisioningTests."
                "test_provisioning_rejects_wrong_image"
            )
        )
        self.assertTrue(
            is_blocked_test_id(
                "test_evidence_provisioning.ProvisioningTests."
                "test_provisioning_rejects_wrong_image"
            )
        )
        self.assertFalse(
            is_blocked_test_id("tests.test_event_store.EventStoreTests.test_ok")
        )

    def test_hash_mismatch_fails(self) -> None:
        system_log = self.root / self.fixture.metadata["tests"]["system_python"]["log"]
        system_log.write_text("changed\n", encoding="utf-8")
        with self.assertRaises(EvidenceError):
            self._build()

    def test_stale_test_result_fingerprint_fails(self) -> None:
        result_path = (
            self.root
            / self.fixture.metadata["tests"]["system_python"]["result"]
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["code_fingerprint_before"] = "0" * 64
        result["code_fingerprint_after"] = "0" * 64
        _write_json(result_path, result)
        self.fixture.metadata["tests"]["system_python"]["result_sha256"] = (
            sha256_file(result_path)
        )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaises(EvidenceError):
            self._build()

    def test_evidence_paths_cannot_escape_repository(self) -> None:
        self.fixture.metadata["tests"]["system_python"]["result"] = (
            "../outside-result.json"
        )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaises(EvidenceError):
            self._build()

    def test_failed_suite_cannot_pass(self) -> None:
        result_path = self.root / self.fixture.metadata["tests"]["venv_python"]["result"]
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["successful"] = False
        _write_json(result_path, result)
        self.fixture.metadata["tests"]["venv_python"]["result_sha256"] = (
            sha256_file(result_path)
        )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaises(EvidenceError):
            self._build()

    def test_code_fingerprint_mismatch_fails(self) -> None:
        (self.root / "src" / "zevorinth" / "example.py").write_text(
            "VALUE = 2\n",
            encoding="utf-8",
        )
        with self.assertRaises(EvidenceError):
            self._build()

    def test_required_control_tests_use_exact_ids(self) -> None:
        result_path = self.root / self.fixture.metadata["tests"]["venv_python"][
            "result"
        ]
        result = json.loads(result_path.read_text(encoding="utf-8"))
        required = result["selected_tests"][0]
        method = required.rsplit(".", 1)[1]
        result["selected_tests"][0] = (
            f"unrelated.control_suite.DecoyTests.{method}"
        )
        _write_json(result_path, result)
        self.fixture.metadata["tests"]["venv_python"]["result_sha256"] = (
            sha256_file(result_path)
        )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaises(EvidenceError):
            self._build()

    def test_worktree_status_hash_drift_fails(self) -> None:
        (self.root / "README.md").write_text(
            "worktree changed after evidence run\n",
            encoding="utf-8",
        )
        with self.assertRaises(EvidenceError):
            self._build()

    def test_frozen_evidence_mismatch_fails(self) -> None:
        frozen_path = self.root / next(iter(self.fixture.frozen))
        frozen_path.write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(EvidenceError):
            self._build()

    def test_mutation_contradiction_fails(self) -> None:
        report_path = self.root / self.fixture.metadata["mutation"]["report"]
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["counts"]["killed"] = 1
        _write_json(report_path, report)
        self.fixture.metadata["mutation"]["report_sha256"] = sha256_file(report_path)
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaises(EvidenceError):
            self._build()

    def test_missing_independent_status_fails(self) -> None:
        review = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "independent-review"
            / "review-manifest.json"
        )
        payload = json.loads(review.read_text(encoding="utf-8"))
        payload.pop("architecture_review")
        _write_json(review, payload)
        self.fixture.metadata["review"]["sha256"] = sha256_file(review)
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaises(EvidenceError):
            self._build()

    def test_review_head_and_hash_mismatches_fail(self) -> None:
        review = self.fixture.complete_reviews()
        payload = json.loads(review.read_text(encoding="utf-8"))
        payload["head"] = "0" * 40
        _write_json(review, payload)
        self.fixture.metadata["review"]["sha256"] = sha256_file(review)
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaises(EvidenceError):
            self._build()

        review = self.fixture.complete_reviews()
        payload = json.loads(review.read_text(encoding="utf-8"))
        report = self.root / payload["code_review"]["report"]
        report.write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(EvidenceError):
            self._build()

    def test_machine_result_cannot_disagree_with_manifest(self) -> None:
        review = self.fixture.complete_reviews()
        payload = json.loads(review.read_text(encoding="utf-8"))
        result_path = self.root / payload["code_review"]["result"]
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["status"] = "REQUEST_CHANGES"
        result["conclusion"] = "REQUEST_CHANGES"
        _write_json(result_path, result)
        payload["code_review"]["result_sha256"] = sha256_file(result_path)
        _write_json(review, payload)
        self.fixture.metadata["review"]["sha256"] = sha256_file(review)
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaises(EvidenceError):
            self._build()

    def test_report_conclusion_cannot_disagree_with_machine_result(self) -> None:
        review = self.fixture.complete_reviews()
        payload = json.loads(review.read_text(encoding="utf-8"))
        report = self.root / payload["code_review"]["report"]
        report.write_text(
            report.read_text(encoding="utf-8").replace(
                "Independent review conclusion: CLEAR",
                "Independent review conclusion: REQUEST_CHANGES",
            ),
            encoding="utf-8",
        )
        payload["code_review"]["report_sha256"] = sha256_file(report)
        result_path = self.root / payload["code_review"]["result"]
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["report_sha256"] = sha256_file(report)
        _write_json(result_path, result)
        payload["code_review"]["result_sha256"] = sha256_file(result_path)
        _write_json(review, payload)
        self.fixture.metadata["review"]["sha256"] = sha256_file(review)
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaises(EvidenceError):
            self._build()

    def test_review_report_requires_conclusion_declaration(self) -> None:
        review = self.fixture.complete_reviews()
        payload = json.loads(review.read_text(encoding="utf-8"))
        report = self.root / payload["code_review"]["report"]
        report.write_text(
            "\n".join(
                line
                for line in report.read_text(encoding="utf-8").splitlines()
                if not line.startswith("Independent review conclusion:")
            )
            + "\n",
            encoding="utf-8",
        )
        payload["code_review"]["report_sha256"] = sha256_file(report)
        result_path = self.root / payload["code_review"]["result"]
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["report_sha256"] = sha256_file(report)
        _write_json(result_path, result)
        payload["code_review"]["result_sha256"] = sha256_file(result_path)
        _write_json(review, payload)
        self.fixture.metadata["review"]["sha256"] = sha256_file(review)
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaises(EvidenceError):
            self._build()

    def test_review_files_must_match_changed_candidate_exactly(self) -> None:
        review = self.fixture.complete_reviews()
        payload = json.loads(review.read_text(encoding="utf-8"))
        extra = self.root / "extra.txt"
        extra.write_text("EXTRA\n", encoding="utf-8")
        extra_record = {
            "path": extra.relative_to(self.root).as_posix(),
            "sha256": sha256_file(extra),
        }
        payload["code_review"]["reviewed_files"].append(extra_record)
        payload["code_review"]["files_reviewed"] += 1
        result_path = self.root / payload["code_review"]["result"]
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["reviewed_files"].append(extra_record)
        result["files_reviewed"] += 1
        _write_json(result_path, result)
        payload["code_review"]["result_sha256"] = sha256_file(result_path)
        _write_json(review, payload)
        self.fixture.metadata["review"]["sha256"] = sha256_file(review)
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaises(EvidenceError):
            self._build()

    def test_review_input_paths_cannot_target_protected_files(self) -> None:
        for relative in (".idea/leak.json", ".env", ".env.local"):
            with self.subTest(relative=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
                with self.assertRaises(EvidenceError):
                    _evidence_file(self.root, relative, "review input")

    def test_zero_file_or_incomplete_review_cannot_pass(self) -> None:
        review = self.fixture.complete_reviews()
        payload = json.loads(review.read_text(encoding="utf-8"))
        payload["code_review"]["files_reviewed"] = 0
        _write_json(review, payload)
        self.fixture.metadata["review"]["sha256"] = sha256_file(review)
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaises(EvidenceError):
            self._build()

    def test_missing_review_report_fails(self) -> None:
        review = self.fixture.complete_reviews()
        payload = json.loads(review.read_text(encoding="utf-8"))
        (self.root / payload["code_review"]["report"]).unlink()
        with self.assertRaises(EvidenceError):
            self._build()

    def test_corrupt_review_manifest_fails(self) -> None:
        review = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "independent-review"
            / "review-manifest.json"
        )
        review.write_text("{not json", encoding="utf-8")
        self.fixture.metadata["review"]["sha256"] = sha256_file(review)
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaises(EvidenceError):
            self._build()

    def test_unavailable_review_channel_keeps_pending(self) -> None:
        self.fixture.complete_reviews(
            code_status="UNAVAILABLE",
            architecture_status="CLEAR",
        )
        summary = self._build()
        self.assertEqual(
            summary["review_status"]["independent_code_review"],
            "UNAVAILABLE",
        )
        self.assertFalse(summary["review_status"]["merge_ready"])
        self.assertEqual(
            summary["candidate_acceptance"],
            "INDEPENDENT_REVIEW_PENDING",
        )
        self.assertIn(
            "code_review:UNAVAILABLE",
            summary["review_status"]["blocking_reasons"],
        )

    def test_unavailable_review_requires_explicit_reason(self) -> None:
        for reason in ("", None, 7):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                review = fixture.complete_reviews(
                    code_status="UNAVAILABLE",
                    architecture_status="CLEAR",
                )
                payload = json.loads(review.read_text(encoding="utf-8"))
                payload["code_review"]["reason"] = reason
                _write_json(review, payload)
                fixture.metadata["review"]["sha256"] = sha256_file(review)
                _write_json(fixture.metadata_path, fixture.metadata)
                with patch(
                    "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                    fixture.frozen,
                ), self.assertRaises(EvidenceError):
                    build_summary(
                        root,
                        fixture.metadata_path,
                        replacement_reason="test",
                    )

    def test_request_changes_and_watch_remain_blocking_risk_states(self) -> None:
        self.fixture.complete_reviews(
            code_status="REQUEST_CHANGES",
            architecture_status="CLEAR",
        )
        summary = self._build()
        self.assertEqual(
            summary["review_status"]["independent_code_review"],
            "REQUEST_CHANGES",
        )
        self.assertFalse(summary["review_status"]["merge_ready"])
        self.assertEqual(
            summary["candidate_acceptance"],
            "INDEPENDENT_REVIEW_BLOCKED",
        )

        self.fixture.complete_reviews(
            code_status="CLEAR",
            architecture_status="WATCH",
        )
        summary = self._build()
        self.assertEqual(
            summary["review_status"]["independent_architecture_review"],
            "WATCH",
        )
        self.assertFalse(summary["review_status"]["merge_ready"])
        self.assertIn(
            "architecture_review:WATCH",
            summary["review_status"]["blocking_reasons"],
        )

    def test_completed_blocking_review_precedes_unavailable_lane(self) -> None:
        self.fixture.complete_reviews(
            code_status="REQUEST_CHANGES",
            architecture_status="UNAVAILABLE",
        )
        summary = self._build()
        self.assertEqual(
            summary["candidate_acceptance"],
            "INDEPENDENT_REVIEW_BLOCKED",
        )
        self.assertIn(
            "architecture_review:UNAVAILABLE",
            summary["review_status"]["blocking_reasons"],
        )

    def test_clear_independent_reviews_enable_merge_readiness_only(self) -> None:
        review = self.fixture.complete_reviews()
        validated = validate_review_manifest(
            self.root,
            review,
            expected_code_fingerprint=code_fingerprint(self.root),
        )
        self.assertTrue(validated["merge_ready"])
        self.assertEqual(
            validated["candidate_acceptance"],
            "INDEPENDENT_REVIEW_CLEAR",
        )
        summary = self._build()
        self.assertEqual(summary["status"], "PASS_EVIDENCE_INTEGRITY")
        self.assertTrue(summary["review_status"]["merge_ready"])

    def test_output_cannot_overwrite_evidence_or_frozen_file(self) -> None:
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ):
            with self.assertRaises(EvidenceError):
                write_summary(
                    self.root,
                    self.fixture.metadata_path,
                    self.root / "evidence" / "mutations.json",
                    replacement_reason="bad",
                )

    def test_output_cannot_escape_audit_root_or_overwrite_source(self) -> None:
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ):
            for output in (
                self.root / "src" / "zevorinth" / "example.py",
                self.root / "outside-summary.json",
                (
                    self.root
                    / "artifacts"
                    / "audits"
                    / "task-control-center-v1"
                    / "unrelated.json"
                ),
            ):
                with self.subTest(output=output), self.assertRaises(
                    EvidenceError
                ):
                    write_summary(
                        self.root,
                        self.fixture.metadata_path,
                        output,
                        replacement_reason="bad",
                    )
            with self.assertRaises(EvidenceError):
                write_summary(
                    self.root,
                    self.fixture.metadata_path,
                    self.root / next(iter(self.fixture.frozen)),
                    replacement_reason="bad",
                )


if __name__ == "__main__":
    unittest.main()
