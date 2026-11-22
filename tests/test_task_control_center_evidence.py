"""Tests for Task Control Center evidence generation."""

from __future__ import annotations

import contextlib
import errno
import inspect
import json
import io
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tests import mutation_runner  # noqa: E402
from tests.task_control_center_evidence import (  # noqa: E402
    BLOCKED_TEST_TERMS,
    BOOTSTRAP_INCOMPLETE,
    CANDIDATE_IDENTITY_VERIFIED,
    CANDIDATE_SCHEMA_VERSION,
    EVIDENCE_BUNDLE_SCHEMA_VERSION,
    EVIDENCE_TOOL_VERSION,
    NOT_AVAILABLE,
    NOT_VERIFIED,
    PASS_EVIDENCE_INTEGRITY,
    BLOCKED,
    ACCEPTED_FOR_MERGE_REVIEW,
    REQUIRED_ARCHITECTURE_REVIEW_SCOPE,
    REQUIRED_CODE_REVIEW_SCOPE,
    REQUIRED_CONTROL_TESTS,
    REVIEW_BINDING_SCHEMA_VERSION,
    SCHEMA_VERSION,
    TEST_RESULT_SCHEMA_VERSION,
    EvidenceError,
    TrustedEvidenceRoot,
    _enumerate_bundle_files,
    _protected_evidence_root,
    _selected_bundle_path,
    _validate_evidence_bundle_with_root,
    _write_acceptance_status,
    _open_absolute_directory,
    _pin_directory_identity,
    _popen_in_trusted_cwd,
    _trusted_repository_scope,
    _evidence_file,
    _canonical_path_identity,
    _validate_relative_candidate_path,
    _validate_counts,
    _parse_json_object_bytes,
    _test_selection_rules_sha256,
    _current_test_discovery,
    _collect_run_evidence_outputs,
    _execute_test_collection,
    _reexecute_mutation_report,
    _run_tests_impl,
    _review_summary_from_binding,
    _validate_previous_summary,
    _validate_mutation_evidence,
    _acceptance_prerequisite_status,
    _issue_acceptance_prerequisite_proof,
    _new_acceptance_prerequisite_owner,
    _make_acceptance_prerequisite_authority,
    _revoke_acceptance_prerequisite_proof,
    _OwnedFDCollection,
    build_candidate_snapshot,
    _evaluate_acceptance_gate_core,
    evaluate_acceptance_gate as evaluate_public_acceptance_gate,
    build_review_binding,
    build_summary,
    canonical_json,
    code_fingerprint,
    file_manifest_sha256,
    git_state,
    is_blocked_test_id,
    main,
    run_evidence,
    read_json,
    sha256_bytes,
    sha256_file,
    validate_browser_evidence,
    validate_candidate_snapshot,
    validate_evidence_bundle,
    validate_acceptance_status,
    validate_summary_is_current,
    validate_review_binding,
    validate_review_manifest,
    write_candidate_snapshot,
    write_review_binding,
    write_summary,
)

FROZEN_RELATIVE_PATHS = (
    "artifacts/audits/phase-3.90/stable-summary.json",
    "artifacts/audits/phase-3.91/stable-summary.json",
    "artifacts/audits/phase-3.92/stable-summary.json",
)


def _write_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(payload), encoding="utf-8")
    return sha256_file(path)


def _owned_file_fd(
    path: Path,
    flags: int = os.O_RDONLY,
    owner_type: type[_OwnedFDCollection] = _OwnedFDCollection,
) -> tuple[_OwnedFDCollection, int]:
    owner = owner_type()
    file_fd = os.open(path, flags)
    owner.register(file_fd)
    return owner, file_fd


def _fd_is_open(file_fd: int) -> bool:
    try:
        os.fstat(file_fd)
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return False
        raise
    return True


def _validate_bundle(
    fixture: EvidenceFixture,
    root: Path,
    bundle_path: Path,
) -> dict:
    with patch(
        "tests.task_control_center_evidence.FROZEN_EVIDENCE",
        fixture.frozen,
    ):
        return validate_evidence_bundle(root, bundle_path)


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
        self._write_mutation_source()
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
        (self.root / "src" / "zevorinth" / "__init__.py").write_text(
            "",
            encoding="utf-8",
        )
        (self.root / "tests").mkdir()
        (self.root / "pyproject.toml").write_text(
            "[project]\nname = \"evidence-fixture\"\nversion = \"0.0.0\"\n",
            encoding="utf-8",
        )
        shutil.copyfile(
            PROJECT_ROOT / "tests" / "mutation_runner.py",
            self.root / "tests" / "mutation_runner.py",
        )
        (self.root / "src" / "zevorinth" / "example.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_example.py").write_text(
            "import unittest\n"
            "from zevorinth.example import VALUE\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        control_tests: dict[str, dict[str, list[str]]] = {}
        for qualified in REQUIRED_CONTROL_TESTS:
            module, class_name, method_name = qualified.rsplit(".", 2)
            control_tests.setdefault(module, {}).setdefault(
                class_name,
                [],
            ).append(method_name)
        for module, classes in control_tests.items():
            source = ["import unittest\n"]
            for class_name, methods in classes.items():
                source.append(f"class {class_name}(unittest.TestCase):\n")
                for method_name in methods:
                    source.append(
                        f"    def {method_name}(self):\n"
                        "        self.assertTrue(True)\n"
                    )
            (self.root / "tests" / f"{module}.py").write_text(
                "".join(source),
                encoding="utf-8",
            )
        (self.root / "tests" / "test_container.py").write_text(
            "import unittest\n"
            "class ContainerTests(unittest.TestCase):\n"
            "    def test_container_runtime_is_filtered(self):\n"
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
            ["git", "add", ".gitignore", "pyproject.toml", "src", "tests"],
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
            "from zevorinth.example import VALUE\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n"
            "    def test_also_ok(self):\n"
            "        self.assertEqual(1 + 1, 2)\n"
            "    def test_candidate_value(self):\n"
            "        self.assertEqual(VALUE, 3)\n",
            encoding="utf-8",
        )

    def _write_frozen(self) -> None:
        for index, relative in enumerate(self.frozen, start=1):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"frozen-{index}\n", encoding="utf-8")
            self.frozen[relative] = sha256_file(path)

    def _write_mutation_source(
        self,
    ) -> tuple[Path, list[dict[str, object]]]:
        source = self.root / "tests" / "mutation_cases.json"
        selected_cases = [
            {
                "id": "control-a",
                "property": "candidate value remains exact",
                "tests": ["tests.test_example.T.test_candidate_value"],
                "patches": [
                    {
                        "path": "src/zevorinth/example.py",
                        "old": "VALUE = 3\n",
                        "new": "VALUE = 4\n",
                    }
                ],
            },
            {
                "id": "control-b",
                "property": "candidate assertion remains exact",
                "tests": ["tests.test_example.T.test_candidate_value"],
                "patches": [
                    {
                        "path": "tests/test_example.py",
                        "old": "self.assertEqual(VALUE, 3)",
                        "new": "self.assertEqual(VALUE, 4)",
                    }
                ],
            },
        ]
        _write_json(
            source,
            {
                "version": "1.1.0",
                "cases": [
                    *selected_cases,
                    {
                        "id": "blocked-control",
                        "tests": ["test_container"],
                    },
                ],
            },
        )
        return source, selected_cases

    def _write_previous(self) -> None:
        state = git_state(self.root)
        fingerprint = code_fingerprint(self.root)
        candidate = build_candidate_snapshot(self.root)
        self.trusted_previous_candidate_snapshot_path = (
            self.run_dir / "trusted-previous-candidate.json"
        )
        _write_json(
            self.trusted_previous_candidate_snapshot_path,
            candidate,
        )
        replacement_reason = "historical replacement"
        previous_sha256 = "a" * 64
        history = [
            {
                "previous_sha256": previous_sha256,
                "replacement_reason": replacement_reason,
            }
        ]
        reviewed_files = [
            {"path": item["path"], "sha256": item["sha256"]}
            for item in candidate["files"]
        ]
        review_lanes = {}
        for key, required_scope in (
            ("code_review", REQUIRED_CODE_REVIEW_SCOPE),
            ("architecture_review", REQUIRED_ARCHITECTURE_REVIEW_SCOPE),
        ):
            review_lanes[key] = {
                "status": "CLEAR",
                "completed": True,
                "conclusion": "CLEAR",
                "files_reviewed": len(reviewed_files),
                "reviewed_files": reviewed_files,
                "report": f"historical-{key}.md",
                "report_sha256": "5" * 64,
                "result": f"historical-{key}.json",
                "result_sha256": "6" * 64,
                "target_repository": candidate["repository"],
                "target_head": candidate["target_head"],
                "target_code_fingerprint": candidate[
                    "code_fingerprint"
                ],
                "covered_scope": sorted(required_scope),
                "uncovered_scope": [],
            }
        tests = {}
        for name in ("system_python", "venv_python"):
            tests[name] = {
                "discovered": 3,
                "selected": 2,
                "filtered": 1,
                "passed": 2,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "expected_failures": 0,
                "unexpected_successes": 0,
                "python": f"/fixture/{name}/python",
                "result": f"<run>/{name}-result.json",
                "result_sha256": "b" * 64,
                "log": f"<run>/{name}.log",
                "log_sha256": "c" * 64,
                "discovery_sha256": "d" * 64,
                "selection_rules_version": "blocked-test-id-v1",
                "selection_rules_sha256": (
                    _test_selection_rules_sha256()
                ),
            }
        _write_json(
            self.output_path,
            {
                "schema_version": SCHEMA_VERSION,
                "scope": "task-control-center-v1",
                "validation_input": {
                    "run_metadata": "<run>/run-metadata.json",
                    "run_metadata_sha256": "f" * 64,
                    "evidence_bundle_path": None,
                    "replacement_reason_sha256": sha256_bytes(
                        replacement_reason.encode("utf-8")
                    ),
                    "trusted_previous_summary_sha256": "4" * 64,
                    "generator_version": "phase-2.7a-21",
                },
                "status": PASS_EVIDENCE_INTEGRITY,
                "validation_mode": "offline_noncontainer",
                "candidate_identity": candidate,
                "evidence_bundle": None,
                "review_binding": None,
                "code": {
                    **state,
                    "code_fingerprint": fingerprint,
                },
                "tests": {
                    "selection": {
                        "blocked_terms": list(BLOCKED_TEST_TERMS),
                        "blocked_modules": [
                            "test_evidence_provisioning",
                            "tests.test_evidence_provisioning",
                        ],
                    },
                    **tests,
                },
                "mutations": {
                    "declared": 2,
                    "executed": 2,
                    "filtered": 0,
                    "killed": 2,
                    "survived": 0,
                    "invalid": 0,
                    "error": 0,
                    "evidence_scopes": {
                        "in_process": 2,
                        "static_source_check": 0,
                        "unverified": 0,
                    },
                    "cases": "<run>/mutation-cases.json",
                    "cases_sha256": "1" * 64,
                    "report": "<run>/mutations.json",
                    "report_sha256": "2" * 64,
                },
                "special_review": {
                    "independent_code_review": "CLEAR",
                    "independent_architecture_review": "CLEAR",
                    "code_review": review_lanes["code_review"],
                    "architecture_review": review_lanes[
                        "architecture_review"
                    ],
                    "merge_ready": True,
                    "blocking_reasons": [],
                    "candidate_acceptance": (
                        "INDEPENDENT_REVIEW_CLEAR"
                    ),
                    "review_clear": True,
                    "uncovered_scope": [],
                    "path": "historical-review.json",
                    "sha256": "3" * 64,
                },
                "review_status": {
                    "independent_code_review": "CLEAR",
                    "independent_architecture_review": "CLEAR",
                    "merge_ready": True,
                    "blocking_reasons": [],
                    "review_gate": "CLEAR",
                },
                "acceptance_gate": {
                    "candidate_identity": CANDIDATE_IDENTITY_VERIFIED,
                    "historical_review": "CLEAR",
                    "content_equivalence_binding": "NOT_AVAILABLE",
                    "current_target_independent_review": "CLEAR",
                    "current_target_review_resolution": "DIRECT_CLEAR",
                    "review_gate": "CLEAR",
                    "acceptance_policy": "target_head_review_required",
                    "tests": "VERIFIED",
                    "frozen_evidence": "VERIFIED",
                    "previous_summary": "AVAILABLE",
                    "previous_summary_trusted": True,
                    "runtime_verification": {
                        "status": "VERIFIED",
                        "scopes": ["browser"],
                        "reason": "historical browser verification",
                        "valid_evidence_count": 1,
                    },
                    "persistent_publication_ancestry": "VERIFIED",
                    "mutation_execution_trust": "VERIFIED",
                    "test_execution_trust": "VERIFIED",
                    "unverified_scopes": [],
                    "blocking_reasons": [],
                    "evidence_integrity_status": PASS_EVIDENCE_INTEGRITY,
                    "acceptance_status": ACCEPTED_FOR_MERGE_REVIEW,
                    "merge_ready": True,
                },
                "constraint_incidents": [
                    "historical docker test invocation"
                ],
                "evidence_history": history,
                "constraint_scope": {
                    "runtime_side_effect_observation": "NOT_INSTRUMENTED",
                    "selection_basis": "historical offline test selection",
                    "container_tests_executed_this_run": None,
                    "docker_backed_tests_executed_this_run": None,
                    "live_tests_executed_this_run": None,
                    "online_tests_executed_this_run": None,
                    "real_model_requests": None,
                    "external_network_requests": None,
                    "dependency_installs": None,
                },
                "frozen_evidence": {
                    relative: {
                        "expected_sha256": digest,
                        "observed_sha256": digest,
                    }
                    for relative, digest in self.frozen.items()
                },
                "superseded_summary": {
                    "previous_sha256": previous_sha256,
                    "replacement_reason": replacement_reason,
                },
                "candidate_acceptance": ACCEPTED_FOR_MERGE_REVIEW,
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
        payload["repository"] = str(self.root.resolve())
        payload["head"] = state["head"]
        payload["code_fingerprint"] = fingerprint
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

    def commit_candidate(self) -> tuple[str, str]:
        base = git_state(self.root)["head"]
        subprocess.run(
            ["git", "add", "--", "src", "tests"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-qm", "candidate changes"],
            cwd=self.root,
            check=True,
        )
        return base, git_state(self.root)["head"]

    def replace_reviewed_files(
        self,
        key: str,
        reviewed_files: list[dict[str, str]],
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
        lane = payload[key]
        lane["reviewed_files"] = reviewed_files
        lane["files_reviewed"] = len(reviewed_files)
        result_path = self.root / lane["result"]
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["reviewed_files"] = reviewed_files
        result["files_reviewed"] = len(reviewed_files)
        _write_json(result_path, result)
        lane["result_sha256"] = sha256_file(result_path)
        _write_json(review, payload)
        self.metadata["review"]["sha256"] = sha256_file(review)
        _write_json(self.metadata_path, self.metadata)
        return review

    def _sync_previous_candidate(self, candidate: dict) -> None:
        previous = json.loads(
            self.output_path.read_text(encoding="utf-8")
        )
        state = git_state(self.root)
        previous["candidate_identity"] = candidate
        previous["code"] = {
            **state,
            "code_fingerprint": candidate["code_fingerprint"],
        }
        reviewed_files = [
            {
                "path": item["path"],
                "sha256": item["sha256"],
            }
            for item in candidate["files"]
        ]
        special = previous["special_review"]
        for key in ("code_review", "architecture_review"):
            lane = special[key]
            lane["files_reviewed"] = len(reviewed_files)
            lane["reviewed_files"] = reviewed_files
            lane["target_repository"] = candidate["repository"]
            lane["target_head"] = candidate["target_head"]
            lane["target_code_fingerprint"] = candidate[
                "code_fingerprint"
            ]
        for path in (self.output_path, self.previous_path):
            _write_json(path, previous)
        _write_json(
            self.trusted_previous_candidate_snapshot_path,
            candidate,
        )
        self.metadata["previous_summary"]["sha256"] = sha256_file(
            self.previous_path
        )
        _write_json(self.metadata_path, self.metadata)

    def write_evidence_bundle(
        self,
        bundle_root: Path,
        *,
        acceptance_policy: str = "content_equivalence_allowed",
        runtime_status: str = "VERIFIED",
        code_status: str = "CLEAR",
        direct_target_review: bool = False,
    ) -> Path:
        bundle_root = bundle_root.resolve()
        if direct_target_review:
            base, target = self.commit_candidate()
            review = self.complete_reviews(code_status=code_status)
        else:
            review = self.complete_reviews(code_status=code_status)
            base, target = self.commit_candidate()
        review_payload = json.loads(review.read_text(encoding="utf-8"))
        candidate = build_candidate_snapshot(
            self.root,
            candidate_mode="committed_range",
            base_head=base,
            target_head=target,
            reviewed_head=review_payload["head"],
        )
        self._sync_previous_candidate(candidate)
        binding_path = None
        if not direct_target_review:
            binding_path = (
                self.root
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / "review-binding-bundle.json"
            )
            write_review_binding(
                self.root,
                review,
                binding_path,
                base_head=base,
                target_head=target,
                generated_at="2026-10-25T00:00:00Z",
            )
        bundle_root.mkdir(parents=True)
        candidate_relative = "candidate-identity.json"
        candidate_path = bundle_root / candidate_relative
        candidate_path.write_text(
            canonical_json(candidate),
            encoding="utf-8",
        )
        review_relative = review.relative_to(self.root).as_posix()
        binding_relative = (
            binding_path.relative_to(self.root).as_posix()
            if binding_path is not None
            else None
        )
        previous_relative = self.previous_path.relative_to(self.root).as_posix()
        copy_paths = {
            review_relative,
            previous_relative,
            *FROZEN_RELATIVE_PATHS,
            review_payload["code_review"]["report"],
            review_payload["code_review"]["result"],
            review_payload["architecture_review"]["report"],
            review_payload["architecture_review"]["result"],
        }
        if binding_relative is not None:
            copy_paths.add(binding_relative)
        for relative in sorted(copy_paths):
            destination = bundle_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / relative, destination)

        browser_relative = (
            "artifacts/audits/task-control-center-v1/browser-bundle.json"
        )
        browser_path = bundle_root / browser_relative
        browser_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(
            browser_path,
            {
                "schema_version": "task-control-center-browser-validation-v1",
                "candidate": {
                    "head": target,
                    "code_fingerprint": candidate["code_fingerprint"],
                    "snapshot": candidate_relative,
                    "snapshot_sha256": sha256_file(candidate_path),
                },
                "browser": {"console_warning_error_count": 0},
                "results": {
                    "active_lease": {
                        "lease_state": "active",
                        "ui_verified": True,
                        "actions": [],
                        "reason": "task_claim_active",
                    },
                    "expired_lease": {
                        "lease_state": "expired",
                        "ui_verified": True,
                        "actions": [],
                        "reason": "task_claim_recovery_required",
                        "forged_post_http_status": 409,
                        "forged_post_error": "task_claim_recovery_required",
                        "tool_started": 0,
                        "task_status_after": "queued",
                    },
                    "released_lease": {
                        "lease_state": "none",
                        "ui_verified": True,
                        "actions": [
                            "revise_unstarted_task",
                            "add_task_note",
                        ],
                        "start_visible": False,
                        "cancel_visible": False,
                    },
                    "preview_reads": {
                        "task_event_artifact_state_unchanged": True,
                        "database_sha256_before": "a" * 64,
                        "database_sha256_after": "a" * 64,
                    },
                    "mobile_layout": {
                        "viewport_width": 375,
                        "horizontal_overflow": False,
                    },
                },
            },
        )
        declared_review = {
            "reviewed_head": review_payload["head"],
        }
        for key in ("code_review", "architecture_review"):
            lane = review_payload[key]
            declared_review[key] = {
                field: lane.get(field)
                for field in (
                    "status",
                    "conclusion",
                    "files_reviewed",
                    "reviewed_files",
                    "report",
                    "report_sha256",
                    "result",
                    "result_sha256",
                )
            }
        frozen_evidence = {
            relative: {
                "path": relative,
                "sha256": sha256_file(self.root / relative),
                "status": "VERIFIED",
            }
            for relative in FROZEN_RELATIVE_PATHS
        }
        referenced_paths = {
            candidate_relative,
            review_relative,
            previous_relative,
            browser_relative,
            *copy_paths,
        }
        roles = {
            candidate_relative: "candidate_snapshot",
            review_relative: "review_manifest",
            previous_relative: "previous_summary",
            browser_relative: "browser_evidence:0",
            review_payload["code_review"]["report"]: "code_review_report",
            review_payload["code_review"]["result"]: "code_review_result",
            review_payload["architecture_review"]["report"]:
                "architecture_review_report",
            review_payload["architecture_review"]["result"]:
                "architecture_review_result",
            **{
                relative: f"frozen_evidence:{relative}"
                for relative in FROZEN_RELATIVE_PATHS
            },
        }
        if binding_relative is not None:
            roles[binding_relative] = "review_binding"
        manifest = {
            "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
            "bundle_id": "phase-2.7a-2-fixture",
            "generator_version": EVIDENCE_TOOL_VERSION,
            "created_at": "2026-10-25T00:00:00Z",
            "acceptance_policy": acceptance_policy,
            "candidate": {
                **{
                    field: candidate[field]
                    for field in (
                        "candidate_mode",
                        "base_head",
                        "reviewed_head",
                        "target_head",
                        "code_fingerprint",
                        "file_count",
                        "file_manifest_sha256",
                        "evidence_status",
                    )
                },
                "snapshot": {
                    "path": candidate_relative,
                    "sha256": sha256_file(candidate_path),
                },
            },
            "review_manifest": {
                "path": review_relative,
                "sha256": sha256_file(self.root / review_relative),
            },
            "review_binding": (
                {
                    "path": binding_relative,
                    "sha256": sha256_file(binding_path),
                }
                if binding_relative is not None
                else None
            ),
            "review": declared_review,
            "previous_summary": {
                "path": previous_relative,
                "sha256": sha256_file(self.previous_path),
                "status": "AVAILABLE",
            },
            "frozen_evidence": frozen_evidence,
            "browser_evidence": [
                {
                    "path": browser_relative,
                    "sha256": sha256_file(browser_path),
                    "status": (
                        "VALID_FOR_CANDIDATE"
                        if runtime_status == "VERIFIED"
                        else "NOT_VERIFIED"
                    ),
                    "reason": "local bundle fixture",
                }
            ],
            "runtime_verification": {
                "status": runtime_status,
                "scopes": ["browser"],
                "reason": "browser evidence was checked in the fixture",
            },
            "files": [
                {
                    "path": relative,
                    "role": roles[relative],
                    "sha256": sha256_file(bundle_root / relative),
                }
                for relative in sorted(referenced_paths)
            ],
        }
        manifest_path = bundle_root / "evidence-bundle.json"
        _write_json(manifest_path, manifest)
        return manifest_path

    def _test_result(
        self,
        name: str,
        *,
        successful: bool = True,
    ) -> tuple[Path, Path]:
        discovery = _current_test_discovery(self.root)
        selected = discovery["selected_tests"]
        filtered = discovery["filtered_tests"]
        result_path = self.run_dir / f"{name}-result.json"
        log_path = self.run_dir / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"Ran {len(selected)} tests\nOK\n",
            encoding="utf-8",
        )
        counts = {
            "discovered": len(selected) + len(filtered),
            "selected": len(selected),
            "filtered": len(filtered),
            "passed": len(selected),
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "expected_failures": 0,
            "unexpected_successes": 0,
        }
        executed = {
            "schema_version": "task-control-center-test-execution-v1",
            "selected_tests": selected,
            "results": [
                {"test": test_id, "outcome": "passed"}
                for test_id in selected
            ],
        }
        _write_json(
            result_path,
            {
                "schema_version": TEST_RESULT_SCHEMA_VERSION,
                "successful": successful,
                "python": f"/fixture/{name}/python",
                "code_fingerprint_before": code_fingerprint(self.root),
                "code_fingerprint_after": code_fingerprint(self.root),
                "counts": counts,
                "discovery": discovery,
                "discovery_sha256": sha256_bytes(
                    canonical_json(discovery).encode("utf-8")
                ),
                "executed": executed,
                "executed_sha256": sha256_bytes(
                    canonical_json(executed).encode("utf-8")
                ),
                "skipped_tests": [],
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
        source, selected_cases = self._write_mutation_source()
        self._write_review()
        cases_path = self.run_dir / "mutation-cases.json"
        cases_path.write_text(
            canonical_json(
                {
                    "version": "1.1.0",
                    "cases": selected_cases,
                }
            ),
            encoding="utf-8",
        )
        report_path = self.run_dir / "mutations.json"
        cases_payload = {"version": "1.1.0", "cases": selected_cases}
        report = _reexecute_mutation_report(
            self.root,
            source_sha256=sha256_file(source),
            cases_sha256=sha256_bytes(
                canonical_json(cases_payload).encode("utf-8")
            ),
            code_fingerprint_value=code_fingerprint(self.root),
            cases_payload=cases_payload,
        )
        _write_json(report_path, report)
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
            "schema_version": SCHEMA_VERSION,
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
        self._publication_patch = patch(
            "tests.task_control_center_evidence."
            "PERSISTENT_PUBLICATION_ANCESTRY_SUPPORTED",
            True,
        )
        self._publication_patch.start()
        self._mutation_trust_patch = patch(
            "tests.task_control_center_evidence."
            "MUTATION_EXECUTION_TRUST_SUPPORTED",
            True,
        )
        self._mutation_trust_patch.start()
        self._test_trust_patch = patch(
            "tests.task_control_center_evidence."
            "TEST_EXECUTION_TRUST_SUPPORTED",
            True,
        )
        self._test_trust_patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = EvidenceFixture(self.root)
        self.trusted_repository_root = TrustedEvidenceRoot(
            self.root,
            "test repository root",
        )

    def tearDown(self) -> None:
        self.trusted_repository_root.close()
        self.temp.cleanup()
        self._test_trust_patch.stop()
        self._mutation_trust_patch.stop()
        self._publication_patch.stop()

    def _build(self, *, trusted: bool = True) -> dict:
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ):
            return build_summary(
                self.root,
                self.fixture.metadata_path,
                replacement_reason="test replacement",
                trusted_previous_summary_sha256=(
                    sha256_file(self.fixture.previous_path)
                    if trusted
                    else None
                ),
                trusted_previous_candidate_snapshot_path=(
                    self.fixture.trusted_previous_candidate_snapshot_path
                    if trusted
                    else None
                ),
            )

    def _acceptance_prerequisites(
        self,
        candidate_identity: dict,
        review_summary: dict,
        *,
        tests_verified: bool = True,
        frozen_verified: bool = True,
        previous_summary_available: bool = True,
        previous_summary_trusted: bool = True,
        runtime_verification: dict | None = None,
        evidence_binding: dict | None = None,
        ttl_seconds: float = 300.0,
    ):
        binding = evidence_binding or {"test": True}
        self._last_acceptance_evidence_binding = binding
        return _issue_acceptance_prerequisite_proof(
            candidate_identity=candidate_identity,
            review_summary=review_summary,
            evidence_binding=binding,
            owner=_new_acceptance_prerequisite_owner(),
            tests_verified=tests_verified,
            frozen_verified=frozen_verified,
            previous_summary_available=previous_summary_available,
            previous_summary_trusted=previous_summary_trusted,
            runtime_verification=runtime_verification,
            ttl_seconds=ttl_seconds,
        )

    def _gate_review_summary(self) -> tuple[dict, dict]:
        review = self.fixture.complete_reviews()
        candidate = build_candidate_snapshot(self.root)
        review_summary = validate_review_manifest(
            self.root,
            review,
            expected_code_fingerprint=candidate["code_fingerprint"],
            changed_candidate_paths=[
                item["path"] for item in candidate["files"]
            ],
        )
        return candidate, review_summary

    def _evaluate_gate(
        self,
        candidate: dict,
        review_summary: dict,
    ) -> dict:
        proof = self._acceptance_prerequisites(
            candidate,
            review_summary,
            runtime_verification={
                "status": "VERIFIED",
                "scopes": ["browser"],
                "reason": "candidate-bound browser evidence",
                "valid_evidence_count": 1,
            }
        )
        return _evaluate_acceptance_gate_core(
            candidate_identity=candidate,
            trusted_repository_root=self.trusted_repository_root,
            review_summary=review_summary,
            review_binding=None,
            acceptance_policy="target_head_review_required",
            acceptance_evidence_binding=(
                self._last_acceptance_evidence_binding
            ),
            acceptance_prerequisites=proof,
        )

    def _evaluate_bundle_gate(
        self,
        bundle: dict,
        *,
        acceptance_policy: str | None = None,
        review_binding: dict | None = None,
        repository_root: TrustedEvidenceRoot | None = None,
    ) -> dict:
        binding = (
            review_binding
            if review_binding is not None
            else bundle["review_binding"]
        )
        binding_root_path = Path(bundle["bundle_root"])
        with TrustedEvidenceRoot(
            binding_root_path,
            "test bundle evidence root",
        ) as binding_root:
            with binding_root.pinned():
                proof = self._acceptance_prerequisites(
                    bundle["candidate_identity"],
                    bundle["review_summary"],
                    evidence_binding={
                        "bundle": bundle["bundle_id"]
                    },
                    runtime_verification=bundle[
                        "runtime_verification"
                    ],
                )
                return _evaluate_acceptance_gate_core(
                    candidate_identity=bundle["candidate_identity"],
                    trusted_repository_root=(
                        repository_root
                        if repository_root is not None
                        else self.trusted_repository_root
                    ),
                    trusted_review_evidence_root=binding_root,
                    trusted_review_manifest_path=(
                        binding_root_path
                        / bundle["review_manifest_reference"]["path"]
                    ),
                    trusted_binding_evidence_root=binding_root,
                    trusted_review_binding_path=(
                        binding_root_path
                        / bundle["review_binding_reference"]["path"]
                    ),
                    review_summary=bundle["review_summary"],
                    review_binding=binding,
                    acceptance_policy=(
                        acceptance_policy
                        if acceptance_policy is not None
                        else bundle["acceptance_policy"]
                    ),
                    acceptance_evidence_binding=(
                        self._last_acceptance_evidence_binding
                    ),
                    acceptance_prerequisites=proof,
                )

    def _evaluate_binding_gate(
        self,
        candidate: dict,
        review_summary: dict,
        binding: dict,
        binding_path: Path,
        *,
        acceptance_policy: str,
        runtime_verification: dict,
    ) -> dict:
        proof = self._acceptance_prerequisites(
            candidate,
            review_summary,
            runtime_verification=runtime_verification,
        )
        return _evaluate_acceptance_gate_core(
            candidate_identity=candidate,
            trusted_repository_root=self.trusted_repository_root,
            trusted_binding_evidence_root=self.trusted_repository_root,
            trusted_review_binding_path=binding_path,
            review_summary=review_summary,
            review_binding=binding,
            acceptance_policy=acceptance_policy,
            acceptance_evidence_binding=(
                self._last_acceptance_evidence_binding
            ),
            acceptance_prerequisites=proof,
        )

    def _configure_bundle_metadata(self, bundle_path: Path) -> None:
        state = git_state(self.root)
        self.fixture.metadata["code"] = {
            **state,
            "code_fingerprint": code_fingerprint(self.root),
        }
        self.fixture.metadata["evidence_bundle"] = {
            "path": str(bundle_path),
            "sha256": sha256_file(bundle_path),
        }
        _write_json(
            self.fixture.metadata_path,
            self.fixture.metadata,
        )

    def _rewrite_previous(self, mutator) -> None:
        previous = json.loads(
            self.fixture.previous_path.read_text(encoding="utf-8")
        )
        mutator(previous)
        _write_json(self.fixture.previous_path, previous)
        self.fixture.metadata["previous_summary"]["sha256"] = sha256_file(
            self.fixture.previous_path
        )
        _write_json(
            self.fixture.metadata_path,
            self.fixture.metadata,
        )

    def _rewrite_bundle_browser(
        self,
        bundle_path: Path,
        *,
        results=None,
        console_errors=None,
        claimed_status: str | None = None,
        mutator=None,
    ) -> None:
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        browser_relative = payload["browser_evidence"][0]["path"]
        browser_path = bundle_path.parent / browser_relative
        browser = json.loads(browser_path.read_text(encoding="utf-8"))
        if results is not None:
            browser["results"] = results
        if console_errors is not None:
            browser["browser_console_errors"] = console_errors
        if claimed_status is not None:
            payload["browser_evidence"][0]["status"] = claimed_status
        if mutator is not None:
            mutator(browser)
        _write_json(browser_path, browser)
        browser_sha = sha256_file(browser_path)
        payload["browser_evidence"][0]["sha256"] = browser_sha
        for item in payload["files"]:
            if item["path"] == browser_relative:
                item["sha256"] = browser_sha
        _write_json(bundle_path, payload)

    def _append_bundle_browser(
        self,
        bundle_path: Path,
        *,
        status: str = "VALID_FOR_CANDIDATE",
        declare: bool = True,
        mutate_browser=None,
    ) -> str:
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        source_relative = payload["browser_evidence"][0]["path"]
        source_path = bundle_path.parent / source_relative
        extra_relative = source_path.with_name(
            "browser-evidence-extra.json"
        ).relative_to(bundle_path.parent).as_posix()
        extra_path = bundle_path.parent / extra_relative
        browser = json.loads(source_path.read_text(encoding="utf-8"))
        if mutate_browser is not None:
            mutate_browser(browser)
        _write_json(extra_path, browser)
        extra_sha256 = sha256_file(extra_path)
        payload["files"].append(
            {
                "path": extra_relative,
                "role": "browser_evidence:1",
                "sha256": extra_sha256,
            }
        )
        if declare:
            payload["browser_evidence"].append(
                {
                    "path": extra_relative,
                    "sha256": extra_sha256,
                    "status": status,
                    "reason": "second declared browser evidence record",
                }
            )
        _write_json(bundle_path, payload)
        return extra_relative

    def test_valid_evidence_generates_deterministic_summary(self) -> None:
        first = self._build()
        second = self._build()
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(first["status"], "NOT_AVAILABLE")
        self.assertEqual(
            first["candidate_acceptance"],
            "NOT_AVAILABLE",
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

    def test_candidate_snapshot_uses_one_captured_generation(self) -> None:
        import tests.task_control_center_evidence as evidence_module

        captured_holder = {}
        original_capture = evidence_module._capture_candidate_file_bytes

        def capture_and_keep(*args, **kwargs):
            captured = original_capture(*args, **kwargs)
            captured_holder["bytes"] = captured
            return captured

        with patch(
            "tests.task_control_center_evidence."
            "_capture_candidate_file_bytes",
            side_effect=capture_and_keep,
        ):
            snapshot = build_candidate_snapshot(self.root)
        captured = captured_holder["bytes"]
        self.assertEqual(
            snapshot["code_fingerprint"],
            evidence_module._code_fingerprint_from_captured(captured),
        )
        self.assertEqual(
            snapshot["file_manifest_sha256"],
            file_manifest_sha256(
                evidence_module._file_entries_from_captured(
                    [item["path"] for item in snapshot["files"]],
                    captured,
                )
            ),
        )
        for item in snapshot["files"]:
            self.assertEqual(
                item["sha256"],
                sha256_bytes(captured[item["path"]]),
            )

    def test_candidate_snapshot_rejects_file_change_during_capture(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        original_read = evidence_module._read_relative_regular_file
        changed = False

        def read_then_change(
            root_fd,
            relative,
            path,
            label,
            *,
            max_bytes,
        ):
            nonlocal changed
            data = original_read(
                root_fd,
                relative,
                path,
                label,
                max_bytes=max_bytes,
            )
            if (
                not changed
                and label == "candidate capture"
                and relative == "src/zevorinth/example.py"
            ):
                path.write_text(
                    data.decode("utf-8") + "# changed during capture\n",
                    encoding="utf-8",
                )
                changed = True
            return data

        with patch(
            "tests.task_control_center_evidence."
            "_read_relative_regular_file",
            side_effect=read_then_change,
        ), self.assertRaisesRegex(
            EvidenceError,
            "changed during capture",
        ):
            build_candidate_snapshot(self.root)

    def test_candidate_snapshot_rejects_path_set_change_during_capture(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        original_capture = evidence_module._capture_candidate_file_bytes
        added_path = self.root / "tests" / "a46_added_during_capture.py"
        deleted_path = self.root / "src" / "zevorinth" / "example.py"
        deleted_bytes = deleted_path.read_bytes()

        def capture_then_add(*args, **kwargs):
            captured = original_capture(*args, **kwargs)
            added_path.write_text("VALUE = 99\n", encoding="utf-8")
            return captured

        with patch(
            "tests.task_control_center_evidence."
            "_capture_candidate_file_bytes",
            side_effect=capture_then_add,
        ), self.assertRaisesRegex(
            EvidenceError,
            "repository state changed|candidate change set changed|"
            "generation changed|generation verification failed",
        ):
            build_candidate_snapshot(self.root)
        added_path.unlink()

        def capture_then_delete(*args, **kwargs):
            captured = original_capture(*args, **kwargs)
            deleted_path.unlink()
            return captured

        try:
            with patch(
                "tests.task_control_center_evidence."
                "_capture_candidate_file_bytes",
                side_effect=capture_then_delete,
            ), self.assertRaisesRegex(
                EvidenceError,
                "repository state changed|candidate change set changed|"
                "generation changed|generation verification failed",
            ):
                build_candidate_snapshot(self.root)
        finally:
            deleted_path.write_bytes(deleted_bytes)

    def test_candidate_snapshot_rejects_cross_file_generation_change(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        first_path = self.root / "src" / "zevorinth" / "example.py"
        original_read = evidence_module._read_relative_regular_file
        changed = False

        def change_after_local_checks(
            root_fd,
            relative,
            path,
            label,
            *,
            max_bytes,
        ):
            nonlocal changed
            data = original_read(
                root_fd,
                relative,
                path,
                label,
                max_bytes=max_bytes,
            )
            if (
                not changed
                and label == "candidate capture"
                and relative == "tests/mutation_cases.json"
            ):
                first_path.write_text(
                    "VALUE = 999\n",
                    encoding="utf-8",
                )
                changed = True
            return data

        with patch(
            "tests.task_control_center_evidence."
            "_read_relative_regular_file",
            side_effect=change_after_local_checks,
        ), self.assertRaisesRegex(
            EvidenceError,
            "generation changed|bytes changed",
        ):
            build_candidate_snapshot(self.root)
        self.assertTrue(changed)

    def test_candidate_snapshot_rejects_restored_metadata_generation(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        first_path = self.root / "src" / "zevorinth" / "example.py"
        original_bytes = first_path.read_bytes()
        original_stat = first_path.stat()
        original_read = evidence_module._read_relative_regular_file
        changed = False

        def change_and_restore(
            root_fd,
            relative,
            path,
            label,
            *,
            max_bytes,
        ):
            nonlocal changed
            data = original_read(
                root_fd,
                relative,
                path,
                label,
                max_bytes=max_bytes,
            )
            if (
                not changed
                and label == "candidate capture"
                and relative == "tests/mutation_cases.json"
            ):
                first_path.write_bytes(original_bytes)
                first_path.write_text(
                    "VALUE = 999\n",
                    encoding="utf-8",
                )
                first_path.write_bytes(original_bytes)
                os.utime(
                    first_path,
                    ns=(
                        original_stat.st_atime_ns,
                        original_stat.st_mtime_ns,
                    ),
                )
                changed = True
            return data

        with patch(
            "tests.task_control_center_evidence."
            "_read_relative_regular_file",
            side_effect=change_and_restore,
        ), self.assertRaisesRegex(
            EvidenceError,
            "generation changed|bytes changed",
        ):
            build_candidate_snapshot(self.root)
        self.assertTrue(changed)
        self.assertEqual(first_path.read_bytes(), original_bytes)
        self.assertNotEqual(
            first_path.stat().st_ctime_ns,
            original_stat.st_ctime_ns,
        )

    def test_candidate_snapshot_rejects_final_directory_symlink(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        nested = self.root / "src" / "zevorinth" / "nested"
        nested.mkdir()
        (nested / "value.py").write_text(
            "NESTED = True\n",
            encoding="utf-8",
        )
        original_read = evidence_module._read_relative_regular_file
        changed = False

        with tempfile.TemporaryDirectory() as outside_temp:
            outside = Path(outside_temp) / "nested"

            def replace_after_read(
                root_fd,
                relative,
                path,
                label,
                *,
                max_bytes,
            ):
                nonlocal changed
                data = original_read(
                    root_fd,
                    relative,
                    path,
                    label,
                    max_bytes=max_bytes,
                )
                if (
                    not changed
                    and label == "candidate generation verification"
                    and relative == "src/zevorinth/nested/value.py"
                ):
                    nested.rename(outside)
                    nested.symlink_to(
                        outside,
                        target_is_directory=True,
                    )
                    changed = True
                return data

            with patch(
                "tests.task_control_center_evidence."
                "_read_relative_regular_file",
                side_effect=replace_after_read,
            ), self.assertRaisesRegex(
                EvidenceError,
                "safely|symbolic link|generation",
            ):
                build_candidate_snapshot(self.root)
        self.assertTrue(changed)

    def test_relative_stat_rejects_intermediate_symlink(self) -> None:
        import tests.task_control_center_evidence as evidence_module

        nested = self.root / "src" / "zevorinth" / "stat-nested"
        nested.mkdir()
        target = nested / "value.py"
        target.write_text("VALUE = 1\n", encoding="utf-8")
        root_fd = _open_absolute_directory(
            self.root,
            "relative stat root",
        )
        try:
            with tempfile.TemporaryDirectory() as outside_temp:
                outside = Path(outside_temp) / "stat-nested"
                nested.rename(outside)
                nested.symlink_to(
                    outside,
                    target_is_directory=True,
                )
                with self.assertRaisesRegex(
                    EvidenceError,
                    "could not be inspected safely",
                ):
                    evidence_module._stat_relative_regular_file(
                        root_fd,
                        "src/zevorinth/stat-nested/value.py",
                        target,
                        "relative stat probe",
                    )
        finally:
            os.close(root_fd)

    def test_open_absolute_directory_closes_first_fd_on_interrupt(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        opened_fds: list[int] = []
        real_open = evidence_module._OS_OPEN
        real_flags = evidence_module._open_flags()

        def open_then_interrupt(*args, **kwargs):
            if len(opened_fds) == 1:
                raise KeyboardInterrupt(
                    "injected interrupt after first open"
                )
            opened = real_open(*args, **kwargs)
            opened_fds.append(opened)
            return opened

        with patch.object(
            evidence_module,
            "_open_flags",
            return_value=real_flags,
        ), patch.object(
            evidence_module,
            "_OS_OPEN",
            side_effect=open_then_interrupt,
        ), self.assertRaisesRegex(
            KeyboardInterrupt,
            "after first open",
        ):
            _open_absolute_directory(self.root, "interrupt root")
        self.assertEqual(len(opened_fds), 1)
        with self.assertRaises(OSError):
            os.fstat(opened_fds[0])

    def test_open_absolute_directory_retains_fds_on_fstat_interrupt(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        opened_fds: list[int] = []
        real_open = evidence_module._OS_OPEN
        real_fstat = evidence_module.os.fstat
        real_flags = evidence_module._open_flags()
        fstat_calls = 0

        def capture_open(*args, **kwargs):
            opened = real_open(*args, **kwargs)
            opened_fds.append(opened)
            return opened

        def fstat_then_interrupt(file_fd):
            nonlocal fstat_calls
            fstat_calls += 1
            if fstat_calls < 2:
                return real_fstat(file_fd)
            real_fstat(file_fd)
            raise KeyboardInterrupt(
                "injected interrupt after fstat"
            )

        try:
            with patch.object(
                evidence_module,
                "_open_flags",
                return_value=real_flags,
            ), patch.object(
                evidence_module,
                "_OS_OPEN",
                side_effect=capture_open,
            ), patch.object(
                evidence_module.os,
                "fstat",
                side_effect=fstat_then_interrupt,
            ), self.assertRaisesRegex(
                KeyboardInterrupt,
                "after fstat",
            ):
                _open_absolute_directory(
                    self.root,
                    "fstat interrupt root",
                )
            self.assertGreaterEqual(len(opened_fds), 2)
            self.assertTrue(_fd_is_open(opened_fds[0]))
        finally:
            for opened_fd in opened_fds:
                try:
                    os.close(opened_fd)
                except OSError:
                    pass

    def test_open_absolute_directory_keeps_interrupt_on_observation_failure(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        real_open = evidence_module._OS_OPEN
        real_fstat = evidence_module.os.fstat
        opened_fds: list[int] = []
        fstat_calls = 0

        def capture_open(*args, **kwargs):
            opened_fd = real_open(*args, **kwargs)
            opened_fds.append(opened_fd)
            return opened_fd

        def interrupt_after_registration(file_fd):
            nonlocal fstat_calls
            fstat_calls += 1
            if fstat_calls < 2:
                return real_fstat(file_fd)
            raise KeyboardInterrupt(
                "injected original interrupt"
            )

        try:
            with patch.object(
                evidence_module,
                "_OS_OPEN",
                side_effect=capture_open,
            ), patch.object(
                evidence_module.os,
                "fstat",
                side_effect=interrupt_after_registration,
            ), self.assertRaisesRegex(
                KeyboardInterrupt,
                "injected original interrupt",
            ):
                _open_absolute_directory(
                    self.root,
                    "close interrupt root",
                )
            self.assertGreaterEqual(len(opened_fds), 2)
            self.assertTrue(_fd_is_open(opened_fds[0]))
        finally:
            for opened_fd in opened_fds:
                try:
                    os.close(opened_fd)
                except OSError:
                    pass

    def test_open_absolute_directory_defers_sigint_until_fd_registered(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        real_flags = evidence_module._open_flags()
        real_open = evidence_module._OS_OPEN
        opened_fds: list[int] = []
        signal_sent = False

        def open_then_signal(*args, **kwargs):
            nonlocal signal_sent
            opened_fd = real_open(*args, **kwargs)
            opened_fds.append(opened_fd)
            if not signal_sent:
                signal_sent = True
                delivered = threading.Event()
                main_thread = threading.main_thread()

                def send_interrupt() -> None:
                    signal.pthread_kill(main_thread.ident, signal.SIGINT)
                    delivered.set()

                sender = threading.Thread(target=send_interrupt)
                sender.start()
                delivered.wait(timeout=10)
                sender.join(timeout=10)
                self.assertFalse(sender.is_alive())
            return opened_fd

        with patch.object(
            evidence_module,
            "_open_flags",
            return_value=real_flags,
        ), patch.object(
            evidence_module,
            "_OS_OPEN",
            side_effect=open_then_signal,
        ), self.assertRaises(KeyboardInterrupt):
            _open_absolute_directory(
                self.root,
                "deferred SIGINT root",
            )
        self.assertTrue(signal_sent)
        self.assertEqual(len(opened_fds), 1)
        with self.assertRaises(OSError):
            os.fstat(opened_fds[0])

    def test_mutation_open_helper_defers_sigint_until_fd_registered(
        self,
    ) -> None:
        real_flags = mutation_runner._copy_directory_flags()
        real_open = mutation_runner._OS_OPEN
        opened_fds: list[int] = []
        signal_sent = False

        def open_then_signal(*args, **kwargs):
            nonlocal signal_sent
            opened_fd = real_open(*args, **kwargs)
            opened_fds.append(opened_fd)
            if not signal_sent:
                signal_sent = True
                delivered = threading.Event()
                main_thread = threading.main_thread()

                def send_interrupt() -> None:
                    signal.pthread_kill(main_thread.ident, signal.SIGINT)
                    delivered.set()

                sender = threading.Thread(target=send_interrupt)
                sender.start()
                delivered.wait(timeout=10)
                sender.join(timeout=10)
                self.assertFalse(sender.is_alive())
            return opened_fd

        with patch.object(
            mutation_runner,
            "_copy_directory_flags",
            return_value=real_flags,
        ), patch.object(
            mutation_runner,
            "_OS_OPEN",
            side_effect=open_then_signal,
        ), self.assertRaises(KeyboardInterrupt):
            mutation_runner._open_absolute_directory_without_links(
                self.root,
                "deferred SIGINT root",
            )
        self.assertTrue(signal_sent)
        self.assertEqual(len(opened_fds), 1)
        with self.assertRaises(OSError):
            os.fstat(opened_fds[0])

    def test_open_absolute_directory_transfers_fd_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory_fd = _open_absolute_directory(
                Path(temp),
                "transferred root",
            )
            try:
                self.assertTrue(
                    stat.S_ISDIR(os.fstat(directory_fd).st_mode)
                )
            finally:
                os.close(directory_fd)

    def test_trusted_root_close_attempts_all_fds_and_is_idempotent(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            root = TrustedEvidenceRoot(
                Path(temp),
                "close ownership root",
            )
            primary_fd = root._fd
            anchor_fd = root._anchor_fd
            self.assertIsNotNone(primary_fd)
            self.assertIsNotNone(anchor_fd)
            primary_private_anchor = root._fd_records[
                primary_fd
            ].anchor_fd
            real_close = evidence_module.os.close
            failed = False

            def fail_primary_close(file_fd):
                nonlocal failed
                real_close(file_fd)
                if file_fd == primary_fd and not failed:
                    failed = True
                    raise OSError("injected root close failure")

            with patch.object(
                evidence_module.os,
                "close",
                side_effect=fail_primary_close,
            ), self.assertRaisesRegex(
                EvidenceError,
                "injected root close failure",
            ) as caught:
                root.close()
            self.assertTrue(failed)
            self.assertEqual(
                caught.exception.fd_close_states,
                {
                    primary_fd: "CLOSED",
                    primary_private_anchor: "UNKNOWN",
                    anchor_fd: "CLOSED",
                },
            )
            self.assertEqual(
                caught.exception.unresolved_fds,
                {primary_private_anchor: "UNKNOWN"},
            )
            self.assertIn(primary_fd, root._fd_records)
            self.assertEqual(
                root._unresolved_fds,
                {primary_private_anchor: "UNKNOWN"},
            )
            with self.assertRaises(OSError):
                os.fstat(primary_fd)
            with self.assertRaises(OSError):
                os.fstat(anchor_fd)
            os.close(primary_private_anchor)
            root.close()

    def test_trusted_root_close_failure_preserves_primary_error(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            root = TrustedEvidenceRoot(
                Path(temp),
                "primary close ownership root",
            )
            primary_fd = root._fd
            real_close = evidence_module.os.close
            failed = False

            def fail_primary_close(file_fd):
                nonlocal failed
                real_close(file_fd)
                if file_fd == primary_fd and not failed:
                    failed = True
                    raise OSError("injected close failure")

            with patch.object(
                evidence_module.os,
                "close",
                side_effect=fail_primary_close,
            ), self.assertRaisesRegex(
                RuntimeError,
                "primary validation",
            ) as caught:
                try:
                    raise RuntimeError("primary validation")
                finally:
                    root.close()
            self.assertTrue(failed)
            self.assertIn(
                "injected close failure",
                "\n".join(caught.exception.__notes__),
            )

    def test_relative_directory_close_failure_does_not_leak_next_fd(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "nested").mkdir()
            root = TrustedEvidenceRoot(
                base,
                "relative directory ownership root",
            )
            real_open = evidence_module.os.open
            real_close = evidence_module.os.close
            flags = evidence_module._open_flags()
            opened_fds: list[int] = []
            failed = False

            def capture_open(*args, **kwargs):
                opened = real_open(*args, **kwargs)
                opened_fds.append(opened)
                return opened

            def fail_first_traversal_close(file_fd):
                nonlocal failed
                real_close(file_fd)
                if not failed:
                    failed = True
                    raise OSError(
                        "injected traversal close failure"
                    )

            try:
                with patch.object(
                    evidence_module,
                    "_open_flags",
                    return_value=flags,
                ), patch.object(
                    evidence_module,
                    "_OS_OPEN",
                    side_effect=capture_open,
                ), patch.object(
                    evidence_module.os,
                    "close",
                    side_effect=fail_first_traversal_close,
                ), self.assertRaisesRegex(
                    EvidenceError,
                    "close failed|could not be opened safely",
                ):
                    with root._relative_directory_scope(
                        "nested/value.py",
                        "relative directory probe",
                    ):
                        self.fail("directory scope should not be entered")
                self.assertTrue(failed)
                self.assertEqual(len(opened_fds), 2)
                for opened_fd in opened_fds:
                    with self.assertRaises(OSError):
                        os.fstat(opened_fd)
            finally:
                root.close()

    def test_trusted_root_anchor_dup_owned_before_mask_restore(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            real_open = evidence_module._OS_OPEN
            real_dup = evidence_module._OS_DUP
            real_mask = evidence_module.signal.pthread_sigmask
            opened_fds: list[int] = []
            duplicated_fd = None
            trigger = False

            def capture_open(*args, **kwargs):
                opened = real_open(*args, **kwargs)
                opened_fds.append(opened)
                return opened

            def capture_dup(file_fd):
                nonlocal trigger, duplicated_fd
                opened = real_dup(file_fd)
                opened_fds.append(opened)
                duplicated_fd = opened
                trigger = True
                return opened

            def interrupt_mask_restore(signals, mask):
                nonlocal trigger
                previous = real_mask(signals, mask)
                if signals == signal.SIG_SETMASK and trigger:
                    trigger = False
                    raise KeyboardInterrupt(
                        "injected mask-restore interrupt"
                    )
                return previous

            with patch.object(
                evidence_module,
                "_OS_OPEN",
                side_effect=capture_open,
            ), patch.object(
                evidence_module,
                "_OS_DUP",
                side_effect=capture_dup,
            ), patch.object(
                evidence_module.signal,
                "pthread_sigmask",
                side_effect=interrupt_mask_restore,
            ), self.assertRaisesRegex(
                KeyboardInterrupt,
                "mask-restore",
            ):
                TrustedEvidenceRoot(
                    Path(temp),
                    "anchor ownership probe",
                )
            self.assertGreaterEqual(len(opened_fds), 2)
            self.assertIsNotNone(duplicated_fd)
            for opened_fd in opened_fds:
                with self.assertRaises(OSError):
                    os.fstat(opened_fd)

    def test_ensure_directory_child_owned_before_mask_restore(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            real_open = evidence_module._OS_OPEN
            real_mask = evidence_module.signal.pthread_sigmask
            opened_fds: list[int] = []
            trigger = False

            def capture_open(path, *args, **kwargs):
                nonlocal trigger
                opened = real_open(path, *args, **kwargs)
                opened_fds.append(opened)
                if path == "nested":
                    trigger = True
                return opened

            def interrupt_mask_restore(signals, mask):
                nonlocal trigger
                previous = real_mask(signals, mask)
                if signals == signal.SIG_SETMASK and trigger:
                    trigger = False
                    raise KeyboardInterrupt(
                        "injected child-open interrupt"
                    )
                return previous

            root = TrustedEvidenceRoot(
                base,
                "child ownership root",
            )
            try:
                with patch.object(
                    evidence_module,
                    "_OS_OPEN",
                    side_effect=capture_open,
                ), patch.object(
                    evidence_module.signal,
                    "pthread_sigmask",
                    side_effect=interrupt_mask_restore,
                ), self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "child-open",
                ):
                    root.ensure_directory(
                        base / "nested",
                        "child ownership directory",
                    )
                self.assertEqual(len(opened_fds), 2)
                for opened_fd in opened_fds:
                    with self.assertRaises(OSError):
                        os.fstat(opened_fd)
            finally:
                root.close()

    def test_ensure_directory_result_owned_through_shield_exit(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = TrustedEvidenceRoot(
                base,
                "result transfer root",
            )
            real_defer = evidence_module._defer_keyboard_interrupt
            real_from_open = TrustedEvidenceRoot._from_open_fd
            child_fd = None
            trigger = False

            @contextmanager
            def interrupt_after_shield():
                nonlocal trigger
                with real_defer():
                    yield
                    if trigger:
                        trigger = False
                        raise KeyboardInterrupt(
                            "injected result transfer interrupt"
                        )

            def transfer_then_mark(
                path,
                directory_fd,
                label,
                **kwargs,
            ):
                nonlocal child_fd, trigger
                child_fd = directory_fd
                transferred = real_from_open(
                    path,
                    directory_fd,
                    label,
                    **kwargs,
                )
                trigger = True
                return transferred

            try:
                with patch.object(
                    evidence_module,
                    "_defer_keyboard_interrupt",
                    side_effect=interrupt_after_shield,
                ), patch.object(
                    TrustedEvidenceRoot,
                    "_from_open_fd",
                    side_effect=transfer_then_mark,
                ), self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "result transfer",
                ):
                    root.ensure_directory(
                        base / "nested",
                        "result transfer directory",
                    )
                self.assertIsNotNone(child_fd)
                with self.assertRaises(OSError):
                    os.fstat(child_fd)
            finally:
                root.close()

    def test_candidate_snapshot_rejects_late_path_after_final_pass(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        late_path = self.root / "tests" / "late-after-final-pass.py"
        original_verify = (
            evidence_module._verify_captured_candidate_generation
        )
        changed = False
        verify_calls = 0

        def verify_then_add(*args, **kwargs):
            nonlocal changed, verify_calls
            verify_calls += 1
            original_verify(*args, **kwargs)
            if verify_calls == 2 and not changed:
                late_path.write_text(
                    "LATE = True\n",
                    encoding="utf-8",
                )
                changed = True

        with patch(
            "tests.task_control_center_evidence."
            "_verify_captured_candidate_generation",
            side_effect=verify_then_add,
        ), self.assertRaisesRegex(
            EvidenceError,
            "generation changed",
        ):
            build_candidate_snapshot(self.root)
        self.assertTrue(changed)

    def test_candidate_snapshot_rejects_late_branch_change(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        original_verify = (
            evidence_module._verify_captured_candidate_generation
        )
        changed = False
        verify_calls = 0

        def verify_then_switch_branch(*args, **kwargs):
            nonlocal changed, verify_calls
            verify_calls += 1
            original_verify(*args, **kwargs)
            if verify_calls == 2 and not changed:
                subprocess.run(
                    ["git", "checkout", "-qb", "a50-late-branch"],
                    cwd=self.root,
                    check=True,
                )
                changed = True

        with patch(
            "tests.task_control_center_evidence."
            "_verify_captured_candidate_generation",
            side_effect=verify_then_switch_branch,
        ), self.assertRaisesRegex(
            EvidenceError,
            "generation changed",
        ):
            build_candidate_snapshot(self.root)
        self.assertTrue(changed)

    def test_candidate_snapshot_checkpoint_aba_is_bounded_not_atomic(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        transient = self.root / "tests" / "checkpoint-aba.py"
        original_verify = (
            evidence_module._verify_captured_candidate_generation
        )
        changed = False

        def verify_with_transient_path(*args, **kwargs):
            nonlocal changed
            original_verify(*args, **kwargs)
            if not changed:
                transient.write_text(
                    "TRANSIENT = True\n",
                    encoding="utf-8",
                )
                transient.unlink()
                changed = True

        with patch(
            "tests.task_control_center_evidence."
            "_verify_captured_candidate_generation",
            side_effect=verify_with_transient_path,
        ):
            snapshot = build_candidate_snapshot(self.root)
        self.assertTrue(changed)
        self.assertFalse(transient.exists())
        self.assertEqual(
            snapshot["evidence_status"],
            CANDIDATE_IDENTITY_VERIFIED,
        )
        self.assertEqual(
            snapshot["status_sha256"],
            git_state(self.root)["status_sha256"],
        )

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

    def test_json_parser_rejects_every_non_finite_float(self) -> None:
        finite = _parse_json_object_bytes(
            b'{"decimal": 1.25, "scientific": 1e3}',
            Path("finite.json"),
        )
        self.assertEqual(finite, {"decimal": 1.25, "scientific": 1000.0})
        for payload in (
            b'{"value": 1e999}',
            b'{"value": -1e999}',
            b'{"value": NaN}',
            b'{"value": Infinity}',
            b'{"value": -Infinity}',
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(
                EvidenceError,
                "non-finite JSON number",
            ):
                _parse_json_object_bytes(payload, Path("nonfinite.json"))

    def test_non_finite_previous_summary_cannot_write_success(self) -> None:
        previous_bytes = b'{"value": 1e999}\n'
        self.fixture.previous_path.write_bytes(previous_bytes)
        self.fixture.metadata["previous_summary"]["sha256"] = sha256_file(
            self.fixture.previous_path
        )
        _write_json(
            self.fixture.metadata_path,
            self.fixture.metadata,
        )
        output = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "stable-summary.json"
        )
        output.write_bytes(b"historical-summary\n")
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ), self.assertRaisesRegex(EvidenceError, "non-finite JSON number"):
            write_summary(
                self.root,
                self.fixture.metadata_path,
                output,
                replacement_reason="non-finite previous summary",
            )
        self.assertEqual(output.read_bytes(), b"historical-summary\n")
        status = json.loads(
            (output.parent / "acceptance-status.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(status["state"], BLOCKED)
        self.assertFalse(status["authoritative"])

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

    def test_previous_summary_requires_full_semantic_contract(self) -> None:
        def minimal(previous):
            previous.clear()
            previous["constraint_incidents"] = ["fabricated history"]

        def forged_acceptance(previous):
            previous["review_status"]["independent_code_review"] = (
                "REQUEST_CHANGES"
            )
            previous["review_status"]["blocking_reasons"] = [
                "forged blocking review hidden"
            ]

        def forged_candidate(previous):
            previous["candidate_identity"]["target_head"] = "0" * 40

        def wrong_schema(previous):
            previous["schema_version"] = "task-control-center-evidence-v0"

        def validation_mismatch(previous):
            previous["superseded_summary"]["replacement_reason"] = (
                "different reason"
            )

        def missing_history(previous):
            previous["evidence_history"] = []

        def inconsistent_history(previous):
            previous["superseded_summary"]["previous_sha256"] = "9" * 64

        def unknown_field(previous):
            previous["acceptance_override"] = True

        cases = {
            "minimal": minimal,
            "forged_acceptance": forged_acceptance,
            "forged_candidate": forged_candidate,
            "wrong_schema": wrong_schema,
            "validation_mismatch": validation_mismatch,
            "missing_history": missing_history,
            "inconsistent_history": inconsistent_history,
            "unknown_field": unknown_field,
        }
        for case, mutator in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                previous = json.loads(
                    fixture.previous_path.read_text(encoding="utf-8")
                )
                mutator(previous)
                _write_json(fixture.previous_path, previous)
                fixture.metadata["previous_summary"]["sha256"] = sha256_file(
                    fixture.previous_path
                )
                _write_json(fixture.metadata_path, fixture.metadata)
                with patch(
                    "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                    fixture.frozen,
                ), self.assertRaises(EvidenceError):
                    build_summary(
                        root,
                        fixture.metadata_path,
                        replacement_reason="forged previous summary",
                    )

    def test_previous_summary_rejects_contradictory_review_state(
        self,
    ) -> None:
        def direct_without_current_review(previous):
            previous["acceptance_gate"][
                "current_target_independent_review"
            ] = "NOT_AVAILABLE"
            previous["acceptance_gate"][
                "current_target_review_resolution"
            ] = "DIRECT_CLEAR"

        def content_without_binding(previous):
            previous["acceptance_gate"]["acceptance_policy"] = (
                "content_equivalence_allowed"
            )
            previous["acceptance_gate"]["review_gate"] = (
                "CONTENT_EQUIVALENT_CLEAR"
            )
            previous["acceptance_gate"][
                "current_target_independent_review"
            ] = "NOT_AVAILABLE"
            previous["acceptance_gate"][
                "current_target_review_resolution"
            ] = "CONTENT_EQUIVALENT_CLEAR"
            previous["acceptance_gate"]["content_equivalence_binding"] = (
                "NOT_AVAILABLE"
            )

        def unverified_marked_accepted(previous):
            previous["acceptance_gate"]["unverified_scopes"] = [
                "candidate acceptance logic"
            ]

        def unsupported_publication_marked_accepted(previous):
            previous["acceptance_gate"][
                "persistent_publication_ancestry"
            ] = "UNSUPPORTED"

        def unsupported_mutation_execution(previous):
            previous["acceptance_gate"][
                "mutation_execution_trust"
            ] = "UNSUPPORTED"

        def unsupported_test_execution(previous):
            previous["acceptance_gate"][
                "test_execution_trust"
            ] = "UNSUPPORTED"

        def content_without_top_level_binding(previous):
            previous["acceptance_gate"]["acceptance_policy"] = (
                "content_equivalence_allowed"
            )
            previous["acceptance_gate"]["review_gate"] = (
                "CONTENT_EQUIVALENT_CLEAR"
            )
            previous["acceptance_gate"][
                "content_equivalence_binding"
            ] = "CONTENT_EQUIVALENT_CLEAR"
            previous["acceptance_gate"][
                "current_target_independent_review"
            ] = "NOT_AVAILABLE"
            previous["acceptance_gate"][
                "current_target_review_resolution"
            ] = "CONTENT_EQUIVALENT_CLEAR"
            previous["review_status"]["review_gate"] = (
                "CONTENT_EQUIVALENT_CLEAR"
            )
            previous["review_binding"] = None

        def special_missing_reference(previous):
            previous["special_review"].pop("path")
            previous["special_review"].pop("sha256")

        def special_merge_false(previous):
            previous["special_review"]["merge_ready"] = False

        def special_blocking(previous):
            previous["special_review"]["blocking_reasons"] = [
                "critical blocker"
            ]

        def special_request_changes(previous):
            previous["special_review"]["independent_code_review"] = (
                "REQUEST_CHANGES"
            )
            previous["special_review"]["code_review"]["status"] = (
                "REQUEST_CHANGES"
            )

        def special_architecture_block(previous):
            previous["special_review"][
                "independent_architecture_review"
            ] = "BLOCK"
            previous["special_review"]["architecture_review"]["status"] = (
                "BLOCK"
            )

        def unresolved_marked_accepted(previous):
            previous["acceptance_gate"][
                "current_target_review_resolution"
            ] = "UNVERIFIED"

        cases = {
            "direct_without_current_review": direct_without_current_review,
            "content_without_binding": content_without_binding,
            "unverified_marked_accepted": unverified_marked_accepted,
            "unsupported_publication": unsupported_publication_marked_accepted,
            "unsupported_mutation_execution": unsupported_mutation_execution,
            "unsupported_test_execution": unsupported_test_execution,
            "content_without_top_level_binding":
                content_without_top_level_binding,
            "special_missing_reference": special_missing_reference,
            "special_merge_false": special_merge_false,
            "special_blocking": special_blocking,
            "special_request_changes": special_request_changes,
            "special_architecture_block": special_architecture_block,
            "unresolved_marked_accepted": unresolved_marked_accepted,
        }
        for name, mutate in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                previous = json.loads(
                    fixture.previous_path.read_text(encoding="utf-8")
                )
                mutate(previous)
                _write_json(fixture.previous_path, previous)
                fixture.metadata["previous_summary"]["sha256"] = sha256_file(
                    fixture.previous_path
                )
                _write_json(fixture.metadata_path, fixture.metadata)
                with patch(
                    "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                    fixture.frozen,
                ), self.assertRaises(EvidenceError):
                    build_summary(
                        root,
                        fixture.metadata_path,
                        replacement_reason="contradictory history",
                        trusted_previous_summary_sha256=sha256_file(
                            fixture.previous_path
                        ),
                        trusted_previous_candidate_snapshot_path=(
                            fixture.trusted_previous_candidate_snapshot_path
                        ),
                    )

    def _assert_previous_review_rejected(
        self,
        mutator,
        reason: str,
    ) -> None:
        self._rewrite_previous(mutator)
        with self.assertRaisesRegex(EvidenceError, reason):
            self._build()

    def test_v2_review_gate_mismatch_rejects_accepted_history(self) -> None:
        self._assert_previous_review_rejected(
            lambda previous: previous["review_status"].__setitem__(
                "review_gate",
                "CONTENT_EQUIVALENT_CLEAR",
            ),
            "review status gate disagrees with acceptance gate",
        )

    def test_v2_missing_review_gate_rejects_accepted_history(self) -> None:
        self._assert_previous_review_rejected(
            lambda previous: previous["review_status"].pop("review_gate"),
            "review_status.review_gate",
        )

    def test_special_candidate_acceptance_must_match_resolution(self) -> None:
        self._assert_previous_review_rejected(
            lambda previous: previous["special_review"].__setitem__(
                "candidate_acceptance",
                "CONTENT_EQUIVALENT_REVIEW_CLEAR",
            ),
            "special review acceptance disagrees with resolution",
        )

    def test_nested_uncovered_scope_cannot_be_hidden(self) -> None:
        def hide_scope(previous):
            previous["special_review"]["code_review"][
                "uncovered_scope"
            ] = ["hidden_scope"]

        self._assert_previous_review_rejected(
            hide_scope,
            "uncovered scope is not propagated",
        )

    def test_nested_lane_target_head_must_match_candidate(self) -> None:
        def replace_head(previous):
            previous["special_review"]["code_review"]["target_head"] = (
                "0" * 40
            )

        self._assert_previous_review_rejected(
            replace_head,
            "target HEAD is not the reviewed candidate",
        )

    def test_nested_lane_target_fingerprint_must_match_candidate(self) -> None:
        def replace_fingerprint(previous):
            previous["special_review"]["architecture_review"][
                "target_code_fingerprint"
            ] = "0" * 64

        self._assert_previous_review_rejected(
            replace_fingerprint,
            "target fingerprint disagrees with candidate",
        )

    def test_incomplete_nested_lane_cannot_be_accepted(self) -> None:
        self._assert_previous_review_rejected(
            lambda previous: previous["special_review"][
                "code_review"
            ].__setitem__("completed", False),
            "completed state contradicts its status",
        )

    def test_nested_lane_conclusion_must_match_status(self) -> None:
        self._assert_previous_review_rejected(
            lambda previous: previous["special_review"][
                "code_review"
            ].__setitem__("conclusion", "REQUEST_CHANGES"),
            "conclusion contradicts its status",
        )

    def test_final_gate_rejects_malformed_nested_review_lanes(self) -> None:
        candidate, valid_summary = self._gate_review_summary()

        def clone() -> dict:
            return json.loads(canonical_json(valid_summary))

        cases = {}

        def wrong_fingerprint(summary):
            summary["code_review"]["target_code_fingerprint"] = "0" * 64

        cases["fingerprint"] = (
            wrong_fingerprint,
            "target fingerprint disagrees with candidate",
        )

        def incomplete(summary):
            summary["code_review"]["completed"] = False

        cases["completed"] = (
            incomplete,
            "completed state contradicts its status",
        )

        def wrong_conclusion(summary):
            summary["code_review"]["conclusion"] = "REQUEST_CHANGES"

        cases["conclusion"] = (
            wrong_conclusion,
            "conclusion contradicts its status",
        )

        def wrong_status(summary):
            summary["code_review"]["status"] = "REQUEST_CHANGES"

        cases["status_rollup"] = (
            wrong_status,
            "status disagrees with rollup",
        )

        def hidden_uncovered(summary):
            summary["code_review"]["uncovered_scope"] = ["hidden_scope"]

        cases["uncovered"] = (
            hidden_uncovered,
            "uncovered scope is inconsistent",
        )

        def wrong_head(summary):
            summary["code_review"]["target_head"] = "0" * 40

        cases["head"] = (
            wrong_head,
            "target HEAD is not the reviewed candidate",
        )

        def wrong_repository(summary):
            summary["code_review"]["target_repository"] = (
                "/other/repository"
            )

        cases["repository"] = (
            wrong_repository,
            "target repository disagrees with candidate",
        )

        def wrong_files(summary):
            summary["code_review"]["reviewed_files"].pop()
            summary["code_review"]["files_reviewed"] -= 1

        cases["reviewed_files"] = (
            wrong_files,
            "reviewed file identity disagrees with candidate",
        )

        def missing_coverage(summary):
            summary["code_review"]["covered_scope"].pop()

        cases["coverage"] = (
            missing_coverage,
            "omitted required scope",
        )

        def colliding_identity(summary):
            summary["code_review"]["report"] = summary[
                "code_review"
            ]["result"]

        cases["report_result"] = (
            colliding_identity,
            "report and result identities collide",
        )

        for name, (mutate, reason) in cases.items():
            with self.subTest(name=name):
                summary = clone()
                mutate(summary)
                with self.assertRaisesRegex(EvidenceError, reason):
                    self._evaluate_gate(candidate, summary)

    def test_final_gate_requires_complete_trusted_candidate_identity(
        self,
    ) -> None:
        candidate, review_summary = self._gate_review_summary()
        mutations = {
            "missing_files": lambda value: value.pop("files"),
            "missing_fingerprint": lambda value: value.pop(
                "code_fingerprint"
            ),
            "missing_manifest": lambda value: value.pop(
                "file_manifest_sha256"
            ),
            "missing_status": lambda value: value.pop("status_sha256"),
            "missing_repository": lambda value: value.pop("repository"),
            "missing_target_head": lambda value: value.pop("target_head"),
            "wrong_file_count": lambda value: value.__setitem__(
                "file_count",
                value["file_count"] + 1,
            ),
            "wrong_manifest": lambda value: value.__setitem__(
                "file_manifest_sha256",
                "0" * 64,
            ),
            "wrong_fingerprint": lambda value: value.__setitem__(
                "code_fingerprint",
                "0" * 64,
            ),
            "wrong_status": lambda value: value.__setitem__(
                "status_sha256",
                "0" * 64,
            ),
            "wrong_repository": lambda value: value.__setitem__(
                "repository",
                "/other/repository",
            ),
            "self_consistent_files": lambda value: (
                value["files"][0].__setitem__("sha256", "0" * 64),
                value.__setitem__(
                    "file_manifest_sha256",
                    file_manifest_sha256(value["files"]),
                ),
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                malformed = json.loads(canonical_json(candidate))
                mutate(malformed)
                with self.assertRaisesRegex(
                    EvidenceError,
                    "candidate|trusted context|file count|manifest|status",
                ):
                    self._evaluate_gate(
                        malformed,
                        review_summary,
                    )

        malformed = json.loads(canonical_json(candidate))
        malformed.pop("files")
        with patch(
            "tests.task_control_center_evidence.build_candidate_snapshot",
            return_value=malformed,
        ), self.assertRaisesRegex(
            EvidenceError,
            "candidate|trusted context|files",
        ):
            self._build()

    def test_completed_lane_target_repository_is_required_and_exact(self) -> None:
        candidate, valid_summary = self._gate_review_summary()
        mutations = {
            "missing": lambda lane: lane.pop("target_repository"),
            "empty": lambda lane: lane.__setitem__(
                "target_repository",
                "",
            ),
            "wrong_type": lambda lane: lane.__setitem__(
                "target_repository",
                7,
            ),
            "other": lambda lane: lane.__setitem__(
                "target_repository",
                "/other/repository",
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                summary = json.loads(canonical_json(valid_summary))
                mutate(summary["architecture_review"])
                with self.assertRaisesRegex(
                    EvidenceError,
                    "target repository",
                ):
                    self._evaluate_gate(candidate, summary)

        previous = json.loads(
            self.fixture.previous_path.read_text(encoding="utf-8")
        )
        previous["special_review"]["architecture_review"].pop(
            "target_repository"
        )
        _write_json(self.fixture.previous_path, previous)
        self.fixture.metadata["previous_summary"]["sha256"] = sha256_file(
            self.fixture.previous_path
        )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaisesRegex(
            EvidenceError,
            "target repository|repository disagrees with trusted context",
        ):
            self._build()

    def test_current_review_manifest_requires_target_repository(self) -> None:
        review = self.fixture.complete_reviews()
        payload = json.loads(review.read_text(encoding="utf-8"))
        payload["code_review"].pop("target_repository")
        _write_json(review, payload)
        self.fixture.metadata["review"]["sha256"] = sha256_file(review)
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaisesRegex(
            EvidenceError,
            "target_repository|target repository",
        ):
            self._build()

    def test_content_equivalent_binding_head_must_match_candidate(self) -> None:
        previous = json.loads(
            self.fixture.previous_path.read_text(encoding="utf-8")
        )
        target_head = previous["candidate_identity"]["target_head"]
        previous["candidate_identity"]["reviewed_head"] = target_head
        previous["acceptance_gate"]["acceptance_policy"] = (
            "content_equivalence_allowed"
        )
        previous["acceptance_gate"]["content_equivalence_binding"] = (
            "CONTENT_EQUIVALENT_CLEAR"
        )
        previous["acceptance_gate"][
            "current_target_independent_review"
        ] = "NOT_AVAILABLE"
        previous["acceptance_gate"]["current_target_review_resolution"] = (
            "CONTENT_EQUIVALENT_CLEAR"
        )
        previous["acceptance_gate"]["review_gate"] = (
            "CONTENT_EQUIVALENT_CLEAR"
        )
        previous["review_status"]["review_gate"] = (
            "CONTENT_EQUIVALENT_CLEAR"
        )
        previous["special_review"]["candidate_acceptance"] = (
            "CONTENT_EQUIVALENT_REVIEW_CLEAR"
        )
        previous["review_binding"] = {
            "path": "history-binding.json",
            "sha256": "5" * 64,
            "scope": "content_equivalence_only",
            "binding_result": "CONTENT_EQUIVALENT_REVIEW_CLEAR",
            "evidence_status": "CONTENT_EQUIVALENT_REVIEW_CLEAR",
            "reviewed_head": "2" * 40,
            "target_head": target_head,
            "acceptance_supported": False,
            "merge_ready": False,
        }
        _write_json(self.fixture.previous_path, previous)
        self.fixture.metadata["previous_summary"]["sha256"] = sha256_file(
            self.fixture.previous_path
        )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaisesRegex(
            EvidenceError,
            "review binding reviewed HEAD|disagrees with trusted snapshot",
        ):
            self._build()

    def test_final_gate_rejects_wrong_content_binding_head(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle",
            )
            bundle = _validate_bundle(self.fixture, self.root, bundle_path)
            binding = json.loads(
                canonical_json(bundle["review_binding"])
            )
            binding["reviewed_head"] = "2" * 40
            with self.assertRaisesRegex(
                EvidenceError,
                "review binding identity disagrees with candidate|"
                "does not match independently validated evidence",
            ):
                self._evaluate_bundle_gate(
                    bundle,
                    review_binding=binding,
                )

    def test_final_gate_requires_complete_binding_semantics(self) -> None:
        bundle_temp = tempfile.TemporaryDirectory()
        self.addCleanup(bundle_temp.cleanup)
        bundle_path = self.fixture.write_evidence_bundle(
            Path(bundle_temp.name) / "bundle",
        )
        bundle = _validate_bundle(self.fixture, self.root, bundle_path)

        candidate = bundle["candidate_identity"]
        review_summary = bundle["review_summary"]
        valid_binding = bundle["review_binding"]

        def clone() -> dict:
            return json.loads(canonical_json(valid_binding))

        def evaluate(binding):
            return self._evaluate_bundle_gate(
                bundle,
                review_binding=binding,
            )

        cases = {
            "missing_schema": lambda value: value.pop("schema_version"),
            "wrong_repository": lambda value: value.__setitem__(
                "repository",
                "/other/repository",
            ),
            "blocked_result": lambda value: value.update(
                {
                    "binding_result":
                        "CONTENT_EQUIVALENT_REVIEW_BLOCKED",
                    "evidence_status":
                        "CONTENT_EQUIVALENT_REVIEW_BLOCKED",
                }
            ),
            "empty_lanes": lambda value: value.__setitem__(
                "review_lanes",
                {},
            ),
            "empty_files": lambda value: value.__setitem__(
                "reviewed_files",
                [],
            ),
            "acceptance_supported": lambda value: value.__setitem__(
                "acceptance_supported",
                True,
            ),
            "merge_ready": lambda value: value.__setitem__(
                "merge_ready",
                True,
            ),
            "wrong_reviewed_head": lambda value: value.__setitem__(
                "reviewed_head",
                "2" * 40,
            ),
            "wrong_target_head": lambda value: value.__setitem__(
                "target_head",
                "2" * 40,
            ),
            "wrong_fingerprint": lambda value: value.__setitem__(
                "code_fingerprint",
                "0" * 64,
            ),
            "wrong_manifest": lambda value: value.__setitem__(
                "reviewed_file_manifest_sha256",
                "0" * 64,
            ),
            "missing_report": lambda value: value["review_lanes"][
                "code_review"
            ].pop("report"),
            "colliding_report_result": lambda value: value[
                "review_lanes"
            ]["code_review"].__setitem__(
                "report",
                value["review_lanes"]["code_review"]["result"],
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                binding = clone()
                mutate(binding)
                with self.assertRaises(EvidenceError):
                    evaluate(binding)

        hidden = clone()
        hidden["review_lanes"]["code_review"]["uncovered_scope"] = [
            "hidden"
        ]
        hidden["review_status"]["reviews_clear"] = False
        hidden["review_status"]["uncovered_scope"] = [
            "code_review:hidden"
        ]
        hidden["binding_result"] = "CONTENT_EQUIVALENT_REVIEW_BLOCKED"
        hidden["evidence_status"] = "CONTENT_EQUIVALENT_REVIEW_BLOCKED"
        with self.assertRaisesRegex(
            EvidenceError,
            "does not match independently validated evidence|"
            "uncovered scope is inconsistent",
        ):
            evaluate(hidden)

    def test_final_gate_reads_actual_binding_evidence(self) -> None:
        bundle_temp = tempfile.TemporaryDirectory()
        self.addCleanup(bundle_temp.cleanup)
        bundle_path = self.fixture.write_evidence_bundle(
            Path(bundle_temp.name) / "bundle",
        )
        bundle = _validate_bundle(self.fixture, self.root, bundle_path)
        candidate = bundle["candidate_identity"]
        review_summary = bundle["review_summary"]
        valid_binding = bundle["review_binding"]

        def evaluate(binding, name):
            binding_path = (
                self.root
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / name
            )
            _write_json(binding_path, binding)
            return self._evaluate_binding_gate(
                candidate,
                review_summary,
                binding,
                binding_path,
                acceptance_policy=bundle["acceptance_policy"],
                runtime_verification=bundle["runtime_verification"],
            )

        phantom = json.loads(canonical_json(valid_binding))
        phantom["review_lanes"]["code_review"]["report"] = (
            "artifacts/audits/task-control-center-v1/missing-report.md"
        )
        phantom["review_lanes"]["code_review"]["report_sha256"] = "8" * 64
        with self.assertRaisesRegex(
            EvidenceError,
            "missing|does not match recomputed content",
        ):
            evaluate(phantom, "phantom-binding.json")

        unexpected_scope = json.loads(canonical_json(valid_binding))
        unexpected_scope["unverified_scope"].append(
            "candidate_acceptance_logic"
        )
        with self.assertRaisesRegex(
            EvidenceError,
            "unpropagated scopes|does not match recomputed content",
        ):
            evaluate(
                unexpected_scope,
                "unexpected-scope-binding.json",
            )

    def test_final_gate_reopens_direct_review_evidence(self) -> None:
        for key, field in (
            ("code_review", "report"),
            ("code_review", "result"),
            ("architecture_review", "report"),
            ("architecture_review", "result"),
        ):
            with self.subTest(key=key, field=field):
                review = self.fixture.complete_reviews()
                candidate = build_candidate_snapshot(self.root)
                review_summary = validate_review_manifest(
                    self.root,
                    review,
                    expected_code_fingerprint=candidate["code_fingerprint"],
                    changed_candidate_paths=[
                        item["path"] for item in candidate["files"]
                    ],
                )
                evidence_path = self.root / review_summary[key][field]
                evidence_path.unlink()
                with self.assertRaisesRegex(
                    EvidenceError,
                    "missing|unreadable|does not match",
                ):
                    self._evaluate_gate(candidate, review_summary)

        review = self.fixture.complete_reviews()
        candidate = build_candidate_snapshot(self.root)
        review_summary = validate_review_manifest(
            self.root,
            review,
            expected_code_fingerprint=candidate["code_fingerprint"],
            changed_candidate_paths=[
                item["path"] for item in candidate["files"]
            ],
        )
        tampered = (
            self.root / review_summary["architecture_review"]["report"]
        )
        tampered.write_text(
            tampered.read_text(encoding="utf-8") + "tampered\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(EvidenceError, "hash|SHA"):
            self._evaluate_gate(candidate, review_summary)

    def test_direct_gate_decision_uses_captured_review_snapshot(
        self,
    ) -> None:
        review = self.fixture.complete_reviews()
        candidate = build_candidate_snapshot(self.root)
        review_summary = validate_review_manifest(
            self.root,
            review,
            expected_code_fingerprint=candidate["code_fingerprint"],
            changed_candidate_paths=[
                item["path"] for item in candidate["files"]
            ],
        )
        import tests.task_control_center_evidence as evidence_module

        original = evidence_module._capture_review_evidence_snapshot

        def capture_then_delete(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            captured = snapshot.summary()
            for key in ("code_review", "architecture_review"):
                for field in ("report", "result"):
                    (self.root / captured[key][field]).unlink()
            return snapshot

        with patch(
            "tests.task_control_center_evidence."
            "_capture_review_evidence_snapshot",
            side_effect=capture_then_delete,
        ):
            gate = self._evaluate_gate(candidate, review_summary)
        self.assertEqual(
            gate["acceptance_status"],
            ACCEPTED_FOR_MERGE_REVIEW,
        )
        self.assertTrue(gate["merge_ready"])
        self.assertFalse(
            (self.root / review_summary["code_review"]["report"]).exists()
        )

    def test_public_gate_cannot_accept_caller_authority(self) -> None:
        candidate, review_summary = self._gate_review_summary()
        gate = evaluate_public_acceptance_gate(
            candidate_identity=candidate,
            trusted_repository_root=self.trusted_repository_root,
            review_summary=review_summary,
            review_binding=None,
            acceptance_policy="target_head_review_required",
        )
        self.assertNotEqual(
            gate["acceptance_status"],
            ACCEPTED_FOR_MERGE_REVIEW,
        )
        self.assertFalse(gate["merge_ready"])
        with self.assertRaises(TypeError):
            evaluate_public_acceptance_gate(
                candidate_identity=candidate,
                trusted_repository_root=self.trusted_repository_root,
                review_summary=review_summary,
                review_binding=None,
                acceptance_policy="target_head_review_required",
                tests_verified=True,
                frozen_verified=True,
                previous_summary_available=True,
                previous_summary_trusted=True,
            )
        import tests.task_control_center_evidence as evidence_module

        forged_proof = evidence_module._AcceptancePrerequisiteProof(
            "0" * 64,
            canonical_json(
                {
                    "candidate_identity_sha256": "0" * 64,
                    "review_summary_sha256": "0" * 64,
                    "evidence_binding_sha256": "0" * 64,
                    "tests_verified": True,
                    "frozen_verified": True,
                    "previous_summary_available": True,
                    "previous_summary_trusted": True,
                    "runtime_verification": {
                        "status": "VERIFIED",
                        "scopes": ["browser"],
                        "reason": "forged",
                        "valid_evidence_count": 1,
                    },
                }
            ),
            "0" * 64,
        )
        with self.assertRaisesRegex(
            EvidenceError,
            "signature is invalid|not an active issuance",
        ):
            _evaluate_acceptance_gate_core(
                candidate_identity=candidate,
                trusted_repository_root=self.trusted_repository_root,
                review_summary=review_summary,
                review_binding=None,
                acceptance_policy="target_head_review_required",
                acceptance_evidence_binding={"test": True},
                acceptance_prerequisites=forged_proof,
            )

    def test_acceptance_proof_binds_candidate_and_review(self) -> None:
        import tests.task_control_center_evidence as evidence_module

        candidate, review_summary = self._gate_review_summary()
        other_candidate = json.loads(canonical_json(candidate))
        other_candidate["code_fingerprint"] = "b" * 64
        cross_candidate = _issue_acceptance_prerequisite_proof(
            candidate_identity=other_candidate,
            review_summary=review_summary,
            evidence_binding={"test": "cross-candidate"},
            owner=_new_acceptance_prerequisite_owner(),
            tests_verified=True,
            frozen_verified=True,
            previous_summary_available=True,
            previous_summary_trusted=True,
            runtime_verification={
                "status": "VERIFIED",
                "scopes": ["browser"],
                "reason": "candidate-bound browser evidence",
                "valid_evidence_count": 1,
            },
        )
        with self.assertRaisesRegex(EvidenceError, "different candidate"):
            _evaluate_acceptance_gate_core(
                candidate_identity=candidate,
                trusted_repository_root=self.trusted_repository_root,
                review_summary=review_summary,
                review_binding=None,
                acceptance_policy="target_head_review_required",
                acceptance_evidence_binding={"test": "cross-candidate"},
                acceptance_prerequisites=cross_candidate,
            )

        different_review = json.loads(canonical_json(review_summary))
        different_review["sha256"] = "f" * 64
        review_proof = _issue_acceptance_prerequisite_proof(
            candidate_identity=candidate,
            review_summary=different_review,
            evidence_binding={"test": "wrong-review"},
            owner=_new_acceptance_prerequisite_owner(),
            tests_verified=True,
            frozen_verified=True,
            previous_summary_available=True,
            previous_summary_trusted=True,
            runtime_verification={
                "status": "VERIFIED",
                "scopes": ["browser"],
                "reason": "candidate-bound browser evidence",
                "valid_evidence_count": 1,
            },
        )
        with self.assertRaisesRegex(EvidenceError, "different review evidence"):
            _evaluate_acceptance_gate_core(
                candidate_identity=candidate,
                trusted_repository_root=self.trusted_repository_root,
                review_summary=review_summary,
                review_binding=None,
                acceptance_policy="target_head_review_required",
                acceptance_evidence_binding={"test": "wrong-review"},
                acceptance_prerequisites=review_proof,
            )

        valid_proof = _issue_acceptance_prerequisite_proof(
            candidate_identity=candidate,
            review_summary=review_summary,
            evidence_binding={"test": "signature"},
            owner=_new_acceptance_prerequisite_owner(),
            tests_verified=True,
            frozen_verified=True,
            previous_summary_available=True,
            previous_summary_trusted=True,
            runtime_verification={
                "status": "VERIFIED",
                "scopes": ["browser"],
                "reason": "candidate-bound browser evidence",
                "valid_evidence_count": 1,
            },
        )
        tampered = replace(
            valid_proof,
            evidence_json=valid_proof.evidence_json.replace(
                '"tests_verified": true',
                '"tests_verified": false',
            ),
        )
        with self.assertRaisesRegex(
            EvidenceError,
            "signature is invalid|not an active issuance",
        ):
            _evaluate_acceptance_gate_core(
                candidate_identity=candidate,
                trusted_repository_root=self.trusted_repository_root,
                review_summary=review_summary,
                review_binding=None,
                acceptance_policy="target_head_review_required",
                acceptance_evidence_binding={"test": "signature"},
                acceptance_prerequisites=tampered,
            )

        reconstructed_proof = evidence_module._AcceptancePrerequisiteProof(
            valid_proof.issuance_id,
            valid_proof.evidence_json,
            valid_proof.signature,
        )
        with self.assertRaisesRegex(EvidenceError, "not an active issuance"):
            _evaluate_acceptance_gate_core(
                candidate_identity=candidate,
                trusted_repository_root=self.trusted_repository_root,
                review_summary=review_summary,
                review_binding=None,
                acceptance_policy="target_head_review_required",
                acceptance_evidence_binding={"test": "signature"},
                acceptance_prerequisites=reconstructed_proof,
            )

        with self.assertRaises(TypeError):
            evidence_module_proof = (
                __import__(
                    "tests.task_control_center_evidence",
                    fromlist=["_issue_acceptance_prerequisite_proof"],
                )._issue_acceptance_prerequisite_proof
            )
            evidence_module_proof(tests_verified=True)

    def test_acceptance_proof_is_consumed_once(self) -> None:
        candidate, review_summary = self._gate_review_summary()
        binding = {"test": "consume-once"}
        proof = self._acceptance_prerequisites(
            candidate,
            review_summary,
            evidence_binding=binding,
            runtime_verification={
                "status": "VERIFIED",
                "scopes": ["browser"],
                "reason": "candidate-bound browser evidence",
                "valid_evidence_count": 1,
            },
        )
        first = _evaluate_acceptance_gate_core(
            candidate_identity=candidate,
            trusted_repository_root=self.trusted_repository_root,
            review_summary=review_summary,
            review_binding=None,
            acceptance_policy="target_head_review_required",
            acceptance_evidence_binding=binding,
            acceptance_prerequisites=proof,
        )
        self.assertEqual(
            first["acceptance_status"],
            ACCEPTED_FOR_MERGE_REVIEW,
        )
        self.assertEqual(
            _acceptance_prerequisite_status(proof),
            "CONSUMED",
        )
        with self.assertRaisesRegex(EvidenceError, "already consumed"):
            _evaluate_acceptance_gate_core(
                candidate_identity=candidate,
                trusted_repository_root=self.trusted_repository_root,
                review_summary=review_summary,
                review_binding=None,
                acceptance_policy="target_head_review_required",
                acceptance_evidence_binding=binding,
                acceptance_prerequisites=proof,
            )

    def test_acceptance_proof_evidence_mismatch_revokes(self) -> None:
        candidate, review_summary = self._gate_review_summary()
        issued_binding = {"test": "evidence-A"}
        proof = self._acceptance_prerequisites(
            candidate,
            review_summary,
            evidence_binding=issued_binding,
        )
        other_binding = {"test": "evidence-B"}
        other_proof = self._acceptance_prerequisites(
            candidate,
            review_summary,
            evidence_binding=other_binding,
        )
        with self.assertRaisesRegex(
            EvidenceError,
            "evidence binding does not match",
        ):
            _evaluate_acceptance_gate_core(
                candidate_identity=candidate,
                trusted_repository_root=self.trusted_repository_root,
                review_summary=review_summary,
                review_binding=None,
                acceptance_policy="target_head_review_required",
                acceptance_evidence_binding=other_binding,
                acceptance_prerequisites=proof,
            )
        self.assertEqual(
            _acceptance_prerequisite_status(proof),
            "REVOKED",
        )
        with self.assertRaisesRegex(EvidenceError, "revoked"):
            _evaluate_acceptance_gate_core(
                candidate_identity=candidate,
                trusted_repository_root=self.trusted_repository_root,
                review_summary=review_summary,
                review_binding=None,
                acceptance_policy="target_head_review_required",
                acceptance_evidence_binding=issued_binding,
                acceptance_prerequisites=proof,
            )
        with self.assertRaisesRegex(
            EvidenceError,
            "evidence binding does not match",
        ):
            _evaluate_acceptance_gate_core(
                candidate_identity=candidate,
                trusted_repository_root=self.trusted_repository_root,
                review_summary=review_summary,
                review_binding=None,
                acceptance_policy="target_head_review_required",
                acceptance_evidence_binding=issued_binding,
                acceptance_prerequisites=other_proof,
            )

    def test_acceptance_proof_revoke_and_expiry(self) -> None:
        candidate, review_summary = self._gate_review_summary()
        revoked = self._acceptance_prerequisites(candidate, review_summary)
        _revoke_acceptance_prerequisite_proof(revoked)
        self.assertEqual(
            _acceptance_prerequisite_status(revoked),
            "REVOKED",
        )
        with self.assertRaisesRegex(EvidenceError, "revoked"):
            _evaluate_acceptance_gate_core(
                candidate_identity=candidate,
                trusted_repository_root=self.trusted_repository_root,
                review_summary=review_summary,
                review_binding=None,
                acceptance_policy="target_head_review_required",
                acceptance_evidence_binding={"test": True},
                acceptance_prerequisites=revoked,
            )

        clock_value = [10.0]
        (
            issue,
            _consume,
            _revoke,
            _owner_revoke,
            proof_status,
            _purge,
            _count,
        ) = _make_acceptance_prerequisite_authority(
            clock=lambda: clock_value[0],
            max_records=8,
            terminal_retention_seconds=2,
        )
        expired = issue(
            candidate_identity=candidate,
            review_summary=review_summary,
            evidence_binding={"test": "expiry"},
            owner=_new_acceptance_prerequisite_owner(),
            tests_verified=True,
            frozen_verified=True,
            previous_summary_available=True,
            previous_summary_trusted=True,
            runtime_verification=None,
            ttl_seconds=5,
        )
        clock_value[0] = 14.999
        self.assertEqual(proof_status(expired), "ACTIVE")
        clock_value[0] = 15.0
        self.assertEqual(proof_status(expired), "EXPIRED")
        with self.assertRaisesRegex(EvidenceError, "expired"):
            _consume(
                expired,
                candidate_identity=candidate,
                review_summary=review_summary,
                evidence_binding={"test": "expiry"},
            )
        self.assertEqual(
            proof_status(expired),
            "EXPIRED",
        )

    def test_acceptance_proof_records_are_bounded_and_purged(self) -> None:
        candidate, review_summary = self._gate_review_summary()
        clock_value = [0.0]
        (
            issue,
            consume,
            revoke,
            owner_revoke,
            status,
            purge,
            count,
        ) = _make_acceptance_prerequisite_authority(
            clock=lambda: clock_value[0],
            max_records=3,
            terminal_retention_seconds=1,
        )

        proofs = []
        for index in range(3):
            proofs.append(
                issue(
                    candidate_identity=candidate,
                    review_summary=review_summary,
                    evidence_binding={"test": f"retention-{index}"},
                    owner=_new_acceptance_prerequisite_owner(),
                    tests_verified=True,
                    frozen_verified=True,
                    previous_summary_available=True,
                    previous_summary_trusted=True,
                    runtime_verification=None,
                    ttl_seconds=100,
                )
            )
        self.assertEqual(count(), 3)
        with self.assertRaisesRegex(EvidenceError, "capacity exhausted"):
            issue(
                candidate_identity=candidate,
                review_summary=review_summary,
                evidence_binding={"test": "overflow"},
                owner=_new_acceptance_prerequisite_owner(),
                tests_verified=True,
                frozen_verified=True,
                previous_summary_available=True,
                previous_summary_trusted=True,
                runtime_verification=None,
                ttl_seconds=100,
            )

        consume(
            proofs[0],
            candidate_identity=candidate,
            review_summary=review_summary,
            evidence_binding={"test": "retention-0"},
        )
        revoke(proofs[1])
        clock_value[0] = 2.0
        purged = purge()
        self.assertEqual(purged, 2)
        self.assertEqual(count(), 1)
        self.assertEqual(status(proofs[2]), "ACTIVE")
        self.assertEqual(status(proofs[0]), "NOT_ACTIVE")
        with self.assertRaisesRegex(
            EvidenceError,
            "already consumed|not an active issuance",
        ):
            consume(
                proofs[0],
                candidate_identity=candidate,
                review_summary=review_summary,
                evidence_binding={"test": "retention-0"},
            )

    def test_zero_retention_status_returns_expired_before_purge(self) -> None:
        candidate, review_summary = self._gate_review_summary()
        clock_value = [10.0]
        (
            issue,
            consume,
            revoke,
            owner_revoke,
            status,
            purge,
            count,
        ) = _make_acceptance_prerequisite_authority(
            clock=lambda: clock_value[0],
            max_records=8,
            terminal_retention_seconds=0,
        )
        binding = {"test": "zero-retention"}
        proof = issue(
            candidate_identity=candidate,
            review_summary=review_summary,
            evidence_binding=binding,
            owner=_new_acceptance_prerequisite_owner(),
            tests_verified=True,
            frozen_verified=True,
            previous_summary_available=True,
            previous_summary_trusted=True,
            runtime_verification=None,
            ttl_seconds=5,
        )
        clock_value[0] = 14.999
        self.assertEqual(status(proof), "ACTIVE")
        clock_value[0] = 15.0
        self.assertEqual(status(proof), "EXPIRED")
        self.assertEqual(status(proof), "EXPIRED")
        self.assertEqual(purge(), 1)
        self.assertEqual(count(), 0)
        self.assertEqual(status(proof), "NOT_ACTIVE")
        with self.assertRaisesRegex(EvidenceError, "not an active issuance"):
            consume(
                proof,
                candidate_identity=candidate,
                review_summary=review_summary,
                evidence_binding=binding,
            )
        with self.assertRaisesRegex(
            EvidenceError,
            "not an active issuance",
        ):
            revoke(proof)

    def test_status_consume_revoke_race_never_restores_active(
        self,
    ) -> None:
        candidate, review_summary = self._gate_review_summary()
        clock_value = [10.0]
        (
            issue,
            consume,
            revoke,
            owner_revoke,
            status,
            _purge,
            _count,
        ) = _make_acceptance_prerequisite_authority(
            clock=lambda: clock_value[0],
            max_records=8,
            terminal_retention_seconds=0,
        )
        binding = {"test": "status-race"}
        proof = issue(
            candidate_identity=candidate,
            review_summary=review_summary,
            evidence_binding=binding,
            owner=_new_acceptance_prerequisite_owner(),
            tests_verified=True,
            frozen_verified=True,
            previous_summary_available=True,
            previous_summary_trusted=True,
            runtime_verification=None,
            ttl_seconds=5,
        )
        clock_value[0] = 15.0
        barrier = threading.Barrier(3)
        outcomes: list[str] = []
        outcomes_lock = threading.Lock()

        def record(action) -> None:
            barrier.wait(timeout=5)
            try:
                outcome = action()
            except EvidenceError as exc:
                outcome = f"error:{exc}"
            with outcomes_lock:
                outcomes.append(str(outcome))

        def run_consume() -> str:
            consume(
                proof,
                candidate_identity=candidate,
                review_summary=review_summary,
                evidence_binding=binding,
            )
            return "CONSUMED"

        def run_revoke() -> str:
            revoke(proof)
            return "REVOKED"

        threads = [
            threading.Thread(target=record, args=(lambda: status(proof),)),
            threading.Thread(target=record, args=(run_consume,)),
            threading.Thread(target=record, args=(run_revoke,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(outcomes), 3)
        self.assertNotIn("ACTIVE", outcomes)
        self.assertNotIn("CONSUMED", outcomes)
        self.assertTrue(
            all(
                outcome in {"EXPIRED", "NOT_ACTIVE", "REVOKED"}
                or "expired" in outcome
                or "not an active issuance" in outcome
                for outcome in outcomes
            )
        )
        self.assertIn(status(proof), {"EXPIRED", "NOT_ACTIVE"})

    def test_acceptance_proof_canonicalization_failure_revokes(self) -> None:
        candidate, review_summary = self._gate_review_summary()
        (
            issue,
            consume,
            _revoke,
            _owner_revoke,
            status,
            _purge,
            _count,
        ) = _make_acceptance_prerequisite_authority()
        proof = issue(
            candidate_identity=candidate,
            review_summary=review_summary,
            evidence_binding={"test": "canonical"},
            owner=_new_acceptance_prerequisite_owner(),
            tests_verified=True,
            frozen_verified=True,
            previous_summary_available=True,
            previous_summary_trusted=True,
            runtime_verification=None,
            ttl_seconds=100,
        )
        with self.assertRaises(TypeError):
            consume(
                proof,
                candidate_identity=candidate,
                review_summary=review_summary,
                evidence_binding={"bad": object()},
            )
        self.assertEqual(status(proof), "REVOKED")
        with self.assertRaisesRegex(EvidenceError, "revoked"):
            consume(
                proof,
                candidate_identity=candidate,
                review_summary=review_summary,
                evidence_binding={"test": "canonical"},
            )

    def test_public_gate_revokes_proof_when_core_preparation_fails(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        candidate, review_summary = self._gate_review_summary()
        (
            issue,
            consume,
            revoke,
            owner_revoke,
            status,
            purge,
            count,
        ) = _make_acceptance_prerequisite_authority(
            max_records=4,
            terminal_retention_seconds=0,
        )
        issued = []

        def capture_issue(**kwargs):
            proof = issue(**kwargs)
            issued.append(proof)
            return proof

        real_canonical_json = evidence_module.canonical_json
        candidate_calls = 0

        def fail_second_candidate_canonicalization(value):
            nonlocal candidate_calls
            if value is candidate:
                candidate_calls += 1
                if candidate_calls % 2 == 0:
                    raise TypeError("injected canonicalization failure")
            return real_canonical_json(value)

        with patch.object(
            evidence_module,
            "_issue_acceptance_prerequisite_proof",
            side_effect=capture_issue,
        ), patch.object(
            evidence_module,
            "_consume_acceptance_prerequisite_proof",
            consume,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_proof",
            revoke,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_owner",
            owner_revoke,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_status",
            status,
        ), patch.object(
            evidence_module,
            "_purge_acceptance_prerequisite_proofs",
            purge,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_record_count",
            count,
        ), patch.object(
            evidence_module,
            "canonical_json",
            side_effect=fail_second_candidate_canonicalization,
        ):
            for _ in range(40):
                with self.assertRaisesRegex(
                    TypeError,
                    "injected canonicalization failure",
                ):
                    evaluate_public_acceptance_gate(
                        candidate_identity=candidate,
                        trusted_repository_root=(
                            self.trusted_repository_root
                        ),
                        review_summary=review_summary,
                        review_binding=None,
                        acceptance_policy=(
                            "target_head_review_required"
                        ),
                    )
            self.assertEqual(len(issued), 40)
            self.assertTrue(
                all(status(proof) == "NOT_ACTIVE" for proof in issued)
            )
            self.assertLessEqual(count(), 4)

    def test_public_gate_failure_revocation_is_safe_concurrently(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        candidate, review_summary = self._gate_review_summary()
        (
            issue,
            consume,
            revoke,
            owner_revoke,
            status,
            purge,
            count,
        ) = _make_acceptance_prerequisite_authority(
            max_records=32,
            terminal_retention_seconds=60,
        )
        issued = []
        issued_lock = threading.Lock()
        worker_count = 16
        barrier = threading.Barrier(worker_count)
        errors: list[BaseException] = []

        def capture_issue(**kwargs):
            proof = issue(**kwargs)
            with issued_lock:
                issued.append(proof)
            return proof

        def fail_before_consume(**_kwargs):
            barrier.wait(timeout=5)
            raise RuntimeError("injected core failure")

        def run_public_gate() -> None:
            try:
                evaluate_public_acceptance_gate(
                    candidate_identity=candidate,
                    trusted_repository_root=self.trusted_repository_root,
                    review_summary=review_summary,
                    review_binding=None,
                    acceptance_policy="target_head_review_required",
                )
            except BaseException as exc:
                errors.append(exc)

        with patch.object(
            evidence_module,
            "_issue_acceptance_prerequisite_proof",
            side_effect=capture_issue,
        ), patch.object(
            evidence_module,
            "_consume_acceptance_prerequisite_proof",
            consume,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_proof",
            revoke,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_owner",
            owner_revoke,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_status",
            status,
        ), patch.object(
            evidence_module,
            "_purge_acceptance_prerequisite_proofs",
            purge,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_record_count",
            count,
        ), patch.object(
            evidence_module,
            "_evaluate_acceptance_gate_core",
            side_effect=fail_before_consume,
        ):
            threads = [
                threading.Thread(target=run_public_gate)
                for _ in range(worker_count)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(len(errors), worker_count)
        self.assertTrue(
            all(
                isinstance(error, RuntimeError)
                and "injected core failure" in str(error)
                for error in errors
            )
        )
        self.assertEqual(len(issued), worker_count)
        self.assertTrue(
            all(status(proof) == "REVOKED" for proof in issued)
        )

    def test_build_summary_revokes_pre_consume_proof_failure(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        (
            issue,
            consume,
            revoke,
            owner_revoke,
            status,
            purge,
            count,
        ) = _make_acceptance_prerequisite_authority(
            max_records=4,
            terminal_retention_seconds=0,
        )
        issued = []

        def capture_issue(**kwargs):
            proof = issue(**kwargs)
            issued.append(proof)
            return proof

        with patch.object(
            evidence_module,
            "_issue_acceptance_prerequisite_proof",
            side_effect=capture_issue,
        ), patch.object(
            evidence_module,
            "_consume_acceptance_prerequisite_proof",
            consume,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_proof",
            revoke,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_owner",
            owner_revoke,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_status",
            status,
        ), patch.object(
            evidence_module,
            "_purge_acceptance_prerequisite_proofs",
            purge,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_record_count",
            count,
        ), patch.object(
            evidence_module,
            "_evaluate_acceptance_gate_core",
            side_effect=RuntimeError("injected summary pre-consume failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected summary pre-consume failure",
            ):
                self._build()
            self.assertEqual(len(issued), 1)
            self.assertEqual(status(issued[0]), "NOT_ACTIVE")
            self.assertEqual(count(), 0)

    def test_acceptance_transaction_bounds_repeated_failures(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        candidate, review_summary = self._gate_review_summary()
        binding = {"test": "repeated-transaction-failure"}
        (
            issue,
            consume,
            revoke,
            owner_revoke,
            status,
            purge,
            count,
        ) = _make_acceptance_prerequisite_authority(
            max_records=4,
            terminal_retention_seconds=0,
        )
        issued = []

        def capture_issue(**kwargs):
            proof = issue(**kwargs)
            issued.append(proof)
            return proof

        with patch.object(
            evidence_module,
            "_issue_acceptance_prerequisite_proof",
            side_effect=capture_issue,
        ), patch.object(
            evidence_module,
            "_consume_acceptance_prerequisite_proof",
            consume,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_proof",
            revoke,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_owner",
            owner_revoke,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_status",
            status,
        ), patch.object(
            evidence_module,
            "_purge_acceptance_prerequisite_proofs",
            purge,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_record_count",
            count,
        ):
            for _ in range(40):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected transaction failure",
                ):
                    with evidence_module._acceptance_prerequisite_transaction(
                        candidate_identity=candidate,
                        review_summary=review_summary,
                        evidence_binding=binding,
                        tests_verified=True,
                        frozen_verified=True,
                        previous_summary_available=True,
                        previous_summary_trusted=True,
                        runtime_verification=None,
                    ):
                        raise RuntimeError("injected transaction failure")
            self.assertEqual(len(issued), 40)
            self.assertTrue(
                all(status(proof) == "NOT_ACTIVE" for proof in issued)
            )
            self.assertEqual(count(), 0)

    def test_acceptance_transaction_recovers_issue_return_failures(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        candidate, review_summary = self._gate_review_summary()
        binding = {"test": "issue-return-failure"}
        (
            issue,
            consume,
            revoke,
            owner_revoke,
            status,
            purge,
            count,
        ) = _make_acceptance_prerequisite_authority(
            max_records=4,
            terminal_retention_seconds=0,
        )
        issued = []

        def issue_then_raise(**kwargs):
            proof = issue(**kwargs)
            issued.append(proof)
            raise RuntimeError("injected issue return failure")

        with patch.object(
            evidence_module,
            "_issue_acceptance_prerequisite_proof",
            side_effect=issue_then_raise,
        ), patch.object(
            evidence_module,
            "_consume_acceptance_prerequisite_proof",
            consume,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_proof",
            revoke,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_owner",
            owner_revoke,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_status",
            status,
        ), patch.object(
            evidence_module,
            "_purge_acceptance_prerequisite_proofs",
            purge,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_record_count",
            count,
        ):
            for _ in range(40):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected issue return failure",
                ):
                    with evidence_module._acceptance_prerequisite_transaction(
                        candidate_identity=candidate,
                        review_summary=review_summary,
                        evidence_binding=binding,
                        tests_verified=True,
                        frozen_verified=True,
                        previous_summary_available=True,
                        previous_summary_trusted=True,
                        runtime_verification=None,
                    ):
                        self.fail("issuance should not yield")
            self.assertEqual(len(issued), 40)
            self.assertTrue(
                all(status(proof) == "NOT_ACTIVE" for proof in issued)
            )
            self.assertEqual(count(), 0)

    def test_revoke_clock_failure_leaves_proof_revoked(self) -> None:
        candidate, review_summary = self._gate_review_summary()
        clock_value = [10.0]

        def clock():
            value = clock_value[0]
            if value is None:
                raise EvidenceError("injected revoke clock failure")
            return value

        (
            issue,
            _consume,
            revoke,
            _owner_revoke,
            status,
            _purge,
            _count,
        ) = _make_acceptance_prerequisite_authority(
            clock=clock,
            max_records=4,
            terminal_retention_seconds=0,
        )
        proof = issue(
            candidate_identity=candidate,
            review_summary=review_summary,
            evidence_binding={"test": "revoke-clock"},
            owner=_new_acceptance_prerequisite_owner(),
            tests_verified=True,
            frozen_verified=True,
            previous_summary_available=True,
            previous_summary_trusted=True,
            runtime_verification=None,
        )
        clock_value[0] = None
        with self.assertRaisesRegex(
            EvidenceError,
            "injected revoke clock failure",
        ):
            revoke(proof)
        clock_value[0] = 10.0
        self.assertEqual(status(proof), "REVOKED")

    def test_acceptance_transaction_revokes_unconsumed_exit(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        candidate, review_summary = self._gate_review_summary()
        binding = {"test": "unconsumed-transaction"}
        (
            issue,
            consume,
            revoke,
            owner_revoke,
            status,
            purge,
            count,
        ) = _make_acceptance_prerequisite_authority(
            max_records=4,
            terminal_retention_seconds=0,
        )
        issued = []

        def capture_issue(**kwargs):
            proof = issue(**kwargs)
            issued.append(proof)
            return proof

        with patch.object(
            evidence_module,
            "_issue_acceptance_prerequisite_proof",
            side_effect=capture_issue,
        ), patch.object(
            evidence_module,
            "_consume_acceptance_prerequisite_proof",
            consume,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_proof",
            revoke,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_owner",
            owner_revoke,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_status",
            status,
        ), patch.object(
            evidence_module,
            "_purge_acceptance_prerequisite_proofs",
            purge,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_record_count",
            count,
        ):
            with evidence_module._acceptance_prerequisite_transaction(
                candidate_identity=candidate,
                review_summary=review_summary,
                evidence_binding=binding,
                tests_verified=True,
                frozen_verified=True,
                previous_summary_available=True,
                previous_summary_trusted=True,
                runtime_verification=None,
            ):
                pass
            self.assertEqual(len(issued), 1)
            self.assertEqual(status(issued[0]), "NOT_ACTIVE")
            self.assertEqual(count(), 0)

    def test_acceptance_transaction_status_failure_cannot_skip_revoke(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        candidate, review_summary = self._gate_review_summary()
        binding = {"test": "status-failure-normal-exit"}
        (
            issue,
            consume,
            revoke,
            owner_revoke,
            status,
            purge,
            count,
        ) = _make_acceptance_prerequisite_authority(
            max_records=4,
            terminal_retention_seconds=60,
        )
        issued = []

        def capture_issue(**kwargs):
            proof = issue(**kwargs)
            issued.append(proof)
            return proof

        with patch.object(
            evidence_module,
            "_issue_acceptance_prerequisite_proof",
            side_effect=capture_issue,
        ), patch.object(
            evidence_module,
            "_consume_acceptance_prerequisite_proof",
            consume,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_proof",
            revoke,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_owner",
            owner_revoke,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_status",
            side_effect=EvidenceError("injected status clock failure"),
        ), patch.object(
            evidence_module,
            "_purge_acceptance_prerequisite_proofs",
            purge,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_record_count",
            count,
        ):
            with evidence_module._acceptance_prerequisite_transaction(
                candidate_identity=candidate,
                review_summary=review_summary,
                evidence_binding=binding,
                tests_verified=True,
                frozen_verified=True,
                previous_summary_available=True,
                previous_summary_trusted=True,
                runtime_verification=None,
            ):
                pass
            self.assertEqual(len(issued), 1)
            self.assertEqual(status(issued[0]), "REVOKED")

    def test_acceptance_owner_token_is_a_bearer_capability(self) -> None:
        candidate, review_summary = self._gate_review_summary()
        (
            issue,
            _consume,
            _revoke,
            owner_revoke,
            status,
            _purge,
            _count,
        ) = _make_acceptance_prerequisite_authority(
            max_records=4,
            terminal_retention_seconds=60,
        )
        owner = _new_acceptance_prerequisite_owner()
        proof = issue(
            candidate_identity=candidate,
            review_summary=review_summary,
            evidence_binding={"test": "bearer-owner"},
            owner=owner,
            tests_verified=True,
            frozen_verified=True,
            previous_summary_available=True,
            previous_summary_trusted=True,
            runtime_verification=None,
        )
        copied_owner = type(owner)(owner.token)
        self.assertEqual(
            owner_revoke(copied_owner),
            ("REVOKED", None),
        )
        self.assertEqual(status(proof), "REVOKED")

    def test_diagnostic_note_failure_preserves_original_exception(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        class NoteFailure(RuntimeError):
            def add_note(self, note):
                raise RuntimeError("injected note failure")

        original = NoteFailure("original business failure")
        evidence_module._append_exception_note(
            original,
            "cleanup diagnostic",
        )
        self.assertEqual(str(original), "original business failure")
        self.assertEqual(
            original.diagnostic_notes,
            ["cleanup diagnostic"],
        )

    def test_acceptance_transaction_preserves_cleanup_failure(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        candidate, review_summary = self._gate_review_summary()
        binding = {"test": "cleanup-failure"}
        clock_value = [10.0]

        def clock():
            value = clock_value[0]
            if value is None:
                raise EvidenceError("injected cleanup failure")
            return value

        (
            issue,
            consume,
            revoke,
            owner_revoke,
            status,
            purge,
            count,
        ) = _make_acceptance_prerequisite_authority(
            clock=clock,
            max_records=4,
            terminal_retention_seconds=60,
        )
        issued = []

        def capture_issue(**kwargs):
            proof = issue(**kwargs)
            issued.append(proof)
            return proof

        with patch.object(
            evidence_module,
            "_issue_acceptance_prerequisite_proof",
            side_effect=capture_issue,
        ), patch.object(
            evidence_module,
            "_consume_acceptance_prerequisite_proof",
            consume,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_proof",
            revoke,
        ), patch.object(
            evidence_module,
            "_revoke_acceptance_prerequisite_owner",
            owner_revoke,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_status",
            status,
        ), patch.object(
            evidence_module,
            "_purge_acceptance_prerequisite_proofs",
            purge,
        ), patch.object(
            evidence_module,
            "_acceptance_prerequisite_record_count",
            count,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected transaction failure",
            ) as caught:
                with evidence_module._acceptance_prerequisite_transaction(
                    candidate_identity=candidate,
                    review_summary=review_summary,
                    evidence_binding=binding,
                    tests_verified=True,
                    frozen_verified=True,
                    previous_summary_available=True,
                    previous_summary_trusted=True,
                    runtime_verification=None,
                ):
                    clock_value[0] = None
                    raise RuntimeError("injected transaction failure")
            self.assertEqual(len(issued), 1)
            clock_value[0] = 10.0
            self.assertEqual(status(issued[0]), "REVOKED")
            self.assertIn(
                "cleanup is incomplete",
                "\n".join(caught.exception.__notes__),
            )

    def test_acceptance_proof_concurrent_consume_has_one_winner(self) -> None:
        candidate, review_summary = self._gate_review_summary()
        binding = {"test": "concurrent"}
        proof = self._acceptance_prerequisites(
            candidate,
            review_summary,
            evidence_binding=binding,
            runtime_verification={
                "status": "VERIFIED",
                "scopes": ["browser"],
                "reason": "candidate-bound browser evidence",
                "valid_evidence_count": 1,
            },
        )
        barrier = threading.Barrier(2)
        results: list[str] = []
        results_lock = threading.Lock()

        def consume() -> None:
            barrier.wait(timeout=5)
            with TrustedEvidenceRoot(
                self.root,
                "concurrent acceptance root",
            ) as root:
                try:
                    gate = _evaluate_acceptance_gate_core(
                        candidate_identity=candidate,
                        trusted_repository_root=root,
                        review_summary=review_summary,
                        review_binding=None,
                        acceptance_policy=(
                            "target_head_review_required"
                        ),
                        acceptance_evidence_binding=binding,
                        acceptance_prerequisites=proof,
                    )
                    outcome = gate["acceptance_status"]
                except EvidenceError as exc:
                    outcome = f"error:{exc}"
            with results_lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=consume),
            threading.Thread(target=consume),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(results), 2)
        accepted = [
            item
            for item in results
            if item == ACCEPTED_FOR_MERGE_REVIEW
        ]
        consumed_errors = [
            item for item in results if "already consumed" in item
        ]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(consumed_errors), 1)

    def test_different_acceptance_proofs_progress_concurrently(self) -> None:
        candidate, review_summary = self._gate_review_summary()
        bindings = [{"test": "parallel-a"}, {"test": "parallel-b"}]
        proofs = [
            self._acceptance_prerequisites(
                candidate,
                review_summary,
                evidence_binding=binding,
                runtime_verification={
                    "status": "VERIFIED",
                    "scopes": ["browser"],
                    "reason": "candidate-bound browser evidence",
                    "valid_evidence_count": 1,
                },
            )
            for binding in bindings
        ]
        barrier = threading.Barrier(2)
        results: list[str] = []
        results_lock = threading.Lock()

        def consume(proof, binding) -> None:
            barrier.wait(timeout=5)
            with TrustedEvidenceRoot(
                self.root,
                "parallel acceptance root",
            ) as root:
                gate = _evaluate_acceptance_gate_core(
                    candidate_identity=candidate,
                    trusted_repository_root=root,
                    review_summary=review_summary,
                    review_binding=None,
                    acceptance_policy="target_head_review_required",
                    acceptance_evidence_binding=binding,
                    acceptance_prerequisites=proof,
                )
            with results_lock:
                results.append(gate["acceptance_status"])

        threads = [
            threading.Thread(target=consume, args=(proof, binding))
            for proof, binding in zip(proofs, bindings)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(
            results,
            [ACCEPTED_FOR_MERGE_REVIEW, ACCEPTED_FOR_MERGE_REVIEW],
        )

    def test_stateful_review_mapping_cannot_split_validation_and_decision(
        self,
    ) -> None:
        class StatefulReview(dict):
            def __init__(self, clear: dict, request_changes: dict) -> None:
                super().__init__(request_changes)
                self.clear = clear
                self.calls = 0

            def items(self):
                self.calls += 1
                if self.calls == 1:
                    return self.clear.items()
                return super().items()

        candidate, clear_summary = self._gate_review_summary()
        self.fixture.complete_reviews(code_status="REQUEST_CHANGES")
        request_summary = validate_review_manifest(
            self.root,
            self.fixture.complete_reviews(code_status="REQUEST_CHANGES"),
            expected_code_fingerprint=candidate["code_fingerprint"],
            changed_candidate_paths=[
                item["path"] for item in candidate["files"]
            ],
        )
        stateful = StatefulReview(clear_summary, request_summary)
        proof = self._acceptance_prerequisites(candidate, clear_summary)
        with self.assertRaisesRegex(
            EvidenceError,
            "different review evidence|does not match independently",
        ):
            _evaluate_acceptance_gate_core(
                candidate_identity=candidate,
                trusted_repository_root=self.trusted_repository_root,
                review_summary=stateful,
                review_binding=None,
                acceptance_policy="target_head_review_required",
                acceptance_evidence_binding={"test": True},
                acceptance_prerequisites=proof,
            )

    def test_review_evidence_deleted_after_initial_validation_blocks_summary(
        self,
    ) -> None:
        self.fixture.complete_reviews()
        original = validate_review_manifest
        calls = 0

        def validate_then_delete(*args, **kwargs):
            nonlocal calls
            validated = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                for key in ("code_review", "architecture_review"):
                    for field in ("report", "result"):
                        (self.root / validated[key][field]).unlink()
            return validated

        with patch(
            "tests.task_control_center_evidence.validate_review_manifest",
            side_effect=validate_then_delete,
        ), self.assertRaisesRegex(
            EvidenceError,
            "missing|unreadable|does not match",
        ):
            self._build()
        self.assertGreaterEqual(calls, 1)

    def test_binding_unverified_scopes_reach_final_gate(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            bundle = _validate_bundle(self.fixture, self.root, bundle_path)
            expected_scopes = set(
                bundle["review_binding"]["unverified_scope"]
            )
            self.assertTrue(expected_scopes)
            gate = self._evaluate_bundle_gate(bundle)
            self.assertEqual(
                gate["content_equivalence_binding"],
                "CONTENT_EQUIVALENT_CLEAR",
            )
            self.assertTrue(
                expected_scopes.issubset(set(gate["unverified_scopes"]))
            )
            self.assertIn(
                "review_binding_unverified_scope",
                gate["blocking_reasons"],
            )
            self.assertEqual(gate["acceptance_status"], NOT_VERIFIED)
            self.assertFalse(gate["merge_ready"])

    def test_content_equivalent_gate_requires_complete_normalized_summary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            bundle = _validate_bundle(self.fixture, self.root, bundle_path)
            original = bundle["review_summary"]
            for case in ("missing_report", "cross_lane"):
                with self.subTest(case=case):
                    forged = json.loads(canonical_json(original))
                    key = "code_review"
                    field = "report" if case == "missing_report" else "result"
                    if case == "missing_report":
                        forged[key][field] = (
                            "artifacts/audits/task-control-center-v1/"
                            "missing-report.md"
                        )
                        forged[key]["report_sha256"] = "9" * 64
                    else:
                        forged[key]["result"] = forged[
                            "architecture_review"
                        ]["result"]
                        forged[key]["result_sha256"] = forged[
                            "architecture_review"
                        ]["result_sha256"]
                    with self.assertRaisesRegex(
                        EvidenceError,
                        "does not match binding-derived evidence",
                    ):
                        self._evaluate_bundle_gate(
                            {
                                **bundle,
                                "review_summary": forged,
                            }
                        )

    def test_content_equivalent_base_head_must_match_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            bundle = _validate_bundle(self.fixture, self.root, bundle_path)
            forged_binding = json.loads(
                canonical_json(bundle["review_binding"])
            )
            forged_binding["base_head"] = "2" * 40
            with patch(
                "tests.task_control_center_evidence.validate_review_binding",
                return_value=forged_binding,
            ), self.assertRaisesRegex(
                EvidenceError,
                "base HEAD disagrees with candidate",
            ):
                self._evaluate_bundle_gate(
                    bundle,
                    review_binding=forged_binding,
                )

    def test_legacy_lane_identity_is_bound_to_historical_code(self) -> None:
        def legacy_previous() -> dict:
            previous = json.loads(
                self.fixture.previous_path.read_text(encoding="utf-8")
            )
            previous["schema_version"] = "task-control-center-evidence-v1"
            previous["candidate_acceptance"] = "INDEPENDENT_REVIEW_CLEAR"
            for field in (
                "validation_input",
                "candidate_identity",
                "evidence_bundle",
                "review_binding",
                "acceptance_gate",
            ):
                previous.pop(field)
            previous["review_status"].pop("review_gate", None)
            return previous

        cases = {
            "head": lambda previous: previous["special_review"][
                "code_review"
            ].__setitem__("target_head", "1" * 40),
            "fingerprint": lambda previous: previous["special_review"][
                "architecture_review"
            ].__setitem__("target_code_fingerprint", "1" * 64),
            "repository": lambda previous: previous["special_review"][
                "code_review"
            ].__setitem__("target_repository", "/other/repository"),
            "missing_repository": lambda previous: previous["special_review"][
                "architecture_review"
            ].pop("target_repository"),
        }
        base_legacy = legacy_previous()
        for name, mutate in cases.items():
            with self.subTest(name=name):
                previous = json.loads(canonical_json(base_legacy))
                mutate(previous)
                _write_json(self.fixture.previous_path, previous)
                self.fixture.metadata["previous_summary"]["sha256"] = (
                    sha256_file(self.fixture.previous_path)
                )
                _write_json(
                    self.fixture.metadata_path,
                    self.fixture.metadata,
                )
                with self.assertRaisesRegex(
                    EvidenceError,
                    "target HEAD|target fingerprint|target repository|"
                    "repository disagrees with trusted context",
                ):
                    self._build()

        with self.assertRaisesRegex(
            EvidenceError,
            "no trusted candidate identity",
        ):
            _validate_previous_summary(
                base_legacy,
                "legacy",
            )

    def test_v2_history_identity_requires_external_trusted_context(self) -> None:
        previous = json.loads(
            self.fixture.previous_path.read_text(encoding="utf-8")
        )
        previous["candidate_identity"]["repository"] = (
            "/other/repository"
        )
        previous["candidate_identity"]["target_head"] = "2" * 40
        previous["candidate_identity"]["reviewed_head"] = "2" * 40
        previous["candidate_identity"]["code_fingerprint"] = "3" * 64
        previous["code"]["head"] = "2" * 40
        previous["code"]["code_fingerprint"] = "3" * 64
        for key in ("code_review", "architecture_review"):
            previous["special_review"][key]["target_repository"] = (
                "/other/repository"
            )
            previous["special_review"][key]["target_head"] = "2" * 40
            previous["special_review"][key][
                "target_code_fingerprint"
            ] = "3" * 64
        _write_json(self.fixture.previous_path, previous)
        self.fixture.metadata["previous_summary"]["sha256"] = sha256_file(
            self.fixture.previous_path
        )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaisesRegex(
            EvidenceError,
            "trusted context",
        ):
            self._build()

        with self.assertRaisesRegex(
            EvidenceError,
            "no external trusted candidate snapshot|"
            "no external trusted candidate identity",
        ):
            _validate_previous_summary(
                previous,
                "previous summary",
                trusted_repository=str(self.root),
                require_trusted_identity=True,
            )
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ), self.assertRaisesRegex(
            EvidenceError,
            "no external trusted candidate snapshot|"
            "no external trusted candidate identity",
        ):
            build_summary(
                self.root,
                self.fixture.metadata_path,
                replacement_reason="missing identity context",
                trusted_previous_summary_sha256=sha256_file(
                    self.fixture.previous_path
                ),
            )

        fake_snapshot_path = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "fake-trusted-candidate.json"
        )
        _write_json(
            fake_snapshot_path,
            previous["candidate_identity"],
        )
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ), self.assertRaisesRegex(
            EvidenceError,
            "target HEAD|trusted repository|repository does not match root",
        ):
            build_summary(
                self.root,
                self.fixture.metadata_path,
                replacement_reason="fake trusted snapshot",
                trusted_previous_summary_sha256=sha256_file(
                    self.fixture.previous_path
                ),
                trusted_previous_candidate_snapshot_path=(
                    fake_snapshot_path
                ),
            )

    def test_self_consistent_forged_predecessor_with_valid_oid_is_rejected(
        self,
    ) -> None:
        candidate = build_candidate_snapshot(self.root)
        forged = json.loads(canonical_json(candidate))
        forged["code_fingerprint"] = "b" * 64
        forged["status_sha256"] = "a" * 64
        forged["files"][0]["sha256"] = "c" * 64
        forged["file_manifest_sha256"] = file_manifest_sha256(
            forged["files"]
        )
        _write_json(
            self.fixture.trusted_previous_candidate_snapshot_path,
            forged,
        )

        previous = json.loads(
            self.fixture.previous_path.read_text(encoding="utf-8")
        )
        for schema in ("task-control-center-evidence-v2", "legacy"):
            with self.subTest(schema=schema):
                versioned = json.loads(canonical_json(previous))
                if schema == "legacy":
                    versioned[
                        "schema_version"
                    ] = "task-control-center-evidence-v1"
                    versioned[
                        "candidate_acceptance"
                    ] = "INDEPENDENT_REVIEW_CLEAR"
                    for field in (
                        "validation_input",
                        "candidate_identity",
                        "evidence_bundle",
                        "review_binding",
                        "acceptance_gate",
                    ):
                        versioned.pop(field, None)
                    versioned["review_status"].pop("review_gate", None)
                else:
                    versioned["candidate_identity"] = forged
                    versioned["code"][
                        "code_fingerprint"
                    ] = forged["code_fingerprint"]
                    versioned["code"]["status_sha256"] = forged[
                        "status_sha256"
                    ]
                _write_json(self.fixture.previous_path, versioned)
                self.fixture.metadata["previous_summary"][
                    "sha256"
                ] = sha256_file(self.fixture.previous_path)
                _write_json(
                    self.fixture.metadata_path,
                    self.fixture.metadata,
                )
                with self.assertRaisesRegex(
                    EvidenceError,
                    "trusted previous candidate snapshot",
                ):
                    self._build()

        cli_output = self.fixture.output_path.with_name(
            "reproduced-summary.json"
        )
        cli_exit = main(
            [
                "summarize",
                "--root",
                str(self.root),
                "--run-metadata",
                str(self.fixture.metadata_path),
                "--output",
                str(cli_output),
                "--replace-reason",
                "forged predecessor",
                "--trusted-previous-sha256",
                sha256_file(self.fixture.previous_path),
                "--trusted-previous-candidate-snapshot",
                str(self.fixture.trusted_previous_candidate_snapshot_path),
            ]
        )
        self.assertEqual(cli_exit, 2)
        self.assertFalse(cli_output.exists())

    def test_summary_currentness_rejects_forged_predecessor_snapshot(
        self,
    ) -> None:
        summary = self._build()
        candidate = build_candidate_snapshot(self.root)
        forged = json.loads(canonical_json(candidate))
        forged["code_fingerprint"] = "b" * 64
        forged["status_sha256"] = "a" * 64
        forged["files"][0]["sha256"] = "c" * 64
        forged["file_manifest_sha256"] = file_manifest_sha256(
            forged["files"]
        )
        _write_json(
            self.fixture.trusted_previous_candidate_snapshot_path,
            forged,
        )
        summary["validation_input"][
            "trusted_previous_candidate_snapshot_path"
        ] = str(self.fixture.trusted_previous_candidate_snapshot_path)
        summary["validation_input"][
            "trusted_previous_candidate_snapshot_sha256"
        ] = sha256_file(
            self.fixture.trusted_previous_candidate_snapshot_path
        )
        output_path = self.fixture.output_path.with_name(
            "reproduced-summary.json"
        )
        _write_json(output_path, summary)
        with self.assertRaisesRegex(
            EvidenceError,
            "trusted previous candidate snapshot",
        ):
            validate_summary_is_current(
                self.root,
                output_path,
                expected_replacement_reason="test replacement",
            )

    def test_candidate_acceptance_is_derived_from_resolution(self) -> None:
        candidate, review_summary = self._gate_review_summary()
        forged = json.loads(canonical_json(review_summary))
        forged["candidate_acceptance"] = (
            "CONTENT_EQUIVALENT_REVIEW_CLEAR"
        )
        with self.assertRaisesRegex(
            EvidenceError,
                "candidate acceptance disagrees with direct review resolution|"
                "does not match independently validated evidence",
        ):
            self._evaluate_gate(candidate, forged)

    def test_valid_previous_summary_remains_usable(self) -> None:
        summary = self._build()
        self.assertEqual(
            summary["constraint_incidents"],
            ["historical docker test invocation"],
        )
        self.assertTrue(summary["evidence_history"])

    def test_bundle_previous_summary_cannot_be_upgraded_by_self_hash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            payload = json.loads(bundle_path.read_text(encoding="utf-8"))
            previous_relative = payload["previous_summary"]["path"]
            previous_path = bundle_path.parent / previous_relative
            _write_json(
                previous_path,
                {"constraint_incidents": ["fabricated bundle history"]},
            )
            previous_sha256 = sha256_file(previous_path)
            payload["previous_summary"]["sha256"] = previous_sha256
            for item in payload["files"]:
                if item["path"] == previous_relative:
                    item["sha256"] = previous_sha256
            _write_json(bundle_path, payload)
            self._configure_bundle_metadata(bundle_path)
            with self.assertRaisesRegex(
                EvidenceError,
                "previous summary|unknown fields|unsupported",
            ):
                self._build()

    def test_previous_summary_requires_independent_trust_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            self._configure_bundle_metadata(bundle_path)
            with patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                self.fixture.frozen,
            ):
                untrusted = build_summary(
                    self.root,
                    self.fixture.metadata_path,
                    replacement_reason="untrusted history",
                    evidence_bundle_path=bundle_path,
                    acceptance_policy="content_equivalence_allowed",
                )
                trusted = build_summary(
                    self.root,
                    self.fixture.metadata_path,
                    replacement_reason="untrusted history",
                    evidence_bundle_path=bundle_path,
                    acceptance_policy="content_equivalence_allowed",
                    trusted_previous_summary_sha256=sha256_file(
                        self.fixture.previous_path
                    ),
                    trusted_previous_candidate_snapshot_path=(
                        self.fixture.trusted_previous_candidate_snapshot_path
                    ),
                )
        self.assertFalse(untrusted["review_status"]["merge_ready"])
        self.assertEqual(
            untrusted["acceptance_gate"]["previous_summary"],
            "AVAILABLE_UNTRUSTED",
        )
        self.assertIn(
            "previous_summary_not_trusted",
            untrusted["acceptance_gate"]["blocking_reasons"],
        )
        self.assertEqual(
            trusted["acceptance_gate"]["previous_summary"],
            "AVAILABLE",
        )
        self.assertFalse(trusted["review_status"]["merge_ready"])
        self.assertEqual(
            trusted["validation_input"][
                "trusted_previous_summary_sha256"
            ],
            sha256_file(self.fixture.previous_path),
        )

    def test_wrong_previous_summary_trust_anchor_fails(self) -> None:
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ), self.assertRaisesRegex(
            EvidenceError,
            "trusted digest",
        ):
            build_summary(
                self.root,
                self.fixture.metadata_path,
                replacement_reason="wrong anchor",
                trusted_previous_summary_sha256="0" * 64,
                trusted_previous_candidate_snapshot_path=(
                    self.fixture.trusted_previous_candidate_snapshot_path
                ),
            )

    def test_accepted_summary_round_trips_to_next_history(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle",
                acceptance_policy="target_head_review_required",
                direct_target_review=True,
            )
            self._configure_bundle_metadata(bundle_path)
            first_anchor = sha256_file(self.fixture.previous_path)
            with patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                self.fixture.frozen,
            ):
                first = build_summary(
                    self.root,
                    self.fixture.metadata_path,
                    replacement_reason="first accepted summary",
                    evidence_bundle_path=bundle_path,
                    acceptance_policy="target_head_review_required",
                    trusted_previous_summary_sha256=first_anchor,
                    trusted_previous_candidate_snapshot_path=(
                        self.fixture.trusted_previous_candidate_snapshot_path
                    ),
                )
                self.assertTrue(first["review_status"]["merge_ready"])
                payload = json.loads(bundle_path.read_text(encoding="utf-8"))
                previous_relative = payload["previous_summary"]["path"]
                next_previous = bundle_path.parent / previous_relative
                next_previous.write_text(
                    canonical_json(first),
                    encoding="utf-8",
                )
                next_anchor = sha256_file(next_previous)
                payload["previous_summary"]["sha256"] = next_anchor
                for item in payload["files"]:
                    if item["path"] == previous_relative:
                        item["sha256"] = next_anchor
                _write_json(bundle_path, payload)
                self._configure_bundle_metadata(bundle_path)
                next_snapshot_path = (
                    bundle_path.parent.parent
                    / "trusted-first-candidate.json"
                )
                _write_json(
                    next_snapshot_path,
                    first["candidate_identity"],
                )
                second = build_summary(
                    self.root,
                    self.fixture.metadata_path,
                    replacement_reason="second accepted summary",
                    evidence_bundle_path=bundle_path,
                    acceptance_policy="target_head_review_required",
                    trusted_previous_summary_sha256=next_anchor,
                    trusted_previous_candidate_snapshot_path=(
                        next_snapshot_path
                    ),
                )
        self.assertTrue(second["review_status"]["merge_ready"])
        self.assertIn(
            {
                "previous_sha256": next_anchor,
                "replacement_reason": "second accepted summary",
            },
            second["evidence_history"],
        )
        self.assertEqual(
            second["superseded_summary"]["previous_sha256"],
            next_anchor,
        )

    def test_target_head_accepted_summary_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle",
                acceptance_policy="target_head_review_required",
                direct_target_review=True,
            )
            self._configure_bundle_metadata(bundle_path)
            first_anchor = sha256_file(self.fixture.previous_path)
            with patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                self.fixture.frozen,
            ):
                first = build_summary(
                    self.root,
                    self.fixture.metadata_path,
                    replacement_reason="target review accepted",
                    evidence_bundle_path=bundle_path,
                    acceptance_policy="target_head_review_required",
                    trusted_previous_summary_sha256=first_anchor,
                    trusted_previous_candidate_snapshot_path=(
                        self.fixture.trusted_previous_candidate_snapshot_path
                    ),
                )
                self.assertEqual(
                    first["acceptance_gate"][
                        "current_target_review_resolution"
                    ],
                    "DIRECT_CLEAR",
                )
                self.assertEqual(
                    first["acceptance_gate"]["unverified_scopes"],
                    [],
                )
                self.assertTrue(first["review_status"]["merge_ready"])

                payload = json.loads(bundle_path.read_text(encoding="utf-8"))
                previous_relative = payload["previous_summary"]["path"]
                next_previous = bundle_path.parent / previous_relative
                next_previous.write_text(
                    canonical_json(first),
                    encoding="utf-8",
                )
                next_anchor = sha256_file(next_previous)
                payload["previous_summary"]["sha256"] = next_anchor
                for item in payload["files"]:
                    if item["path"] == previous_relative:
                        item["sha256"] = next_anchor
                _write_json(bundle_path, payload)
                self._configure_bundle_metadata(bundle_path)
                next_snapshot_path = (
                    bundle_path.parent.parent
                    / "trusted-target-candidate.json"
                )
                _write_json(
                    next_snapshot_path,
                    first["candidate_identity"],
                )
                second = build_summary(
                    self.root,
                    self.fixture.metadata_path,
                    replacement_reason="target review round trip",
                    evidence_bundle_path=bundle_path,
                    acceptance_policy="target_head_review_required",
                    trusted_previous_summary_sha256=next_anchor,
                    trusted_previous_candidate_snapshot_path=(
                        next_snapshot_path
                    ),
                )
        self.assertTrue(second["review_status"]["merge_ready"])
        self.assertEqual(
            second["validation_input"]["trusted_previous_summary_sha256"],
            next_anchor,
        )

    def test_complete_legacy_previous_summary_remains_supported(self) -> None:
        previous = json.loads(
            self.fixture.previous_path.read_text(encoding="utf-8")
        )
        previous["schema_version"] = "task-control-center-evidence-v1"
        previous["candidate_acceptance"] = "INDEPENDENT_REVIEW_CLEAR"
        for field in (
            "validation_input",
            "candidate_identity",
            "evidence_bundle",
            "review_binding",
            "acceptance_gate",
        ):
            previous.pop(field)
        previous["review_status"].pop("review_gate", None)
        _write_json(self.fixture.previous_path, previous)
        self.fixture.metadata["previous_summary"]["sha256"] = sha256_file(
            self.fixture.previous_path
        )
        _write_json(
            self.fixture.metadata_path,
            self.fixture.metadata,
        )
        summary = self._build()
        self.assertEqual(
            summary["constraint_incidents"],
            ["historical docker test invocation"],
        )

    def test_complete_content_equivalent_previous_summary_is_supported(
        self,
    ) -> None:
        previous = json.loads(
            self.fixture.previous_path.read_text(encoding="utf-8")
        )
        previous["acceptance_gate"]["acceptance_policy"] = (
            "content_equivalence_allowed"
        )
        previous["acceptance_gate"]["content_equivalence_binding"] = (
            "CONTENT_EQUIVALENT_CLEAR"
        )
        previous["acceptance_gate"]["current_target_independent_review"] = (
            "NOT_AVAILABLE"
        )
        previous["acceptance_gate"]["current_target_review_resolution"] = (
            "CONTENT_EQUIVALENT_CLEAR"
        )
        previous["acceptance_gate"]["review_gate"] = (
            "CONTENT_EQUIVALENT_CLEAR"
        )
        previous["review_status"]["review_gate"] = (
            "CONTENT_EQUIVALENT_CLEAR"
        )
        previous["candidate_identity"]["reviewed_head"] = (
            previous["candidate_identity"]["target_head"]
        )
        _write_json(
            self.fixture.trusted_previous_candidate_snapshot_path,
            previous["candidate_identity"],
        )
        previous["special_review"]["candidate_acceptance"] = (
            "CONTENT_EQUIVALENT_REVIEW_CLEAR"
        )
        previous["review_binding"] = {
            "path": "history-binding.json",
            "sha256": "5" * 64,
            "scope": "content_equivalence_only",
            "binding_result": "CONTENT_EQUIVALENT_REVIEW_CLEAR",
            "evidence_status": "CONTENT_EQUIVALENT_REVIEW_CLEAR",
            "unverified_scope": [],
            "reviewed_head": (
                previous["candidate_identity"]["target_head"]
            ),
            "target_head": previous["candidate_identity"]["target_head"],
            "acceptance_supported": False,
            "merge_ready": False,
        }
        _write_json(self.fixture.previous_path, previous)
        self.fixture.metadata["previous_summary"]["sha256"] = sha256_file(
            self.fixture.previous_path
        )
        _write_json(
            self.fixture.metadata_path,
            self.fixture.metadata,
        )
        self._build()
        previous["review_binding"]["unverified_scope"] = [
            "browser_interaction"
        ]
        _write_json(self.fixture.previous_path, previous)
        self.fixture.metadata["previous_summary"]["sha256"] = sha256_file(
            self.fixture.previous_path
        )
        _write_json(
            self.fixture.metadata_path,
            self.fixture.metadata,
        )
        with self.assertRaisesRegex(
            EvidenceError,
            "unverified scope is not propagated",
        ):
            self._build()

    def test_test_discovery_manifest_is_recomputed_and_strict(self) -> None:
        cases = (
            "missing_schema",
            "unsupported_schema",
            "omitted_test",
            "fake_filtered",
            "overlap",
            "duplicate",
            "skipped_outside_selected",
            "duplicate_skipped",
            "extra_test",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                result_path = (
                    root
                    / fixture.metadata["tests"]["system_python"]["result"]
                )
                result = json.loads(result_path.read_text(encoding="utf-8"))
                discovery = result["discovery"]
                selected = discovery["selected_tests"]
                filtered = discovery["filtered_tests"]
                if case == "missing_schema":
                    result.pop("schema_version")
                elif case == "unsupported_schema":
                    result["schema_version"] = "task-control-center-test-result-v1"
                elif case == "omitted_test":
                    removed = selected.pop(0)
                    discovery["discovered_tests"].remove(removed)
                    result["counts"]["selected"] -= 1
                    result["counts"]["discovered"] -= 1
                    result["counts"]["passed"] -= 1
                elif case == "fake_filtered":
                    moved = selected.pop(0)
                    filtered.append(moved)
                    filtered.sort()
                    result["counts"]["selected"] -= 1
                    result["counts"]["filtered"] += 1
                elif case == "overlap":
                    filtered.append(selected[0])
                    filtered.sort()
                    result["counts"]["filtered"] += 1
                    discovery["discovered_tests"].append(selected[0])
                    discovery["discovered_tests"].sort()
                elif case == "duplicate":
                    selected.append(selected[0])
                    selected.sort()
                    result["counts"]["selected"] += 1
                    result["counts"]["passed"] += 1
                elif case == "skipped_outside_selected":
                    result["counts"]["passed"] -= 1
                    result["counts"]["skipped"] += 1
                    result["skipped_tests"] = [
                        {
                            "test": "test_missing.MissingTests.test_missing",
                            "reason": "synthetic missing test",
                        }
                    ]
                elif case == "duplicate_skipped":
                    result["counts"]["passed"] -= 2
                    result["counts"]["skipped"] += 2
                    result["skipped_tests"] = [
                        {"test": selected[0], "reason": "first"},
                        {"test": selected[0], "reason": "second"},
                    ]
                else:
                    extra = "test_unrelated.UnrelatedTests.test_extra"
                    selected.append(extra)
                    selected.sort()
                    discovery["discovered_tests"].append(extra)
                    discovery["discovered_tests"].sort()
                    result["counts"]["selected"] += 1
                    result["counts"]["discovered"] += 1
                    result["counts"]["passed"] += 1
                if "schema_version" in result and case not in {
                    "missing_schema",
                    "unsupported_schema",
                }:
                    result["discovery_sha256"] = sha256_bytes(
                        canonical_json(discovery).encode("utf-8")
                    )
                _write_json(result_path, result)
                fixture.metadata["tests"]["system_python"][
                    "result_sha256"
                ] = sha256_file(result_path)
                _write_json(fixture.metadata_path, fixture.metadata)
                with patch(
                    "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                    fixture.frozen,
                ), self.assertRaises(EvidenceError):
                    build_summary(
                        root,
                        fixture.metadata_path,
                        replacement_reason="discovery tamper",
                    )

    def test_test_runner_emits_current_discovery_manifest(self) -> None:
        result_path = self.root / "artifacts" / "result.json"
        log_path = self.root / "artifacts" / "result.log"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(PROJECT_ROOT / "tests" / "task_control_center_evidence.py"),
                "_run-tests",
                "--root",
                str(self.root),
                "--result",
                str(result_path),
                "--log",
                str(log_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        current = _current_test_discovery(self.root)
        self.assertEqual(result["schema_version"], TEST_RESULT_SCHEMA_VERSION)
        self.assertEqual(result["discovery"], current)
        self.assertEqual(
            result["discovery_sha256"],
            sha256_bytes(canonical_json(current).encode("utf-8")),
        )
        self.assertTrue(result["successful"])
        self.assertIn("OK", log_path.read_text(encoding="utf-8"))

    def test_runner_records_the_exact_executed_dynamic_collection(self) -> None:
        module_name = "test_a26_dynamic_loader"
        calls_path = self.root / "tests" / "a26-load-calls.txt"
        executed_path = self.root / "tests" / "a26-executed.txt"
        (self.root / "tests" / f"{module_name}.py").write_text(
            "import unittest\n"
            "from pathlib import Path\n"
            "here = Path(__file__).with_name\n"
            "def record(path, value):\n"
            "    with path.open('a', encoding='utf-8') as handle:\n"
            "        handle.write(value + '\\n')\n"
            "class DynamicTests(unittest.TestCase):\n"
            "    def test_first_variant(self):\n"
            f"        record(here('{executed_path.name}'), 'first')\n"
            "    def test_second_variant(self):\n"
            f"        record(here('{executed_path.name}'), 'second')\n"
            "def load_tests(loader, tests, pattern):\n"
            f"    path = here('{calls_path.name}')\n"
            "    record(path, 'load')\n"
            "    count = len(path.read_text(encoding='utf-8').splitlines())\n"
            "    name = ('test_first_variant'\n"
            "            if count == 1 else 'test_second_variant')\n"
            "    return unittest.TestSuite([DynamicTests(name)])\n",
            encoding="utf-8",
        )
        result_path = self.root / "artifacts" / "dynamic-result.json"
        log_path = self.root / "artifacts" / "dynamic.log"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(PROJECT_ROOT / "tests" / "task_control_center_evidence.py"),
                "_run-tests",
                "--root",
                str(self.root),
                "--result",
                str(result_path),
                "--log",
                str(log_path),
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(calls_path.read_text(encoding="utf-8").splitlines(), ["load"])
        self.assertEqual(
            executed_path.read_text(encoding="utf-8").splitlines(),
            ["first"],
        )
        dynamic_ids = [
            item
            for item in payload["discovery"]["selected_tests"]
            if "DynamicTests" in item
        ]
        executed_ids = [
            item["test"]
            for item in payload["executed"]["results"]
            if "DynamicTests" in item["test"]
        ]
        self.assertEqual(
            dynamic_ids,
            [f"{module_name}.DynamicTests.test_first_variant"],
        )
        self.assertEqual(executed_ids, dynamic_ids)

    def test_candidate_cannot_replace_unittest_runner(self) -> None:
        marker = self.root / "tests" / "runner-method-executed.txt"
        (self.root / "tests" / "test_a28_runner_forgery.py").write_text(
            "import unittest\n"
            "from pathlib import Path\n"
            "MARKER = Path(__file__).with_name('runner-method-executed.txt')\n"
            "def forged_run(self, suite):\n"
            "    result = self._makeResult()\n"
            "    result.startTestRun()\n"
            "    def walk(value):\n"
            "        if isinstance(value, unittest.TestCase):\n"
            "            result.startTest(value)\n"
            "            result.addSuccess(value)\n"
            "            result.stopTest(value)\n"
            "        else:\n"
            "            for child in value:\n"
            "                walk(child)\n"
            "    walk(suite)\n"
            "    result.stopTestRun()\n"
            "    return result\n"
            "unittest.TextTestRunner.run = forged_run\n"
            "class Forged(unittest.TestCase):\n"
            "    def test_never_runs(self):\n"
            "        MARKER.write_text('executed', encoding='utf-8')\n",
            encoding="utf-8",
        )
        result_path = self.root / "artifacts" / "runner-forgery-result.json"
        log_path = self.root / "artifacts" / "runner-forgery.log"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(PROJECT_ROOT / "tests" / "task_control_center_evidence.py"),
                "_run-tests",
                "--root",
                str(self.root),
                "--result",
                str(result_path),
                "--log",
                str(log_path),
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn(
            "modified unittest execution primitives",
            completed.stderr,
        )
        self.assertFalse(marker.exists())
        self.assertFalse(result_path.exists())
        self.assertFalse(log_path.exists())

    def test_evidence_cli_emits_structured_diagnostics(self) -> None:
        import tests.task_control_center_evidence as evidence_module

        def fail_snapshot(*args, **kwargs):
            del args, kwargs
            error = EvidenceError("injected structured failure")
            error.transaction_state = "ABSENT"
            error.residual_paths = ["README.md"]
            error.residual_unknown = False
            error.close_diagnostics = [
                "directory: injected close failure"
            ]
            error.signal_mask_state = "BLOCKED"
            error.secondary_exceptions = [
                {
                    "type": "KeyboardInterrupt",
                    "message": "pending interrupt",
                    "phase": "sigint_mask_restore",
                }
            ]
            error.add_note("injected diagnostic note")
            error.__cause__ = PermissionError("injected cause")
            raise error

        stderr = io.StringIO()
        with patch.object(
            evidence_module,
            "write_candidate_snapshot",
            side_effect=fail_snapshot,
        ), contextlib.redirect_stderr(stderr):
            returncode = main(
                [
                    "--json-errors",
                    "snapshot",
                    "--root",
                    str(self.root),
                    "--output",
                    str(self.root / "snapshot.json"),
                ]
            )
        self.assertEqual(returncode, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(
            payload["schema_version"],
            "task-control-center-error-v1",
        )
        self.assertEqual(payload["transaction_state"], "UNKNOWN")
        self.assertEqual(
            payload["residual"],
            {
                "status": "PRESENT",
                "paths": ["README.md"],
            },
        )
        self.assertEqual(
            payload["close_diagnostics"],
            ["directory: injected close failure"],
        )
        self.assertEqual(payload["signal_mask_state"], "BLOCKED")
        self.assertEqual(
            payload["secondary_exceptions"],
            [
                {
                    "type": "KeyboardInterrupt",
                    "message": "pending interrupt",
                    "phase": "sigint_mask_restore",
                }
            ],
        )
        self.assertEqual(
            payload["primary_exception"],
            {
                "type": "PermissionError",
                "message": "injected cause",
            },
        )
        self.assertIn(
            "injected diagnostic note",
            payload["notes"],
        )

    def test_evidence_cli_serialization_failure_preserves_primary(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        def fail_snapshot(*args, **kwargs):
            del args, kwargs
            raise EvidenceError("injected serialization failure")

        real_dumps = evidence_module.json.dumps
        calls = 0

        def fail_first_dump(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TypeError("injected serializer failure")
            return real_dumps(*args, **kwargs)

        stderr = io.StringIO()
        with patch.object(
            evidence_module,
            "write_candidate_snapshot",
            side_effect=fail_snapshot,
        ), patch.object(
            evidence_module.json,
            "dumps",
            side_effect=fail_first_dump,
        ), contextlib.redirect_stderr(stderr):
            returncode = main(
                [
                    "--json-errors",
                    "snapshot",
                    "--root",
                    str(self.root),
                    "--output",
                    str(self.root / "snapshot.json"),
                ]
            )
        self.assertEqual(returncode, 2)
        self.assertEqual(calls, 1)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(
            payload["error_type"],
            "EvidenceError",
        )
        self.assertEqual(
            payload["message"],
            "injected serialization failure",
        )
        self.assertEqual(
            payload["diagnostic_output_status"],
            "FALLBACK",
        )
        self.assertEqual(payload["transaction_state"], "UNKNOWN")
        self.assertEqual(
            payload["residual"]["status"],
            "UNKNOWN",
        )
        self.assertTrue(payload["residual_unknown"])

    def test_evidence_cli_signal_state_getter_failure_is_unknown(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        class SignalGetterFailure(EvidenceError):
            @property
            def signal_mask_state(self):
                raise RuntimeError("signal state getter failed")

        def fail_snapshot(*args, **kwargs):
            del args, kwargs
            raise SignalGetterFailure("primary diagnostic failure")

        stderr = io.StringIO()
        with patch.object(
            evidence_module,
            "write_candidate_snapshot",
            side_effect=fail_snapshot,
        ), contextlib.redirect_stderr(stderr):
            returncode = main(
                [
                    "--json-errors",
                    "snapshot",
                    "--root",
                    str(self.root),
                    "--output",
                    str(self.root / "snapshot.json"),
                ]
            )
        self.assertEqual(returncode, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["error_type"], "SignalGetterFailure")
        self.assertEqual(payload["message"], "primary diagnostic failure")
        self.assertEqual(payload["signal_mask_state"], "UNKNOWN")
        self.assertTrue(
            any(
                item.startswith("signal_mask_state:")
                for item in payload["diagnostic_errors"]
            )
        )

    def test_evidence_cli_attribute_error_getters_are_diagnosed(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        class AttributeErrorDiagnostics(EvidenceError):
            @property
            def signal_mask_state(self):
                raise AttributeError("signal getter attribute failure")

            @property
            def __cause__(self):
                raise AttributeError("cause getter attribute failure")

            @property
            def __context__(self):
                raise AttributeError("context getter attribute failure")

        def fail_snapshot(*args, **kwargs):
            del args, kwargs
            raise AttributeErrorDiagnostics("primary diagnostic failure")

        stderr = io.StringIO()
        with patch.object(
            evidence_module,
            "write_candidate_snapshot",
            side_effect=fail_snapshot,
        ), contextlib.redirect_stderr(stderr):
            returncode = main(
                [
                    "--json-errors",
                    "snapshot",
                    "--root",
                    str(self.root),
                    "--output",
                    str(self.root / "snapshot.json"),
                ]
            )
        self.assertEqual(returncode, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["signal_mask_state"], "UNKNOWN")
        self.assertEqual(
            payload["primary_exception"]["type"],
            "UNKNOWN",
        )
        self.assertEqual(
            payload["context_exception"]["type"],
            "UNKNOWN",
        )
        for attribute in (
            "signal_mask_state:",
            "__cause__:",
            "__context__:",
        ):
            self.assertTrue(
                any(
                    item.startswith(attribute)
                    for item in payload["diagnostic_errors"]
                )
            )

    def test_evidence_cli_unhashable_string_states_are_unknown(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        class UnhashableState(str):
            __hash__ = None

        error = EvidenceError("primary diagnostic failure")
        error.signal_mask_state = UnhashableState("UNKNOWN")
        error.transaction_state = UnhashableState("COMMITTED")
        error.durability_state = UnhashableState("CONFIRMED")
        error.target_state = UnhashableState("CHANGED")

        def fail_snapshot(*args, **kwargs):
            del args, kwargs
            raise error

        stderr = io.StringIO()
        with patch.object(
            evidence_module,
            "write_candidate_snapshot",
            side_effect=fail_snapshot,
        ), contextlib.redirect_stderr(stderr):
            returncode = main(
                [
                    "--json-errors",
                    "snapshot",
                    "--root",
                    str(self.root),
                    "--output",
                    str(self.root / "snapshot.json"),
                ]
            )
        self.assertEqual(returncode, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["signal_mask_state"], "UNKNOWN")
        self.assertEqual(payload["transaction_state"], "UNKNOWN")
        self.assertEqual(payload["durability_state"], "UNKNOWN")
        self.assertEqual(payload["target_state"], "UNKNOWN")
        for field in (
            "signal_mask_state",
            "transaction_state",
            "durability_state",
            "target_state",
        ):
            self.assertTrue(
                any(
                    item.startswith(f"{field}")
                    for item in payload["diagnostic_errors"]
                )
            )

    def test_evidence_cli_state_getter_failures_are_diagnosed(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        class StateGetterFailure(EvidenceError):
            @property
            def transaction_state(self):
                raise RuntimeError("transaction getter failed")

            @property
            def durability_state(self):
                raise RuntimeError("durability getter failed")

            @property
            def target_state(self):
                raise RuntimeError("target getter failed")

        def fail_snapshot(*args, **kwargs):
            del args, kwargs
            raise StateGetterFailure("primary diagnostic failure")

        stderr = io.StringIO()
        with patch.object(
            evidence_module,
            "write_candidate_snapshot",
            side_effect=fail_snapshot,
        ), contextlib.redirect_stderr(stderr):
            returncode = main(
                [
                    "--json-errors",
                    "snapshot",
                    "--root",
                    str(self.root),
                    "--output",
                    str(self.root / "snapshot.json"),
                ]
            )
        self.assertEqual(returncode, 2)
        payload = json.loads(stderr.getvalue())
        for field in (
            "transaction_state",
            "durability_state",
            "target_state",
        ):
            self.assertEqual(payload[field], "UNKNOWN")
            self.assertTrue(
                any(
                    item.startswith(f"{field}:")
                    for item in payload["diagnostic_errors"]
                )
            )

    def test_evidence_cli_terminal_serialization_is_manual_fallback(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        def fail_snapshot(*args, **kwargs):
            del args, kwargs
            raise EvidenceError("original primary serialization error")

        stderr = io.StringIO()
        with patch.object(
            evidence_module,
            "write_candidate_snapshot",
            side_effect=fail_snapshot,
        ), patch.object(
            evidence_module.json,
            "dumps",
            side_effect=TypeError("dumps failed"),
        ), contextlib.redirect_stderr(stderr):
            returncode = main(
                [
                    "--json-errors",
                    "snapshot",
                    "--root",
                    str(self.root),
                    "--output",
                    str(self.root / "snapshot.json"),
                ]
            )
        self.assertEqual(returncode, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["error_type"], "EvidenceError")
        self.assertEqual(
            payload["message"],
            "original primary serialization error",
        )
        self.assertEqual(
            payload["diagnostic_output_status"],
            "FALLBACK",
        )
        self.assertIn(
            "diagnostic serialization failed: dumps failed",
            payload["diagnostic_errors"],
        )

    def test_evidence_cli_stderr_failure_is_contained(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        class FailingStderr:
            def write(self, text):
                del text
                raise OSError("injected stderr output failure")

            def flush(self):
                raise OSError("injected stderr flush failure")

        def fail_snapshot(*args, **kwargs):
            del args, kwargs
            raise EvidenceError("primary output failure")

        escaped_error = None
        with patch.object(
            evidence_module,
            "write_candidate_snapshot",
            side_effect=fail_snapshot,
        ), contextlib.redirect_stderr(FailingStderr()):
            try:
                returncode = main(
                    [
                        "--json-errors",
                        "snapshot",
                        "--root",
                        str(self.root),
                        "--output",
                        str(self.root / "snapshot.json"),
                    ]
                )
            except BaseException as exc:
                escaped_error = f"{type(exc).__name__}: {exc}"
                returncode = None
        self.assertIsNone(
            escaped_error,
            f"CLI escaped with {escaped_error}",
        )
        self.assertEqual(returncode, 2)

    def test_evidence_cli_invalid_signal_state_is_unknown(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        invalid_values = ([], {}, object(), 7)
        for invalid_value in invalid_values:
            with self.subTest(value_type=type(invalid_value).__name__):
                error = EvidenceError("primary diagnostic failure")
                error.signal_mask_state = invalid_value

                def fail_snapshot(*args, **kwargs):
                    del args, kwargs
                    raise error

                stderr = io.StringIO()
                escaped_error = None
                with patch.object(
                    evidence_module,
                    "write_candidate_snapshot",
                    side_effect=fail_snapshot,
                ), contextlib.redirect_stderr(stderr):
                    try:
                        returncode = main(
                            [
                                "--json-errors",
                                "snapshot",
                                "--root",
                                str(self.root),
                                "--output",
                                str(self.root / "snapshot.json"),
                            ]
                        )
                    except BaseException as exc:
                        escaped_error = (
                            f"{type(exc).__name__}: {exc}"
                        )
                        returncode = None
                self.assertIsNone(
                    escaped_error,
                    f"CLI escaped with {escaped_error}",
                )
                self.assertEqual(returncode, 2)
                payload = json.loads(stderr.getvalue())
                self.assertEqual(
                    payload["message"],
                    "primary diagnostic failure",
                )
                self.assertEqual(
                    payload["signal_mask_state"],
                    "UNKNOWN",
                )
                self.assertTrue(
                    any(
                        item.startswith(
                            "signal_mask_state invalid value:"
                        )
                        for item in payload["diagnostic_errors"]
                    )
                )

    def test_evidence_cli_exception_chain_getters_fail_safely(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        for attribute in ("__cause__", "__context__"):
            with self.subTest(attribute=attribute):
                class BrokenChainError(EvidenceError):
                    pass

                def fail_getter(self):
                    raise RuntimeError(f"{attribute} getter failed")

                setattr(
                    BrokenChainError,
                    attribute,
                    property(fail_getter),
                )

                def fail_snapshot(*args, **kwargs):
                    del args, kwargs
                    raise BrokenChainError("primary diagnostic failure")

                stderr = io.StringIO()
                escaped_error = None
                with patch.object(
                    evidence_module,
                    "write_candidate_snapshot",
                    side_effect=fail_snapshot,
                ), contextlib.redirect_stderr(stderr):
                    try:
                        returncode = main(
                            [
                                "--json-errors",
                                "snapshot",
                                "--root",
                                str(self.root),
                                "--output",
                                str(self.root / "snapshot.json"),
                            ]
                        )
                    except BaseException as exc:
                        escaped_error = (
                            f"{type(exc).__name__}: {exc}"
                        )
                        returncode = None
                self.assertIsNone(
                    escaped_error,
                    f"CLI escaped with {escaped_error}",
                )
                self.assertEqual(returncode, 2)
                payload = json.loads(stderr.getvalue())
                self.assertEqual(payload["error_type"], "BrokenChainError")
                self.assertEqual(
                    payload["message"],
                    "primary diagnostic failure",
                )
                self.assertEqual(
                    payload[
                        "primary_exception"
                        if attribute == "__cause__"
                        else "context_exception"
                    ]["type"],
                    "UNKNOWN",
                )
                self.assertTrue(
                    any(
                        item.startswith(f"{attribute}:")
                        for item in payload["diagnostic_errors"]
                    )
                )

    def test_evidence_cli_broken_exception_string_fails_safely(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        class BrokenStringError(EvidenceError):
            def __str__(self):
                raise RuntimeError("exception formatting failed")

        def fail_snapshot(*args, **kwargs):
            del args, kwargs
            error = BrokenStringError()
            error.__cause__ = BrokenStringError()
            raise error

        stderr = io.StringIO()
        with patch.object(
            evidence_module,
            "write_candidate_snapshot",
            side_effect=fail_snapshot,
        ), contextlib.redirect_stderr(stderr):
            returncode = main(
                [
                    "--json-errors",
                    "snapshot",
                    "--root",
                    str(self.root),
                    "--output",
                    str(self.root / "snapshot.json"),
                ]
            )
        self.assertEqual(returncode, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["error_type"], "BrokenStringError")
        self.assertEqual(
            payload["message"],
            "<unavailable diagnostic value>",
        )
        self.assertEqual(
            payload["primary_exception"]["message"],
            "<unavailable diagnostic value>",
        )

    def test_evidence_cli_multiple_diagnostic_failures_preserve_primary(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        class MultipleDiagnosticFailure(EvidenceError):
            @property
            def signal_mask_state(self):
                raise RuntimeError("signal getter failed")

            @property
            def secondary_exceptions(self):
                raise RuntimeError("secondary getter failed")

            @property
            def __cause__(self):
                raise RuntimeError("cause getter failed")

            @property
            def __context__(self):
                raise RuntimeError("context getter failed")

            def __str__(self):
                raise RuntimeError("string formatting failed")

        def fail_snapshot(*args, **kwargs):
            del args, kwargs
            raise MultipleDiagnosticFailure()

        stderr = io.StringIO()
        escaped_error = None
        with patch.object(
            evidence_module,
            "write_candidate_snapshot",
            side_effect=fail_snapshot,
        ), contextlib.redirect_stderr(stderr):
            try:
                returncode = main(
                    [
                        "--json-errors",
                        "snapshot",
                        "--root",
                        str(self.root),
                        "--output",
                        str(self.root / "snapshot.json"),
                    ]
                )
            except BaseException as exc:
                escaped_error = f"{type(exc).__name__}: {exc}"
                returncode = None
        self.assertIsNone(
            escaped_error,
            f"CLI escaped with {escaped_error}",
        )
        self.assertEqual(returncode, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(
            payload["error_type"],
            "MultipleDiagnosticFailure",
        )
        self.assertEqual(
            payload["message"],
            "<unavailable diagnostic value>",
        )
        self.assertEqual(payload["signal_mask_state"], "UNKNOWN")
        self.assertEqual(payload["secondary_exceptions"], [])
        self.assertEqual(
            payload["primary_exception"]["type"],
            "UNKNOWN",
        )
        self.assertEqual(
            payload["context_exception"]["type"],
            "UNKNOWN",
        )
        for attribute in (
            "signal_mask_state:",
            "secondary_exceptions:",
            "__cause__:",
            "__context__:",
        ):
            self.assertTrue(
                any(
                    item.startswith(attribute)
                    for item in payload["diagnostic_errors"]
                )
            )

    def test_evidence_cli_residual_getter_failure_is_unknown(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        for getter_error in (
            RuntimeError("residual getter runtime failure"),
            KeyboardInterrupt("residual getter interrupt"),
        ):
            with self.subTest(error_type=type(getter_error).__name__):
                class BrokenResidualError(EvidenceError):
                    @property
                    def residual_paths(self):
                        raise getter_error

                def fail_snapshot(*args, **kwargs):
                    del args, kwargs
                    error = BrokenResidualError(
                        "broken residual getter"
                    )
                    error.transaction_state = "COMMITTED"
                    error.durability_state = "CONFIRMED"
                    error.target_state = "COMMITTED"
                    error.residual_unknown = False
                    raise error

                stderr = io.StringIO()
                with patch.object(
                    evidence_module,
                    "write_candidate_snapshot",
                    side_effect=fail_snapshot,
                ), contextlib.redirect_stderr(stderr):
                    returncode = main(
                        [
                            "--json-errors",
                            "snapshot",
                            "--root",
                            str(self.root),
                            "--output",
                            str(self.root / "snapshot.json"),
                        ]
                    )
                self.assertEqual(returncode, 2)
                payload = json.loads(stderr.getvalue())
                self.assertEqual(
                    payload["transaction_state"],
                    "COMMITTED",
                )
                self.assertEqual(
                    payload["durability_state"],
                    "CONFIRMED",
                )
                self.assertEqual(
                    payload["residual"],
                    {
                        "status": "UNKNOWN",
                        "paths": [],
                    },
                )
                self.assertTrue(payload["residual_unknown"])
                self.assertTrue(
                    any(
                        "residual_paths" in item
                        for item in payload["diagnostic_errors"]
                    )
                )

    def test_executed_test_manifest_tampering_fails(self) -> None:
        result_path = self.root / self.fixture.metadata["tests"][
            "system_python"
        ]["result"]
        original_bytes = result_path.read_bytes()

        def omit(result):
            result["executed"]["results"].pop()

        def duplicate(result):
            result["executed"]["results"].append(
                dict(result["executed"]["results"][0])
            )

        def unattributable(result):
            result["executed"]["results"][0]["test"] = (
                "test_unrelated.MissingTests.test_extra"
            )

        try:
            for name, mutate in {
                "omit": omit,
                "duplicate": duplicate,
                "unattributable": unattributable,
            }.items():
                with self.subTest(name=name):
                    result = json.loads(canonical_json(json.loads(
                        original_bytes
                    )))
                    mutate(result)
                    result["executed_sha256"] = sha256_bytes(
                        canonical_json(result["executed"]).encode("utf-8")
                    )
                    _write_json(result_path, result)
                    self.fixture.metadata["tests"]["system_python"][
                        "result_sha256"
                    ] = sha256_file(result_path)
                    _write_json(
                        self.fixture.metadata_path,
                        self.fixture.metadata,
                    )
                    with self.assertRaisesRegex(
                        EvidenceError,
                        "execution outcomes|executed selected|repeats|"
                        "do not match executed",
                    ):
                        self._build()
        finally:
            result_path.write_bytes(original_bytes)

    def test_subtests_count_once_at_parent_method_granularity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            (root / "tests" / "test_a28_subtests.py").write_text(
                "import unittest\n"
                "class Subtests(unittest.TestCase):\n"
                "    def test_mixed(self):\n"
                "        for index in (1, 2, 3):\n"
                "            with self.subTest(index=index):\n"
                "                self.assertNotEqual(index, 2)\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            result_path = root / "artifacts" / "subtest-result.json"
            log_path = root / "artifacts" / "subtest.log"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            returncode = _run_tests_impl(
                root,
                result_path=result_path,
                log_path=log_path,
            )
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(returncode, 1)
        self.assertEqual(payload["counts"]["selected"], 1)
        self.assertEqual(payload["counts"]["failed"], 1)
        self.assertEqual(payload["counts"]["passed"], 0)
        self.assertEqual(
            payload["executed"]["results"],
            [
                {
                    "test":
                        "test_a28_subtests.Subtests.test_mixed",
                    "outcome": "failed",
                }
            ],
        )

    def test_unexpected_successes_are_recorded_and_never_accepted(self) -> None:
        module_name = "test_a30_unexpected_outcomes"
        (self.root / "tests" / f"{module_name}.py").write_text(
            "import unittest\n"
            "class OutcomeTests(unittest.TestCase):\n"
            "    @unittest.expectedFailure\n"
            "    def test_expected_failure_is_observed(self):\n"
            "        self.fail('expected failure')\n"
            "    @unittest.expectedFailure\n"
            "    def test_unexpected_success_one(self):\n"
            "        self.assertTrue(True)\n"
            "    @unittest.expectedFailure\n"
            "    def test_unexpected_success_two(self):\n"
            "        self.assertTrue(True)\n"
            "    def test_plain_pass(self):\n"
            "        self.assertTrue(True)\n"
            "    def test_plain_failure(self):\n"
            "        self.fail('ordinary failure')\n"
            "    def test_plain_error(self):\n"
            "        raise RuntimeError('ordinary error')\n",
            encoding="utf-8",
        )
        result_path = self.root / "artifacts" / "unexpected-result.json"
        log_path = self.root / "artifacts" / "unexpected.log"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(
                    PROJECT_ROOT
                    / "tests"
                    / "task_control_center_evidence.py"
                ),
                "_run-tests",
                "--root",
                str(self.root),
                "--result",
                str(result_path),
                "--log",
                str(log_path),
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        prefix = f"{module_name}.OutcomeTests."
        expected_outcomes = {
            f"{prefix}test_expected_failure_is_observed": "expected_failure",
            f"{prefix}test_unexpected_success_one": "unexpected_success",
            f"{prefix}test_unexpected_success_two": "unexpected_success",
            f"{prefix}test_plain_pass": "passed",
            f"{prefix}test_plain_failure": "failed",
            f"{prefix}test_plain_error": "error",
        }
        actual_outcomes = {
            item["test"]: item["outcome"]
            for item in payload["executed"]["results"]
            if item["test"] in expected_outcomes
        }
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertTrue(result_path.is_file())
        self.assertTrue(log_path.is_file())
        self.assertFalse(payload["successful"])
        self.assertEqual(actual_outcomes, expected_outcomes)
        self.assertEqual(payload["counts"]["expected_failures"], 1)
        self.assertEqual(payload["counts"]["unexpected_successes"], 2)
        self.assertEqual(payload["counts"]["failed"], 1)
        self.assertEqual(payload["counts"]["errors"], 1)
        self.assertEqual(
            payload["counts"]["selected"],
            len(payload["executed"]["results"]),
        )
        self.assertEqual(
            payload["counts"]["selected"],
            sum(
                payload["counts"][field]
                for field in (
                    "passed",
                    "failed",
                    "errors",
                    "skipped",
                    "expected_failures",
                    "unexpected_successes",
                )
            ),
        )
        self.assertIn("unexpected successes", log_path.read_text(
            encoding="utf-8"
        ))
        with self.assertRaisesRegex(
            EvidenceError,
            "non-successful test outcomes",
        ):
            _validate_counts(payload["counts"], "unexpected outcomes")

    def test_test_discovery_rejects_loader_import_failures(self) -> None:
        (self.root / "tests" / "test_import_failure.py").write_text(
            "import module_that_does_not_exist_for_a24\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            EvidenceError,
            "import failure",
        ):
            _current_test_discovery(self.root)

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

    def test_test_execution_refuses_blocked_ids(self) -> None:
        class BlockedDockerTests(unittest.TestCase):
            def test_should_not_execute(self) -> None:
                raise AssertionError("blocked test executed")

        with self.assertRaisesRegex(
            EvidenceError,
            "refused blocked IDs",
        ):
            _execute_test_collection(
                [BlockedDockerTests("test_should_not_execute")],
                output=io.StringIO(),
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

    def test_successful_flag_cannot_hide_failed_or_boolean_counts(self) -> None:
        result_paths = [
            self.root / self.fixture.metadata["tests"][name]["result"]
            for name in ("system_python", "venv_python")
        ]
        for result_path in result_paths:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["successful"] = True
            result["counts"]["passed"] -= 1
            result["counts"]["failed"] += 1
            _write_json(result_path, result)
        for name in ("system_python", "venv_python"):
            result_path = self.root / self.fixture.metadata["tests"][name]["result"]
            self.fixture.metadata["tests"][name]["result_sha256"] = (
                sha256_file(result_path)
            )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaisesRegex(
            EvidenceError,
            "non-successful test outcomes",
        ):
            self._build()

        for name, result_path in zip(
            ("system_python", "venv_python"),
            result_paths,
        ):
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["counts"]["failed"] = 0
            result["counts"]["passed"] += 1
            _write_json(result_path, result)
            self.fixture.metadata["tests"][name]["result_sha256"] = (
                sha256_file(result_path)
            )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)

        result_path = result_paths[0]
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["counts"]["failed"] = False
        _write_json(result_path, result)
        self.fixture.metadata["tests"]["system_python"]["result_sha256"] = (
            sha256_file(result_path)
        )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaisesRegex(
            EvidenceError,
            "non-negative integer",
        ):
            self._build()

        result["counts"]["failed"] = 0
        result["successful"] = 1
        _write_json(result_path, result)
        self.fixture.metadata["tests"]["system_python"]["result_sha256"] = (
            sha256_file(result_path)
        )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaisesRegex(
            EvidenceError,
            "not successful",
        ):
            self._build()

    def test_skipped_required_control_test_cannot_pass(self) -> None:
        result_path = self.root / self.fixture.metadata["tests"]["system_python"][
            "result"
        ]
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["counts"]["passed"] -= 1
        result["counts"]["skipped"] += 1
        skipped_required = next(
            test_id
            for test_id in result["discovery"]["selected_tests"]
            if test_id in REQUIRED_CONTROL_TESTS
        )
        result["skipped_tests"] = [
            {
                "test": skipped_required,
                "reason": "synthetic required-test skip",
            }
        ]
        for execution in result["executed"]["results"]:
            if execution["test"] == skipped_required:
                execution["outcome"] = "skipped"
        result["executed_sha256"] = sha256_bytes(
            canonical_json(result["executed"]).encode("utf-8")
        )
        _write_json(result_path, result)
        self.fixture.metadata["tests"]["system_python"]["result_sha256"] = (
            sha256_file(result_path)
        )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaisesRegex(
            EvidenceError,
            "skipped a required control test",
        ):
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
        required = result["discovery"]["selected_tests"][0]
        method = required.rsplit(".", 1)[1]
        result["discovery"]["selected_tests"][0] = (
            f"unrelated.control_suite.DecoyTests.{method}"
        )
        result["discovery"]["discovered_tests"][0] = (
            f"unrelated.control_suite.DecoyTests.{method}"
        )
        result["discovery_sha256"] = sha256_bytes(
            canonical_json(result["discovery"]).encode("utf-8")
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

    def test_mutation_execution_evidence_cannot_be_self_asserted(self) -> None:
        report_path = (
            self.root / self.fixture.metadata["mutation"]["report"]
        )
        original_bytes = report_path.read_bytes()
        original = json.loads(original_bytes)

        def forged_execution(report):
            execution = report["results"][0]["execution_evidence"]
            execution["executed_in_process"] = False
            execution["trace_missing"] = True
            execution["claim"] = "forged"

        def unknown_field(report):
            report["results"][0]["execution_evidence"]["claim"] = "forged"

        def missing_command(report):
            report["results"][0].pop("test")

        def static_scope_with_inprocess_aggregate(report):
            result = report["results"][0]
            result["mutated_lines_executed"]["executed"] = False
            for segment in result["mutated_lines_executed"]["segments"]:
                segment["hit"] = False
            execution = result["execution_evidence"]
            execution["scope"] = "static_source_check"
            execution["executed_in_process"] = False
            execution["static_source_check"] = True

        def aggregate_drift(report):
            report["execution_evidence_counts"]["in_process"] = 1
            report["execution_evidence_counts"]["child_process"] = 1

        def mixed_valid_and_forged(report):
            report["results"][1]["execution_evidence"][
                "executed_in_process"
            ] = False

        attacks = {
            "trace_missing": forged_execution,
            "unknown_field": unknown_field,
            "missing_command": missing_command,
            "scope_mismatch": static_scope_with_inprocess_aggregate,
            "aggregate_drift": aggregate_drift,
            "mixed": mixed_valid_and_forged,
        }
        try:
            for name, attack in attacks.items():
                with self.subTest(name=name):
                    report = json.loads(canonical_json(original))
                    attack(report)
                    _write_json(report_path, report)
                    self.fixture.metadata["mutation"]["report_sha256"] = (
                        sha256_file(report_path)
                    )
                    _write_json(
                        self.fixture.metadata_path,
                        self.fixture.metadata,
                    )
                    with self.assertRaisesRegex(
                        EvidenceError,
                        "execution|trace|scope|count|unknown|test record|"
                        "independent",
                    ):
                        _validate_mutation_evidence(
                            self.root,
                            self.fixture.metadata["mutation"],
                        )
        finally:
            report_path.write_bytes(original_bytes)

    def test_mutation_report_must_match_independent_reexecution(self) -> None:
        report_path = (
            self.root / self.fixture.metadata["mutation"]["report"]
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["results"][0]["test"]["exit_code"] = 0
        _write_json(report_path, report)
        self.fixture.metadata["mutation"]["report_sha256"] = sha256_file(
            report_path
        )
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaisesRegex(
            EvidenceError,
            "did not fail|independent",
        ):
            _validate_mutation_evidence(
                self.root,
                self.fixture.metadata["mutation"],
            )

    def test_mutation_patch_cannot_escape_or_race_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            sentinel = base / "outside-sentinel.py"
            sentinel.write_text("ORIGINAL = True\n", encoding="utf-8")
            original = sentinel.read_bytes()

            def case(path: str, old: str = "ORIGINAL = True", new: str = "False"):
                return {
                    "id": "unsafe-path",
                    "property": "path safety",
                    "tests": ["tests.test_example.T.test_ok"],
                    "patches": [
                        {
                            "path": path,
                            "old": old,
                            "new": new,
                        }
                    ],
                }

            for path in (str(sentinel), "../outside-sentinel.py", "C:/x.py"):
                with self.subTest(path=path):
                    result = mutation_runner._evaluate_mutation(case(path))
                    self.assertEqual(result["result"], "invalid")
                    self.assertEqual(sentinel.read_bytes(), original)

            def project(name: str) -> Path:
                root = base / name
                (root / "src").mkdir(parents=True)
                (root / "tests").mkdir()
                (root / "pyproject.toml").write_text(
                    "[project]\nname='a28'\nversion='0.0.0'\n",
                    encoding="utf-8",
                )
                return root

            symlink_project = project("symlink-project")
            (symlink_project / "src" / "target.py").symlink_to(sentinel)
            with patch.object(mutation_runner, "PROJECT_ROOT", symlink_project):
                result = mutation_runner._evaluate_mutation(
                    case("src/target.py", "ORIGINAL", "MUTATED")
                )
            self.assertEqual(result["result"], "invalid")
            self.assertIn("symbolic link", result["reason"])
            self.assertEqual(sentinel.read_bytes(), original)

            special_project = project("special-project")
            os.mkfifo(special_project / "src" / "target.py")
            with patch.object(mutation_runner, "PROJECT_ROOT", special_project):
                result = mutation_runner._evaluate_mutation(
                    case("src/target.py", "ORIGINAL", "MUTATED")
                )
            self.assertEqual(result["result"], "invalid")
            self.assertIn("special file", result["reason"])
            self.assertEqual(sentinel.read_bytes(), original)

            valid_project = project("valid-project")
            (valid_project / "src" / "target.py").write_text(
                "ORIGINAL = True\n",
                encoding="utf-8",
            )
            real_write = mutation_runner._write_snapshot_file

            def change_before_write(
                root: Path,
                relative: str,
                text: str,
                identity: tuple[int, int],
                digest: str,
                mode: int,
            ) -> None:
                (root / relative).write_text(
                    "CHANGED = True\n",
                    encoding="utf-8",
                )
                real_write(root, relative, text, identity, digest, mode)

            with patch.object(
                mutation_runner,
                "PROJECT_ROOT",
                valid_project,
            ), patch.object(
                mutation_runner,
                "_write_snapshot_file",
                side_effect=change_before_write,
            ):
                result = mutation_runner._evaluate_mutation(
                    case("src/target.py", "ORIGINAL = True", "ORIGINAL = False")
                )
            self.assertEqual(result["result"], "invalid")
            self.assertIn("changed after validation", result["reason"])
            self.assertEqual(sentinel.read_bytes(), original)

            with patch.object(mutation_runner, "PROJECT_ROOT", valid_project):
                mixed = case("src/target.py", "ORIGINAL = True", "ORIGINAL = False")
                mixed["patches"].append(
                    {
                        "path": str(sentinel),
                        "old": "ORIGINAL",
                        "new": "MUTATED",
                    }
                )
                result = mutation_runner._evaluate_mutation(mixed)
            self.assertEqual(result["result"], "invalid")
            self.assertEqual(sentinel.read_bytes(), original)

    def test_mutation_source_copy_uses_fixed_directory_identity(
        self,
    ) -> None:
        real_listdir = mutation_runner.os.listdir

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target = base / "target"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "value.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            mutation_runner._copy_tree_without_links(source, target)
            self.assertEqual(
                (target / "nested" / "value.py").read_text(
                    encoding="utf-8"
                ),
                "VALUE = 1\n",
            )

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            backup = base / "source-original"
            external = base / "external"
            target = base / "target"
            source.mkdir()
            external.mkdir()
            (source / "inside.py").write_text(
                "INSIDE = True\n",
                encoding="utf-8",
            )
            (external / "outside.py").write_text(
                "SECRET = True\n",
                encoding="utf-8",
            )
            calls = 0

            def replace_between_check_and_copy(path_fd):
                nonlocal calls
                calls += 1
                if calls == 2:
                    source.rename(backup)
                    source.symlink_to(external, target_is_directory=True)
                return real_listdir(path_fd)

            with patch.object(
                mutation_runner.os,
                "listdir",
                side_effect=replace_between_check_and_copy,
            ), self.assertRaises(mutation_runner.UnsafePatchError):
                mutation_runner._copy_tree_without_links(source, target)
            self.assertGreaterEqual(calls, 2)
            self.assertFalse(target.exists())
            self.assertTrue((external / "outside.py").is_file())

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target = base / "target"
            outside = base / "outside.py"
            source.mkdir()
            outside.write_text("SECRET = True\n", encoding="utf-8")
            (source / "nested-link").symlink_to(
                outside.parent,
                target_is_directory=True,
            )
            with self.assertRaisesRegex(
                mutation_runner.UnsafePatchError,
                "symbolic link",
            ):
                mutation_runner._copy_tree_without_links(source, target)
            self.assertFalse(target.exists())

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target = base / "target"
            source.mkdir()
            first = source / "first.py"
            first.write_text("VALUE = 1\n", encoding="utf-8")
            os.link(first, source / "hardlink.py")
            with self.assertRaisesRegex(
                mutation_runner.UnsafePatchError,
                "multiply-linked|hard",
            ):
                mutation_runner._copy_tree_without_links(source, target)
            self.assertFalse(target.exists())

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target = base / "target"
            source.mkdir()
            value_path = source / "value.py"
            value_path.write_text("VALUE = 1\n", encoding="utf-8")
            calls = 0

            def change_during_copy(path_fd):
                nonlocal calls
                calls += 1
                if calls == 2:
                    value_path.write_text(
                        "VALUE = 2\n",
                        encoding="utf-8",
                    )
                return real_listdir(path_fd)

            with patch.object(
                mutation_runner.os,
                "listdir",
                side_effect=change_during_copy,
            ), self.assertRaisesRegex(
                mutation_runner.UnsafePatchError,
                "changed during source copy",
            ):
                mutation_runner._copy_tree_without_links(source, target)
            self.assertFalse(target.exists())

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target = base / "target"
            source.mkdir()
            (source / "blocked.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            real_read = mutation_runner.os.read
            blocked = False

            def deny_source_file(file_fd, size):
                nonlocal blocked
                if not blocked:
                    blocked = True
                    raise PermissionError("injected source permission error")
                return real_read(file_fd, size)

            with patch.object(
                mutation_runner.os,
                "read",
                side_effect=deny_source_file,
            ), self.assertRaises(mutation_runner.UnsafePatchError):
                mutation_runner._copy_tree_without_links(source, target)
            self.assertTrue(blocked)
            self.assertFalse(target.exists())

    def test_mutation_empty_tree_copy_keeps_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target = base / "target"
            source.mkdir(mode=0o700)
            mutation_runner._copy_tree_without_links(source, target)
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])

    def test_mutation_tree_copy_rejects_source_target_overlap(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            source.mkdir(mode=0o700)
            marker = source / "marker.txt"
            marker.write_text("SOURCE\n", encoding="utf-8")
            nested_target = source / "target"
            with patch.object(
                mutation_runner,
                "_copy_directory_contents",
                side_effect=RuntimeError(
                    "overlap copy should not start"
                ),
            ), self.assertRaisesRegex(
                mutation_runner.UnsafePatchError,
                "contained by the source",
            ):
                mutation_runner._copy_tree_without_links(
                    source,
                    nested_target,
                )
            self.assertFalse(nested_target.exists())
            self.assertEqual(
                marker.read_text(encoding="utf-8"),
                "SOURCE\n",
            )

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "same"
            source.mkdir(mode=0o700)
            (source / "marker.txt").write_text(
                "SOURCE\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                mutation_runner.UnsafePatchError,
                "already exists",
            ):
                mutation_runner._copy_tree_without_links(
                    source,
                    source,
                )
            self.assertEqual(
                (source / "marker.txt").read_text(encoding="utf-8"),
                "SOURCE\n",
            )

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "target"
            target.mkdir(mode=0o700)
            source = target / "source"
            source.mkdir(mode=0o700)
            (source / "marker.txt").write_text(
                "SOURCE\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                mutation_runner.UnsafePatchError,
                "already exists",
            ):
                mutation_runner._copy_tree_without_links(
                    source,
                    target,
                )
            self.assertTrue(source.is_dir())

    def test_mutation_directory_no_replace_preserves_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target_dir = base / "target"
            target_dir.mkdir(mode=0o700)
            existing = target_dir / "nested"
            existing.mkdir(mode=0o700)
            (existing / "sentinel").write_text(
                "ORIGINAL\n",
                encoding="utf-8",
            )
            target_fd = os.open(
                target_dir,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                ledger = {}
                with self.assertRaises(FileExistsError):
                    mutation_runner._create_private_directory_entry(
                        target_fd,
                        "nested",
                        "nested",
                        ledger,
                    )
                cleanup = mutation_runner._clear_ledgered_entries(
                    target_fd,
                    ledger,
                )
                self.assertEqual(cleanup.residual_paths, [])
                self.assertEqual(
                    (existing / "sentinel").read_text(
                        encoding="utf-8"
                    ),
                    "ORIGINAL\n",
                )
            finally:
                os.close(target_fd)

    def test_mutation_directory_commit_race_preserves_foreign_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target_fd = os.open(
                base,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            real_rename = mutation_runner._rename_directory_no_replace
            foreign_identity = None
            try:
                ledger = {}

                def create_foreign_then_rename(
                    parent_fd: int,
                    source_name: str,
                    target_name: str,
                ) -> None:
                    os.mkdir(
                        target_name,
                        0o700,
                        dir_fd=parent_fd,
                    )
                    real_rename(
                        parent_fd,
                        source_name,
                        target_name,
                    )

                with patch.object(
                    mutation_runner,
                    "_rename_directory_no_replace",
                    side_effect=create_foreign_then_rename,
                ), self.assertRaises(FileExistsError):
                    mutation_runner._create_private_directory_entry(
                        target_fd,
                        "nested",
                        "nested",
                        ledger,
                    )
                foreign_metadata = os.stat(
                    base / "nested",
                    follow_symlinks=False,
                )
                foreign_identity = (
                    foreign_metadata.st_dev,
                    foreign_metadata.st_ino,
                )
                cleanup = mutation_runner._clear_ledgered_entries(
                    target_fd,
                    ledger,
                )
                self.assertEqual(cleanup.residual_paths, [])
                self.assertTrue((base / "nested").is_dir())
                self.assertEqual(
                    sorted(path.name for path in (base / "nested").iterdir()),
                    [],
                )
                self.assertEqual(
                    (
                        os.stat(
                            base / "nested",
                            follow_symlinks=False,
                        ).st_dev,
                        os.stat(
                            base / "nested",
                            follow_symlinks=False,
                        ).st_ino,
                    ),
                    foreign_identity,
                )
            finally:
                os.close(target_fd)

    def test_mutation_directory_commit_allows_one_concurrent_winner(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            parent_fd = os.open(
                base,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            barrier = threading.Barrier(2)
            outcomes: list[
                tuple[str, int | None, dict]
            ] = []
            outcomes_lock = threading.Lock()

            def create(relative: str) -> None:
                ledger: dict = {}
                barrier.wait(timeout=10)
                try:
                    opened_fd, _identity = (
                        mutation_runner._create_private_directory_entry(
                            parent_fd,
                            "winner",
                            relative,
                            ledger,
                        )
                    )
                    outcome = ("success", opened_fd, ledger)
                except FileExistsError:
                    outcome = ("exists", None, ledger)
                with outcomes_lock:
                    outcomes.append(outcome)

            threads = [
                threading.Thread(target=create, args=(relative,))
                for relative in ("first", "second")
            ]
            try:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=20)
                self.assertTrue(
                    all(not thread.is_alive() for thread in threads)
                )
                self.assertEqual(
                    sum(item[0] == "success" for item in outcomes),
                    1,
                )
                self.assertEqual(
                    sum(item[0] == "exists" for item in outcomes),
                    1,
                )
                winner_fd = next(
                    item[1]
                    for item in outcomes
                    if item[0] == "success"
                )
                self.assertIsNotNone(winner_fd)
                os.close(winner_fd)
                loser = next(
                    item for item in outcomes if item[0] == "exists"
                )
                cleanup = mutation_runner._clear_ledgered_entries(
                    parent_fd,
                    loser[2],
                )
                self.assertEqual(cleanup.residual_paths, [])
                self.assertTrue((base / "winner").is_dir())
            finally:
                os.close(parent_fd)

    def test_mutation_directory_commit_requires_platform_primitive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            parent_fd = os.open(
                Path(temp),
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                with patch.object(
                    mutation_runner.sys,
                    "platform",
                    "linux",
                ), self.assertRaisesRegex(
                    mutation_runner.UnsafePatchError,
                    "unavailable",
                ):
                    mutation_runner._rename_directory_no_replace(
                        parent_fd,
                        "source",
                        "target",
                    )
            finally:
                os.close(parent_fd)

    def test_mutation_project_copy_rejects_mixed_source_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "project"
            target = base / "target"
            (project / "src").mkdir(parents=True)
            (project / "tests").mkdir()
            (project / "pyproject.toml").write_text(
                "[project]\nname='a50'\nversion='0.0.0'\n",
                encoding="utf-8",
            )
            source_file = project / "src" / "value.py"
            source_file.write_text("VALUE = 1\n", encoding="utf-8")
            (project / "tests" / "test_value.py").write_text(
                "import unittest\n",
                encoding="utf-8",
            )
            target.mkdir(mode=0o700)
            original_copy = mutation_runner._copy_directory_contents

            def copy_then_change(
                source_fd,
                target_fd,
                *,
                prefix,
                ledger=None,
            ):
                original_copy(
                    source_fd,
                    target_fd,
                    prefix=prefix,
                    ledger=ledger,
                )
                if prefix == "src":
                    source_file.write_text(
                        "VALUE = 2\n",
                        encoding="utf-8",
                    )

            with patch.object(
                mutation_runner,
                "PROJECT_ROOT",
                project,
            ), patch.object(
                mutation_runner,
                "_copy_directory_contents",
                side_effect=copy_then_change,
            ), self.assertRaisesRegex(
                mutation_runner.UnsafePatchError,
                "aggregate copy|changed during",
            ):
                mutation_runner._copy_project(target)
            self.assertEqual(list(target.iterdir()), [])

    def test_mutation_copy_target_replacement_stays_bounded(self) -> None:
        real_open_directory = (
            mutation_runner._open_absolute_directory_without_links
        )

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.py"
            target_parent = base / "target-parent"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            target_parent.mkdir(mode=0o700)
            target = target_parent / "value.py"
            mutation_runner._copy_file_without_links(source, target)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "VALUE = 1\n",
            )

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            target_parent = base / "target-parent"
            detached = base / "target-parent-detached"
            outside = base / "outside"
            target_parent.mkdir(mode=0o700)
            outside.mkdir()
            target = target_parent / "value.py"
            changed = False

            def open_then_replace_target_parent(path, label):
                nonlocal changed
                opened = real_open_directory(path, label)
                if (
                    not changed
                    and path == target_parent
                    and label == "mutation target parent"
                ):
                    target_parent.rename(detached)
                    target_parent.symlink_to(
                        outside,
                        target_is_directory=True,
                    )
                    changed = True
                return opened

            with patch.object(
                mutation_runner,
                "_open_absolute_directory_without_links",
                side_effect=open_then_replace_target_parent,
            ), self.assertRaisesRegex(
                mutation_runner.UnsafePatchError,
                "replaced during copy|safely",
            ):
                mutation_runner._copy_file_without_links(
                    source,
                    target,
                )
            self.assertTrue(changed)
            self.assertFalse((outside / "value.py").exists())
            self.assertTrue(target_parent.is_symlink())
            self.assertTrue(detached.is_dir())
            self.assertEqual(list(detached.iterdir()), [])

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target_parent = base / "target-parent"
            detached = base / "target-detached"
            outside = base / "outside"
            source.mkdir()
            target_parent.mkdir(mode=0o700)
            outside.mkdir()
            (source / "value.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            target = target_parent / "target"
            original_copy = mutation_runner._copy_directory_contents
            changed = False

            def replace_target_then_fail(
                source_fd,
                target_fd,
                *,
                prefix,
                ledger=None,
            ):
                nonlocal changed
                original_copy(
                    source_fd,
                    target_fd,
                    prefix=prefix,
                    ledger=ledger,
                )
                if not changed:
                    target.rename(detached)
                    target.symlink_to(
                        outside,
                        target_is_directory=True,
                    )
                    changed = True
                    raise RuntimeError("injected post-copy failure")

            with patch.object(
                mutation_runner,
                "_copy_directory_contents",
                side_effect=replace_target_then_fail,
            ), self.assertRaisesRegex(
                RuntimeError,
                "injected post-copy failure",
            ) as caught:
                mutation_runner._copy_tree_without_links(source, target)
            self.assertTrue(changed)
            self.assertFalse((outside / "value.py").exists())
            self.assertTrue(target.is_symlink())
            self.assertTrue(detached.is_dir())
            self.assertEqual(list(detached.iterdir()), [])
            self.assertIn(
                ".",
                caught.exception.residual_paths,
            )

    def test_mutation_staging_uses_one_captured_generation(self) -> None:
        import tests.task_control_center_evidence as evidence_module

        source_file = self.root / "src" / "zevorinth" / "example.py"
        original_bytes = source_file.read_bytes()
        expected_fingerprint = code_fingerprint(self.root)
        original_write = TrustedEvidenceRoot._write_bytes_staging
        changed = False

        def write_then_change(
            root,
            path,
            data,
            label,
        ):
            nonlocal changed
            original_write(root, path, data, label)
            if (
                not changed
                and label.startswith("mutation snapshot ")
            ):
                source_file.write_text(
                    "VALUE = 999\n",
                    encoding="utf-8",
                )
                source_file.write_bytes(original_bytes)
                changed = True

        with tempfile.TemporaryDirectory() as child_temp:
            child_path = Path(child_temp)
            with TrustedEvidenceRoot(
                child_path,
                "mutation staging test root",
            ) as child_root:
                with patch.object(
                    TrustedEvidenceRoot,
                    "_write_bytes_staging",
                    autospec=True,
                    side_effect=write_then_change,
                ), self.assertRaisesRegex(
                    EvidenceError,
                    "generation changed|changed during staging",
                ):
                    evidence_module._prepare_mutation_project_snapshot(
                        self.root,
                        child_root,
                        child_path,
                        expected_code_fingerprint=expected_fingerprint,
                    )
        self.assertTrue(changed)
        self.assertEqual(source_file.read_bytes(), original_bytes)

    def test_mutation_staging_root_is_exclusively_private(self) -> None:
        import tests.task_control_center_evidence as evidence_module

        expected_fingerprint = code_fingerprint(self.root)
        with tempfile.TemporaryDirectory() as child_temp:
            child_path = Path(child_temp)
            with TrustedEvidenceRoot(
                child_path,
                "mutation exclusive staging root",
            ) as child_root:
                project_root_owner = (
                    evidence_module._prepare_mutation_project_snapshot(
                        self.root,
                        child_root,
                        child_path,
                        expected_code_fingerprint=expected_fingerprint,
                    )
                )
                with project_root_owner:
                    metadata = project_root_owner.path.stat()
                    self.assertEqual(
                        stat.S_IMODE(metadata.st_mode) & 0o077,
                        0,
                    )

    @unittest.skipUnless(sys.platform == "darwin", "macOS ACL test")
    def test_mutation_staging_root_rejects_inherited_macos_acl(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        expected_fingerprint = code_fingerprint(self.root)
        acl = (
            "everyone allow list,search,readattr,readextattr,"
            "readsecurity,writeattr,writeextattr,add_file,"
            "add_subdirectory,delete_child,directory_inherit,"
            "file_inherit"
        )
        with tempfile.TemporaryDirectory() as child_temp:
            child_path = Path(child_temp)
            with TrustedEvidenceRoot(
                child_path,
                "mutation inherited ACL staging root",
            ) as child_root:
                with child_root.pinned():
                    subprocess.run(
                        [
                            "chmod",
                            "+a",
                            acl,
                            str(child_path),
                        ],
                        check=True,
                    )
                    with self.assertRaisesRegex(
                        EvidenceError,
                        "exclusively private",
                    ):
                        evidence_module._prepare_mutation_project_snapshot(
                            self.root,
                            child_root,
                            child_path,
                            expected_code_fingerprint=(
                                expected_fingerprint
                            ),
                        )
                    project_root = child_path / "mutation-project"
                    self.assertTrue(project_root.is_dir())
                    self.assertEqual(list(project_root.iterdir()), [])

    def test_mutation_staging_write_stays_on_validated_held_root(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        expected_fingerprint = code_fingerprint(self.root)
        with tempfile.TemporaryDirectory() as child_temp:
            child_path = Path(child_temp)
            project_root = child_path / "mutation-project"
            detached_root = child_path / "mutation-project-detached"
            outside = child_path / "outside"
            outside.mkdir()
            real_write = TrustedEvidenceRoot._write_bytes_staging
            swapped = False

            def replace_staging_root_on_first_write(
                owner,
                path,
                data,
                label,
            ):
                nonlocal swapped
                if (
                    not swapped
                    and owner.label == "mutation staging root"
                    and label.startswith("mutation snapshot ")
                ):
                    project_root.rename(detached_root)
                    project_root.symlink_to(
                        outside,
                        target_is_directory=True,
                    )
                    swapped = True
                real_write(owner, path, data, label)

            with TrustedEvidenceRoot(
                child_path,
                "mutation held-root test",
            ) as child_root:
                with child_root.pinned():
                    with patch.object(
                        TrustedEvidenceRoot,
                        "_write_bytes_staging",
                        autospec=True,
                        side_effect=replace_staging_root_on_first_write,
                    ), self.assertRaisesRegex(
                        EvidenceError,
                        "anchor|reachable|safely",
                    ):
                        evidence_module._prepare_mutation_project_snapshot(
                            self.root,
                            child_root,
                            child_path,
                            expected_code_fingerprint=(
                                expected_fingerprint
                            ),
                        )
            self.assertTrue(swapped)
            self.assertEqual(list(outside.iterdir()), [])

    def test_mutation_staging_transfer_is_owned_until_caller_receives_it(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        expected_fingerprint = code_fingerprint(self.root)
        real_mask = evidence_module.signal.pthread_sigmask
        trigger = False
        transferred_owner = None
        real_prepare = evidence_module._prepare_mutation_project_snapshot

        def prepare_then_mark(*args, **kwargs):
            nonlocal trigger, transferred_owner
            transferred_owner = real_prepare(*args, **kwargs)
            trigger = True
            return transferred_owner

        def interrupt_mask_restore(signals, mask):
            nonlocal trigger
            previous = real_mask(signals, mask)
            if signals == signal.SIG_SETMASK and trigger:
                trigger = False
                raise KeyboardInterrupt(
                    "injected staging transfer interrupt"
                )
            return previous

        with tempfile.TemporaryDirectory() as child_temp:
            child_path = Path(child_temp)
            with TrustedEvidenceRoot(
                child_path,
                "staging transfer test root",
            ) as child_root:
                with patch.object(
                    evidence_module,
                    "_prepare_mutation_project_snapshot",
                    side_effect=prepare_then_mark,
                ), patch.object(
                    evidence_module.signal,
                    "pthread_sigmask",
                    side_effect=interrupt_mask_restore,
                ), self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "staging transfer",
                ):
                    with evidence_module._owned_mutation_project_snapshot(
                        self.root,
                        child_root,
                        child_path,
                        expected_code_fingerprint=(
                            expected_fingerprint
                        ),
                    ):
                        self.fail("interrupted owner must not be yielded")
        self.assertIsNotNone(transferred_owner)
        self.assertIsNone(transferred_owner._fd)
        self.assertIsNone(transferred_owner._anchor_fd)

    def test_mutation_identity_pending_survives_lookup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.py"
            target_dir = base / "target"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            target_dir.mkdir(mode=0o700)
            source_fd = os.open(source, os.O_RDONLY)
            target_fd = os.open(
                target_dir,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                metadata = os.fstat(source_fd)
                ledger = {}
                real_fstat = mutation_runner.os.fstat
                failed = False
                failed_fd = None

                def fail_target_identity(file_fd):
                    nonlocal failed, failed_fd
                    if file_fd != source_fd and not failed:
                        failed = True
                        failed_fd = file_fd
                        raise OSError("injected target identity failure")
                    return real_fstat(file_fd)

                with patch.object(
                    mutation_runner.os,
                    "fstat",
                    side_effect=fail_target_identity,
                ), self.assertRaisesRegex(
                    OSError,
                    "injected target identity failure",
                ):
                    mutation_runner._copy_open_file_to_directory(
                        source_fd,
                        metadata,
                        target_fd,
                        "value.py",
                        "value.py",
                        ledger,
                        "value.py",
                    )
                self.assertTrue((target_dir / "value.py").is_file())
                self.assertEqual(
                    ledger["value.py"][0],
                    "pending-file",
                )
                cleanup = mutation_runner._clear_ledgered_entries(
                    target_fd,
                    ledger,
                )
                self.assertEqual(cleanup.residual_paths, ["value.py"])
                self.assertTrue((target_dir / "value.py").is_file())
                self.assertIsNotNone(failed_fd)
                with self.assertRaises(OSError):
                    os.fstat(failed_fd)
            finally:
                os.close(target_fd)
                os.close(source_fd)

    def test_mutation_identity_pending_ledger_update_failure(self) -> None:
        class FailFinalLedger(dict):
            def __setitem__(self, key, value):
                if key in self and not value[0].startswith("pending-"):
                    raise RuntimeError("injected ledger update failure")
                super().__setitem__(key, value)

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.py"
            target_dir = base / "target"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            target_dir.mkdir(mode=0o700)
            source_fd = os.open(source, os.O_RDONLY)
            target_fd = os.open(
                target_dir,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                ledger = FailFinalLedger()
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected ledger update failure",
                ):
                    mutation_runner._copy_open_file_to_directory(
                        source_fd,
                        os.fstat(source_fd),
                        target_fd,
                        "value.py",
                        "value.py",
                        ledger,
                        "value.py",
                    )
                self.assertEqual(
                    ledger["value.py"][0],
                    "pending-file",
                )
                cleanup = mutation_runner._clear_ledgered_entries(
                    target_fd,
                    ledger,
                )
                self.assertEqual(cleanup.residual_paths, ["value.py"])
                self.assertTrue((target_dir / "value.py").is_file())
            finally:
                os.close(target_fd)
                os.close(source_fd)

    def test_mutation_directory_identity_pending_is_diagnosed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target_dir = base / "target"
            (source / "nested").mkdir(parents=True)
            target_dir.mkdir(mode=0o700)
            source_fd = os.open(
                source,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            target_fd = os.open(
                target_dir,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                flags = mutation_runner._copy_directory_flags()
                real_stat = mutation_runner.os.stat
                failed = False

                def fail_target_directory_identity(path, *args, **kwargs):
                    nonlocal failed
                    if (
                        not failed
                        and path == "nested"
                        and kwargs.get("dir_fd") == target_fd
                    ):
                        failed = True
                        raise OSError(
                            "injected directory identity failure"
                        )
                    return real_stat(path, *args, **kwargs)

                ledger = {}
                with patch.object(
                    mutation_runner,
                    "_copy_directory_flags",
                    return_value=flags,
                ), patch.object(
                    mutation_runner.os,
                    "stat",
                    side_effect=fail_target_directory_identity,
                ), self.assertRaisesRegex(
                    OSError,
                    "injected directory identity failure",
                ):
                    mutation_runner._copy_directory_contents(
                        source_fd,
                        target_fd,
                        prefix="",
                        ledger=ledger,
                    )
                self.assertEqual(
                    ledger["nested"][0],
                    "pending-directory",
                )
                cleanup = mutation_runner._clear_ledgered_entries(
                    target_fd,
                    ledger,
                )
                self.assertEqual(cleanup.residual_paths, ["nested"])
                self.assertTrue((target_dir / "nested").is_dir())
            finally:
                os.close(target_fd)
                os.close(source_fd)

    def test_mutation_directory_fstat_failure_is_residual(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target_dir = Path(temp) / "target"
            target_dir.mkdir(mode=0o700)
            target_fd = os.open(
                target_dir,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                real_fstat = mutation_runner.os.fstat
                failed = False

                def fail_created_directory_identity(file_fd):
                    nonlocal failed
                    if file_fd != target_fd and not failed:
                        failed = True
                        raise OSError(
                            "injected directory fstat failure"
                        )
                    return real_fstat(file_fd)

                ledger = {}
                with patch.object(
                    mutation_runner.os,
                    "fstat",
                    side_effect=fail_created_directory_identity,
                ), self.assertRaisesRegex(
                    OSError,
                    "injected directory fstat failure",
                ):
                    mutation_runner._create_private_directory_entry(
                        target_fd,
                        "nested",
                        "nested",
                        ledger,
                    )
                self.assertEqual(
                    ledger["nested"][0],
                    "pending-directory",
                )
                cleanup = mutation_runner._clear_ledgered_entries(
                    target_fd,
                    ledger,
                )
                self.assertEqual(len(cleanup.residual_paths), 1)
                self.assertTrue(
                    cleanup.residual_paths[0].startswith(".mutation-dir-")
                )
                self.assertFalse((target_dir / "nested").exists())
            finally:
                os.close(target_fd)

    def test_mutation_directory_identity_uses_held_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target_dir = base / "target"
            detached = base / "created-nested"
            target_dir.mkdir(mode=0o700)
            target_fd = os.open(
                target_dir,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                ledger = {}
                real_stat = mutation_runner.os.stat
                real_flags = mutation_runner._copy_directory_flags()
                final_stat_calls = 0

                def replace_before_identity(path, *args, **kwargs):
                    nonlocal final_stat_calls
                    if path == "nested" and kwargs.get("dir_fd") == target_fd:
                        final_stat_calls += 1
                        if final_stat_calls == 1:
                            (target_dir / "nested").rename(detached)
                            (target_dir / "nested").mkdir(mode=0o700)
                            (target_dir / "nested" / "foreign").write_text(
                                "FOREIGN\n",
                                encoding="utf-8",
                            )
                    return real_stat(path, *args, **kwargs)

                with patch.object(
                    mutation_runner,
                    "_copy_directory_flags",
                    return_value=real_flags,
                ), patch.object(
                    mutation_runner.os,
                    "stat",
                    side_effect=replace_before_identity,
                ), self.assertRaisesRegex(
                    mutation_runner.UnsafePatchError,
                    "identity changed",
                ):
                    mutation_runner._create_private_directory_entry(
                        target_fd,
                        "nested",
                        "nested",
                        ledger,
                    )
                self.assertEqual(
                    ledger["nested"][0],
                    "pending-directory",
                )
                cleanup = mutation_runner._clear_ledgered_entries(
                    target_fd,
                    ledger,
                )
                self.assertEqual(cleanup.residual_paths, ["nested"])
                self.assertTrue((target_dir / "nested").is_dir())
                self.assertTrue(
                    (target_dir / "nested" / "foreign").is_file()
                )
            finally:
                os.close(target_fd)

    def test_mutation_residual_diagnostics_survive_add_note_failure(
        self,
    ) -> None:
        class NoteFailure(RuntimeError):
            def add_note(self, note):
                raise RuntimeError("injected note failure")

        error = NoteFailure("original")
        mutation_runner._attach_cleanup_residual(
            error,
            ["src/value.py", "src"],
        )
        self.assertEqual(
            error.residual_paths,
            ["src", "src/value.py"],
        )

    def test_mutation_result_propagates_residual_paths(self) -> None:
        case = {
            "id": "residual-result",
            "property": "residual result propagation",
            "path": "src/value.py",
            "old": "VALUE = 1\n",
            "new": "VALUE = 2\n",
            "tests": ["tests.test_value.ValueTests.test_value"],
        }
        error = mutation_runner.UnsafePatchError(
            "mutation project copy failed"
        )
        error.residual_paths = ["src/pending.py", "src"]
        with patch.object(
            mutation_runner,
            "_copy_project",
            side_effect=error,
        ):
            result = mutation_runner._evaluate_mutation(case)
        self.assertEqual(result["result"], "invalid")
        self.assertEqual(
            result["residual"],
            {
                "status": "PRESERVED",
                "paths": ["src", "src/pending.py"],
            },
        )
        self.assertIn("mutation project copy failed", result["reason"])

    def test_mutation_result_reports_unknown_residual_state(self) -> None:
        case = {
            "id": "unknown-residual",
            "property": "unknown residual propagation",
            "path": "src/value.py",
            "old": "VALUE = 1\n",
            "new": "VALUE = 2\n",
            "tests": ["tests.test_value.ValueTests.test_value"],
        }
        error = mutation_runner.UnsafePatchError(
            "mutation project copy failed"
        )
        error.residual_unknown = True
        with patch.object(
            mutation_runner,
            "_copy_project",
            side_effect=error,
        ):
            result = mutation_runner._evaluate_mutation(case)
        self.assertEqual(result["result"], "invalid")
        self.assertEqual(
            result["residual"],
            {
                "status": "UNKNOWN",
                "paths": [],
            },
        )

    def test_mutation_result_reports_fd_close_state(self) -> None:
        error = mutation_runner.UnsafePatchError(
            "fd close state"
        )
        error.fd_close_states = {
            7: "OPEN",
            8: "CLOSED",
        }
        error.unresolved_fds = {7: "OPEN"}
        result = mutation_runner._mutation_invalid_result(
            {"id": "fd-close-state"},
            "fd close state",
            error=error,
        )
        self.assertEqual(
            result["fd_close_states"],
            {
                "7": "OPEN",
                "8": "CLOSED",
            },
        )
        self.assertEqual(
            result["unresolved_fds"],
            {"7": "OPEN"},
        )
        self.assertEqual(result["diagnostic_errors"], [])

    def test_mutation_target_requires_exclusive_private_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "project"
            target = base / "target"
            (project / "src").mkdir(parents=True)
            (project / "tests").mkdir()
            (project / "pyproject.toml").write_text(
                "[project]\nname='exclusive'\nversion='0.0.0'\n",
                encoding="utf-8",
            )
            (project / "src" / "value.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            (project / "tests" / "test_value.py").write_text(
                "import unittest\n",
                encoding="utf-8",
            )
            target.mkdir(mode=0o755)
            os.chmod(target, 0o755)
            with patch.object(
                mutation_runner,
                "PROJECT_ROOT",
                project,
            ), self.assertRaisesRegex(
                mutation_runner.UnsafePatchError,
                "owner-only",
            ):
                mutation_runner._copy_project(target)
            self.assertEqual(list(target.iterdir()), [])

    def test_mutation_tree_root_is_rechecked_after_create(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target = base / "target"
            source.mkdir()
            (source / "value.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            original_open = mutation_runner._open_relative_directory

            def open_then_widen(opened_fds, parent_fd, name, label):
                opened = original_open(
                    opened_fds,
                    parent_fd,
                    name,
                    label,
                )
                if label == "mutation copy target":
                    os.chmod(target, 0o755)
                return opened

            with patch.object(
                mutation_runner,
                "_open_relative_directory",
                side_effect=open_then_widen,
            ), self.assertRaisesRegex(
                mutation_runner.UnsafePatchError,
                "owner-only",
            ):
                mutation_runner._copy_tree_without_links(source, target)
            self.assertFalse((target / "value.py").exists())

    def test_mutation_tree_root_interrupt_leaves_residual(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target = base / "target"
            source.mkdir()
            (source / "value.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            real_mkdir = mutation_runner.os.mkdir
            real_flags = mutation_runner._copy_directory_flags()

            def create_then_interrupt(path, mode=0o777, *, dir_fd=None):
                real_mkdir(path, mode, dir_fd=dir_fd)
                raise KeyboardInterrupt("injected post-mkdir interrupt")

            with patch.object(
                mutation_runner,
                "_copy_directory_flags",
                return_value=real_flags,
            ), patch.object(
                mutation_runner.os,
                "mkdir",
                side_effect=create_then_interrupt,
            ), self.assertRaises(KeyboardInterrupt) as caught:
                mutation_runner._copy_tree_without_links(source, target)
            self.assertTrue(target.exists())
            self.assertEqual(
                caught.exception.residual_paths,
                ["."],
            )

    def test_mutation_cleanup_observation_failure_preserves_primary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target = base / "target"
            source.mkdir()
            real_mkdir = mutation_runner.os.mkdir
            real_stat = mutation_runner.os.stat
            real_flags = mutation_runner._copy_directory_flags()
            target_created = False

            def create_then_interrupt(path, mode=0o777, *, dir_fd=None):
                nonlocal target_created
                real_mkdir(path, mode, dir_fd=dir_fd)
                if path == target.name:
                    target_created = True
                    raise KeyboardInterrupt(
                        "injected primary mkdir interrupt"
                    )

            def fail_cleanup_observation(path, *args, **kwargs):
                if target_created and path == target.name:
                    raise PermissionError(
                        "injected cleanup observation failure"
                    )
                return real_stat(path, *args, **kwargs)

            with patch.object(
                mutation_runner,
                "_copy_directory_flags",
                return_value=real_flags,
            ), patch.object(
                mutation_runner.os,
                "mkdir",
                side_effect=create_then_interrupt,
            ), patch.object(
                mutation_runner.os,
                "stat",
                side_effect=fail_cleanup_observation,
            ), self.assertRaisesRegex(
                KeyboardInterrupt,
                "primary mkdir",
            ) as caught:
                mutation_runner._copy_tree_without_links(source, target)
            self.assertTrue(target_created)
            self.assertTrue(caught.exception.residual_unknown)
            self.assertTrue(target.exists())

    def test_mutation_cleanup_interrupt_preserves_oserror_primary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target = base / "target"
            source.mkdir(mode=0o700)
            (source / "value.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            real_copy_file = mutation_runner._copy_open_file_to_directory
            real_stat = mutation_runner.os.stat
            real_flags = mutation_runner._copy_directory_flags()
            copy_primary_raised = False

            def copy_then_fail(*args, **kwargs):
                nonlocal copy_primary_raised
                result = real_copy_file(*args, **kwargs)
                copy_primary_raised = True
                raise OSError("injected copy primary")

            def interrupt_cleanup_observation(
                path,
                *args,
                **kwargs,
            ):
                if copy_primary_raised and path == "value.py":
                    raise KeyboardInterrupt(
                        "injected cleanup observation interrupt"
                    )
                return real_stat(path, *args, **kwargs)

            with patch.object(
                mutation_runner,
                "_copy_open_file_to_directory",
                side_effect=copy_then_fail,
            ), patch.object(
                mutation_runner,
                "_copy_directory_flags",
                return_value=real_flags,
            ), patch.object(
                mutation_runner.os,
                "stat",
                side_effect=interrupt_cleanup_observation,
            ), self.assertRaises(
                mutation_runner.UnsafePatchError
            ) as caught:
                mutation_runner._copy_tree_without_links(source, target)
            self.assertTrue(copy_primary_raised)
            self.assertIsInstance(
                caught.exception.__cause__,
                OSError,
            )
            self.assertIn(
                "injected copy primary",
                str(caught.exception.__cause__),
            )
            self.assertTrue(caught.exception.residual_unknown)
            self.assertIn(
                "value.py",
                caught.exception.residual_paths,
            )
            self.assertIn(
                "cleanup existence or ownership is unknown for value.py",
                "\n".join(caught.exception.__notes__),
            )
            self.assertEqual(
                (target / "value.py").read_text(encoding="utf-8"),
                "VALUE = 1\n",
            )

    def test_mutation_close_aggregator_continues_after_baseexception(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "first").write_text("first\n", encoding="utf-8")
            (base / "second").write_text("second\n", encoding="utf-8")
            first_owner, first_fd = _owned_file_fd(
                base / "first",
                owner_type=mutation_runner._OwnedFDCollection,
            )
            second_owner, second_fd = _owned_file_fd(
                base / "second",
                owner_type=mutation_runner._OwnedFDCollection,
            )
            ownership = {
                first_fd: first_owner.record(first_fd),
                second_fd: second_owner.record(second_fd),
            }
            real_close = mutation_runner.os.close
            failed = False

            def interrupt_first_close(file_fd):
                nonlocal failed
                real_close(file_fd)
                if file_fd == first_fd and not failed:
                    failed = True
                    raise KeyboardInterrupt("injected close interrupt")

            with patch.object(
                mutation_runner.os,
                "close",
                side_effect=interrupt_first_close,
            ), self.assertRaisesRegex(
                KeyboardInterrupt,
                "injected close interrupt",
            ):
                mutation_runner._close_fds_preserving_primary(
                    [
                        (first_fd, "first", None),
                        (second_fd, "second", None),
                    ],
                    ownership_records=ownership,
                )
            self.assertTrue(failed)
            with self.assertRaises(OSError):
                os.fstat(first_fd)
            with self.assertRaises(OSError):
                os.fstat(second_fd)

    def test_mutation_close_aggregator_preserves_primary_on_interrupt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "first").write_text("first\n", encoding="utf-8")
            (base / "second").write_text("second\n", encoding="utf-8")
            first_owner, first_fd = _owned_file_fd(
                base / "first",
                owner_type=mutation_runner._OwnedFDCollection,
            )
            second_owner, second_fd = _owned_file_fd(
                base / "second",
                owner_type=mutation_runner._OwnedFDCollection,
            )
            ownership = {
                first_fd: first_owner.record(first_fd),
                second_fd: second_owner.record(second_fd),
            }
            real_close = mutation_runner.os.close
            failed = False

            def interrupt_first_close(file_fd):
                nonlocal failed
                real_close(file_fd)
                if file_fd == first_fd and not failed:
                    failed = True
                    raise KeyboardInterrupt("injected close interrupt")

            with patch.object(
                mutation_runner.os,
                "close",
                side_effect=interrupt_first_close,
            ), self.assertRaisesRegex(
                RuntimeError,
                "primary operation",
            ) as caught:
                try:
                    raise RuntimeError("primary operation")
                finally:
                    mutation_runner._close_fds_preserving_primary(
                        [
                            (first_fd, "first", None),
                            (second_fd, "second", None),
                        ],
                        ownership_records=ownership,
                    )
            self.assertTrue(failed)
            self.assertIn(
                "injected close interrupt",
                "\n".join(caught.exception.__notes__),
            )
            with self.assertRaises(OSError):
                os.fstat(first_fd)
            with self.assertRaises(OSError):
                os.fstat(second_fd)

    def test_registered_close_interrupt_retires_uncertain_numeric_fd(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "owned"
            path.write_text("owned\n", encoding="utf-8")
            owner, read_fd = _owned_file_fd(
                path,
                owner_type=mutation_runner._OwnedFDCollection,
            )
            real_close = mutation_runner.os.close
            calls = 0
            owner_retained_before_native = False
            fd_open_before_native = False

            def interrupt_before_syscall(file_fd):
                nonlocal calls, owner_retained_before_native
                nonlocal fd_open_before_native
                calls += 1
                if calls == 1:
                    owner_retained_before_native = file_fd in owner
                    try:
                        os.fstat(file_fd)
                    except OSError:
                        fd_open_before_native = False
                    else:
                        fd_open_before_native = True
                    raise KeyboardInterrupt(
                        "injected pre-native-close interrupt"
                    )
                return real_close(file_fd)

            try:
                with patch.object(
                    mutation_runner.os,
                    "close",
                    side_effect=interrupt_before_syscall,
                ), self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "pre-native-close",
                ) as caught:
                    mutation_runner._close_registered_fd(
                        owner,
                        read_fd,
                        "retired close",
                    )
                self.assertEqual(calls, 1)
                self.assertNotIn(read_fd, owner)
                self.assertEqual(
                    caught.exception.fd_close_states[read_fd],
                    "UNKNOWN",
                )
                self.assertEqual(
                    caught.exception.unresolved_fds[read_fd],
                    "UNKNOWN",
                )
                os.fstat(read_fd)
            finally:
                owner.close_all_anchors()
                try:
                    real_close(read_fd)
                except OSError:
                    pass
            self.assertTrue(owner_retained_before_native)
            self.assertTrue(fd_open_before_native)

    def test_registered_close_post_syscall_interrupt_does_not_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "owned"
            path.write_text("owned\n", encoding="utf-8")
            owner, read_fd = _owned_file_fd(
                path,
                owner_type=mutation_runner._OwnedFDCollection,
            )
            real_close = mutation_runner.os.close
            calls = 0

            def close_then_interrupt(file_fd):
                nonlocal calls
                calls += 1
                real_close(file_fd)
                raise KeyboardInterrupt(
                    "injected post-native-close interrupt"
                )

            with patch.object(
                mutation_runner.os,
                "close",
                side_effect=close_then_interrupt,
            ), self.assertRaisesRegex(
                KeyboardInterrupt,
                "post-native-close",
            ) as caught:
                mutation_runner._close_registered_fd(
                    owner,
                    read_fd,
                    "post-native close",
                )
            self.assertEqual(calls, 1)
            self.assertNotIn(read_fd, owner)
            self.assertTrue(caught.exception.close_diagnostics)
            self.assertEqual(
                caught.exception.fd_close_states[read_fd],
                "CLOSED",
            )
            self.assertTrue(
                caught.exception.unresolved_fds
            )
            with self.assertRaises(OSError):
                os.fstat(read_fd)

    def test_registered_close_oserror_does_not_retry_numeric_fd(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "owned"
            path.write_text("owned\n", encoding="utf-8")
            owner, read_fd = _owned_file_fd(
                path,
                owner_type=mutation_runner._OwnedFDCollection,
            )
            real_close = mutation_runner.os.close
            calls = 0

            def close_before_native(file_fd):
                nonlocal calls
                calls += 1
                del file_fd
                raise OSError("injected pre-native close OSError")

            try:
                with patch.object(
                    mutation_runner.os,
                    "close",
                    side_effect=close_before_native,
                ), self.assertRaises(
                    mutation_runner.UnsafePatchError
                ) as caught:
                    mutation_runner._close_registered_fd(
                        owner,
                        read_fd,
                        "OSError retired",
                    )
                self.assertEqual(calls, 1)
                self.assertNotIn(read_fd, owner)
                self.assertEqual(
                    caught.exception.fd_close_states[read_fd],
                    "UNKNOWN",
                )
                self.assertEqual(
                    caught.exception.unresolved_fds[read_fd],
                    "UNKNOWN",
                )
                self.assertEqual(
                    len(caught.exception.fd_close_states),
                    2,
                )
                os.fstat(read_fd)
            finally:
                owner.close_all_anchors()
                try:
                    real_close(read_fd)
                except OSError:
                    pass

    def test_owned_fds_retire_uncertain_numeric_fd_without_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "owned"
            path.write_text("owned\n", encoding="utf-8")
            owner, read_fd = _owned_file_fd(
                path,
                owner_type=mutation_runner._OwnedFDCollection,
            )
            real_close = mutation_runner.os.close
            calls = 0

            def interrupt_before_native(file_fd):
                nonlocal calls
                del file_fd
                calls += 1
                raise KeyboardInterrupt(
                    "repeated pre-native owner interrupt"
                )

            try:
                with patch.object(
                    mutation_runner.os,
                    "close",
                    side_effect=interrupt_before_native,
                ), self.assertRaisesRegex(
                    RuntimeError,
                    "primary owner operation",
                ) as caught:
                    try:
                        raise RuntimeError("primary owner operation")
                    finally:
                        mutation_runner._close_owned_fds(
                            owner,
                            "repeated owner close",
                        )
                self.assertEqual(calls, 1)
                self.assertNotIn(read_fd, owner)
                self.assertEqual(
                    caught.exception.fd_close_states[read_fd],
                    "UNKNOWN",
                )
                self.assertEqual(
                    caught.exception.unresolved_fds[read_fd],
                    "UNKNOWN",
                )
                os.fstat(read_fd)
            finally:
                owner.close_all_anchors()
                try:
                    real_close(read_fd)
                except OSError:
                    pass

    def test_registered_close_fd_reuse_is_unknown_not_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            first = base / "first"
            second = base / "second"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            owner, file_fd = _owned_file_fd(
                first,
                owner_type=mutation_runner._OwnedFDCollection,
            )
            real_close = mutation_runner.os.close
            reused_fd = None

            def close_then_reuse(file_fd_to_close):
                nonlocal reused_fd
                real_close(file_fd_to_close)
                reused_fd = os.open(second, os.O_RDONLY)
                raise KeyboardInterrupt(
                    "injected close interruption after FD reuse"
                )

            try:
                with patch.object(
                    mutation_runner.os,
                    "close",
                    side_effect=close_then_reuse,
                ), self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "after FD reuse",
                ) as caught:
                    mutation_runner._close_registered_fd(
                        owner,
                        file_fd,
                        "reused close",
                    )
                self.assertEqual(reused_fd, file_fd)
                self.assertNotIn(file_fd, owner)
                self.assertEqual(
                    caught.exception.fd_close_states[file_fd],
                    "REUSED_OR_REPLACED",
                )
                self.assertEqual(
                    caught.exception.unresolved_fds[file_fd],
                    "REUSED_OR_REPLACED",
                )
                self.assertEqual(
                    len(caught.exception.fd_close_states),
                    2,
                )
                os.fstat(file_fd)
            finally:
                owner.close_all_anchors()
                if reused_fd is not None:
                    try:
                        real_close(reused_fd)
                    except OSError:
                        pass

    def test_close_same_inode_reuse_never_retries_numeric_fd(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        for module in (evidence_module, mutation_runner):
            with self.subTest(module=module.__name__):
                expected_error = (
                    evidence_module.EvidenceError
                    if module is evidence_module
                    else mutation_runner.UnsafePatchError
                )
                with tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / "same-inode"
                    path.write_text("same inode\n", encoding="utf-8")
                    real_close = module.os.close
                    owner = (
                        evidence_module._OwnedFDCollection()
                        if module is evidence_module
                        else mutation_runner._OwnedFDCollection()
                    )
                    original_fd = os.open(path, os.O_RDONLY)
                    owner.register(original_fd)
                    close_calls = 0
                    replacement_fd = None

                    def close_then_reopen_same_inode(file_fd):
                        nonlocal close_calls, replacement_fd
                        close_calls += 1
                        if close_calls != 1:
                            return real_close(file_fd)
                        real_close(file_fd)
                        opened = os.open(path, os.O_RDONLY)
                        if opened != file_fd:
                            os.dup2(opened, file_fd)
                            real_close(opened)
                        replacement_fd = file_fd
                        raise OSError(
                            errno.EIO,
                            "injected same-inode close ambiguity",
                        )

                    try:
                        with patch.object(
                            module.os,
                            "close",
                            side_effect=close_then_reopen_same_inode,
                        ), self.assertRaises(expected_error) as caught:
                            module._close_registered_fd(
                                owner,
                                original_fd,
                                "same-inode reuse",
                            )
                        self.assertEqual(close_calls, 1)
                        self.assertEqual(replacement_fd, original_fd)
                        self.assertEqual(
                            caught.exception.fd_close_states[
                                original_fd
                            ],
                            "UNKNOWN",
                        )
                        self.assertEqual(
                            caught.exception.unresolved_fds[
                                original_fd
                            ],
                            "UNKNOWN",
                        )
                        self.assertEqual(
                            len(caught.exception.fd_close_states),
                            2,
                        )
                        self.assertNotIn(original_fd, owner)
                        os.fstat(original_fd)
                    finally:
                        owner.close_all_anchors()
                        if replacement_fd is not None:
                            try:
                                real_close(replacement_fd)
                            except OSError:
                                pass

    def test_registered_owner_rejects_external_same_inode_reuse(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        for module in (evidence_module, mutation_runner):
            with self.subTest(module=module.__name__):
                expected_error = (
                    evidence_module.EvidenceError
                    if module is evidence_module
                    else module.UnsafePatchError
                )
                with tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / "same-inode"
                    path.write_text("same inode\n", encoding="utf-8")
                    owner = module._OwnedFDCollection()
                    original_fd = os.open(path, os.O_RDONLY)
                    owner.register(original_fd)
                    real_close = module.os.close
                    real_close(original_fd)
                    replacement_fd = os.open(path, os.O_RDONLY)
                    if replacement_fd != original_fd:
                        os.dup2(replacement_fd, original_fd)
                        real_close(replacement_fd)
                        replacement_fd = original_fd
                    close_calls = 0

                    def count_close(file_fd):
                        nonlocal close_calls
                        close_calls += 1
                        real_close(file_fd)

                    try:
                        with patch.object(
                            module.os,
                            "close",
                            side_effect=count_close,
                        ), self.assertRaises(expected_error) as caught:
                            module._close_registered_fd(
                                owner,
                                original_fd,
                                "external same-inode reuse",
                            )
                        self.assertEqual(close_calls, 0)
                        self.assertEqual(
                            caught.exception.fd_close_states[original_fd],
                            "UNKNOWN",
                        )
                        self.assertNotIn(original_fd, owner)
                        os.fstat(replacement_fd)
                    finally:
                        owner.close_all_anchors()
                        if _fd_is_open(replacement_fd):
                            real_close(replacement_fd)

    def test_registered_owner_rejects_external_different_inode_reuse(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        for module in (evidence_module, mutation_runner):
            with self.subTest(module=module.__name__):
                expected_error = (
                    evidence_module.EvidenceError
                    if module is evidence_module
                    else module.UnsafePatchError
                )
                with tempfile.TemporaryDirectory() as temp:
                    base = Path(temp)
                    original = base / "original"
                    foreign = base / "foreign"
                    original.write_text("original\n", encoding="utf-8")
                    foreign.write_text("foreign\n", encoding="utf-8")
                    owner = module._OwnedFDCollection()
                    original_fd = os.open(original, os.O_RDONLY)
                    owner.register(original_fd)
                    real_close = module.os.close
                    real_close(original_fd)
                    replacement_fd = os.open(foreign, os.O_RDONLY)
                    if replacement_fd != original_fd:
                        os.dup2(replacement_fd, original_fd)
                        real_close(replacement_fd)
                        replacement_fd = original_fd
                    close_calls = 0

                    def count_close(file_fd):
                        nonlocal close_calls
                        close_calls += 1
                        real_close(file_fd)

                    try:
                        with patch.object(
                            module.os,
                            "close",
                            side_effect=count_close,
                        ), self.assertRaises(expected_error) as caught:
                            module._close_registered_fd(
                                owner,
                                original_fd,
                                "external different-inode reuse",
                            )
                        self.assertEqual(close_calls, 0)
                        self.assertEqual(
                            caught.exception.fd_close_states[original_fd],
                            "REUSED_OR_REPLACED",
                        )
                        os.fstat(replacement_fd)
                    finally:
                        owner.close_all_anchors()
                        if _fd_is_open(replacement_fd):
                            real_close(replacement_fd)

    def test_registered_owner_rejects_reused_anchor_without_close(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        for module in (evidence_module, mutation_runner):
            expected_error = (
                evidence_module.EvidenceError
                if module is evidence_module
                else module.UnsafePatchError
            )
            for replacement_kind in ("same-inode", "different-inode"):
                with self.subTest(
                    module=module.__name__,
                    replacement=replacement_kind,
                ), tempfile.TemporaryDirectory() as temp:
                    base = Path(temp)
                    original_path = base / "original"
                    original_path.write_text("original\n", encoding="utf-8")
                    foreign_path = base / "foreign"
                    foreign_path.write_text("foreign\n", encoding="utf-8")
                    replacement_path = (
                        original_path
                        if replacement_kind == "same-inode"
                        else foreign_path
                    )
                    owner = module._OwnedFDCollection()
                    original_fd = os.open(original_path, os.O_RDONLY)
                    replacement_anchor_fd = None
                    close_calls = 0
                    try:
                        owner.register(original_fd)
                        record = owner.record(original_fd)
                        self.assertIsNotNone(record)
                        anchor_fd = record.anchor_fd
                        os.close(anchor_fd)
                        opened = os.open(
                            replacement_path,
                            os.O_RDONLY,
                        )
                        if opened != anchor_fd:
                            os.dup2(opened, anchor_fd)
                            os.close(opened)
                        replacement_anchor_fd = anchor_fd
                        real_close = module.os.close

                        def count_close(file_fd):
                            nonlocal close_calls
                            del file_fd
                            close_calls += 1

                        with patch.object(
                            module.os,
                            "close",
                            side_effect=count_close,
                        ), self.assertRaises(expected_error) as caught:
                            module._close_registered_fd(
                                owner,
                                original_fd,
                                "reused ownership anchor",
                            )
                        self.assertEqual(close_calls, 0)
                        self.assertTrue(_fd_is_open(original_fd))
                        self.assertTrue(
                            _fd_is_open(replacement_anchor_fd)
                        )
                        self.assertEqual(
                            caught.exception.fd_close_states[
                                original_fd
                            ],
                            "UNKNOWN",
                        )
                        self.assertIn(
                            replacement_anchor_fd,
                            caught.exception.unresolved_fds,
                        )
                    finally:
                        for file_fd in (
                            original_fd,
                            replacement_anchor_fd,
                        ):
                            if (
                                file_fd is not None
                                and _fd_is_open(file_fd)
                            ):
                                os.close(file_fd)

    def test_registered_owner_closed_anchor_is_unknown_and_repeat_safe(
        self,
    ) -> None:
        for module in (
            __import__(
                "tests.task_control_center_evidence",
                fromlist=["_OwnedFDCollection"],
            ),
            mutation_runner,
        ):
            expected_error = (
                EvidenceError
                if module.__name__.endswith(
                    "task_control_center_evidence"
                )
                else module.UnsafePatchError
            )
            with self.subTest(module=module.__name__
            ), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "owned"
                path.write_text("owned\n", encoding="utf-8")
                owner = module._OwnedFDCollection()
                file_fd = os.open(path, os.O_RDONLY)
                try:
                    owner.register(file_fd)
                    record = owner.record(file_fd)
                    self.assertIsNotNone(record)
                    os.close(record.anchor_fd)
                    with self.assertRaises(expected_error) as caught:
                        module._close_registered_fd(
                            owner,
                            file_fd,
                            "closed ownership anchor",
                        )
                    self.assertTrue(_fd_is_open(file_fd))
                    self.assertEqual(
                        caught.exception.fd_close_states[file_fd],
                        "UNKNOWN",
                    )
                    self.assertEqual(
                        caught.exception.fd_close_states[
                            record.anchor_fd
                        ],
                        "CLOSED",
                    )
                    with self.assertRaises(expected_error):
                        module._close_registered_fd(
                            owner,
                            file_fd,
                            "repeated invalid anchor cleanup",
                        )
                    self.assertTrue(_fd_is_open(file_fd))
                finally:
                    if _fd_is_open(file_fd):
                        os.close(file_fd)

    def test_registered_owner_anchor_observation_failure_retains_fds(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        for module in (evidence_module, mutation_runner):
            expected_error = (
                evidence_module.EvidenceError
                if module is evidence_module
                else module.UnsafePatchError
            )
            with self.subTest(
                module=module.__name__,
            ), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "owned"
                path.write_text("owned\n", encoding="utf-8")
                owner = module._OwnedFDCollection()
                file_fd = os.open(path, os.O_RDONLY)
                try:
                    owner.register(file_fd)
                    record = owner.record(file_fd)
                    self.assertIsNotNone(record)
                    real_fstat = module.os.fstat

                    def fail_anchor_fstat(file_fd_to_observe):
                        if file_fd_to_observe == record.anchor_fd:
                            raise OSError(
                                errno.EIO,
                                "injected anchor observation failure",
                            )
                        return real_fstat(file_fd_to_observe)

                    with patch.object(
                        module.os,
                        "fstat",
                        side_effect=fail_anchor_fstat,
                    ), self.assertRaises(expected_error) as caught:
                        module._close_registered_fd(
                            owner,
                            file_fd,
                            "anchor observation failure",
                        )
                    self.assertEqual(
                        caught.exception.fd_close_states[file_fd],
                        "UNKNOWN",
                    )
                    self.assertEqual(
                        caught.exception.fd_close_states[
                            record.anchor_fd
                        ],
                        "UNKNOWN",
                    )
                    self.assertTrue(_fd_is_open(file_fd))
                    self.assertTrue(_fd_is_open(record.anchor_fd))
                finally:
                    for file_fd_to_close in (
                        file_fd,
                        record.anchor_fd
                        if "record" in locals()
                        and record is not None
                        else None,
                    ):
                        if (
                            file_fd_to_close is not None
                            and _fd_is_open(file_fd_to_close)
                        ):
                            os.close(file_fd_to_close)

    def test_registered_owner_anchor_close_failure_is_diagnosed(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        for module in (evidence_module, mutation_runner):
            expected_error = (
                evidence_module.EvidenceError
                if module is evidence_module
                else module.UnsafePatchError
            )
            with self.subTest(
                module=module.__name__,
            ), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "owned"
                path.write_text("owned\n", encoding="utf-8")
                owner = module._OwnedFDCollection()
                file_fd = os.open(path, os.O_RDONLY)
                try:
                    owner.register(file_fd)
                    record = owner.record(file_fd)
                    self.assertIsNotNone(record)
                    real_close = module._OS_CLOSE

                    def fail_anchor_close(file_fd_to_close):
                        if file_fd_to_close == record.anchor_fd:
                            raise OSError(
                                errno.EIO,
                                "injected anchor close failure",
                            )
                        return real_close(file_fd_to_close)

                    with patch.object(
                        module,
                        "_OS_CLOSE",
                        side_effect=fail_anchor_close,
                    ), self.assertRaises(expected_error) as caught:
                        module._close_registered_fd(
                            owner,
                            file_fd,
                            "anchor close failure",
                        )
                    self.assertEqual(
                        caught.exception.fd_close_states[file_fd],
                        "CLOSED",
                    )
                    self.assertEqual(
                        caught.exception.fd_close_states[
                            record.anchor_fd
                        ],
                        "UNKNOWN",
                    )
                    self.assertEqual(
                        caught.exception.unresolved_fds[
                            record.anchor_fd
                        ],
                        "UNKNOWN",
                    )
                    self.assertTrue(_fd_is_open(record.anchor_fd))
                finally:
                    for file_fd_to_close in (
                        file_fd,
                        record.anchor_fd
                        if "record" in locals()
                        and record is not None
                        else None,
                    ):
                        if (
                            file_fd_to_close is not None
                            and _fd_is_open(file_fd_to_close)
                        ):
                            os.close(file_fd_to_close)

    def test_registered_owner_abandon_rejects_reused_anchor(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        for module in (evidence_module, mutation_runner):
            expected_error = (
                evidence_module.EvidenceError
                if module is evidence_module
                else module.UnsafePatchError
            )
            for replacement_kind in ("same-inode", "different-inode"):
                with self.subTest(
                    module=module.__name__,
                    replacement=replacement_kind,
                ), tempfile.TemporaryDirectory() as temp:
                    base = Path(temp)
                    original = base / "original"
                    foreign = base / "foreign"
                    original.write_text("original\n", encoding="utf-8")
                    foreign.write_text("foreign\n", encoding="utf-8")
                    replacement_path = (
                        original
                        if replacement_kind == "same-inode"
                        else foreign
                    )
                    owner = module._OwnedFDCollection()
                    original_fd = os.open(original, os.O_RDONLY)
                    replacement_anchor_fd = None
                    try:
                        owner.register(original_fd)
                        record = owner.record(original_fd)
                        self.assertIsNotNone(record)
                        anchor_fd = record.anchor_fd
                        os.close(anchor_fd)
                        opened = os.open(
                            replacement_path,
                            os.O_RDONLY,
                        )
                        if opened != anchor_fd:
                            os.dup2(opened, anchor_fd)
                            os.close(opened)
                        replacement_anchor_fd = anchor_fd
                        with self.assertRaises(expected_error) as caught:
                            owner.abandon_anchor(original_fd)
                        self.assertTrue(_fd_is_open(original_fd))
                        self.assertTrue(
                            _fd_is_open(replacement_anchor_fd)
                        )
                        self.assertIn(
                            replacement_anchor_fd,
                            caught.exception.unresolved_fds,
                        )
                        self.assertIn(original_fd, owner)
                        owner.close_all_anchors()
                        self.assertTrue(_fd_is_open(original_fd))
                        self.assertTrue(
                            _fd_is_open(replacement_anchor_fd)
                        )
                        self.assertIn(
                            replacement_anchor_fd,
                            owner._unresolved_anchor_states,
                        )
                    finally:
                        for file_fd in (
                            original_fd,
                            replacement_anchor_fd,
                        ):
                            if (
                                file_fd is not None
                                and _fd_is_open(file_fd)
                            ):
                                os.close(file_fd)

    def test_registered_owner_primary_close_failure_retains_anchor(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        for module in (evidence_module, mutation_runner):
            expected_error = (
                evidence_module.EvidenceError
                if module is evidence_module
                else module.UnsafePatchError
            )
            with self.subTest(
                module=module.__name__,
            ), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "owned"
                path.write_text("owned\n", encoding="utf-8")
                owner = module._OwnedFDCollection()
                file_fd = os.open(path, os.O_RDONLY)
                record = None
                try:
                    owner.register(file_fd)
                    record = owner.record(file_fd)
                    self.assertIsNotNone(record)
                    real_close = module.os.close

                    def fail_primary_close(file_fd_to_close):
                        if file_fd_to_close == file_fd:
                            raise OSError(
                                errno.EIO,
                                "injected primary-only close failure",
                            )
                        return real_close(file_fd_to_close)

                    with patch.object(
                        module.os,
                        "close",
                        side_effect=fail_primary_close,
                    ), self.assertRaises(expected_error) as caught:
                        module._close_registered_fd(
                            owner,
                            file_fd,
                            "primary-only close failure",
                        )
                    self.assertIsNotNone(owner.record(file_fd))
                    self.assertTrue(_fd_is_open(file_fd))
                    self.assertTrue(_fd_is_open(record.anchor_fd))
                    self.assertEqual(
                        caught.exception.fd_close_states[file_fd],
                        "UNKNOWN",
                    )
                    self.assertEqual(
                        caught.exception.fd_close_states[
                            record.anchor_fd
                        ],
                        "UNKNOWN",
                    )
                finally:
                    owner.close_all_anchors()
                    for file_fd_to_close in (
                        file_fd,
                        record.anchor_fd
                        if record is not None
                        else None,
                    ):
                        if (
                            file_fd_to_close is not None
                            and _fd_is_open(file_fd_to_close)
                        ):
                            os.close(file_fd_to_close)

    def test_registered_owner_lock_release_failure_retains_anchor(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        for module in (evidence_module, mutation_runner):
            expected_error = (
                evidence_module.EvidenceError
                if module is evidence_module
                else module.UnsafePatchError
            )
            with self.subTest(
                module=module.__name__,
            ), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "owned"
                path.write_text("owned\n", encoding="utf-8")
                owner = module._OwnedFDCollection()
                file_fd = os.open(path, os.O_RDONLY)
                record = None
                try:
                    owner.register(file_fd)
                    record = owner.record(file_fd)
                    self.assertIsNotNone(record)
                    real_release = module._release_ownership_lock

                    def fail_anchor_unlock(anchor_fd):
                        if anchor_fd == record.anchor_fd:
                            return OSError(
                                errno.EIO,
                                "injected anchor unlock failure",
                            )
                        return real_release(anchor_fd)

                    with patch.object(
                        module,
                        "_release_ownership_lock",
                        side_effect=fail_anchor_unlock,
                    ), self.assertRaises(expected_error) as caught:
                        module._close_registered_fd(
                            owner,
                            file_fd,
                            "anchor unlock failure",
                        )
                    self.assertEqual(
                        caught.exception.fd_close_states[file_fd],
                        "CLOSED",
                    )
                    self.assertEqual(
                        caught.exception.fd_close_states[
                            record.anchor_fd
                        ],
                        "UNKNOWN",
                    )
                    self.assertIsNotNone(owner.record(file_fd))
                    self.assertTrue(_fd_is_open(record.anchor_fd))
                finally:
                    owner.close_all_anchors()
                    for file_fd_to_close in (
                        file_fd,
                        record.anchor_fd
                        if record is not None
                        else None,
                    ):
                        if (
                            file_fd_to_close is not None
                            and _fd_is_open(file_fd_to_close)
                        ):
                            os.close(file_fd_to_close)

    def test_registered_owner_cleanup_is_serialized(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        for module in (evidence_module, mutation_runner):
            with self.subTest(
                module=module.__name__,
            ), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "owned"
                path.write_text("owned\n", encoding="utf-8")
                owner = module._OwnedFDCollection()
                file_fd = os.open(path, os.O_RDONLY)
                owner.register(file_fd)
                first_entered = threading.Event()
                release_first = threading.Event()
                second_started = threading.Event()
                second_done = threading.Event()
                real_locked = module._close_registered_fd_locked

                def blocked_locked(
                    opened_fds,
                    target_fd,
                    label,
                    **kwargs,
                ):
                    if not first_entered.is_set():
                        first_entered.set()
                        release_first.wait(timeout=5)
                    return real_locked(
                        opened_fds,
                        target_fd,
                        label,
                        **kwargs,
                    )

                def first_cleanup():
                    module._close_registered_fd(
                        owner,
                        file_fd,
                        "first serialized cleanup",
                    )

                def second_cleanup():
                    second_started.set()
                    try:
                        module._close_registered_fd(
                            owner,
                            file_fd,
                            "second serialized cleanup",
                        )
                    except BaseException:
                        pass
                    finally:
                        second_done.set()

                try:
                    with patch.object(
                        module,
                        "_close_registered_fd_locked",
                        side_effect=blocked_locked,
                    ):
                        first_thread = threading.Thread(
                            target=first_cleanup,
                        )
                        second_thread = threading.Thread(
                            target=second_cleanup,
                        )
                        first_thread.start()
                        self.assertTrue(
                            first_entered.wait(timeout=5)
                        )
                        second_thread.start()
                        self.assertTrue(
                            second_started.wait(timeout=5)
                        )
                        self.assertFalse(
                            second_done.wait(timeout=0.05)
                        )
                        release_first.set()
                        first_thread.join(timeout=5)
                        second_thread.join(timeout=5)
                    self.assertFalse(first_thread.is_alive())
                    self.assertFalse(second_thread.is_alive())
                    with self.assertRaises(OSError):
                        os.fstat(file_fd)
                finally:
                    release_first.set()
                    for owner_anchor in list(
                        owner._records.values()
                    ):
                        try:
                            os.close(owner_anchor.anchor_fd)
                        except OSError:
                            pass
                    if _fd_is_open(file_fd):
                        os.close(file_fd)

    def test_close_all_anchors_is_serialized(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        for module in (evidence_module, mutation_runner):
            with self.subTest(
                module=module.__name__,
            ), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "owned"
                path.write_text("owned\n", encoding="utf-8")
                owner = module._OwnedFDCollection()
                file_fd = os.open(path, os.O_RDONLY)
                owner.register(file_fd)
                anchor_fd = owner.record(file_fd).anchor_fd
                first_entered = threading.Event()
                release_first = threading.Event()
                second_started = threading.Event()
                second_done = threading.Event()
                real_close = module._OS_CLOSE

                def blocked_anchor_close(file_fd_to_close):
                    if (
                        file_fd_to_close == anchor_fd
                        and not first_entered.is_set()
                    ):
                        first_entered.set()
                        release_first.wait(timeout=5)
                    return real_close(file_fd_to_close)

                def first_cleanup():
                    owner.close_all_anchors()

                def second_cleanup():
                    second_started.set()
                    try:
                        owner.close_all_anchors()
                    finally:
                        second_done.set()

                try:
                    with patch.object(
                        module,
                        "_OS_CLOSE",
                        side_effect=blocked_anchor_close,
                    ):
                        first_thread = threading.Thread(
                            target=first_cleanup,
                        )
                        second_thread = threading.Thread(
                            target=second_cleanup,
                        )
                        first_thread.start()
                        self.assertTrue(
                            first_entered.wait(timeout=5)
                        )
                        second_thread.start()
                        self.assertTrue(
                            second_started.wait(timeout=5)
                        )
                        self.assertFalse(
                            second_done.wait(timeout=0.05)
                        )
                        release_first.set()
                        first_thread.join(timeout=5)
                        second_thread.join(timeout=5)
                    self.assertFalse(first_thread.is_alive())
                    self.assertFalse(second_thread.is_alive())
                    with self.assertRaises(OSError):
                        os.fstat(anchor_fd)
                    self.assertTrue(_fd_is_open(file_fd))
                finally:
                    release_first.set()
                    if _fd_is_open(file_fd):
                        os.close(file_fd)

    def test_unregistered_numeric_owner_is_not_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "unregistered"
            path.write_text("unregistered\n", encoding="utf-8")
            file_fd = os.open(path, os.O_RDONLY)
            owner = [file_fd]
            real_close = mutation_runner.os.close
            calls = 0

            def count_close(observed_fd):
                nonlocal calls
                calls += 1
                real_close(observed_fd)

            try:
                with patch.object(
                    mutation_runner.os,
                    "close",
                    side_effect=count_close,
                ), self.assertRaises(
                    mutation_runner.UnsafePatchError
                ) as caught:
                    mutation_runner._close_registered_fd(
                        owner,
                        file_fd,
                        "unregistered owner",
                    )
                self.assertEqual(calls, 0)
                self.assertEqual(
                    caught.exception.fd_close_states[file_fd],
                    "UNKNOWN",
                )
                self.assertTrue(_fd_is_open(file_fd))
            finally:
                try:
                    real_close(file_fd)
                except OSError:
                    pass

    def test_ownership_lock_capability_failure_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "lock-failure"
            path.write_text("lock failure\n", encoding="utf-8")
            owner, file_fd = _owned_file_fd(
                path,
                owner_type=mutation_runner._OwnedFDCollection,
            )
            real_close = mutation_runner.os.close
            close_calls = 0

            def unsupported_lock(*args, **kwargs):
                del args, kwargs
                raise OSError(errno.ENOTSUP, "flock unsupported")

            def count_close(observed_fd):
                nonlocal close_calls
                close_calls += 1
                real_close(observed_fd)

            try:
                with patch.object(
                    mutation_runner.fcntl,
                    "flock",
                    side_effect=unsupported_lock,
                ), patch.object(
                    mutation_runner.os,
                    "close",
                    side_effect=count_close,
                ), self.assertRaises(
                    mutation_runner.UnsafePatchError
                ) as caught:
                    mutation_runner._close_registered_fd(
                        owner,
                        file_fd,
                        "unsupported ownership lock",
                    )
                self.assertEqual(close_calls, 0)
                self.assertEqual(
                    caught.exception.fd_close_states[file_fd],
                    "UNKNOWN",
                )
                self.assertTrue(_fd_is_open(file_fd))
            finally:
                owner.close_all_anchors()
                try:
                    real_close(file_fd)
                except OSError:
                    pass

    def test_owned_fd_collections_release_anchors_without_growth(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            path = base / "owned"
            path.write_text("owned\n", encoding="utf-8")
            before = len(os.listdir("/dev/fd"))
            for _index in range(50):
                owner = mutation_runner._OwnedFDCollection()
                file_fd = os.open(path, os.O_RDONLY)
                owner.register(file_fd)
                mutation_runner._close_owned_fds(
                    owner,
                    "repeated owned collection",
                )
                self.assertEqual(owner, [])
            after = len(os.listdir("/dev/fd"))
            self.assertLessEqual(after, before + 1)

    def test_trusted_root_same_inode_reuse_does_not_close_replacement(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            root_path = Path(temp)
            root = TrustedEvidenceRoot(
                root_path,
                "same-inode trusted root",
            )
            primary_fd = root._fd
            anchor_fd = root._anchor_fd
            self.assertIsNotNone(primary_fd)
            self.assertIsNotNone(anchor_fd)
            primary_private_anchor = root._fd_records[
                primary_fd
            ].anchor_fd
            real_close = evidence_module.os.close
            close_calls = 0
            replacement_fd = None

            def close_then_reopen_same_inode(file_fd):
                nonlocal close_calls, replacement_fd
                close_calls += 1
                if close_calls != 1:
                    return real_close(file_fd)
                real_close(file_fd)
                opened = os.open(
                    root_path,
                    os.O_RDONLY | os.O_DIRECTORY,
                )
                if opened != file_fd:
                    os.dup2(opened, file_fd)
                    real_close(opened)
                replacement_fd = file_fd
                raise OSError(
                    errno.EIO,
                    "injected root same-inode close ambiguity",
                )

            with patch.object(
                evidence_module.os,
                "close",
                side_effect=close_then_reopen_same_inode,
            ), self.assertRaises(EvidenceError) as caught:
                root.close()
            self.assertEqual(close_calls, 2)
            self.assertEqual(replacement_fd, primary_fd)
            self.assertEqual(
                caught.exception.fd_close_states,
                {
                    primary_fd: "UNKNOWN",
                    primary_private_anchor: "UNKNOWN",
                    anchor_fd: "CLOSED",
                },
            )
            self.assertEqual(
                root._unresolved_fds,
                {
                    primary_fd: "UNKNOWN",
                    primary_private_anchor: "UNKNOWN",
                },
            )
            self.assertIsNone(root._fd)
            self.assertIsNone(root._anchor_fd)
            os.fstat(primary_fd)
            with self.assertRaises(OSError):
                os.fstat(anchor_fd)
            real_close(primary_fd)

    def test_trusted_root_rejects_external_same_inode_reuse(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            root_path = Path(temp)
            root = TrustedEvidenceRoot(
                root_path,
                "external same-inode trusted root",
            )
            primary_fd = root._fd
            anchor_fd = root._anchor_fd
            primary_private_anchor = root._fd_records[
                primary_fd
            ].anchor_fd
            self.assertIsNotNone(primary_fd)
            self.assertIsNotNone(anchor_fd)
            real_close = evidence_module.os.close
            real_close(primary_fd)
            replacement_fd = os.open(
                root_path,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            if replacement_fd != primary_fd:
                os.dup2(replacement_fd, primary_fd)
                real_close(replacement_fd)
                replacement_fd = primary_fd
            calls = {primary_fd: 0, anchor_fd: 0}

            def count_close(file_fd):
                calls[file_fd] = calls.get(file_fd, 0) + 1
                real_close(file_fd)

            try:
                with patch.object(
                    evidence_module.os,
                    "close",
                    side_effect=count_close,
                ), self.assertRaises(EvidenceError) as caught:
                    root.close()
                self.assertEqual(calls[primary_fd], 0)
                self.assertEqual(calls[anchor_fd], 1)
                self.assertEqual(
                    caught.exception.fd_close_states[primary_fd],
                    "UNKNOWN",
                )
                self.assertEqual(
                    root._unresolved_fds[primary_fd],
                    "UNKNOWN",
                )
                self.assertTrue(_fd_is_open(replacement_fd))
            finally:
                if _fd_is_open(replacement_fd):
                    real_close(replacement_fd)

    def test_trusted_root_rejects_reused_private_anchor(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root_path = base / "root"
            root_path.mkdir()
            foreign = base / "foreign"
            foreign.write_text("foreign\n", encoding="utf-8")
            root = TrustedEvidenceRoot(
                root_path,
                "private anchor replacement root",
            )
            primary_fd = root._fd
            self.assertIsNotNone(primary_fd)
            record = root._fd_records[primary_fd]
            private_anchor_fd = record.anchor_fd
            os.close(private_anchor_fd)
            opened = os.open(foreign, os.O_RDONLY)
            if opened != private_anchor_fd:
                os.dup2(opened, private_anchor_fd)
                os.close(opened)
            try:
                with self.assertRaises(EvidenceError) as caught:
                    root.close()
                self.assertTrue(_fd_is_open(primary_fd))
                self.assertTrue(_fd_is_open(private_anchor_fd))
                self.assertEqual(
                    caught.exception.fd_close_states[primary_fd],
                    "UNKNOWN",
                )
                self.assertEqual(
                    caught.exception.fd_close_states[
                        private_anchor_fd
                    ],
                    "REUSED_OR_REPLACED",
                )
                self.assertIn(
                    private_anchor_fd,
                    caught.exception.unresolved_fds,
                )
            finally:
                for file_fd in (
                    primary_fd,
                    private_anchor_fd,
                    root._fd,
                    root._anchor_fd,
                ):
                    if (
                        file_fd is not None
                        and _fd_is_open(file_fd)
                    ):
                        os.close(file_fd)

    def test_trusted_root_close_retires_uncertain_primary_owner(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            root = TrustedEvidenceRoot(
                Path(temp),
                "retained close owner",
            )
            primary_fd = root._fd
            anchor_fd = root._anchor_fd
            primary_private_anchor = root._fd_records[
                primary_fd
            ].anchor_fd
            real_close = evidence_module.os.close
            calls = 0
            owner_retained_before_first_attempt = False

            def interrupt_before_syscall(file_fd):
                nonlocal calls, owner_retained_before_first_attempt
                calls += 1
                if calls == 1:
                    owner_retained_before_first_attempt = (
                        file_fd == root._fd
                    )
                    raise KeyboardInterrupt(
                        "injected root pre-native-close interrupt"
                    )
                return real_close(file_fd)

            with patch.object(
                evidence_module.os,
                "close",
                side_effect=interrupt_before_syscall,
            ), self.assertRaisesRegex(
                KeyboardInterrupt,
                "root pre-native-close",
            ) as caught:
                root.close()
            self.assertTrue(owner_retained_before_first_attempt)
            self.assertEqual(calls, 2)
            self.assertIsNone(root._fd)
            self.assertIsNone(root._anchor_fd)
            self.assertEqual(
                caught.exception.fd_close_states,
                {
                    primary_fd: "UNKNOWN",
                    primary_private_anchor: "UNKNOWN",
                    anchor_fd: "CLOSED",
                },
            )
            self.assertEqual(
                root._unresolved_fds,
                {
                    primary_fd: "UNKNOWN",
                    primary_private_anchor: "UNKNOWN",
                },
            )
            os.fstat(primary_fd)
            with self.assertRaises(OSError):
                os.fstat(anchor_fd)
            real_close(primary_fd)

    def test_trusted_root_close_oserror_retires_both_numeric_fds(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            root = TrustedEvidenceRoot(
                Path(temp),
                "OSError root close",
            )
            primary_fd = root._fd
            anchor_fd = root._anchor_fd
            primary_private_anchor = root._fd_records[
                primary_fd
            ].anchor_fd
            anchor_private_anchor = root._fd_records[
                anchor_fd
            ].anchor_fd
            real_close = evidence_module.os.close
            calls_by_fd: dict[int, int] = {}

            def close_before_native(file_fd):
                count = calls_by_fd.get(file_fd, 0) + 1
                calls_by_fd[file_fd] = count
                raise OSError(
                    "injected root pre-native close OSError"
                )

            with patch.object(
                evidence_module.os,
                "close",
                side_effect=close_before_native,
            ), self.assertRaises(EvidenceError) as caught:
                root.close()
            self.assertIsNone(root._fd)
            self.assertIsNone(root._anchor_fd)
            self.assertEqual(
                calls_by_fd,
                {
                    primary_fd: 1,
                    anchor_fd: 1,
                },
            )
            self.assertEqual(
                caught.exception.fd_close_states,
                {
                    primary_fd: "UNKNOWN",
                    primary_private_anchor: "UNKNOWN",
                    anchor_fd: "UNKNOWN",
                    anchor_private_anchor: "UNKNOWN",
                },
            )
            self.assertEqual(
                root._unresolved_fds,
                {
                    primary_fd: "UNKNOWN",
                    primary_private_anchor: "UNKNOWN",
                    anchor_fd: "UNKNOWN",
                    anchor_private_anchor: "UNKNOWN",
                },
            )
            os.fstat(primary_fd)
            os.fstat(primary_private_anchor)
            os.fstat(anchor_fd)
            os.fstat(anchor_private_anchor)
            real_close(primary_fd)
            real_close(primary_private_anchor)
            real_close(anchor_fd)
            real_close(anchor_private_anchor)

    def test_close_diagnostics_aggregate_across_owned_fds(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "first").write_text("first\n", encoding="utf-8")
            (base / "second").write_text("second\n", encoding="utf-8")
            first_owner, first_fd = _owned_file_fd(
                base / "first",
                owner_type=mutation_runner._OwnedFDCollection,
            )
            second_owner, second_fd = _owned_file_fd(
                base / "second",
                owner_type=mutation_runner._OwnedFDCollection,
            )
            ownership = {
                first_fd: first_owner.record(first_fd),
                second_fd: second_owner.record(second_fd),
            }

            def fail_before_native(file_fd):
                raise OSError(f"injected close failure for {file_fd}")

            with patch.object(
                mutation_runner.os,
                "close",
                side_effect=fail_before_native,
            ), self.assertRaisesRegex(
                RuntimeError,
                "primary operation",
            ) as caught:
                try:
                    raise RuntimeError("primary operation")
                finally:
                    mutation_runner._close_fds_preserving_primary(
                        [
                            (first_fd, "first close", None),
                            (second_fd, "second close", None),
                        ],
                        ownership_records=ownership,
                    )
            self.assertEqual(
                caught.exception.close_diagnostics,
                [
                    f"first close: injected close failure for {first_fd}",
                    f"second close: injected close failure for {second_fd}",
                ],
            )
            self.assertEqual(
                len(
                    [
                        note
                        for note in caught.exception.__notes__
                        if "close failed" in note
                    ]
                ),
                2,
            )

    def test_evidence_scope_close_oserror_preserves_transaction_metadata(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            transaction_state = {
                "transaction_state": "COMMITTED",
                "residual_paths": ["payload.json"],
                "residual_unknown": False,
                "durability_state": "UNKNOWN",
                "target_state": "COMMITTED",
            }
            directory_fd = None
            real_close = evidence_module.os.close
            failed = False

            def close_then_error(file_fd):
                nonlocal failed
                if file_fd == directory_fd and not failed:
                    failed = True
                    real_close(file_fd)
                    raise OSError(
                        "injected observation close failure"
                    )
                return real_close(file_fd)

            with TrustedEvidenceRoot(base, "scope close diagnostics") as root:
                with patch.object(
                    evidence_module.os,
                    "close",
                    side_effect=close_then_error,
                ), self.assertRaisesRegex(
                    RuntimeError,
                    "primary directory operation",
                ) as caught:
                    with root._relative_directory_scope(
                        "payload.json",
                        "scope diagnostics",
                        transaction_state,
                    ) as opened_fd:
                        directory_fd = opened_fd
                        raise RuntimeError("primary directory operation")
            self.assertTrue(failed)
            self.assertEqual(
                caught.exception.transaction_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.durability_state,
                "UNKNOWN",
            )
            self.assertEqual(
                caught.exception.target_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.residual_paths,
                ["payload.json"],
            )
            self.assertTrue(caught.exception.close_diagnostics)
            self.assertIn(
                "injected observation close failure",
                "\n".join(caught.exception.close_diagnostics),
            )

    def test_evidence_cleanup_observation_interrupt_preserves_primary(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_stat = evidence_module.os.stat

            def interrupt_temporary_observation(
                path,
                *args,
                **kwargs,
            ):
                if str(path).startswith(".payload.json.tmp-"):
                    raise KeyboardInterrupt(
                        "injected cleanup observation interrupt"
                    )
                return real_stat(path, *args, **kwargs)

            with TrustedEvidenceRoot(
                base,
                "cleanup observation interrupt root",
            ) as root:
                with patch.object(
                    evidence_module.os,
                    "write",
                    side_effect=PermissionError("write primary"),
                ), patch.object(
                    evidence_module.os,
                    "stat",
                    side_effect=interrupt_temporary_observation,
                ), self.assertRaises(EvidenceError) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "cleanup observation payload",
                    )
            self.assertIsInstance(
                caught.exception.__cause__,
                PermissionError,
            )
            self.assertEqual(
                caught.exception.transaction_state,
                "UNCOMMITTED",
            )
            self.assertEqual(
                caught.exception.durability_state,
                "NOT_ATTEMPTED",
            )
            self.assertEqual(
                caught.exception.target_state,
                "ABSENT",
            )
            self.assertTrue(caught.exception.residual_unknown)
            self.assertIn(
                "cleanup observation interrupt",
                "\n".join(caught.exception.__notes__),
            )
            self.assertFalse(target.exists())
            self.assertEqual(
                len(
                    [
                        path
                        for path in base.iterdir()
                        if path.name.startswith(".payload.json.tmp-")
                    ]
                ),
                1,
            )

    def test_evidence_cleanup_close_interrupt_preserves_primary(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            transaction_state = {
                "transaction_state": "UNCOMMITTED",
                "residual_paths": [],
                "residual_unknown": True,
            }
            directory_fd = None
            real_close = evidence_module.os.close
            failed = False

            def close_then_interrupt(file_fd):
                nonlocal failed
                if file_fd == directory_fd and not failed:
                    failed = True
                    real_close(file_fd)
                    raise KeyboardInterrupt(
                        "injected cleanup close interrupt"
                    )
                return real_close(file_fd)

            with TrustedEvidenceRoot(base, "scope close interrupt") as root:
                with patch.object(
                    evidence_module.os,
                    "close",
                    side_effect=close_then_interrupt,
                ), self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "primary cleanup interrupt",
                ) as caught:
                    with root._relative_directory_scope(
                        "payload.json",
                        "scope close interrupt",
                        transaction_state,
                    ) as opened_fd:
                        directory_fd = opened_fd
                        raise KeyboardInterrupt(
                            "primary cleanup interrupt"
                        )
            self.assertTrue(failed)
            self.assertEqual(
                caught.exception.transaction_state,
                "UNCOMMITTED",
            )
            self.assertTrue(caught.exception.residual_unknown)
            self.assertIn(
                "injected cleanup close interrupt",
                "\n".join(caught.exception.close_diagnostics),
            )

    def test_mutation_snapshot_write_error_preserves_close_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "value.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            metadata = target.stat()
            temporary_fds: set[int] = set()
            real_open = mutation_runner._OS_OPEN
            real_close = mutation_runner.os.close
            close_failed = False

            def capture_temporary(path, flags, *args, **kwargs):
                opened = real_open(path, flags, *args, **kwargs)
                if (
                    str(path).startswith(".value.py.mutation-")
                    and flags & os.O_WRONLY
                ):
                    temporary_fds.add(opened)
                return opened

            def fail_temporary_close(file_fd):
                nonlocal close_failed
                if file_fd in temporary_fds and not close_failed:
                    close_failed = True
                    real_close(file_fd)
                    raise OSError("injected temporary close failure")
                real_close(file_fd)

            with patch.object(
                mutation_runner,
                "_OS_OPEN",
                side_effect=capture_temporary,
            ), patch.object(
                mutation_runner.os,
                "write",
                side_effect=OSError("injected snapshot write failure"),
            ), patch.object(
                mutation_runner.os,
                "close",
                side_effect=fail_temporary_close,
            ), self.assertRaisesRegex(
                OSError,
                "injected snapshot write failure",
            ) as caught:
                mutation_runner._write_snapshot_file(
                    root,
                    "value.py",
                    "VALUE = 2\n",
                    (metadata.st_dev, metadata.st_ino),
                    mutation_runner.hashlib.sha256(
                        b"VALUE = 1\n"
                    ).hexdigest(),
                    stat.S_IMODE(metadata.st_mode),
                )
            self.assertTrue(close_failed)
            self.assertIn(
                "injected temporary close failure",
                "\n".join(caught.exception.__notes__),
            )
            self.assertIn(
                "injected temporary close failure",
                "\n".join(caught.exception.close_diagnostics),
            )
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "VALUE = 1\n",
            )
            self.assertEqual(
                [
                    path.name
                    for path in root.iterdir()
                    if path.name.startswith(".value.py.mutation-")
                ],
                [],
            )

    def test_mutation_snapshot_temporary_owned_before_mask_restore(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "value.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            metadata = target.stat()
            real_open = mutation_runner._OS_OPEN
            real_mask = mutation_runner.signal.pthread_sigmask
            temporary_fds: list[int] = []
            trigger = False

            def capture_open(path, *args, **kwargs):
                nonlocal trigger
                opened = real_open(path, *args, **kwargs)
                if str(path).startswith(".value.py.mutation-"):
                    temporary_fds.append(opened)
                    trigger = True
                return opened

            def interrupt_mask_restore(signals, mask):
                nonlocal trigger
                previous = real_mask(signals, mask)
                if signals == signal.SIG_SETMASK and trigger:
                    trigger = False
                    raise KeyboardInterrupt(
                        "injected temporary ownership interrupt"
                    )
                return previous

            with patch.object(
                mutation_runner,
                "_OS_OPEN",
                side_effect=capture_open,
            ), patch.object(
                mutation_runner.signal,
                "pthread_sigmask",
                side_effect=interrupt_mask_restore,
            ), self.assertRaisesRegex(
                KeyboardInterrupt,
                "temporary ownership",
            ):
                mutation_runner._write_snapshot_file(
                    root,
                    "value.py",
                    "VALUE = 2\n",
                    (metadata.st_dev, metadata.st_ino),
                    mutation_runner.hashlib.sha256(
                        b"VALUE = 1\n"
                    ).hexdigest(),
                    stat.S_IMODE(metadata.st_mode),
                )
            self.assertEqual(len(temporary_fds), 1)
            with self.assertRaises(OSError):
                os.fstat(temporary_fds[0])
            self.assertEqual(
                [
                    path.name
                    for path in root.iterdir()
                    if path.name.startswith(".value.py.mutation-")
                ],
                [],
            )
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "VALUE = 1\n",
            )

    def test_mutation_snapshot_post_replace_interrupt_reports_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "value.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            metadata = target.stat()
            real_replace = mutation_runner.os.replace

            def replace_then_interrupt(*args, **kwargs):
                real_replace(*args, **kwargs)
                raise KeyboardInterrupt(
                    "injected post-replace interrupt"
                )

            with patch.object(
                mutation_runner.os,
                "replace",
                side_effect=replace_then_interrupt,
            ), self.assertRaisesRegex(
                KeyboardInterrupt,
                "post-replace",
            ) as caught:
                mutation_runner._write_snapshot_file(
                    root,
                    "value.py",
                    "VALUE = 2\n",
                    (metadata.st_dev, metadata.st_ino),
                    mutation_runner.hashlib.sha256(
                        b"VALUE = 1\n"
                    ).hexdigest(),
                    stat.S_IMODE(metadata.st_mode),
                )
            self.assertEqual(
                caught.exception.residual_paths,
                ["value.py"],
            )
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "VALUE = 2\n",
            )

    def test_evidence_staging_post_replace_interrupt_reports_commit(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_replace = evidence_module.os.replace

            def replace_then_interrupt(*args, **kwargs):
                real_replace(*args, **kwargs)
                raise KeyboardInterrupt(
                    "injected evidence post-replace interrupt"
                )

            with TrustedEvidenceRoot(
                base,
                "evidence snapshot ownership root",
            ) as root:
                with patch.object(
                    evidence_module.os,
                    "replace",
                    side_effect=replace_then_interrupt,
                ), self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "evidence post-replace",
                ) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "evidence staging payload",
                    )
            self.assertEqual(
                caught.exception.residual_paths,
                ["payload.json"],
            )
            self.assertEqual(
                target.read_bytes(),
                b'{"value":1}\n',
            )

    def test_evidence_replace_interrupt_before_local_flag_keeps_commit(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_replace_and_record = (
                evidence_module._replace_and_record_namespace_commit
            )

            def replace_foreign_then_interrupt(
                source_name,
                destination_name,
                *,
                source_directory_fd,
                destination_directory_fd,
                transaction_state,
            ):
                result = real_replace_and_record(
                    source_name,
                    destination_name,
                    source_directory_fd=source_directory_fd,
                    destination_directory_fd=destination_directory_fd,
                    transaction_state=transaction_state,
                )
                target.write_bytes(b"FOREIGN-RACE\n")
                raise KeyboardInterrupt(
                    "injected pre-local-flag interrupt"
                )

            with TrustedEvidenceRoot(
                base,
                "commit fact ownership root",
            ) as root:
                with patch.object(
                    evidence_module,
                    "_replace_and_record_namespace_commit",
                    side_effect=replace_foreign_then_interrupt,
                ), self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "pre-local-flag",
                ) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "commit fact payload",
                    )
            self.assertEqual(
                caught.exception.transaction_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.durability_state,
                "UNKNOWN",
            )
            self.assertEqual(
                caught.exception.target_state,
                "CHANGED",
            )
            self.assertEqual(
                caught.exception.residual_paths,
                ["payload.json"],
            )
            self.assertEqual(
                target.read_bytes(),
                b"FOREIGN-RACE\n",
            )

    def test_evidence_replace_sigint_keeps_recorded_commit(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_replace = evidence_module.os.replace

            def replace_foreign_then_sigint(*args, **kwargs):
                real_replace(*args, **kwargs)
                target.write_bytes(b"FOREIGN-SIGINT\n")
                os.kill(os.getpid(), signal.SIGINT)
                return None

            with TrustedEvidenceRoot(
                base,
                "SIGINT commit fact root",
            ) as root:
                with patch.object(
                    evidence_module.os,
                    "replace",
                    side_effect=replace_foreign_then_sigint,
                ), self.assertRaisesRegex(
                    KeyboardInterrupt,
                    ".*",
                ) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "SIGINT commit payload",
                    )
            self.assertEqual(
                caught.exception.transaction_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.durability_state,
                "UNKNOWN",
            )
            self.assertEqual(
                caught.exception.target_state,
                "CHANGED",
            )
            self.assertEqual(
                caught.exception.residual_paths,
                ["payload.json"],
            )
            self.assertEqual(
                target.read_bytes(),
                b"FOREIGN-SIGINT\n",
            )

    def test_evidence_replace_primary_error_survives_pending_sigint(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"

            def signal_then_oserror(*args, **kwargs):
                del args, kwargs
                os.kill(os.getpid(), signal.SIGINT)
                raise OSError("primary replace failure")

            with TrustedEvidenceRoot(
                base,
                "replace primary signal root",
            ) as root:
                with patch.object(
                    evidence_module.os,
                    "replace",
                    side_effect=signal_then_oserror,
                ), self.assertRaises(EvidenceError) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "replace primary payload",
                    )
            self.assertIsInstance(caught.exception.__cause__, OSError)
            self.assertEqual(
                str(caught.exception.__cause__),
                "primary replace failure",
            )
            self.assertEqual(
                caught.exception.signal_mask_state,
                "RESTORED",
            )
            self.assertTrue(
                any(
                    item["type"] == "KeyboardInterrupt"
                    and item["phase"] == "sigint_mask_restore"
                    for item in caught.exception.secondary_exceptions
                )
            )
            self.assertFalse(target.exists())

    def test_close_primary_error_survives_pending_sigint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "owned"
            path.write_text("owned\n", encoding="utf-8")
            owner, file_fd = _owned_file_fd(
                path,
                owner_type=mutation_runner._OwnedFDCollection,
            )

            def signal_then_oserror(observed_fd):
                self.assertEqual(observed_fd, file_fd)
                os.kill(os.getpid(), signal.SIGINT)
                raise OSError("primary close failure")

            try:
                with patch.object(
                    mutation_runner.os,
                    "close",
                    side_effect=signal_then_oserror,
                ), self.assertRaises(
                    mutation_runner.UnsafePatchError
                ) as caught:
                    mutation_runner._close_registered_fd(
                        owner,
                        file_fd,
                        "close primary signal",
                    )
                self.assertIsInstance(
                    caught.exception.__cause__,
                    OSError,
                )
                self.assertEqual(
                    str(caught.exception.__cause__),
                    "primary close failure",
                )
                self.assertEqual(
                    caught.exception.signal_mask_state,
                    "RESTORED",
                )
                self.assertTrue(
                    any(
                        item["type"] == "KeyboardInterrupt"
                        for item in caught.exception.secondary_exceptions
                    )
                )
            finally:
                owner.close_all_anchors()
                try:
                    os.close(file_fd)
                except OSError:
                    pass

    def test_mask_restore_failure_preserves_primary_and_state(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        real_mask = evidence_module.signal.pthread_sigmask
        baseline = real_mask(signal.SIG_BLOCK, set())
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = ".payload.tmp"
            destination = "payload.json"
            (base / source).write_bytes(b"NEW\n")
            directory_fd = os.open(
                base,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            transaction_state = {"transaction_state": "UNCOMMITTED"}
            primary = OSError("primary operation")

            def fail_restore(signals, mask):
                if signals == signal.SIG_SETMASK:
                    raise OSError("mask restore failed")
                return real_mask(signals, mask)

            try:
                with patch.object(
                    evidence_module.signal,
                    "pthread_sigmask",
                    side_effect=fail_restore,
                ):
                    try:
                        with evidence_module._defer_keyboard_interrupt():
                            raise primary
                    except OSError as caught:
                        observed = caught
                self.assertIs(observed, primary)
                self.assertEqual(
                    observed.signal_mask_state,
                    "BLOCKED",
                )
                self.assertTrue(
                    any(
                        item["message"] == "mask restore failed"
                        for item in observed.secondary_exceptions
                    )
                )
            finally:
                real_mask(signal.SIG_SETMASK, baseline)
                os.close(directory_fd)

    def test_mask_restore_broken_string_preserves_primary(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        class BrokenRestoreString(OSError):
            def __str__(self):
                raise RuntimeError("restore secondary formatting failed")

        for module in (evidence_module, mutation_runner):
            with self.subTest(module=module.__name__):
                real_mask = module.signal.pthread_sigmask
                baseline = real_mask(signal.SIG_BLOCK, set())
                primary = OSError("primary before broken secondary")

                def fail_restore(signals, mask):
                    if signals == signal.SIG_SETMASK:
                        raise BrokenRestoreString(
                            "broken restore secondary"
                        )
                    return real_mask(signals, mask)

                observed = None
                try:
                    with patch.object(
                        module.signal,
                        "pthread_sigmask",
                        side_effect=fail_restore,
                    ):
                        try:
                            with module._defer_keyboard_interrupt():
                                raise primary
                        except BaseException as exc:
                            observed = exc
                    self.assertIs(observed, primary)
                    self.assertEqual(
                        observed.signal_mask_state,
                        "BLOCKED",
                    )
                    self.assertTrue(
                        any(
                            item["type"] == "BrokenRestoreString"
                            and item["message"]
                            == "<unavailable diagnostic value>"
                            for item in observed.secondary_exceptions
                        )
                    )
                finally:
                    real_mask(signal.SIG_SETMASK, baseline)

    def test_unavailable_sigint_mask_fails_before_replace(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = ".payload.tmp"
            destination = "payload.json"
            (base / source).write_bytes(b"NEW\n")
            directory_fd = os.open(
                base,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            replace_calls = 0

            def count_replace(*args, **kwargs):
                nonlocal replace_calls
                replace_calls += 1
                return None

            try:
                with patch.object(
                    evidence_module.os,
                    "name",
                    "nt",
                ), patch.object(
                    evidence_module.os,
                    "replace",
                    side_effect=count_replace,
                ), self.assertRaisesRegex(
                    EvidenceError,
                    "SIGINT shielding.*unavailable",
                ):
                    evidence_module._replace_and_record_namespace_commit(
                        source,
                        destination,
                        source_directory_fd=directory_fd,
                        destination_directory_fd=directory_fd,
                        transaction_state={
                            "transaction_state": "UNCOMMITTED"
                        },
                    )
                self.assertEqual(replace_calls, 0)
            finally:
                os.close(directory_fd)

    def test_evidence_staging_rejects_uncommitted_final_mode(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_replace = evidence_module.os.replace

            def replace_with_wrong_mode(*args, **kwargs):
                real_replace(*args, **kwargs)
                os.chmod(target, 0o644)

            with TrustedEvidenceRoot(
                base,
                "uncommitted contract root",
            ) as root:
                with patch.object(
                    evidence_module.os,
                    "replace",
                    side_effect=replace_with_wrong_mode,
                ), self.assertRaisesRegex(
                    EvidenceError,
                    "final target contract was not satisfied",
                ) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "uncommitted evidence payload",
                    )
            self.assertEqual(
                caught.exception.transaction_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.durability_state,
                "UNKNOWN",
            )
            self.assertEqual(
                caught.exception.target_state,
                "CHANGED",
            )
            self.assertEqual(
                caught.exception.residual_paths,
                ["payload.json"],
            )
            self.assertEqual(target.read_bytes(), b'{"value":1}\n')
            self.assertEqual(
                stat.S_IMODE(target.stat().st_mode),
                0o644,
            )

    def test_evidence_committed_oserror_preserves_top_level_residual(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_replace = evidence_module.os.replace

            def replace_then_error(*args, **kwargs):
                real_replace(*args, **kwargs)
                raise OSError("committed replacement failed after commit")

            with TrustedEvidenceRoot(
                base,
                "committed OSError root",
            ) as root:
                with patch.object(
                    evidence_module.os,
                    "replace",
                    side_effect=replace_then_error,
                ), self.assertRaises(EvidenceError) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "committed OSError payload",
                    )
            self.assertEqual(
                caught.exception.transaction_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.durability_state,
                "UNKNOWN",
            )
            self.assertEqual(
                caught.exception.target_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.residual_paths,
                ["payload.json"],
            )
            self.assertIsInstance(
                caught.exception.__cause__,
                OSError,
            )
            self.assertEqual(target.read_bytes(), b'{"value":1}\n')

    def test_evidence_replace_failure_before_effect_is_uncommitted_clean(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"

            def fail_before_replace(*args, **kwargs):
                del args, kwargs
                raise OSError("injected pre-effect replace failure")

            with TrustedEvidenceRoot(
                base,
                "pre-effect replace root",
            ) as root:
                with patch.object(
                    evidence_module.os,
                    "replace",
                    side_effect=fail_before_replace,
                ), self.assertRaises(EvidenceError) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "pre-effect replace payload",
                    )
            self.assertEqual(
                caught.exception.transaction_state,
                "UNCOMMITTED",
            )
            self.assertEqual(
                caught.exception.durability_state,
                "NOT_ATTEMPTED",
            )
            self.assertEqual(
                caught.exception.target_state,
                "ABSENT",
            )
            self.assertFalse(
                hasattr(caught.exception, "residual_paths")
            )
            self.assertFalse(target.exists())
            self.assertEqual(
                [
                    path
                    for path in base.iterdir()
                    if path.name.startswith(".payload.json.tmp-")
                ],
                [],
            )

    def test_evidence_directory_fsync_failure_keeps_namespace_commit(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_replace = evidence_module.os.replace
            real_fsync = evidence_module.os.fsync
            replace_completed = False
            fsync_failed = False

            def mark_replace(*args, **kwargs):
                nonlocal replace_completed
                real_replace(*args, **kwargs)
                replace_completed = True

            def fail_directory_fsync(file_fd):
                nonlocal fsync_failed
                if replace_completed and not fsync_failed:
                    fsync_failed = True
                    raise OSError(
                        "injected directory fsync failure"
                    )
                return real_fsync(file_fd)

            with TrustedEvidenceRoot(
                base,
                "durability failure root",
            ) as root:
                with patch.object(
                    evidence_module.os,
                    "replace",
                    side_effect=mark_replace,
                ), patch.object(
                    evidence_module.os,
                    "fsync",
                    side_effect=fail_directory_fsync,
                ), self.assertRaises(EvidenceError) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "durability failure payload",
                    )
            self.assertTrue(fsync_failed)
            self.assertEqual(
                caught.exception.transaction_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.durability_state,
                "UNKNOWN",
            )
            self.assertEqual(
                caught.exception.target_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.residual_paths,
                ["payload.json"],
            )
            self.assertEqual(
                target.read_bytes(),
                b'{"value":1}\n',
            )

    def test_evidence_external_change_after_replace_preserves_commit(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_replace = evidence_module.os.replace
            real_fsync = evidence_module.os.fsync
            replace_completed = False
            external_change_injected = False

            def mark_replace(*args, **kwargs):
                nonlocal replace_completed
                real_replace(*args, **kwargs)
                replace_completed = True

            def change_then_fail_fsync(file_fd):
                nonlocal external_change_injected
                if replace_completed and not external_change_injected:
                    external_change_injected = True
                    target.write_bytes(b"FOREIGN\n")
                    raise OSError(
                        "injected fsync after external change"
                    )
                return real_fsync(file_fd)

            with TrustedEvidenceRoot(
                base,
                "external change after commit root",
            ) as root:
                with patch.object(
                    evidence_module.os,
                    "replace",
                    side_effect=mark_replace,
                ), patch.object(
                    evidence_module.os,
                    "fsync",
                    side_effect=change_then_fail_fsync,
                ), self.assertRaises(EvidenceError) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "external change payload",
                    )
            self.assertTrue(external_change_injected)
            self.assertEqual(
                caught.exception.transaction_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.durability_state,
                "UNKNOWN",
            )
            self.assertEqual(
                caught.exception.target_state,
                "CHANGED",
            )
            self.assertEqual(
                caught.exception.residual_paths,
                ["payload.json"],
            )
            self.assertEqual(target.read_bytes(), b"FOREIGN\n")

    def test_evidence_observation_unknown_keeps_namespace_commit(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_open = evidence_module._OS_OPEN
            real_replace = evidence_module.os.replace
            replace_completed = False

            def mark_replace(*args, **kwargs):
                nonlocal replace_completed
                real_replace(*args, **kwargs)
                replace_completed = True

            def fail_target_observation(path, *args, **kwargs):
                if replace_completed and path == target.name:
                    raise PermissionError(
                        "injected committed observation failure"
                    )
                return real_open(path, *args, **kwargs)

            with TrustedEvidenceRoot(
                base,
                "committed observation unknown root",
            ) as root:
                with patch.object(
                    evidence_module,
                    "_OS_OPEN",
                    side_effect=fail_target_observation,
                ), patch.object(
                    evidence_module.os,
                    "replace",
                    side_effect=mark_replace,
                ), self.assertRaises(EvidenceError) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "committed observation payload",
                    )
            self.assertTrue(replace_completed)
            self.assertEqual(
                caught.exception.transaction_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.durability_state,
                "UNKNOWN",
            )
            self.assertEqual(
                caught.exception.target_state,
                "UNKNOWN",
            )
            self.assertTrue(caught.exception.residual_unknown)
            self.assertEqual(
                target.read_bytes(),
                b'{"value":1}\n',
            )

    def test_evidence_committed_close_interrupt_preserves_residual(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_replace = evidence_module.os.replace
            real_close = evidence_module._close_fds_preserving_primary
            committed = False
            interrupted = False

            def replace_then_mark(*args, **kwargs):
                nonlocal committed
                real_replace(*args, **kwargs)
                committed = True

            def interrupt_directory_close(
                descriptors,
                *,
                attempted_fds=None,
                close_states=None,
                ownership_records=None,
            ):
                nonlocal interrupted
                real_close(
                    descriptors,
                    attempted_fds=attempted_fds,
                    close_states=close_states,
                    ownership_records=ownership_records,
                )
                if committed and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt(
                        "injected committed directory close interrupt"
                    )
                return None

            with TrustedEvidenceRoot(
                base,
                "committed close interrupt root",
            ) as root:
                with patch.object(
                    evidence_module.os,
                    "replace",
                    side_effect=replace_then_mark,
                ), patch.object(
                    evidence_module,
                    "_close_fds_preserving_primary",
                    side_effect=interrupt_directory_close,
                ), self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "committed directory close",
                ) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "committed close payload",
                    )
            self.assertEqual(
                caught.exception.transaction_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.durability_state,
                "UNKNOWN",
            )
            self.assertEqual(
                caught.exception.target_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.residual_paths,
                ["payload.json"],
            )
            self.assertEqual(target.read_bytes(), b'{"value":1}\n')

    def test_evidence_committed_scope_close_keeps_durability(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_close_owned = evidence_module._close_owned_fds
            interrupted = False

            def close_then_interrupt(
                opened_fds,
                label,
                *,
                residual_paths=None,
            ):
                nonlocal interrupted
                real_close_owned(
                    opened_fds,
                    label,
                )
                if (
                    label == "durability scope traversal"
                    and not interrupted
                ):
                    interrupted = True
                    raise KeyboardInterrupt(
                        "committed scope close interrupt"
                    )

            with TrustedEvidenceRoot(
                base,
                "durability scope root",
            ) as root:
                with patch.object(
                    evidence_module,
                    "_close_owned_fds",
                    side_effect=close_then_interrupt,
                ), self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "committed scope close",
                ) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "durability scope",
                    )
            self.assertTrue(interrupted)
            self.assertEqual(
                caught.exception.transaction_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.durability_state,
                "CONFIRMED",
            )
            self.assertEqual(
                caught.exception.target_state,
                "COMMITTED",
            )
            self.assertEqual(
                caught.exception.residual_paths,
                ["payload.json"],
            )
            self.assertEqual(
                target.read_bytes(),
                b'{"value":1}\n',
            )

    def test_evidence_staging_temporary_owned_before_mask_restore(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_open = evidence_module._OS_OPEN
            real_mask = evidence_module.signal.pthread_sigmask
            temporary_fds: list[int] = []
            trigger = False

            def capture_open(path, *args, **kwargs):
                nonlocal trigger
                opened = real_open(path, *args, **kwargs)
                if str(path).startswith(".payload.json.tmp-"):
                    temporary_fds.append(opened)
                    trigger = True
                return opened

            def interrupt_mask_restore(signals, mask):
                nonlocal trigger
                previous = real_mask(signals, mask)
                if signals == signal.SIG_SETMASK and trigger:
                    trigger = False
                    raise KeyboardInterrupt(
                        "injected evidence temporary ownership interrupt"
                    )
                return previous

            with TrustedEvidenceRoot(
                base,
                "evidence temporary ownership root",
            ) as root:
                with patch.object(
                    evidence_module,
                    "_OS_OPEN",
                    side_effect=capture_open,
                ), patch.object(
                    evidence_module.signal,
                    "pthread_sigmask",
                    side_effect=interrupt_mask_restore,
                ), self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "temporary ownership",
                ):
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "evidence temporary payload",
                    )
            self.assertEqual(len(temporary_fds), 1)
            with self.assertRaises(OSError):
                os.fstat(temporary_fds[0])
            self.assertEqual(
                [
                    path.name
                    for path in base.iterdir()
                    if path.name.startswith(".payload.json.tmp-")
                ],
                [],
            )
            self.assertFalse(target.exists())

    def test_evidence_temporary_unlink_error_keeps_top_level_residual(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_unlink = evidence_module.os.unlink
            temporary_names: list[str] = []

            def fail_temporary_unlink(path, *args, **kwargs):
                if str(path).startswith(".payload.json.tmp-"):
                    temporary_names.append(str(path))
                    raise PermissionError(
                        "injected evidence temporary unlink failure"
                    )
                return real_unlink(path, *args, **kwargs)

            with TrustedEvidenceRoot(
                base,
                "evidence unlink residual root",
            ) as root:
                with patch.object(
                    evidence_module.os,
                    "write",
                    side_effect=OSError(
                        "injected evidence write failure"
                    ),
                ), patch.object(
                    evidence_module.os,
                    "unlink",
                    side_effect=fail_temporary_unlink,
                ), self.assertRaises(EvidenceError) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "evidence unlink payload",
                    )
            self.assertEqual(len(temporary_names), 1)
            self.assertEqual(
                caught.exception.residual_paths,
                temporary_names,
            )
            self.assertTrue(
                (base / temporary_names[0]).is_file()
            )
            self.assertFalse(target.exists())

    def test_evidence_unlink_success_then_interrupt_is_not_residual(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            real_unlink = evidence_module.os.unlink
            temporary_names: list[str] = []

            def unlink_then_interrupt(path, *args, **kwargs):
                if str(path).startswith(".payload.json.tmp-"):
                    real_unlink(path, *args, **kwargs)
                    temporary_names.append(str(path))
                    raise KeyboardInterrupt(
                        "injected post-unlink interrupt"
                    )
                return real_unlink(path, *args, **kwargs)

            with TrustedEvidenceRoot(
                base,
                "post-unlink evidence root",
            ) as root:
                with patch.object(
                    evidence_module.os,
                    "write",
                    side_effect=OSError("injected evidence write failure"),
                ), patch.object(
                    evidence_module.os,
                    "unlink",
                    side_effect=unlink_then_interrupt,
                ), self.assertRaises(EvidenceError) as caught:
                    root._write_bytes_staging(
                        target,
                        b'{"value":1}\n',
                        "post-unlink evidence payload",
                    )
            self.assertEqual(len(temporary_names), 1)
            self.assertFalse((base / temporary_names[0]).exists())
            self.assertFalse(target.exists())
            self.assertEqual(
                caught.exception.transaction_state,
                "UNCOMMITTED",
            )
            self.assertFalse(
                hasattr(caught.exception, "residual_paths")
            )
            self.assertFalse(
                getattr(caught.exception, "residual_unknown", False)
            )
            self.assertIn(
                "injected post-unlink interrupt",
                "\n".join(caught.exception.__notes__),
            )

    def test_mutation_snapshot_unlink_success_then_interrupt_is_not_residual(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "value.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            metadata = target.stat()
            real_unlink = mutation_runner.os.unlink
            temporary_names: list[str] = []

            def unlink_then_interrupt(path, *args, **kwargs):
                if str(path).startswith(".value.py.mutation-"):
                    real_unlink(path, *args, **kwargs)
                    temporary_names.append(str(path))
                    raise KeyboardInterrupt(
                        "injected snapshot post-unlink interrupt"
                    )
                return real_unlink(path, *args, **kwargs)

            with patch.object(
                mutation_runner.os,
                "write",
                side_effect=OSError("injected snapshot write failure"),
            ), patch.object(
                mutation_runner.os,
                "unlink",
                side_effect=unlink_then_interrupt,
            ), self.assertRaisesRegex(
                OSError,
                "injected snapshot write failure",
            ) as caught:
                mutation_runner._write_snapshot_file(
                    root,
                    "value.py",
                    "VALUE = 2\n",
                    (metadata.st_dev, metadata.st_ino),
                    mutation_runner.hashlib.sha256(
                        b"VALUE = 1\n"
                    ).hexdigest(),
                    stat.S_IMODE(metadata.st_mode),
                )
            self.assertEqual(len(temporary_names), 1)
            self.assertFalse((root / temporary_names[0]).exists())
            self.assertFalse(
                hasattr(caught.exception, "residual_paths")
            )
            self.assertFalse(
                getattr(caught.exception, "residual_unknown", False)
            )
            self.assertIn(
                "injected snapshot post-unlink interrupt",
                "\n".join(caught.exception.__notes__),
            )
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "VALUE = 1\n",
            )

    def test_mutation_file_copy_error_preserves_target_close_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            target_parent = base / "target"
            target_parent.mkdir(mode=0o700)
            target = target_parent / "value.py"
            target_fds: set[int] = set()
            real_open = mutation_runner._OS_OPEN
            real_close = mutation_runner.os.close
            real_flags = mutation_runner._copy_directory_flags()
            close_failed = False

            def capture_target(path, flags, *args, **kwargs):
                opened = real_open(path, flags, *args, **kwargs)
                if (
                    path == target.name
                    and flags & os.O_WRONLY
                ):
                    target_fds.add(opened)
                return opened

            def fail_target_close(file_fd):
                nonlocal close_failed
                if file_fd in target_fds and not close_failed:
                    close_failed = True
                    real_close(file_fd)
                    raise OSError("injected target close failure")
                real_close(file_fd)

            with patch.object(
                mutation_runner,
                "_copy_directory_flags",
                return_value=real_flags,
            ), patch.object(
                mutation_runner,
                "_OS_OPEN",
                side_effect=capture_target,
            ), patch.object(
                mutation_runner.os,
                "read",
                side_effect=OSError("injected source copy failure"),
            ), patch.object(
                mutation_runner.os,
                "close",
                side_effect=fail_target_close,
            ), self.assertRaises(
                mutation_runner.UnsafePatchError
            ) as caught:
                mutation_runner._copy_file_without_links(
                    source,
                    target,
                )
            self.assertTrue(close_failed)
            self.assertIsInstance(
                caught.exception.__cause__,
                OSError,
            )
            self.assertIn(
                "injected source copy failure",
                str(caught.exception.__cause__),
            )
            notes = "\n".join(caught.exception.__notes__)
            self.assertIn("injected target close failure", notes)
            self.assertFalse(target.exists())

    def test_mutation_created_target_reported_before_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            target_parent = base / "target"
            target_parent.mkdir(mode=0o700)
            target = target_parent / "value.py"
            real_open = mutation_runner._OS_OPEN
            real_mask = mutation_runner.signal.pthread_sigmask
            trigger = False

            def open_then_trigger(path, *args, **kwargs):
                nonlocal trigger
                opened = real_open(path, *args, **kwargs)
                if (
                    path == target.name
                    and args
                    and args[0] & os.O_WRONLY
                ):
                    trigger = True
                return opened

            def interrupt_mask_restore(signals, mask):
                nonlocal trigger
                previous = real_mask(signals, mask)
                if signals == signal.SIG_SETMASK and trigger:
                    trigger = False
                    raise KeyboardInterrupt(
                        "injected pre-marker target interrupt"
                    )
                return previous

            with patch.object(
                mutation_runner,
                "_OS_OPEN",
                side_effect=open_then_trigger,
            ), patch.object(
                mutation_runner.signal,
                "pthread_sigmask",
                side_effect=interrupt_mask_restore,
            ), self.assertRaisesRegex(
                KeyboardInterrupt,
                "pre-marker",
            ) as caught:
                mutation_runner._copy_file_without_links(
                    source,
                    target,
                )
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"")
            self.assertEqual(
                caught.exception.residual_paths,
                ["value.py"],
            )
            self.assertTrue(caught.exception.residual_unknown)

    def test_mutation_tree_success_close_failure_reports_residual(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target = base / "target"
            source.mkdir()
            (source / "value.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            real_open = mutation_runner._open_relative_directory
            real_close = mutation_runner.os.close
            target_root_fd = None
            close_failed = False

            def capture_target_root(opened_fds, parent_fd, name, label):
                nonlocal target_root_fd
                opened = real_open(
                    opened_fds,
                    parent_fd,
                    name,
                    label,
                )
                if (
                    target_root_fd is None
                    and label == "mutation copy target"
                ):
                    target_root_fd = opened
                return opened

            def fail_target_root_close(file_fd):
                nonlocal close_failed
                if file_fd == target_root_fd and not close_failed:
                    close_failed = True
                    real_close(file_fd)
                    raise OSError(
                        "injected committed target close failure"
                    )
                real_close(file_fd)

            with patch.object(
                mutation_runner,
                "_open_relative_directory",
                side_effect=capture_target_root,
            ), patch.object(
                mutation_runner.os,
                "close",
                side_effect=fail_target_root_close,
            ), self.assertRaises(
                mutation_runner.UnsafePatchError
            ) as caught:
                mutation_runner._copy_tree_without_links(source, target)
            self.assertTrue(close_failed)
            self.assertEqual(
                caught.exception.residual_paths,
                ["."],
            )
            self.assertEqual(
                (target / "value.py").read_text(encoding="utf-8"),
                "VALUE = 1\n",
            )

    def test_mutation_snapshot_committed_close_failure_reports_residual(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "value.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            metadata = target.stat()
            real_parent = mutation_runner._open_snapshot_parent
            real_close = mutation_runner.os.close
            directory_fd = None
            failed = False

            def capture_parent(path, relative, owned_fds=None):
                nonlocal directory_fd
                opened_fd, name = real_parent(
                    path,
                    relative,
                    owned_fds,
                )
                directory_fd = opened_fd
                return opened_fd, name

            def fail_parent_close(file_fd):
                nonlocal failed
                real_close(file_fd)
                if file_fd == directory_fd and not failed:
                    failed = True
                    raise OSError(
                        "injected committed snapshot close failure"
                    )

            with patch.object(
                mutation_runner,
                "_open_snapshot_parent",
                side_effect=capture_parent,
            ), patch.object(
                mutation_runner.os,
                "close",
                side_effect=fail_parent_close,
            ), self.assertRaises(
                mutation_runner.UnsafePatchError
            ) as caught:
                mutation_runner._write_snapshot_file(
                    root,
                    "value.py",
                    "VALUE = 2\n",
                    (metadata.st_dev, metadata.st_ino),
                    mutation_runner.hashlib.sha256(
                        b"VALUE = 1\n"
                    ).hexdigest(),
                    stat.S_IMODE(metadata.st_mode),
                )
            self.assertTrue(failed)
            self.assertEqual(
                caught.exception.residual_paths,
                ["value.py"],
            )
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "VALUE = 2\n",
            )

    def test_mutation_tree_postcommit_interrupt_reports_residual(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            target = base / "target"
            source.mkdir()
            (source / "value.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            source_lines, start_line = inspect.getsourcelines(
                mutation_runner._copy_tree_without_links
            )
            assignment_index = next(
                index
                for index, line in enumerate(source_lines)
                if line.strip() == "target_committed = True"
            )
            trigger_index = next(
                index
                for index in range(
                    assignment_index + 1,
                    len(source_lines),
                )
                if source_lines[index].strip()
                == "if target_identity is None:"
            )
            trigger_line = start_line + trigger_index
            triggered = False

            def interrupt_after_commit(frame, event, _arg):
                nonlocal triggered
                if (
                    not triggered
                    and event == "line"
                    and frame.f_code
                    is mutation_runner._copy_tree_without_links.__code__
                    and frame.f_lineno == trigger_line
                ):
                    triggered = True
                    raise KeyboardInterrupt(
                        "injected post-commit interrupt"
                    )
                return interrupt_after_commit

            previous_trace = sys.gettrace()
            try:
                sys.settrace(interrupt_after_commit)
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "injected post-commit interrupt",
                ) as caught:
                    mutation_runner._copy_tree_without_links(
                        source,
                        target,
                    )
            finally:
                sys.settrace(previous_trace)
            self.assertTrue(triggered)
            self.assertEqual(caught.exception.residual_paths, ["."])
            self.assertEqual(
                (target / "value.py").read_text(encoding="utf-8"),
                "VALUE = 1\n",
            )

    def test_mutation_target_transfer_interrupt_has_one_close_owner(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.py"
            target = base / "value.py"
            foreign = base / "foreign.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            foreign.write_text("FOREIGN = 1\n", encoding="utf-8")
            real_open = mutation_runner._open_registered_fd
            real_close = mutation_runner.os.close
            real_fstat = mutation_runner.os.fstat
            target_fd = None
            foreign_fd = None
            target_close_calls = 0
            foreign_fd_open_after_operation = False
            interrupted = False

            def capture_target_open(
                opened_fds,
                path,
                flags,
                *args,
                **kwargs,
            ):
                nonlocal target_fd
                opened = real_open(
                    opened_fds,
                    path,
                    flags,
                    *args,
                    **kwargs,
                )
                if path == target.name and flags & os.O_WRONLY:
                    target_fd = opened
                return opened

            def interrupt_first_target_observation(file_fd):
                nonlocal interrupted
                if file_fd == target_fd and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt(
                        "injected target transfer interrupt"
                    )
                return real_fstat(file_fd)

            def close_target_and_reuse(file_fd):
                nonlocal foreign_fd, target_close_calls
                if file_fd != target_fd:
                    return real_close(file_fd)
                target_close_calls += 1
                real_close(file_fd)
                if foreign_fd is None:
                    foreign_fd = os.open(foreign, os.O_RDONLY)
                return None

            try:
                with patch.object(
                    mutation_runner,
                    "_open_registered_fd",
                    side_effect=capture_target_open,
                ), patch.object(
                    mutation_runner.os,
                    "fstat",
                    side_effect=interrupt_first_target_observation,
                ), patch.object(
                    mutation_runner.os,
                    "close",
                    side_effect=close_target_and_reuse,
                ):
                    with self.assertRaisesRegex(
                        KeyboardInterrupt,
                        "target transfer interrupt",
                    ):
                        mutation_runner._copy_file_without_links(
                            source,
                            target,
                        )
            finally:
                if foreign_fd is not None:
                    try:
                        os.fstat(foreign_fd)
                    except OSError:
                        foreign_fd_open_after_operation = False
                    else:
                        foreign_fd_open_after_operation = True
                    try:
                        real_close(foreign_fd)
                    except OSError:
                        pass
            self.assertTrue(interrupted)
            self.assertEqual(target_close_calls, 1)
            self.assertIsNotNone(foreign_fd)
            self.assertEqual(foreign_fd, target_fd)
            self.assertTrue(foreign_fd_open_after_operation)

    def test_evidence_close_interrupt_retires_live_numeric_fd(
        self,
    ) -> None:
        import tests.task_control_center_evidence as evidence_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "payload.json"
            captured: list[int] = []
            real_open = evidence_module._OS_OPEN
            real_close = evidence_module.os.close
            close_calls = 0

            def capture_open(path, *args, **kwargs):
                opened = real_open(path, *args, **kwargs)
                if str(path).startswith(".payload.json.tmp-"):
                    captured.append(opened)
                return opened

            def interrupt_before_native(file_fd):
                nonlocal close_calls
                if captured and file_fd == captured[0]:
                    close_calls += 1
                    raise KeyboardInterrupt(
                        "repeated temporary close interrupt"
                    )
                return real_close(file_fd)

            try:
                with TrustedEvidenceRoot(
                    base,
                    "repeated close evidence root",
                ) as root:
                    with patch.object(
                        evidence_module,
                        "_OS_OPEN",
                        side_effect=capture_open,
                    ), patch.object(
                        evidence_module.os,
                        "write",
                        side_effect=OSError(
                            "injected evidence write failure"
                        ),
                    ), patch.object(
                        evidence_module.os,
                        "close",
                        side_effect=interrupt_before_native,
                    ), self.assertRaises(EvidenceError) as caught:
                        root._write_bytes_staging(
                            target,
                            b'{"value":1}\n',
                            "repeated close payload",
                        )
                self.assertEqual(len(captured), 1)
                self.assertEqual(close_calls, 1)
                self.assertEqual(
                    caught.exception.fd_close_states[captured[0]],
                    "UNKNOWN",
                )
                self.assertEqual(
                    caught.exception.unresolved_fds[captured[0]],
                    "UNKNOWN",
                )
                self.assertFalse(target.exists())
                self.assertEqual(
                    [
                        path
                        for path in base.iterdir()
                        if path.name.startswith(".payload.json.tmp-")
                    ],
                    [],
                )
                os.fstat(captured[0])
            finally:
                if captured:
                    try:
                        real_close(captured[0])
                    except OSError:
                        pass

    def test_mutation_snapshot_unlink_failure_reports_real_residual(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "value.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            metadata = target.stat()
            real_unlink = mutation_runner.os.unlink
            temporary_names: list[str] = []

            def fail_temporary_unlink(path, *args, **kwargs):
                if str(path).startswith(".value.py.mutation-"):
                    temporary_names.append(str(path))
                    raise PermissionError("injected unlink failure")
                return real_unlink(path, *args, **kwargs)

            with patch.object(
                mutation_runner.os,
                "write",
                side_effect=OSError("injected snapshot write failure"),
            ), patch.object(
                mutation_runner.os,
                "unlink",
                side_effect=fail_temporary_unlink,
            ), self.assertRaisesRegex(
                OSError,
                "injected snapshot write failure",
            ) as caught:
                mutation_runner._write_snapshot_file(
                    root,
                    "value.py",
                    "VALUE = 2\n",
                    (metadata.st_dev, metadata.st_ino),
                    mutation_runner.hashlib.sha256(
                        b"VALUE = 1\n"
                    ).hexdigest(),
                    stat.S_IMODE(metadata.st_mode),
                )
            self.assertEqual(
                caught.exception.residual_paths,
                temporary_names,
            )
            self.assertEqual(len(temporary_names), 1)
            self.assertTrue(
                (root / temporary_names[0]).is_file()
            )

    def test_mutation_close_interrupt_retires_live_numeric_fd(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "value.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            metadata = target.stat()
            captured: list[int] = []
            real_open = mutation_runner._OS_OPEN
            real_close = mutation_runner.os.close
            close_calls = 0

            def capture_open(path, flags, *args, **kwargs):
                opened = real_open(path, flags, *args, **kwargs)
                if (
                    str(path).startswith(".value.py.mutation-")
                    and flags & os.O_WRONLY
                ):
                    captured.append(opened)
                return opened

            def interrupt_before_native(file_fd):
                nonlocal close_calls
                if captured and file_fd == captured[0]:
                    close_calls += 1
                    raise KeyboardInterrupt(
                        "repeated snapshot close interrupt"
                    )
                return real_close(file_fd)

            try:
                with patch.object(
                    mutation_runner,
                    "_OS_OPEN",
                    side_effect=capture_open,
                ), patch.object(
                    mutation_runner.os,
                    "write",
                    side_effect=OSError(
                        "injected snapshot write failure"
                    ),
                ), patch.object(
                    mutation_runner.os,
                    "close",
                    side_effect=interrupt_before_native,
                ), self.assertRaisesRegex(
                    OSError,
                    "injected snapshot write failure",
                ) as caught:
                    mutation_runner._write_snapshot_file(
                        root,
                        "value.py",
                        "VALUE = 2\n",
                        (metadata.st_dev, metadata.st_ino),
                        mutation_runner.hashlib.sha256(
                            b"VALUE = 1\n"
                        ).hexdigest(),
                        stat.S_IMODE(metadata.st_mode),
                    )
                self.assertEqual(len(captured), 1)
                self.assertEqual(close_calls, 1)
                self.assertEqual(
                    caught.exception.fd_close_states[captured[0]],
                    "UNKNOWN",
                )
                self.assertEqual(
                    caught.exception.unresolved_fds[captured[0]],
                    "UNKNOWN",
                )
                self.assertEqual(
                    [
                        path
                        for path in root.iterdir()
                        if path.name.startswith(".value.py.mutation-")
                    ],
                    [],
                )
                self.assertEqual(
                    target.read_text(encoding="utf-8"),
                    "VALUE = 1\n",
                )
                os.fstat(captured[0])
            finally:
                if captured:
                    try:
                        real_close(captured[0])
                    except OSError:
                        pass

    def test_mutation_snapshot_prewrite_failure_has_no_false_residual(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "value.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            metadata = target.stat()
            with self.assertRaisesRegex(
                mutation_runner.UnsafePatchError,
                "changed after validation",
            ) as caught:
                mutation_runner._write_snapshot_file(
                    root,
                    "value.py",
                    "VALUE = 2\n",
                    (metadata.st_dev, metadata.st_ino + 1),
                    mutation_runner.hashlib.sha256(
                        b"VALUE = 1\n"
                    ).hexdigest(),
                    stat.S_IMODE(metadata.st_mode),
                )
            self.assertFalse(
                hasattr(caught.exception, "residual_paths")
            )
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "VALUE = 1\n",
            )

    def test_mutation_cleanup_close_failure_is_unknown_not_residual(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target_dir = Path(temp) / "target"
            target_dir.mkdir(mode=0o700)
            created = target_dir / "created.txt"
            created.write_text("CREATED\n", encoding="utf-8")
            metadata = created.stat()
            target_fd = os.open(
                target_dir,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            real_close = mutation_runner.os.close
            failed = False

            def fail_parent_close(file_fd):
                nonlocal failed
                real_close(file_fd)
                if file_fd != target_fd and not failed:
                    failed = True
                    raise OSError("injected cleanup close failure")

            try:
                ledger = {
                    "created.txt": (
                        "file",
                        metadata.st_dev,
                        metadata.st_ino,
                    )
                }
                with patch.object(
                    mutation_runner.os,
                    "close",
                    side_effect=fail_parent_close,
                ):
                    report = mutation_runner._clear_ledgered_entries(
                        target_fd,
                        ledger,
                    )
                self.assertTrue(failed)
                self.assertFalse(created.exists())
                self.assertEqual(report.residual_paths, [])
                self.assertTrue(report.unknown)
                self.assertTrue(report.diagnostics)
            finally:
                os.close(target_fd)

    def test_mutation_cleanup_unknown_stat_serializes_unknown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target_dir = Path(temp) / "target"
            target_dir.mkdir(mode=0o700)
            created = target_dir / "created.txt"
            created.write_text("CREATED\n", encoding="utf-8")
            metadata = created.stat()
            created.unlink()
            target_fd = os.open(
                target_dir,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            real_stat = mutation_runner.os.stat
            real_flags = mutation_runner._copy_directory_flags()

            def fail_created_stat(path, *args, **kwargs):
                if (
                    path == "created.txt"
                ):
                    raise PermissionError(
                        "injected unknown cleanup existence"
                    )
                return real_stat(path, *args, **kwargs)

            try:
                ledger = {
                    "created.txt": (
                        "file",
                        metadata.st_dev,
                        metadata.st_ino,
                    )
                }
                with patch.object(
                    mutation_runner,
                    "_copy_directory_flags",
                    return_value=real_flags,
                ), patch.object(
                    mutation_runner.os,
                    "stat",
                    side_effect=fail_created_stat,
                ):
                    report = mutation_runner._clear_ledgered_entries(
                        target_fd,
                        ledger,
                    )
                self.assertFalse(created.exists())
                self.assertEqual(
                    report.residual_paths,
                    ["created.txt"],
                )
                self.assertTrue(report.unknown)
                error = mutation_runner.UnsafePatchError(
                    "cleanup state unknown"
                )
                mutation_runner._attach_cleanup_report(error, report)
                machine = mutation_runner._mutation_invalid_result(
                    {"id": "unknown-stat"},
                    "cleanup state unknown",
                    error=error,
                )
                self.assertEqual(
                    machine["residual"],
                    {
                        "status": "UNKNOWN",
                        "paths": ["created.txt"],
                    },
                )
            finally:
                os.close(target_fd)

    @unittest.skipUnless(sys.platform == "darwin", "macOS ACL test")
    def test_mutation_private_root_rejects_macos_acl(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            os.chmod(root, 0o700)
            subprocess.run(
                [
                    "chmod",
                    "+a",
                    (
                        "everyone allow list,search,readattr,readextattr,"
                        "readsecurity,writeattr,writeextattr,add_file,"
                        "add_subdirectory,delete_child"
                    ),
                    str(root),
                ],
                check=True,
            )
            root_fd = mutation_runner._open_absolute_directory_without_links(
                root,
                "ACL root",
            )
            try:
                with self.assertRaisesRegex(
                    mutation_runner.UnsafePatchError,
                    "extended ACL",
                ):
                    mutation_runner._assert_exclusive_private_directory(
                        root_fd,
                        "ACL root",
                    )
            finally:
                os.close(root_fd)

    @unittest.skipUnless(sys.platform == "darwin", "macOS ACL test")
    def test_mutation_private_root_acl_query_failure_is_closed(
        self,
    ) -> None:
        class Function:
            argtypes = None
            restype = None

            def __init__(self, call):
                self.call = call

            def __call__(self, *args):
                return self.call(*args)

        class Library:
            acl_get_fd = Function(lambda _fd: None)
            acl_free = Function(lambda _acl: 0)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            os.chmod(root, 0o700)
            root_fd = mutation_runner._open_absolute_directory_without_links(
                root,
                "ACL query failure root",
            )
            try:
                for error_number in (0, errno.EIO):
                    with self.subTest(error_number=error_number), patch.object(
                        mutation_runner.ctypes,
                        "CDLL",
                        return_value=Library(),
                    ), patch.object(
                        mutation_runner.ctypes.util,
                        "find_library",
                        return_value="libc",
                    ), patch.object(
                        mutation_runner.ctypes,
                        "set_errno",
                    ), patch.object(
                        mutation_runner.ctypes,
                        "get_errno",
                        return_value=error_number,
                    ), self.assertRaisesRegex(
                        mutation_runner.UnsafePatchError,
                        "could not be inspected safely",
                    ):
                        mutation_runner._assert_exclusive_private_directory(
                            root_fd,
                            "ACL query failure root",
                        )
            finally:
                os.close(root_fd)

    def test_mutation_cleanup_preserves_inner_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "project"
            target = base / "target"
            (project / "src").mkdir(parents=True)
            (project / "tests").mkdir()
            (project / "pyproject.toml").write_text(
                "[project]\nname='a52'\nversion='0.0.0'\n",
                encoding="utf-8",
            )
            (project / "src" / "value.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            (project / "tests" / "test_value.py").write_text(
                "import unittest\n",
                encoding="utf-8",
            )
            target.mkdir(mode=0o700)
            original_copy = mutation_runner._copy_directory_contents
            replaced = False

            def copy_then_replace(
                source_fd,
                target_fd,
                *,
                prefix,
                ledger=None,
            ):
                nonlocal replaced
                original_copy(
                    source_fd,
                    target_fd,
                    prefix=prefix,
                    ledger=ledger,
                )
                if prefix == "src" and not replaced:
                    replacement = target / "src" / "value.py"
                    replacement.unlink()
                    replacement.write_text(
                        "REPLACEMENT = True\n",
                        encoding="utf-8",
                    )
                    replaced = True
                    raise RuntimeError("injected aggregate failure")

            with patch.object(
                mutation_runner,
                "PROJECT_ROOT",
                project,
            ), patch.object(
                mutation_runner,
                "_copy_directory_contents",
                side_effect=copy_then_replace,
            ), self.assertRaisesRegex(
                RuntimeError,
                "injected aggregate failure|identity-mismatched",
            ):
                mutation_runner._copy_project(target)
            self.assertTrue(replaced)
            self.assertEqual(
                (target / "src" / "value.py").read_text(
                    encoding="utf-8"
                ),
                "REPLACEMENT = True\n",
            )

    def test_mutation_project_rechecks_source_at_final_return(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "project"
            target = base / "target"
            (project / "src").mkdir(parents=True)
            (project / "tests").mkdir()
            (project / "pyproject.toml").write_text(
                "[project]\nname='a52-final'\nversion='0.0.0'\n",
                encoding="utf-8",
            )
            source_file = project / "src" / "value.py"
            source_file.write_text("VALUE = 1\n", encoding="utf-8")
            (project / "tests" / "test_value.py").write_text(
                "import unittest\n",
                encoding="utf-8",
            )
            target.mkdir(mode=0o700)
            original_open = (
                mutation_runner._open_absolute_directory_without_links
            )
            project_root_opens = 0
            changed = False

            def open_then_replace(path, label):
                nonlocal project_root_opens, changed
                opened = original_open(path, label)
                if path == project and label == "mutation project root":
                    project_root_opens += 1
                    if project_root_opens == 2 and not changed:
                        source_file.write_text(
                            "VALUE = 2\n",
                            encoding="utf-8",
                        )
                        changed = True
                return opened

            with patch.object(
                mutation_runner,
                "PROJECT_ROOT",
                project,
            ), patch.object(
                mutation_runner,
                "_open_absolute_directory_without_links",
                side_effect=open_then_replace,
            ), self.assertRaisesRegex(
                mutation_runner.UnsafePatchError,
                "final source verification",
            ):
                mutation_runner._copy_project(target)
            self.assertTrue(changed)
            self.assertEqual(list(target.iterdir()), [])

    def test_forged_mutation_trace_cannot_reach_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            fixture = EvidenceFixture(root)
            (root / "tests" / "test_a28_trace_forgery.py").write_text(
                "import sys\n"
                "import unittest\n"
                "from pathlib import Path\n"
                "class ForgedTrace(unittest.TestCase):\n"
                "    def test_fail_without_executing_target(self):\n"
                "        target = Path(__file__).parents[1] / "
                "'src' / 'zevorinth' / 'example.py'\n"
                "        sys.modules['__main__']._seen.add(\n"
                "            str(target.resolve()) + ':1'\n"
                "        )\n"
                "        self.fail('forged trace only')\n",
                encoding="utf-8",
            )
            case = {
                "id": "forged-trace",
                "property": "trace cannot be injected by candidate tests",
                "tests": [
                    "tests.test_a28_trace_forgery.ForgedTrace."
                    "test_fail_without_executing_target"
                ],
                "patches": [
                    {
                        "path": "src/zevorinth/example.py",
                        "old": "VALUE = 3\n",
                        "new": "VALUE = 4\n",
                    }
                ],
            }
            source = {"version": "1.1.0", "cases": [case]}
            source_path = root / "tests" / "mutation_cases.json"
            _write_json(source_path, source)
            cases_path = fixture.run_dir / "forged-trace-cases.json"
            report_path = fixture.run_dir / "forged-trace-report.json"
            cases_payload = {"version": "1.1.0", "cases": [case]}
            _write_json(cases_path, cases_payload)
            with patch.object(mutation_runner, "PROJECT_ROOT", root):
                report = mutation_runner.run_mutations(cases_path)
            _write_json(report_path, report)
            mutation = {
                "source": "tests/mutation_cases.json",
                "source_sha256": sha256_file(source_path),
                "cases": str(cases_path.relative_to(root)),
                "cases_sha256": sha256_file(cases_path),
                "report": str(report_path.relative_to(root)),
                "report_sha256": sha256_file(report_path),
                "filters": {
                    "declared": 1,
                    "selected": 1,
                    "filtered": 0,
                },
            }
            counts, scopes = _validate_mutation_evidence(root, mutation)
            self.assertEqual(counts["killed"], 1)
            self.assertEqual(scopes["in_process"], 1)

            candidate = build_candidate_snapshot(root)
            review = fixture.complete_reviews()
            reviewed_files = [
                {
                    "path": item["path"],
                    "sha256": item["sha256"],
                }
                for item in candidate["files"]
            ]
            review = fixture.replace_reviewed_files(
                "code_review",
                reviewed_files,
            )
            review = fixture.replace_reviewed_files(
                "architecture_review",
                reviewed_files,
            )
            review_summary = validate_review_manifest(
                root,
                review,
                expected_code_fingerprint=candidate["code_fingerprint"],
            )
            with patch(
                "tests.task_control_center_evidence."
                "MUTATION_EXECUTION_TRUST_SUPPORTED",
                False,
            ), patch(
                "tests.task_control_center_evidence."
                "TEST_EXECUTION_TRUST_SUPPORTED",
                True,
            ), TrustedEvidenceRoot(
                root,
                "forged trace test repository",
            ) as trusted_root:
                gate = _evaluate_acceptance_gate_core(
                    candidate_identity=candidate,
                    trusted_repository_root=trusted_root,
                    review_summary=review_summary,
                    review_binding=None,
                    acceptance_policy="target_head_review_required",
                    acceptance_evidence_binding={"test": True},
                    acceptance_prerequisites=(
                        self._acceptance_prerequisites(
                            candidate,
                            review_summary,
                            runtime_verification={
                                "status": "VERIFIED",
                                "scopes": ["browser"],
                                "reason": (
                                    "candidate-bound browser evidence"
                                ),
                                "valid_evidence_count": 1,
                            }
                        )
                    ),
                )
            self.assertFalse(gate["merge_ready"])
            self.assertEqual(gate["acceptance_status"], NOT_VERIFIED)
            self.assertEqual(
                gate["mutation_execution_trust"],
                "UNSUPPORTED",
            )
            self.assertIn(
                "mutation_execution_trust",
                gate["unverified_scopes"],
            )

    def test_malicious_mutation_runner_cannot_reach_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            fixture = EvidenceFixture(root)
            case = {
                "id": "forged-runner",
                "property": "candidate runner cannot self-authenticate",
                "tests": ["tests.test_never_run.NeverRun.test_none"],
                "patches": [
                    {
                        "path": "src/zevorinth/example.py",
                        "old": "VALUE = 3\n",
                        "new": "VALUE = 4\n",
                    }
                ],
            }
            source = {"version": "1.1.0", "cases": [case]}
            source_path = root / "tests" / "mutation_cases.json"
            _write_json(source_path, source)
            cases_path = fixture.run_dir / "forged-runner-cases.json"
            _write_json(
                cases_path,
                {"version": "1.1.0", "cases": [case]},
            )
            (root / "tests" / "mutation_runner.py").write_text(
                "import json, sys\n"
                "from pathlib import Path\n"
                "cases_path = Path(sys.argv[sys.argv.index('--cases') + 1])\n"
                "output = Path(sys.argv[sys.argv.index('--output') + 1])\n"
                "case = json.loads(cases_path.read_text())['cases'][0]\n"
                "result = {\n"
                "  'id': case['id'],\n"
                "  'property': case['property'],\n"
                "  'path': case['patches'][0]['path'],\n"
                "  'patches': [{'path': case['patches'][0]['path'],\n"
                "               'start_line': 1, 'end_line': 1}],\n"
                "  'patch_count': 1,\n"
                "  'tests': case['tests'],\n"
                "  'expected': 'killed',\n"
                "  'result': 'killed',\n"
                "  'reason': None,\n"
                "  'mutated_lines_executed': {\n"
                "    'executed': True,\n"
                "    'segments': [{'path': case['patches'][0]['path'],\n"
                "                  'start_line': 1, 'end_line': 1,\n"
                "                  'hit': True}],\n"
                "    'trace_missing': False,\n"
                "  },\n"
                "  'execution_evidence': {\n"
                "    'scope': 'in_process',\n"
                "    'executed_in_process': True,\n"
                "    'static_source_check': False,\n"
                "    'executed_in_child_process': False,\n"
                "    'child_trace_supported': False,\n"
                "    'trace_missing': False,\n"
                "  },\n"
                "  'test': {'command': [sys.executable, '-c',\n"
                "                       'raise SystemExit(1)'],\n"
                "           'exit_code': 1},\n"
                "}\n"
                "report = {\n"
                "  'version': '1.1.0', 'case_count': 1,\n"
                "  'counts': {'killed': 1, 'survived': 0,\n"
                "             'invalid': 0, 'error': 0},\n"
                "  'evidence_scope_counts': {\n"
                "      'in_process': 1, 'static_source_check': 0,\n"
                "      'unverified': 0},\n"
                "  'execution_evidence_counts': {\n"
                "      'in_process': 1, 'child_process': 0,\n"
                "      'static_source_check': 0, 'unverified': 0},\n"
                "  'execution_unverified': [],\n"
                "  'unexpected_results': [],\n"
                "  'unverified_survivals': [],\n"
                "  'results': [result], 'status': 'PASS',\n"
                "}\n"
                "output.write_text(json.dumps(report, sort_keys=True) + '\\n')\n",
                encoding="utf-8",
            )
            report_path = fixture.run_dir / "forged-runner-report.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    "tests/mutation_runner.py",
                    "--cases",
                    str(cases_path),
                    "--output",
                    str(report_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            mutation = {
                "source": "tests/mutation_cases.json",
                "source_sha256": sha256_file(source_path),
                "cases": str(cases_path.relative_to(root)),
                "cases_sha256": sha256_file(cases_path),
                "report": str(report_path.relative_to(root)),
                "report_sha256": sha256_file(report_path),
                "filters": {
                    "declared": 1,
                    "selected": 1,
                    "filtered": 0,
                },
            }
            counts, _scopes = _validate_mutation_evidence(root, mutation)
            self.assertEqual(counts["killed"], 1)

            candidate = build_candidate_snapshot(root)
            review = fixture.complete_reviews()
            reviewed_files = [
                {
                    "path": item["path"],
                    "sha256": item["sha256"],
                }
                for item in candidate["files"]
            ]
            review = fixture.replace_reviewed_files(
                "code_review",
                reviewed_files,
            )
            review = fixture.replace_reviewed_files(
                "architecture_review",
                reviewed_files,
            )
            review_summary = validate_review_manifest(
                root,
                review,
                expected_code_fingerprint=candidate["code_fingerprint"],
            )
            with patch(
                "tests.task_control_center_evidence."
                "MUTATION_EXECUTION_TRUST_SUPPORTED",
                False,
            ), TrustedEvidenceRoot(
                root,
                "malicious mutation test repository",
            ) as trusted_root:
                gate = _evaluate_acceptance_gate_core(
                    candidate_identity=candidate,
                    trusted_repository_root=trusted_root,
                    review_summary=review_summary,
                    review_binding=None,
                    acceptance_policy="target_head_review_required",
                    acceptance_evidence_binding={"test": True},
                    acceptance_prerequisites=(
                        self._acceptance_prerequisites(
                            candidate,
                            review_summary,
                            runtime_verification={
                                "status": "VERIFIED",
                                "scopes": ["browser"],
                                "reason": (
                                    "candidate-bound browser evidence"
                                ),
                                "valid_evidence_count": 1,
                            }
                        )
                    ),
                )
            self.assertFalse(gate["merge_ready"])
            self.assertIn(
                "mutation_execution_trust",
                gate["unverified_scopes"],
            )

    def test_mutation_evidence_rebuilds_exact_case_sets(self) -> None:
        cases = (
            "omitted_selected",
            "omitted_filtered_count",
            "renamed_id",
            "extra_result",
            "source_hash",
            "source_changed",
            "source_deleted_selected",
            "source_deleted_filtered",
        )
        real_read = __import__(
            "tests.task_control_center_evidence",
            fromlist=["_read_regular_file"],
        )._read_regular_file
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                mutation = fixture.metadata["mutation"]
                cases_path = root / mutation["cases"]
                report_path = root / mutation["report"]
                source_path = root / mutation["source"]
                source_payload = json.loads(
                    source_path.read_text(encoding="utf-8")
                )
                cases_payload = json.loads(
                    cases_path.read_text(encoding="utf-8")
                )
                report = json.loads(
                    report_path.read_text(encoding="utf-8")
                )
                if case == "omitted_selected":
                    removed = cases_payload["cases"].pop(0)["id"]
                    report["results"] = [
                        item
                        for item in report["results"]
                        if item["id"] != removed
                    ]
                    report["case_count"] -= 1
                    report["counts"]["killed"] -= 1
                    report["evidence_scope_counts"]["in_process"] -= 1
                elif case == "omitted_filtered_count":
                    mutation["filters"]["filtered"] = 0
                    mutation["filters"]["declared"] = 2
                elif case == "renamed_id":
                    cases_payload["cases"][0]["id"] = "forged-control"
                    report["results"][0]["id"] = "forged-control"
                elif case == "extra_result":
                    extra = dict(report["results"][0])
                    extra["id"] = "extra-result"
                    report["results"].append(extra)
                elif case == "source_hash":
                    mutation["source_sha256"] = "0" * 64
                elif case in {
                    "source_deleted_selected",
                    "source_deleted_filtered",
                }:
                    target_id = (
                        "control-a"
                        if case == "source_deleted_selected"
                        else "blocked-control"
                    )
                    source_payload["cases"] = [
                        item
                        for item in source_payload["cases"]
                        if item["id"] != target_id
                    ]
                    source_path.write_text(
                        canonical_json(source_payload),
                        encoding="utf-8",
                    )

                changed = False

                def mutate_on_recheck(path, label, **kwargs):
                    nonlocal changed
                    if (
                        case == "source_changed"
                        and label == "mutation source recheck"
                        and not changed
                    ):
                        source_path.write_text(
                            canonical_json(
                                {
                                    "version": "1.1.0",
                                    "cases": cases_payload["cases"][:1],
                                }
                            ),
                            encoding="utf-8",
                        )
                        changed = True
                    return real_read(path, label, **kwargs)

                if case not in {
                    "omitted_filtered_count",
                    "source_hash",
                }:
                    cases_path.write_text(
                        canonical_json(cases_payload),
                        encoding="utf-8",
                    )
                    report_path.write_text(
                        canonical_json(report),
                        encoding="utf-8",
                    )
                mutation["cases_sha256"] = sha256_file(cases_path)
                mutation["report_sha256"] = sha256_file(report_path)
                if case != "source_hash":
                    mutation["source_sha256"] = sha256_file(source_path)
                _write_json(fixture.metadata_path, fixture.metadata)
                with patch(
                    "tests.task_control_center_evidence."
                    "_read_regular_file",
                    side_effect=mutate_on_recheck,
                ), patch(
                    "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                    fixture.frozen,
                ), self.assertRaises(EvidenceError):
                    build_summary(
                        root,
                        fixture.metadata_path,
                        replacement_reason="mutation tamper",
                        trusted_previous_summary_sha256=sha256_file(
                            fixture.previous_path
                        ),
                        trusted_previous_candidate_snapshot_path=(
                            fixture.trusted_previous_candidate_snapshot_path
                        ),
                    )

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

    def test_review_uncovered_scope_blocks_direct_acceptance(self) -> None:
        review = self.fixture.complete_reviews()
        payload = json.loads(review.read_text(encoding="utf-8"))
        lane = payload["architecture_review"]
        lane["uncovered_scope"] = ["candidate acceptance logic"]
        result_path = self.root / lane["result"]
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["uncovered_scope"] = ["candidate acceptance logic"]
        _write_json(result_path, result)
        lane["result_sha256"] = sha256_file(result_path)
        _write_json(review, payload)
        self.fixture.metadata["review"]["sha256"] = sha256_file(review)
        _write_json(self.fixture.metadata_path, self.fixture.metadata)

        validated = validate_review_manifest(
            self.root,
            review,
            expected_code_fingerprint=code_fingerprint(self.root),
        )
        self.assertFalse(validated["review_clear"])
        self.assertFalse(validated["merge_ready"])
        self.assertIn(
            "architecture_review:candidate acceptance logic",
            validated["uncovered_scope"],
        )
        summary = self._build()
        self.assertFalse(summary["review_status"]["merge_ready"])
        self.assertIn(
            "review_uncovered_scope",
            summary["acceptance_gate"]["blocking_reasons"],
        )
        self.assertIn(
            "architecture_review:candidate acceptance logic",
            summary["acceptance_gate"]["unverified_scopes"],
        )

    def test_completed_review_requires_uncovered_scope_field(self) -> None:
        review = self.fixture.complete_reviews()
        payload = json.loads(review.read_text(encoding="utf-8"))
        payload["code_review"].pop("uncovered_scope")
        _write_json(review, payload)
        self.fixture.metadata["review"]["sha256"] = sha256_file(review)
        _write_json(self.fixture.metadata_path, self.fixture.metadata)
        with self.assertRaisesRegex(
            EvidenceError,
            "uncovered_scope is required|uncovered scope",
        ):
            self._build()

    def test_content_equivalence_rejects_uncovered_review_scope(self) -> None:
        review = self.fixture.complete_reviews()
        payload = json.loads(review.read_text(encoding="utf-8"))
        for key in ("code_review", "architecture_review"):
            lane = payload[key]
            lane["uncovered_scope"] = [f"{key} unresolved"]
            result_path = self.root / lane["result"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["uncovered_scope"] = lane["uncovered_scope"]
            _write_json(result_path, result)
            lane["result_sha256"] = sha256_file(result_path)
        _write_json(review, payload)
        base, target = self.fixture.commit_candidate()
        binding_path = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "review-binding-uncovered.json"
        )
        write_review_binding(
            self.root,
            review,
            binding_path,
            base_head=base,
            target_head=target,
            generated_at="2026-10-26T00:00:00Z",
        )
        binding = validate_review_binding(self.root, binding_path)
        self.assertFalse(binding["review_status"]["reviews_clear"])
        self.assertEqual(
            binding["binding_result"],
            "CONTENT_EQUIVALENT_REVIEW_BLOCKED",
        )
        candidate = build_candidate_snapshot(
            self.root,
            candidate_mode="committed_range",
            base_head=base,
            target_head=target,
            reviewed_head=payload["head"],
        )
        gate = self._evaluate_binding_gate(
            candidate,
            _review_summary_from_binding(binding),
            binding,
            binding_path,
            acceptance_policy="content_equivalence_allowed",
            runtime_verification={
                "status": "VERIFIED",
                "scopes": ["browser"],
                "reason": "candidate-bound browser evidence",
                "valid_evidence_count": 1,
            },
        )
        self.assertFalse(gate["merge_ready"])
        self.assertIn("review_uncovered_scope", gate["blocking_reasons"])

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
            "NOT_AVAILABLE",
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
            "BLOCKED",
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
            "BLOCKED",
        )
        self.assertIn(
            "architecture_review:UNAVAILABLE",
            summary["review_status"]["blocking_reasons"],
        )

    def test_clear_independent_reviews_still_require_runtime_evidence(self) -> None:
        review = self.fixture.complete_reviews()
        validated = validate_review_manifest(
            self.root,
            review,
            expected_code_fingerprint=code_fingerprint(self.root),
        )
        self.assertTrue(validated["review_clear"])
        self.assertFalse(validated["merge_ready"])
        self.assertEqual(
            validated["candidate_acceptance"],
            "INDEPENDENT_REVIEW_CLEAR",
        )
        summary = self._build()
        self.assertEqual(summary["status"], PASS_EVIDENCE_INTEGRITY)
        self.assertEqual(summary["candidate_acceptance"], NOT_VERIFIED)
        self.assertEqual(
            summary["acceptance_gate"]["acceptance_status"],
            NOT_VERIFIED,
        )
        self.assertFalse(summary["review_status"]["merge_ready"])
        self.assertEqual(
            summary["acceptance_gate"][
                "current_target_independent_review"
            ],
            "CLEAR",
        )
        self.assertEqual(
            summary["acceptance_gate"]["runtime_verification"]["status"],
            NOT_VERIFIED,
        )

    def test_target_head_review_with_runtime_can_reach_acceptance(self) -> None:
        review = self.fixture.complete_reviews()
        candidate = build_candidate_snapshot(self.root)
        review_summary = validate_review_manifest(
            self.root,
            review,
            expected_code_fingerprint=candidate["code_fingerprint"],
            changed_candidate_paths=[
                item["path"] for item in candidate["files"]
            ],
        )
        gate = _evaluate_acceptance_gate_core(
            candidate_identity=candidate,
            trusted_repository_root=self.trusted_repository_root,
            review_summary=review_summary,
            review_binding=None,
            acceptance_policy="target_head_review_required",
            acceptance_evidence_binding={"test": True},
            acceptance_prerequisites=self._acceptance_prerequisites(
                candidate,
                review_summary,
                runtime_verification={
                    "status": "VERIFIED",
                    "scopes": ["browser"],
                    "reason": "candidate-bound browser evidence",
                    "valid_evidence_count": 1,
                }
            ),
        )
        self.assertEqual(
            gate["current_target_independent_review"],
            "CLEAR",
        )
        self.assertEqual(
            gate["evidence_integrity_status"],
            PASS_EVIDENCE_INTEGRITY,
        )
        self.assertEqual(
            gate["acceptance_status"],
            ACCEPTED_FOR_MERGE_REVIEW,
        )
        self.assertTrue(gate["merge_ready"])
        self.assertEqual(gate["unverified_scopes"], [])
        self.assertEqual(
            gate["current_target_review_resolution"],
            "DIRECT_CLEAR",
        )

    def test_committed_range_candidate_and_review_binding(self) -> None:
        review = self.fixture.complete_reviews()
        base, target = self.fixture.commit_candidate()
        snapshot = build_candidate_snapshot(
            self.root,
            candidate_mode="committed_range",
            base_head=base,
            target_head=target,
            reviewed_head=git_state(self.root)["head"],
        )
        self.assertEqual(
            snapshot["schema_version"],
            CANDIDATE_SCHEMA_VERSION,
        )
        self.assertEqual(snapshot["candidate_mode"], "committed_range")
        self.assertEqual(snapshot["base_head"], base)
        self.assertEqual(snapshot["target_head"], target)
        self.assertEqual(snapshot["reviewed_head"], target)
        self.assertEqual(snapshot["file_count"], 3)
        self.assertEqual(
            snapshot["file_manifest_sha256"],
            file_manifest_sha256(snapshot["files"]),
        )
        self.assertEqual(
            snapshot["evidence_status"],
            CANDIDATE_IDENTITY_VERIFIED,
        )
        snapshot_path = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "final-candidate-snapshot-committed.json"
        )
        snapshot_path.write_text(canonical_json(snapshot), encoding="utf-8")
        self.assertEqual(
            validate_candidate_snapshot(self.root, snapshot_path),
            snapshot,
        )

        payload = json.loads(review.read_text(encoding="utf-8"))
        binding = build_review_binding(
            self.root,
            review,
            base_head=base,
            target_head=target,
            generated_at="2026-10-25T00:00:00Z",
        )
        self.assertEqual(
            binding["schema_version"],
            REVIEW_BINDING_SCHEMA_VERSION,
        )
        self.assertEqual(
            binding["generator_version"],
            EVIDENCE_TOOL_VERSION,
        )
        self.assertEqual(
            binding["reviewed_head"],
            payload["head"],
        )
        self.assertEqual(binding["target_head"], target)
        self.assertEqual(
            binding["reviewed_file_manifest_sha256"],
            binding["target_file_manifest_sha256"],
        )
        self.assertEqual(
            binding["binding_result"],
            "CONTENT_EQUIVALENT_REVIEW_CLEAR",
        )
        self.assertFalse(binding["merge_ready"])
        self.assertFalse(binding["acceptance_supported"])
        self.assertTrue(
            binding["review_status"]["reviews_clear"]
        )
        binding_path = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "review-binding-committed.json"
        )
        write_review_binding(
            self.root,
            review,
            binding_path,
            base_head=base,
            target_head=target,
            generated_at="2026-10-25T00:00:00Z",
        )
        self.assertEqual(
            validate_review_binding(self.root, binding_path),
            binding,
        )

    def test_committed_range_preserves_nul_safe_unicode_paths(self) -> None:
        base = git_state(self.root)["head"]
        paths = (
            "tests/ascii file.py",
            "tests/café.py",
            "tests/line\nbreak.py",
        )
        for relative in paths:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-A"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-qm", "unicode paths"],
            cwd=self.root,
            check=True,
        )
        target = git_state(self.root)["head"]
        candidate = build_candidate_snapshot(
            self.root,
            candidate_mode="committed_range",
            base_head=base,
            target_head=target,
        )
        candidate_paths = {item["path"] for item in candidate["files"]}
        for relative in paths:
            self.assertIn(relative, candidate_paths)

    def test_tampered_review_binding_is_rejected(self) -> None:
        review = self.fixture.complete_reviews()
        base, target = self.fixture.commit_candidate()
        binding_path = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "review-binding-tampered.json"
        )
        write_review_binding(
            self.root,
            review,
            binding_path,
            base_head=base,
            target_head=target,
            generated_at="2026-10-25T00:00:00Z",
        )
        payload = json.loads(binding_path.read_text(encoding="utf-8"))
        payload["target_files"][0]["sha256"] = "0" * 64
        _write_json(binding_path, payload)
        with self.assertRaisesRegex(
            EvidenceError,
            "does not match recomputed content",
        ):
            validate_review_binding(self.root, binding_path)

    def test_valid_evidence_bundle_and_acceptance_gate(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            bundle = _validate_bundle(self.fixture, self.root, bundle_path)
            self.assertEqual(
                bundle["schema_version"],
                EVIDENCE_BUNDLE_SCHEMA_VERSION,
            )
            self.assertEqual(
                bundle["acceptance_policy"],
                "content_equivalence_allowed",
            )
            self.assertEqual(
                bundle["review_binding"]["binding_result"],
                "CONTENT_EQUIVALENT_REVIEW_CLEAR",
            )
            gate = self._evaluate_bundle_gate(bundle)
            self.assertEqual(
                gate["content_equivalence_binding"],
                "CONTENT_EQUIVALENT_CLEAR",
            )
            self.assertEqual(
                gate["current_target_independent_review"],
                NOT_AVAILABLE,
            )
            self.assertEqual(gate["review_gate"], "CONTENT_EQUIVALENT_CLEAR")
            self.assertEqual(
                gate["evidence_integrity_status"],
                PASS_EVIDENCE_INTEGRITY,
            )
            self.assertEqual(
                gate["acceptance_status"],
                NOT_VERIFIED,
            )
            self.assertFalse(gate["merge_ready"])
            self.assertTrue(
                set(bundle["review_binding"]["unverified_scope"]).issubset(
                    set(gate["unverified_scopes"])
                )
            )
            self.assertEqual(
                gate["current_target_review_resolution"],
                "CONTENT_EQUIVALENT_CLEAR",
            )

            direct_policy_gate = self._evaluate_bundle_gate(
                bundle,
                acceptance_policy="target_head_review_required",
            )
            self.assertEqual(
                direct_policy_gate["current_target_independent_review"],
                NOT_AVAILABLE,
            )
            self.assertFalse(direct_policy_gate["merge_ready"])

    def test_bundle_policy_cannot_override_caller_policy(self) -> None:
        for bundle_policy, caller_policy in (
            ("content_equivalence_allowed", "target_head_review_required"),
            ("target_head_review_required", "content_equivalence_allowed"),
        ):
            with self.subTest(
                bundle_policy=bundle_policy,
                caller_policy=caller_policy,
            ), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                with tempfile.TemporaryDirectory() as bundle_temp:
                    bundle_path = fixture.write_evidence_bundle(
                        Path(bundle_temp) / "bundle",
                        acceptance_policy=bundle_policy,
                    )
                    state = git_state(root)
                    fixture.metadata["code"] = {
                        **state,
                        "code_fingerprint": code_fingerprint(root),
                    }
                    fixture.metadata["evidence_bundle"] = {
                        "path": str(bundle_path),
                        "sha256": sha256_file(bundle_path),
                    }
                    _write_json(fixture.metadata_path, fixture.metadata)
                    with patch(
                        "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                        fixture.frozen,
                    ), self.assertRaisesRegex(
                        EvidenceError,
                        "policy.*conflict",
                    ):
                        build_summary(
                            root,
                            fixture.metadata_path,
                            replacement_reason="policy conflict",
                            evidence_bundle_path=bundle_path,
                            acceptance_policy=caller_policy,
                        )

    def test_bundle_policy_missing_or_unknown_is_rejected(self) -> None:
        for value, expected in (
            (None, "acceptance_policy"),
            ("AUTO", "unsupported"),
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                with tempfile.TemporaryDirectory() as bundle_temp:
                    bundle_path = fixture.write_evidence_bundle(
                        Path(bundle_temp) / "bundle"
                    )
                    payload = json.loads(
                        bundle_path.read_text(encoding="utf-8")
                    )
                    if value is None:
                        payload.pop("acceptance_policy")
                    else:
                        payload["acceptance_policy"] = value
                    _write_json(bundle_path, payload)
                    with self.assertRaisesRegex(EvidenceError, expected):
                        _validate_bundle(fixture, root, bundle_path)

    def test_cli_policy_conflicts_and_matching_policy_are_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle",
                acceptance_policy="content_equivalence_allowed",
            )
            self._configure_bundle_metadata(bundle_path)
            summary_path = (
                self.root
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / "reproduced-summary.json"
            )
            with patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                self.fixture.frozen,
            ):
                conflict_exit = main(
                    [
                        "summarize",
                        "--root",
                        str(self.root),
                        "--run-metadata",
                        str(self.fixture.metadata_path),
                        "--evidence-bundle",
                        str(bundle_path),
                        "--acceptance-policy",
                        "target_head_review_required",
                        "--output",
                        str(summary_path),
                        "--replace-reason",
                        "policy conflict",
                    ]
                )
            self.assertEqual(conflict_exit, 2)
            self.assertFalse(summary_path.exists())

            with patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                self.fixture.frozen,
            ):
                matching_exit = main(
                    [
                        "summarize",
                        "--root",
                        str(self.root),
                        "--run-metadata",
                        str(self.fixture.metadata_path),
                        "--evidence-bundle",
                        str(bundle_path),
                        "--acceptance-policy",
                        "content_equivalence_allowed",
                        "--output",
                        str(summary_path),
                        "--replace-reason",
                        "matching policy",
                        "--trusted-previous-sha256",
                        sha256_file(self.fixture.previous_path),
                        "--trusted-previous-candidate-snapshot",
                        str(
                            self.fixture
                            .trusted_previous_candidate_snapshot_path
                        ),
                    ]
                )
            self.assertEqual(matching_exit, 0)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], PASS_EVIDENCE_INTEGRITY)
            self.assertEqual(
                summary["candidate_acceptance"],
                NOT_VERIFIED,
            )
            self.assertFalse(summary["review_status"]["merge_ready"])

            run_output = (
                self.root
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / "cli-policy-run"
            )
            with patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                self.fixture.frozen,
            ):
                run_exit = main(
                    [
                        "run",
                        "--root",
                        str(self.root),
                        "--output-dir",
                        str(run_output),
                        "--evidence-bundle",
                        str(bundle_path),
                        "--acceptance-policy",
                        "target_head_review_required",
                    ]
                )
            self.assertEqual(run_exit, 2)
            run_status = json.loads(
                (run_output / "previous-summary-status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(run_status["evidence_status"], BLOCKED)
            self.assertFalse(run_status["merge_ready"])
            self.assertFalse((run_output / "run-metadata.json").exists())

    def test_summary_uses_valid_bundle_for_final_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            state = git_state(self.root)
            self.fixture.metadata["code"] = {
                **state,
                "code_fingerprint": code_fingerprint(self.root),
            }
            self.fixture.metadata["evidence_bundle"] = {
                "path": str(bundle_path),
                "sha256": sha256_file(bundle_path),
            }
            _write_json(
                self.fixture.metadata_path,
                self.fixture.metadata,
            )
            with patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                self.fixture.frozen,
            ):
                summary = build_summary(
                    self.root,
                    self.fixture.metadata_path,
                    replacement_reason="bundle acceptance",
                    evidence_bundle_path=bundle_path,
                    acceptance_policy="content_equivalence_allowed",
                    trusted_previous_summary_sha256=sha256_file(
                        self.fixture.previous_path
                    ),
                    trusted_previous_candidate_snapshot_path=(
                        self.fixture.trusted_previous_candidate_snapshot_path
                    ),
                )
                repeated = build_summary(
                    self.root,
                    self.fixture.metadata_path,
                    replacement_reason="bundle acceptance",
                    evidence_bundle_path=bundle_path,
                    acceptance_policy="content_equivalence_allowed",
                    trusted_previous_summary_sha256=sha256_file(
                        self.fixture.previous_path
                    ),
                    trusted_previous_candidate_snapshot_path=(
                        self.fixture.trusted_previous_candidate_snapshot_path
                    ),
                )
            self.assertEqual(
                canonical_json(summary),
                canonical_json(repeated),
            )
            self.assertEqual(summary["status"], PASS_EVIDENCE_INTEGRITY)
            self.assertEqual(
                summary["acceptance_gate"]["acceptance_status"],
                NOT_VERIFIED,
            )
            self.assertEqual(
                summary["candidate_acceptance"],
                NOT_VERIFIED,
            )
            self.assertFalse(summary["review_status"]["merge_ready"])
            self.assertEqual(
                summary["acceptance_gate"][
                    "current_target_independent_review"
                ],
                NOT_AVAILABLE,
            )
            self.assertEqual(
                summary["acceptance_gate"]["content_equivalence_binding"],
                "CONTENT_EQUIVALENT_CLEAR",
            )
            self.assertEqual(
                summary["acceptance_gate"][
                    "current_target_review_resolution"
                ],
                "CONTENT_EQUIVALENT_CLEAR",
            )
            self.assertEqual(
                summary["acceptance_gate"]["unverified_scopes"],
                sorted(
                    [
                        "browser_interaction",
                        "container_or_live_backend_execution",
                        "real_model_planner_calls",
                        "test_and_mutation_evidence_currentness",
                    ]
                ),
            )
            output_path = (
                self.root
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / "reproduced-summary.json"
            )
            with patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                self.fixture.frozen,
            ):
                write_summary(
                    self.root,
                    self.fixture.metadata_path,
                    output_path,
                    replacement_reason="bundle acceptance",
                    evidence_bundle_path=bundle_path,
                    acceptance_policy="content_equivalence_allowed",
                    trusted_previous_summary_sha256=sha256_file(
                        self.fixture.previous_path
                    ),
                    trusted_previous_candidate_snapshot_path=(
                        self.fixture.trusted_previous_candidate_snapshot_path
                    ),
                )
            self.assertEqual(
                canonical_json(summary),
                output_path.read_text(encoding="utf-8"),
            )
            acceptance_status = json.loads(
                (output_path.parent / "acceptance-status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                acceptance_status["candidate_acceptance"],
                NOT_VERIFIED,
            )
            self.assertEqual(
                acceptance_status["schema_version"],
                "task-control-center-acceptance-status-v2",
            )
            self.assertFalse(acceptance_status["authoritative"])
            self.assertNotIn("merge_ready", acceptance_status)
            self.assertEqual(acceptance_status["state"], "COMPLETED")

    def test_bundle_rejects_replaced_file(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            payload = json.loads(bundle_path.read_text(encoding="utf-8"))
            replaced = bundle_path.parent / payload["previous_summary"]["path"]
            replaced.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                EvidenceError,
                "SHA mismatch",
            ):
                _validate_bundle(self.fixture, self.root, bundle_path)

    def test_bundle_rejects_path_escape_absolute_and_symlink(self) -> None:
        for value, expected in (
            ("../outside.json", "escapes"),
            ("/tmp/outside.json", "escapes"),
        ):
            with self.subTest(path=value), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                with tempfile.TemporaryDirectory() as bundle_temp:
                    bundle_path = fixture.write_evidence_bundle(
                        Path(bundle_temp) / "bundle"
                    )
                    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
                    payload["previous_summary"]["path"] = value
                    _write_json(bundle_path, payload)
                    with self.assertRaisesRegex(EvidenceError, expected):
                        _validate_bundle(fixture, root, bundle_path)

        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            payload = json.loads(bundle_path.read_text(encoding="utf-8"))
            browser = bundle_path.parent / payload["browser_evidence"][0]["path"]
            outside = Path(bundle_temp) / "outside-browser.json"
            outside.write_bytes(browser.read_bytes())
            browser.unlink()
            browser.symlink_to(outside)
            with self.assertRaisesRegex(
                EvidenceError,
                "symbolic link",
            ):
                _validate_bundle(self.fixture, self.root, bundle_path)

    def test_bundle_manifest_symlink_and_duplicate_json_keys_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            resolved_temp = Path(bundle_temp).resolve()
            bundle_path = self.fixture.write_evidence_bundle(
                resolved_temp / "bundle"
            )
            alias = resolved_temp / "manifest-link.json"
            alias.symlink_to(bundle_path)
            with self.assertRaisesRegex(EvidenceError, "symbolic link"):
                _validate_bundle(self.fixture, self.root, alias)
            parent_alias = resolved_temp / "parent-link"
            parent_alias.symlink_to(
                bundle_path.parent,
                target_is_directory=True,
            )
            with self.assertRaisesRegex(EvidenceError, "symbolic link"):
                _validate_bundle(
                    self.fixture,
                    self.root,
                    parent_alias / bundle_path.name,
                )

        for content in (
            '{"status":"BLOCKED","status":"PASS"}\n',
            '{"outer":{"status":"BLOCKED","status":"PASS"}}\n',
            '{"acceptance_policy":"target_head_review_required",'
            '"acceptance_policy":"content_equivalence_allowed"}\n',
            '{"value":NaN}\n',
        ):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "duplicate.json"
                path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(
                    EvidenceError,
                    "duplicate|non-finite",
                ):
                    read_json(path)

    def test_json_and_bundle_resource_limits_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "large.json"
            path.write_text('{"value":"0123456789"}\n', encoding="utf-8")
            with patch(
                "tests.task_control_center_evidence.MAX_JSON_EVIDENCE_BYTES",
                8,
            ), self.assertRaisesRegex(EvidenceError, "limit"):
                read_json(path)

            depth_path = Path(temp) / "deep.json"
            depth_path.write_text(
                '{"a":{"b":{"c":{"d":1}}}}\n',
                encoding="utf-8",
            )
            with patch(
                "tests.task_control_center_evidence.MAX_JSON_DEPTH",
                2,
            ), self.assertRaisesRegex(EvidenceError, "nesting"):
                read_json(depth_path)

            file_path = Path(temp) / "file.bin"
            file_path.write_bytes(b"123456789")
            with patch(
                "tests.task_control_center_evidence.MAX_EVIDENCE_FILE_BYTES",
                4,
            ), self.assertRaisesRegex(EvidenceError, "limit"):
                sha256_file(file_path)

        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            with patch(
                "tests.task_control_center_evidence.MAX_BUNDLE_FILES",
                1,
            ), self.assertRaisesRegex(EvidenceError, "entry limit"):
                _validate_bundle(self.fixture, self.root, bundle_path)

    def test_bundle_rejects_unknown_fields_wrong_roles_and_path_aliases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            baseline = json.loads(bundle_path.read_text(encoding="utf-8"))

            mutations = []
            top = json.loads(json.dumps(baseline))
            top["unexpected_failure_override"] = {"status": "BLOCKED"}
            mutations.append(("top-level", top))
            candidate = json.loads(json.dumps(baseline))
            candidate["candidate"]["unexpected"] = True
            mutations.append(("candidate", candidate))
            runtime = json.loads(json.dumps(baseline))
            runtime["runtime_verification"]["unexpected"] = True
            mutations.append(("runtime", runtime))
            roles = json.loads(json.dumps(baseline))
            roles["files"][0]["role"] = roles["files"][1]["role"]
            mutations.append(("duplicate-role", roles))
            file_extra = json.loads(json.dumps(baseline))
            file_extra["files"][0]["unexpected"] = True
            mutations.append(("file-extra-field", file_extra))
            mismatch = json.loads(json.dumps(baseline))
            mismatch["files"][0]["role"] = "wrong-role"
            mutations.append(("role-mismatch", mismatch))
            for name, payload in mutations:
                with self.subTest(case=name):
                    _write_json(bundle_path, payload)
                    with self.assertRaises(EvidenceError):
                        _validate_bundle(
                            self.fixture,
                            self.root,
                            bundle_path,
                        )

            alias = json.loads(json.dumps(baseline))
            browser_relative = alias["browser_evidence"][0]["path"]
            browser_path = bundle_path.parent / browser_relative
            browser = json.loads(browser_path.read_text(encoding="utf-8"))
            alias_path = "Candidate-Identity.json"
            browser["candidate"]["snapshot"] = alias_path
            _write_json(browser_path, browser)
            browser_sha = sha256_file(browser_path)
            alias["browser_evidence"][0]["sha256"] = browser_sha
            for item in alias["files"]:
                if item["path"] == browser_relative:
                    item["sha256"] = browser_sha
            alias["files"].append(
                {
                    "path": alias_path,
                    "role": "browser_candidate_snapshot",
                    "sha256": sha256_file(
                        bundle_path.parent / "candidate-identity.json"
                    ),
                }
            )
            _write_json(bundle_path, alias)
            with self.assertRaisesRegex(EvidenceError, "alias|duplicate|identity"):
                _validate_bundle(self.fixture, self.root, bundle_path)

    def test_bundle_rejects_missing_frozen_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            payload = json.loads(bundle_path.read_text(encoding="utf-8"))
            relative = next(iter(payload["frozen_evidence"]))
            (bundle_path.parent / relative).unlink()
            with self.assertRaisesRegex(EvidenceError, "missing"):
                _validate_bundle(self.fixture, self.root, bundle_path)

    def test_bundle_rejects_undeclared_and_protected_files(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            (bundle_path.parent / "unexpected.txt").write_text(
                "unexpected\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                EvidenceError,
                "undeclared files",
            ):
                _validate_bundle(self.fixture, self.root, bundle_path)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = EvidenceFixture(root)
            with tempfile.TemporaryDirectory() as bundle_temp:
                bundle_path = fixture.write_evidence_bundle(
                    Path(bundle_temp) / "bundle"
                )
                secret_path = bundle_path.parent / ".env"
                secret_path.write_text("placeholder\n", encoding="utf-8")
                payload = json.loads(bundle_path.read_text(encoding="utf-8"))
                payload["files"].append(
                    {
                        "path": ".env",
                        "role": "undeclared secret",
                        "sha256": sha256_bytes(secret_path.read_bytes()),
                    }
                )
                _write_json(bundle_path, payload)
                with self.assertRaisesRegex(
                    EvidenceError,
                    "protected",
                ):
                    _validate_bundle(fixture, root, bundle_path)

    def test_all_bundle_entrypoints_reject_manifest_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            resolved_temp = Path(bundle_temp).resolve()
            bundle_path = self.fixture.write_evidence_bundle(
                resolved_temp / "bundle",
                acceptance_policy="content_equivalence_allowed",
            )
            alias = resolved_temp / "manifest-entry-link.json"
            alias.symlink_to(bundle_path)
            self._configure_bundle_metadata(bundle_path)

            with self.assertRaisesRegex(EvidenceError, "symbolic link"):
                build_summary(
                    self.root,
                    self.fixture.metadata_path,
                    replacement_reason="symlink",
                    evidence_bundle_path=alias,
                    acceptance_policy="content_equivalence_allowed",
                )
            output_path = (
                self.root
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / "reproduced-summary.json"
            )
            with self.assertRaisesRegex(EvidenceError, "symbolic link"):
                write_summary(
                    self.root,
                    self.fixture.metadata_path,
                    output_path,
                    replacement_reason="symlink",
                    evidence_bundle_path=alias,
                    acceptance_policy="content_equivalence_allowed",
                )
            self.assertFalse(output_path.exists())

            validate_exit = main(
                [
                    "validate-bundle",
                    "--root",
                    str(self.root),
                    "--bundle",
                    str(alias),
                ]
            )
            self.assertEqual(validate_exit, 2)

            summarize_exit = main(
                [
                    "summarize",
                    "--root",
                    str(self.root),
                    "--run-metadata",
                    str(self.fixture.metadata_path),
                    "--evidence-bundle",
                    str(alias),
                    "--acceptance-policy",
                    "content_equivalence_allowed",
                    "--output",
                    str(output_path),
                    "--replace-reason",
                    "symlink",
                ]
            )
            self.assertEqual(summarize_exit, 2)

            run_output = (
                self.root
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / "symlink-run"
            )
            run_exit = main(
                [
                    "run",
                    "--root",
                    str(self.root),
                    "--output-dir",
                    str(run_output),
                    "--evidence-bundle",
                    str(alias),
                    "--acceptance-policy",
                    "content_equivalence_allowed",
                ]
            )
            self.assertEqual(run_exit, 2)
            run_status = json.loads(
                (run_output / "previous-summary-status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(run_status["evidence_status"], BLOCKED)
            self.assertFalse((run_output / "run-metadata.json").exists())

    def test_parent_replacement_cannot_read_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as root_temp, tempfile.TemporaryDirectory() as outside_temp:
            root = Path(root_temp).resolve()
            outside = Path(outside_temp).resolve()
            parent = root / "safe"
            parent.mkdir()
            inside = parent / "file.txt"
            inside.write_text("inside\n", encoding="utf-8")
            self.assertEqual(
                sha256_file(inside, root=root),
                sha256_bytes(b"inside\n"),
            )
            checked = _evidence_file(root, "safe/file.txt", "probe")
            parent.rename(root / "safe-original")
            parent.symlink_to(outside, target_is_directory=True)
            (outside / "file.txt").write_text("outside\n", encoding="utf-8")
            observed_reads: list[bytes] = []
            real_read = os.read

            def observe_read(file_descriptor: int, size: int) -> bytes:
                data = real_read(file_descriptor, size)
                observed_reads.append(data)
                return data

            with patch(
                "tests.task_control_center_evidence.os.read",
                side_effect=observe_read,
            ), self.assertRaises(EvidenceError):
                sha256_file(checked, root=root)
            self.assertNotIn(b"outside\n", b"".join(observed_reads))

    def test_manifest_replacement_after_root_open_cannot_escape(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_temp,
            tempfile.TemporaryDirectory() as bundle_temp,
            tempfile.TemporaryDirectory() as outside_temp,
        ):
            root = Path(root_temp)
            fixture = EvidenceFixture(root)
            bundle = fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            outside_bundle = Path(outside_temp) / "outside-bundle"
            shutil.copytree(bundle.parent, outside_bundle)
            outside_manifest = outside_bundle / bundle.name
            payload = json.loads(
                outside_manifest.read_text(encoding="utf-8")
            )
            payload["bundle_id"] = "outside-manifest-sentinel"
            _write_json(outside_manifest, payload)

            real_open_directory = _open_absolute_directory
            swapped = False

            def replace_manifest_after_root_open(
                path: Path,
                label: str,
            ) -> int:
                nonlocal swapped
                directory_fd = real_open_directory(path, label)
                if label == "evidence bundle root" and not swapped:
                    bundle.unlink()
                    bundle.symlink_to(outside_manifest)
                    swapped = True
                return directory_fd

            observed_reads: list[bytes] = []
            real_read = os.read

            def observe_read(file_descriptor: int, size: int) -> bytes:
                data = real_read(file_descriptor, size)
                observed_reads.append(data)
                return data

            with patch(
                "tests.task_control_center_evidence._open_absolute_directory",
                side_effect=replace_manifest_after_root_open,
            ), patch(
                "tests.task_control_center_evidence.os.read",
                side_effect=observe_read,
            ), patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                fixture.frozen,
            ), self.assertRaisesRegex(
                EvidenceError,
                "symbolic link|safely",
            ):
                validate_evidence_bundle(root, bundle)
            self.assertTrue(swapped)
            self.assertNotIn(
                b"outside-manifest-sentinel",
                b"".join(observed_reads),
            )

    def test_bundle_root_identity_prevents_real_directory_replacement(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as root_temp,
            tempfile.TemporaryDirectory() as bundle_temp,
            tempfile.TemporaryDirectory() as outside_temp,
        ):
            root = Path(root_temp)
            fixture = EvidenceFixture(root)
            bundle = fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            outside_bundle = Path(outside_temp) / "outside-bundle"
            shutil.copytree(bundle.parent, outside_bundle)
            outside_manifest = outside_bundle / bundle.name
            payload = json.loads(
                outside_manifest.read_text(encoding="utf-8")
            )
            payload["bundle_id"] = "outside-real-root-sentinel"
            _write_json(outside_manifest, payload)

            real_open_directory = _open_absolute_directory
            swapped = False

            def replace_root_after_open(path: Path, label: str) -> int:
                nonlocal swapped
                directory_fd = real_open_directory(path, label)
                if label == "evidence bundle root" and not swapped:
                    original = bundle.parent
                    original.rename(original.with_name("bundle-original"))
                    shutil.copytree(outside_bundle, original)
                    swapped = True
                return directory_fd

            observed_reads: list[bytes] = []
            real_read = os.read

            def observe_read(file_descriptor: int, size: int) -> bytes:
                data = real_read(file_descriptor, size)
                observed_reads.append(data)
                return data

            with patch(
                "tests.task_control_center_evidence._open_absolute_directory",
                side_effect=replace_root_after_open,
            ), patch(
                "tests.task_control_center_evidence.os.read",
                side_effect=observe_read,
            ), patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                fixture.frozen,
            ), self.assertRaisesRegex(
                EvidenceError,
                "identity changed",
            ):
                validate_evidence_bundle(root, bundle)
            self.assertTrue(swapped)
            self.assertNotIn(
                b"outside-real-root-sentinel",
                b"".join(observed_reads),
            )

    def test_nested_validation_cannot_switch_to_external_clear_bundle(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as root_temp,
            tempfile.TemporaryDirectory() as bundle_temp,
            tempfile.TemporaryDirectory() as outside_temp,
        ):
            root = Path(root_temp)
            fixture = EvidenceFixture(root)
            bundle = fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle",
                code_status="CLEAR",
            )
            internal_root = bundle.parent
            external_root = Path(outside_temp) / "external-bundle"
            shutil.copytree(internal_root, external_root)

            def remove_binding(bundle_root: Path) -> dict:
                manifest_path = bundle_root / bundle.name
                payload = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                payload["review_binding"] = None
                binding_paths = [
                    item["path"]
                    for item in payload["files"]
                    if item["role"] == "review_binding"
                ]
                payload["files"] = [
                    item
                    for item in payload["files"]
                    if item["role"] != "review_binding"
                ]
                _write_json(manifest_path, payload)
                for relative in binding_paths:
                    path = bundle_root / relative
                    if path.is_file():
                        path.unlink()
                return payload

            payload = remove_binding(internal_root)
            remove_binding(external_root)

            review_path = internal_root / payload["review_manifest"]["path"]
            review = json.loads(review_path.read_text(encoding="utf-8"))
            code_review = review["code_review"]
            result_path = internal_root / code_review["result"]
            report_path = internal_root / code_review["report"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["status"] = "REQUEST_CHANGES"
            result["conclusion"] = "REQUEST_CHANGES"
            _write_json(result_path, result)
            report_path.write_text(
                report_path.read_text(encoding="utf-8").replace(
                    "Independent review conclusion: CLEAR",
                    "Independent review conclusion: REQUEST_CHANGES",
                ),
                encoding="utf-8",
            )
            code_review["status"] = "REQUEST_CHANGES"
            code_review["conclusion"] = "REQUEST_CHANGES"
            code_review["result_sha256"] = sha256_bytes(
                result_path.read_bytes()
            )
            code_review["report_sha256"] = sha256_bytes(
                report_path.read_bytes()
            )
            _write_json(review_path, review)
            payload["review_manifest"]["sha256"] = sha256_bytes(
                review_path.read_bytes()
            )
            for item in payload["files"]:
                path = internal_root / item["path"]
                if path.is_file():
                    item["sha256"] = sha256_bytes(path.read_bytes())
            _write_json(internal_root / bundle.name, payload)

            real_candidate = validate_candidate_snapshot
            real_review = validate_review_manifest
            swaps = 0
            observed_reads: list[bytes] = []
            external_read = False
            real_read = os.read
            external_identities = {
                (path.stat().st_dev, path.stat().st_ino)
                for path in external_root.rglob("*")
                if path.is_file()
            }

            def observe_read(file_descriptor: int, size: int) -> bytes:
                nonlocal external_read
                data = real_read(file_descriptor, size)
                observed_reads.append(data)
                metadata = os.fstat(file_descriptor)
                if (metadata.st_dev, metadata.st_ino) in external_identities:
                    external_read = True
                return data

            def with_external_root(function):
                def wrapped(*args, **kwargs):
                    nonlocal swaps
                    swaps += 1
                    backup = internal_root.with_name(
                        f"bundle-original-{swaps}"
                    )
                    internal_root.rename(backup)
                    internal_root.symlink_to(
                        external_root,
                        target_is_directory=True,
                    )
                    try:
                        return function(*args, **kwargs)
                    finally:
                        internal_root.unlink()
                        backup.rename(internal_root)

                return wrapped

            with patch(
                "tests.task_control_center_evidence.validate_candidate_snapshot",
                side_effect=with_external_root(real_candidate),
            ), patch(
                "tests.task_control_center_evidence.validate_review_manifest",
                side_effect=with_external_root(real_review),
            ), patch(
                "tests.task_control_center_evidence.os.read",
                side_effect=observe_read,
            ), patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                fixture.frozen,
            ), self.assertRaisesRegex(
                EvidenceError,
                "identity changed|safely|unavailable|current Git state",
            ):
                validate_evidence_bundle(root, bundle)
            self.assertGreaterEqual(swaps, 1)
            self.assertTrue(observed_reads)
            self.assertFalse(external_read)

    def test_bundle_enumeration_uses_pinned_fd_during_root_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            internal_root = base / "bundle"
            (internal_root / "nested").mkdir(parents=True)
            (internal_root / "evidence-bundle.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            (internal_root / "inside.txt").write_text(
                "inside\n",
                encoding="utf-8",
            )
            (internal_root / "nested" / "inside-nested.txt").write_text(
                "inside-nested\n",
                encoding="utf-8",
            )
            external_root = base / "external"
            external_root.mkdir()
            (external_root / "external-name-sentinel.txt").write_text(
                "outside\n",
                encoding="utf-8",
            )
            root_fd = _open_absolute_directory(internal_root, "test root")
            swapped = False
            real_listdir = os.listdir

            def replace_during_enumeration(path_or_fd):
                nonlocal swapped
                if not swapped:
                    internal_root.rename(base / "bundle-original")
                    internal_root.symlink_to(
                        external_root,
                        target_is_directory=True,
                    )
                    swapped = True
                return real_listdir(path_or_fd)

            try:
                with patch(
                    "tests.task_control_center_evidence.os.listdir",
                    side_effect=replace_during_enumeration,
                ):
                    discovered = _enumerate_bundle_files(
                        root_fd,
                        "evidence-bundle.json",
                    )
            finally:
                os.close(root_fd)
            self.assertTrue(swapped)
            self.assertEqual(
                discovered,
                {
                    _canonical_path_identity("inside.txt", "bundle file"),
                    _canonical_path_identity(
                        "nested/inside-nested.txt",
                        "bundle file",
                    ),
                },
            )
            self.assertNotIn(
                _canonical_path_identity(
                    "external-name-sentinel.txt",
                    "bundle file",
                ),
                discovered,
            )

    def test_summary_build_cannot_reopen_replaced_bundle_root(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as bundle_temp,
            tempfile.TemporaryDirectory() as replacement_temp,
        ):
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            replacement = Path(replacement_temp) / "replacement"
            shutil.copytree(bundle_path.parent, replacement)
            self._configure_bundle_metadata(bundle_path)

            real_validate = _validate_evidence_bundle_with_root
            swapped = False
            replacement_identities: set[tuple[int, int]] = set()
            replacement_read = False
            real_read = os.read

            def validate_then_replace(
                root: Path,
                manifest: Path,
                trusted_root,
            ) -> dict:
                nonlocal swapped
                result = real_validate(root, manifest, trusted_root)
                original_root = bundle_path.parent
                original_root.rename(
                    original_root.with_name("bundle-original")
                )
                shutil.copytree(replacement, original_root)
                replacement_identities.update(
                    (path.stat().st_dev, path.stat().st_ino)
                    for path in original_root.rglob("*")
                    if path.is_file()
                )
                swapped = True
                return result

            def observe_read(file_descriptor: int, size: int) -> bytes:
                nonlocal replacement_read
                data = real_read(file_descriptor, size)
                metadata = os.fstat(file_descriptor)
                if (
                    metadata.st_dev,
                    metadata.st_ino,
                ) in replacement_identities:
                    replacement_read = True
                return data

            with patch(
                "tests.task_control_center_evidence._validate_evidence_bundle_with_root",
                side_effect=validate_then_replace,
            ), patch(
                "tests.task_control_center_evidence.os.read",
                side_effect=observe_read,
            ), patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                self.fixture.frozen,
            ), self.assertRaisesRegex(
                EvidenceError,
                "identity changed|safely",
            ):
                build_summary(
                    self.root,
                    self.fixture.metadata_path,
                    replacement_reason="root replacement",
                    evidence_bundle_path=bundle_path,
                    acceptance_policy="content_equivalence_allowed",
                )
            self.assertTrue(swapped)
            self.assertFalse(replacement_read)

    def test_run_evidence_holds_bundle_root_after_validation(self) -> None:
        with (
            tempfile.TemporaryDirectory() as bundle_temp,
            tempfile.TemporaryDirectory() as replacement_temp,
        ):
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            replacement = Path(replacement_temp) / "replacement"
            shutil.copytree(bundle_path.parent, replacement)
            output_dir = (
                self.root
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / "run-root-replacement"
            )
            real_validate = _validate_evidence_bundle_with_root
            swapped = False
            replacement_identities: set[tuple[int, int]] = set()
            replacement_read = False
            real_read = os.read

            def validate_then_replace(
                root: Path,
                manifest: Path,
                trusted_root,
            ) -> dict:
                nonlocal swapped
                result = real_validate(root, manifest, trusted_root)
                original_root = bundle_path.parent
                original_root.rename(
                    original_root.with_name("bundle-original")
                )
                shutil.copytree(replacement, original_root)
                replacement_identities.update(
                    (path.stat().st_dev, path.stat().st_ino)
                    for path in original_root.rglob("*")
                    if path.is_file()
                )
                swapped = True
                return result

            def observe_read(file_descriptor: int, size: int) -> bytes:
                nonlocal replacement_read
                data = real_read(file_descriptor, size)
                metadata = os.fstat(file_descriptor)
                if (
                    metadata.st_dev,
                    metadata.st_ino,
                ) in replacement_identities:
                    replacement_read = True
                return data

            with patch(
                "tests.task_control_center_evidence._validate_evidence_bundle_with_root",
                side_effect=validate_then_replace,
            ), patch(
                "tests.task_control_center_evidence.os.read",
                side_effect=observe_read,
            ), patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                self.fixture.frozen,
            ), self.assertRaisesRegex(
                EvidenceError,
                "identity changed|safely|unavailable",
            ):
                run_evidence(
                    self.root,
                    output_dir,
                    None,
                    evidence_bundle_path=bundle_path,
                    acceptance_policy="content_equivalence_allowed",
                )
            self.assertTrue(swapped)
            self.assertFalse(replacement_read)

    def test_run_evidence_rejects_post_pin_root_symlink(self) -> None:
        with (
            tempfile.TemporaryDirectory() as repo_temp,
            tempfile.TemporaryDirectory() as outside_temp,
        ):
            parent = Path(repo_temp)
            root = parent / "repo"
            root.mkdir()
            fixture = EvidenceFixture(root)
            outside = Path(outside_temp) / "outside"
            shutil.copytree(root, outside)
            output_dir = (
                root
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / "post-pin-symlink"
            )
            real_scope = __import__(
                "tests.task_control_center_evidence",
                fromlist=["_trusted_repository_scope"],
            )._trusted_repository_scope
            swapped = False

            @contextmanager
            def scope_then_swap(repository: Path, label: str = "repository root"):
                nonlocal swapped
                with real_scope(repository, label) as trusted_root:
                    if not swapped:
                        repository.rename(parent / "repo-original")
                        repository.symlink_to(
                            outside,
                            target_is_directory=True,
                        )
                        swapped = True
                    yield trusted_root

            with patch(
                "tests.task_control_center_evidence._trusted_repository_scope",
                side_effect=scope_then_swap,
            ), self.assertRaisesRegex(
                EvidenceError,
                "identity changed|safely|unavailable",
            ):
                run_evidence(
                    root,
                    output_dir,
                    review_manifest_path=(
                        root
                        / "artifacts"
                        / "audits"
                        / "task-control-center-v1"
                        / "independent-review"
                        / "review-manifest.json"
                    ),
                    previous_summary_path=fixture.previous_path,
                )
            self.assertTrue(swapped)
            self.assertFalse(
                (
                    outside
                    / "artifacts"
                    / "audits"
                    / "task-control-center-v1"
                    / "post-pin-symlink"
                ).exists()
            )

    def test_run_evidence_rejects_audit_directory_symlinks(self) -> None:
        output_parent = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
        )
        with tempfile.TemporaryDirectory() as outside_temp:
            outside = Path(outside_temp)
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("outside\n", encoding="utf-8")
            before = {
                path.relative_to(outside).as_posix(): path.read_bytes()
                for path in outside.rglob("*")
                if path.is_file()
            }
            output_parent.rename(output_parent.with_name("audit-original"))
            output_parent.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(
                EvidenceError,
                "symbolic link|safely",
            ):
                run_evidence(
                    self.root,
                    output_parent / "preexisting-symlink",
                    None,
                )
            after = {
                path.relative_to(outside).as_posix(): path.read_bytes()
                for path in outside.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

        audit = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
        )
        with tempfile.TemporaryDirectory() as outside_temp:
            outside = Path(outside_temp)
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("outside\n", encoding="utf-8")
            before = {
                path.relative_to(outside).as_posix(): path.read_bytes()
                for path in outside.rglob("*")
                if path.is_file()
            }
            real_scope = __import__(
                "tests.task_control_center_evidence",
                fromlist=["_trusted_repository_scope"],
            )._trusted_repository_scope
            swapped = False

            @contextmanager
            def scope_then_replace_audit(
                repository: Path,
                label: str = "repository root",
            ):
                nonlocal swapped
                with real_scope(repository, label) as trusted_root:
                    audit.rename(audit.with_name("audit-safe"))
                    audit.symlink_to(outside, target_is_directory=True)
                    swapped = True
                    yield trusted_root

            with patch(
                "tests.task_control_center_evidence._trusted_repository_scope",
                side_effect=scope_then_replace_audit,
            ), self.assertRaisesRegex(
                EvidenceError,
                "symbolic link|safely",
            ):
                run_evidence(
                    self.root,
                    audit / "post-pin-symlink",
                    None,
                )
            self.assertTrue(swapped)
            after = {
                path.relative_to(outside).as_posix(): path.read_bytes()
                for path in outside.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_run_evidence_output_labels_stay_on_fixed_directory(
        self,
    ) -> None:
        labels = (
            "normal",
            "bootstrap input status",
            "previous summary snapshot",
            "candidate identity",
            "system_python result",
            "system_python log",
            "venv_python result",
            "venv_python log",
            "mutation cases",
            "mutation report",
            "run metadata",
        )
        for label in labels:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                parent = Path(temp)
                root = parent / "repo"
                root.mkdir()
                fixture = EvidenceFixture(root)
                outside = parent / "outside"
                outside.mkdir()
                sentinel = outside / "sentinel.txt"
                sentinel.write_text("outside\n", encoding="utf-8")
                output_dir = (
                    root
                    / "artifacts"
                    / "audits"
                    / "task-control-center-v1"
                    / f"output-{label.replace(' ', '-')}"
                )
                real_write = TrustedEvidenceRoot._write_bytes_staging
                swapped = False

                def attack_write(
                    trusted_root: TrustedEvidenceRoot,
                    path: Path,
                    data: bytes,
                    write_label: str,
                ) -> None:
                    nonlocal swapped
                    if (
                        not swapped
                        and label != "normal"
                        and trusted_root.label == "evidence output directory"
                        and write_label == label
                    ):
                        output_dir.rename(
                            output_dir.with_name(output_dir.name + "-detached")
                        )
                        output_dir.symlink_to(
                            outside,
                            target_is_directory=True,
                        )
                        swapped = True
                    real_write(
                        trusted_root,
                        path,
                        data,
                        write_label,
                    )

                def fake_collect(
                    selected_root: Path,
                    selected_output: Path,
                    *,
                    code_before: str,
                    output_root: TrustedEvidenceRoot,
                ):
                    payloads = {
                        "system_python-result.json": b"{}\n",
                        "system_python.log": b"system\n",
                        "venv_python-result.json": b"{}\n",
                        "venv_python.log": b"venv\n",
                        "mutation-cases.json": b'{"cases":[]}\n',
                        "mutations.json": b'{"counts":{}}\n',
                    }
                    write_labels = {
                        "system_python-result.json": "system_python result",
                        "system_python.log": "system_python log",
                        "venv_python-result.json": "venv_python result",
                        "venv_python.log": "venv_python log",
                        "mutation-cases.json": "mutation cases",
                        "mutations.json": "mutation report",
                    }
                    for name, data in payloads.items():
                        output_root.write_bytes_atomic(
                            selected_output / name,
                            data,
                            write_labels[name],
                        )
                    return (
                        {
                            name: {
                                "result": str(
                                    (
                                        selected_output
                                        / f"{name}-result.json"
                                    ).relative_to(selected_root)
                                ),
                                "result_sha256": sha256_bytes(b"{}\n"),
                                "log": str(
                                    (
                                        selected_output / f"{name}.log"
                                    ).relative_to(selected_root)
                                ),
                                "log_sha256": sha256_bytes(b"log\n"),
                            }
                            for name in ("system_python", "venv_python")
                        },
                        {"declared": 0, "selected": 0, "filtered": 0},
                        selected_output / "mutation-cases.json",
                        selected_output / "mutations.json",
                    )

                run_kwargs = {}
                patches = [
                    patch(
                        "tests.task_control_center_evidence."
                        "TrustedEvidenceRoot._write_bytes_staging",
                        autospec=True,
                        side_effect=attack_write,
                    ),
                    patch(
                        "tests.task_control_center_evidence."
                        "_collect_run_evidence_outputs",
                        side_effect=fake_collect,
                    ),
                    patch(
                        "tests.task_control_center_evidence."
                        "validate_review_manifest",
                        return_value={},
                    ),
                    patch(
                        "tests.task_control_center_evidence."
                        "build_candidate_snapshot",
                        return_value={"candidate_mode": "working_tree"},
                    ),
                    patch(
                        "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                        fixture.frozen,
                    ),
                ]
                if label != "bootstrap input status":
                    run_kwargs = {
                        "previous_summary_path": fixture.previous_path,
                        "review_manifest_path": (
                            root
                            / "artifacts"
                            / "audits"
                            / "task-control-center-v1"
                            / "independent-review"
                            / "review-manifest.json"
                        ),
                    }
                contexts = []
                for item in patches:
                    context = item.__enter__()
                    contexts.append((item, context))
                try:
                    if label == "bootstrap input status":
                        with self.assertRaisesRegex(
                            EvidenceError,
                            "previous summary input is required|trust anchor|"
                            "no longer reachable",
                        ):
                            run_evidence(root, output_dir, None)
                    else:
                        run_error: Exception | None = None
                        try:
                            run_evidence(root, output_dir, **run_kwargs)
                        except EvidenceError as exc:
                            run_error = exc
                        if label == "normal":
                            self.assertIsNone(run_error)
                finally:
                    for item, _context in reversed(contexts):
                        item.__exit__(None, None, None)
                self.assertEqual(swapped, label != "normal")
                if label == "normal":
                    self.assertTrue(
                        (output_dir / "run-metadata.json").is_file()
                    )
                self.assertEqual(
                    {
                        path.relative_to(outside).as_posix(): path.read_bytes()
                        for path in outside.rglob("*")
                        if path.is_file()
                    },
                    {"sentinel.txt": b"outside\n"},
                )

    def test_child_output_directory_is_removed_on_failure(self) -> None:
        venv_python = self.root / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.symlink_to(Path(sys.executable))
        output_dir = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "child-cleanup"
        )
        captured_result_path: Path | None = None

        def fail_child(
            selected_root: Path,
            command: list[str],
            label: str,
            *,
            text: bool = False,
        ):
            del selected_root, label, text
            nonlocal captured_result_path
            captured_result_path = Path(
                command[command.index("--result") + 1]
            )
            raise OSError("synthetic child launch failure")

        with _trusted_repository_scope(self.root) as repository_root:
            output_root = repository_root.ensure_directory(
                output_dir,
                "evidence output directory",
            )
            try:
                code_before = code_fingerprint(self.root)
                with patch(
                    "tests.task_control_center_evidence."
                    "_run_subprocess_in_trusted_cwd",
                    side_effect=fail_child,
                ), self.assertRaisesRegex(
                    OSError,
                    "synthetic child launch failure",
                ):
                    _collect_run_evidence_outputs(
                        self.root,
                        output_dir,
                        code_before=code_before,
                        output_root=output_root,
                    )
            finally:
                output_root.close()
        self.assertIsNotNone(captured_result_path)
        self.assertFalse(captured_result_path.parent.exists())

    def test_normal_child_evidence_collection_uses_fixed_output_root(
        self,
    ) -> None:
        venv_python = self.root / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.symlink_to(Path(sys.executable))
        (self.root / "tests" / "mutation_runner.py").write_text(
            "import argparse\n"
            "import json\n"
            "parser = argparse.ArgumentParser()\n"
            'parser.add_argument("--cases")\n'
            'parser.add_argument("--output")\n'
            "args = parser.parse_args()\n"
            "with open(args.output, 'w', encoding='utf-8') as handle:\n"
            "    json.dump({'status': 'PASS', 'counts': {}}, handle)\n",
            encoding="utf-8",
        )
        output_dir = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "normal-child-evidence"
        )
        with _trusted_repository_scope(self.root) as repository_root:
            output_root = repository_root.ensure_directory(
                output_dir,
                "evidence output directory",
            )
            try:
                code_before = code_fingerprint(self.root)
                evidence, filters, cases_path, report_path = (
                    _collect_run_evidence_outputs(
                        self.root,
                        output_dir,
                        code_before=code_before,
                        output_root=output_root,
                    )
                )
            finally:
                output_root.close()
        self.assertEqual(filters["declared"], 3)
        self.assertEqual(set(evidence), {"system_python", "venv_python"})
        self.assertTrue(cases_path.is_file())
        self.assertTrue(report_path.is_file())
        for record in evidence.values():
            self.assertTrue((self.root / record["result"]).is_file())
            self.assertTrue((self.root / record["log"]).is_file())

    def test_repository_root_replacement_cannot_change_summary_acceptance(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as repo_temp,
            tempfile.TemporaryDirectory() as bundle_temp,
        ):
            repository_parent = Path(repo_temp)
            root = repository_parent / "repo"
            root.mkdir()
            fixture = EvidenceFixture(root)
            bundle_path = fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            self._configure_bundle_metadata(bundle_path)
            replacement = repository_parent / "replacement"
            shutil.copytree(root, replacement)
            metadata_path = (
                replacement
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / "fixture-run"
                / "run-metadata.json"
            )
            metadata = json.loads(
                metadata_path.read_text(encoding="utf-8")
            )
            for name, interpreter in (
                ("system_python", "/replacement/system/python"),
                ("venv_python", "/replacement/venv/python"),
            ):
                result_path = replacement / metadata["tests"][name]["result"]
                result = json.loads(
                    result_path.read_text(encoding="utf-8")
                )
                result["python"] = interpreter
                _write_json(result_path, result)
                metadata["tests"][name]["result_sha256"] = sha256_file(
                    result_path
                )
            _write_json(metadata_path, metadata)

            real_selected_bundle_path = _selected_bundle_path
            swapped = False
            replacement_identities: set[tuple[int, int]] = set()
            replacement_read = False
            real_read = os.read

            def select_then_replace(
                selected_root: Path,
                metadata: Path,
                explicit_bundle: Path | None,
            ) -> Path | None:
                nonlocal swapped
                selected = real_selected_bundle_path(
                    selected_root,
                    metadata,
                    explicit_bundle,
                )
                root.rename(repository_parent / "repo-original")
                shutil.copytree(replacement, root)
                replacement_identities.update(
                    (path.stat().st_dev, path.stat().st_ino)
                    for path in root.rglob("*")
                    if path.is_file()
                )
                swapped = True
                return selected

            def observe_read(file_descriptor: int, size: int) -> bytes:
                nonlocal replacement_read
                data = real_read(file_descriptor, size)
                metadata = os.fstat(file_descriptor)
                if (
                    metadata.st_dev,
                    metadata.st_ino,
                ) in replacement_identities:
                    replacement_read = True
                return data

            with patch(
                "tests.task_control_center_evidence._selected_bundle_path",
                side_effect=select_then_replace,
            ), patch(
                "tests.task_control_center_evidence.os.read",
                side_effect=observe_read,
            ), patch(
                "tests.task_control_center_evidence.FROZEN_EVIDENCE",
                fixture.frozen,
            ), self.assertRaisesRegex(
                EvidenceError,
                "identity changed|safely|unavailable",
            ):
                build_summary(
                    root,
                    root
                    / "artifacts"
                    / "audits"
                    / "task-control-center-v1"
                    / "fixture-run"
                    / "run-metadata.json",
                    replacement_reason="repository root replacement",
                    evidence_bundle_path=bundle_path,
                    acceptance_policy="content_equivalence_allowed",
                )
            self.assertTrue(swapped)
            self.assertFalse(replacement_read)

    def test_trusted_root_identity_failure_does_not_leak_fds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            path = base / "root"
            path.mkdir()
            first = TrustedEvidenceRoot(path, "original root")
            before = len(os.listdir("/dev/fd"))
            path.rename(base / "root-original")
            path.mkdir()
            errors = 0
            try:
                with _pin_directory_identity(path, first.assert_active()):
                    for _ in range(20):
                        try:
                            TrustedEvidenceRoot(path, "mismatched root")
                        except EvidenceError as exc:
                            errors += 1
                            self.assertIn("identity changed", str(exc))
                after = len(os.listdir("/dev/fd"))
            finally:
                first.close()
            self.assertEqual(errors, 20)
            self.assertEqual(after, before)
            with TrustedEvidenceRoot(path, "independent root") as independent:
                self.assertGreater(independent.assert_active(), 2)

    def test_candidate_fingerprint_and_git_state_reject_replaced_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            root = parent / "repo"
            root.mkdir()
            EvidenceFixture(root)
            replacement = parent / "replacement"
            shutil.copytree(root, replacement)
            with _trusted_repository_scope(root):
                root.rename(parent / "repo-original")
                shutil.copytree(replacement, root)
                with self.assertRaisesRegex(
                    EvidenceError,
                    "identity changed",
                ):
                    code_fingerprint(root)
                with self.assertRaisesRegex(
                    EvidenceError,
                    "identity changed",
                ):
                    git_state(root)
                with self.assertRaisesRegex(
                    EvidenceError,
                    "identity changed",
                ):
                    build_candidate_snapshot(root)

    def test_subprocess_cwd_is_bound_to_open_directory_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            root = parent / "repo"
            replacement = parent / "replacement"
            detached = parent / "detached"
            root.mkdir()
            replacement.mkdir()
            (root / "value.txt").write_text("ORIGINAL\n", encoding="utf-8")
            (replacement / "value.txt").write_text(
                "REPLACEMENT\n",
                encoding="utf-8",
            )
            process = _popen_in_trusted_cwd(
                root,
                [
                    "/bin/sh",
                    "-c",
                    'printf "READY\\n"; IFS= read -r token; cat value.txt',
                ],
                "swap-and-restore child",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(process.stdout.readline(), "READY\n")
            root.rename(detached)
            replacement.rename(root)
            process.stdin.write("continue\n")
            process.stdin.close()
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            returncode = process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()
            root.rename(replacement)
            detached.rename(root)
            self.assertEqual(returncode, 0, stderr)
            self.assertEqual(stdout, "ORIGINAL\n")

    def test_code_fingerprint_swap_and_restore_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            root = parent / "repo"
            root.mkdir()
            EvidenceFixture(root)
            replacement = parent / "replacement"
            detached = parent / "detached"
            shutil.copytree(root, replacement)
            (replacement / "src" / "zevorinth" / "example.py").write_text(
                "VALUE = 999\n",
                encoding="utf-8",
            )
            real_git = __import__(
                "tests.task_control_center_evidence",
                fromlist=["_run_git_process"],
            )._run_git_process
            swapped = False

            def swap_then_git(root_path: Path, arguments: list[str]):
                nonlocal swapped
                if not swapped:
                    root.rename(detached)
                    replacement.rename(root)
                    swapped = True
                    try:
                        return real_git(root, arguments)
                    finally:
                        root.rename(replacement)
                        detached.rename(root)
                return real_git(root, arguments)

            with patch(
                "tests.task_control_center_evidence._run_git_process",
                side_effect=swap_then_git,
            ), self.assertRaisesRegex(
                EvidenceError,
                "identity changed|safely",
            ):
                code_fingerprint(root)
            self.assertTrue(swapped)

    def test_review_manifest_rejects_repository_symlink_aliases(self) -> None:
        manifest_path = self.fixture.complete_reviews()
        alias = self.root.parent / f"{self.root.name}-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        try:
            original = json.loads(manifest_path.read_text(encoding="utf-8"))
            alias_payload = json.loads(json.dumps(original))
            alias_payload["repository"] = str(alias)
            _write_json(manifest_path, alias_payload)
            with self.assertRaisesRegex(
                EvidenceError,
                "exact canonical repository path",
            ):
                validate_review_manifest(
                    self.root,
                    manifest_path,
                    expected_code_fingerprint=code_fingerprint(self.root),
                )

            lane_payload = json.loads(json.dumps(original))
            lane_payload["code_review"]["target_repository"] = str(alias)
            _write_json(manifest_path, lane_payload)
            result_path = self.root / lane_payload["code_review"]["result"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["target_repository"] = str(alias)
            _write_json(result_path, result)
            lane_payload["code_review"]["result_sha256"] = sha256_file(
                result_path
            )
            _write_json(manifest_path, lane_payload)
            with self.assertRaisesRegex(
                EvidenceError,
                "exact canonical repository path",
            ):
                validate_review_manifest(
                    self.root,
                    manifest_path,
                    expected_code_fingerprint=code_fingerprint(self.root),
                )
        finally:
            alias.unlink()

    def test_review_manifest_rejects_root_replacement_after_pin(self) -> None:
        manifest_path = self.fixture.complete_reviews()
        with (
            tempfile.TemporaryDirectory() as replacement_temp,
            tempfile.TemporaryDirectory() as original_temp,
        ):
            replacement = Path(replacement_temp) / "replacement"
            shutil.copytree(self.root, replacement)
            real_git_state = git_state
            swapped = False

            def state_then_replace(root: Path) -> dict:
                nonlocal swapped
                state = real_git_state(root)
                self.root.rename(Path(original_temp) / "original-repo")
                shutil.copytree(replacement, self.root)
                swapped = True
                return state

            with patch(
                "tests.task_control_center_evidence.git_state",
                side_effect=state_then_replace,
            ), self.assertRaisesRegex(
                EvidenceError,
                "identity changed|safely",
            ):
                validate_review_manifest(
                    self.root,
                    manifest_path,
                    expected_code_fingerprint=code_fingerprint(self.root),
                )
            self.assertTrue(swapped)

    def test_trusted_root_closed_after_scope_and_nested_api_rejects_replace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "inside.txt").write_text("inside\n", encoding="utf-8")
            trusted = TrustedEvidenceRoot(root, "test evidence root")
            trusted.close()
            with self.assertRaisesRegex(EvidenceError, "closed"):
                trusted.assert_active()

        with (
            tempfile.TemporaryDirectory() as root_temp,
            tempfile.TemporaryDirectory() as bundle_temp,
        ):
            root = Path(root_temp)
            fixture = EvidenceFixture(root)
            bundle = fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            payload = json.loads(bundle.read_text(encoding="utf-8"))
            candidate = json.loads(
                (bundle.parent / "candidate-identity.json").read_text(
                    encoding="utf-8"
                )
            )
            review_relative = payload["review_manifest"]["path"]
            real_protected = _protected_evidence_root
            swapped = False

            def replace_after_protected(path: Path, label: str) -> Path:
                nonlocal swapped
                canonical = real_protected(path, label)
                if not swapped:
                    original = bundle.parent
                    original.rename(original.with_name("bundle-original"))
                    original.mkdir()
                    swapped = True
                return canonical

            with patch(
                "tests.task_control_center_evidence._protected_evidence_root",
                side_effect=replace_after_protected,
            ), self.assertRaisesRegex(
                EvidenceError,
                "identity changed|missing|safely",
            ):
                validate_review_manifest(
                    root,
                    bundle.parent / review_relative,
                    expected_code_fingerprint=candidate["code_fingerprint"],
                    expected_head=candidate["reviewed_head"],
                    changed_candidate_paths=[
                        item["path"] for item in candidate["files"]
                    ],
                    evidence_root=bundle.parent,
                )
            self.assertTrue(swapped)

    def test_public_nested_apis_hold_root_for_full_call(self) -> None:
        for case in ("candidate", "review", "binding", "browser"):
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as root_temp,
                tempfile.TemporaryDirectory() as bundle_temp,
            ):
                root = Path(root_temp)
                fixture = EvidenceFixture(root)
                bundle = fixture.write_evidence_bundle(
                    Path(bundle_temp) / "bundle"
                )
                payload = json.loads(bundle.read_text(encoding="utf-8"))
                candidate = json.loads(
                    (bundle.parent / "candidate-identity.json").read_text(
                        encoding="utf-8"
                    )
                )
                real_protected = _protected_evidence_root
                swapped = False

                def replace_after_protected(path: Path, label: str) -> Path:
                    nonlocal swapped
                    canonical = real_protected(path, label)
                    if not swapped:
                        original = bundle.parent
                        original.rename(original.with_name("bundle-original"))
                        original.mkdir()
                        swapped = True
                    return canonical

                with patch(
                    "tests.task_control_center_evidence._protected_evidence_root",
                    side_effect=replace_after_protected,
                ), self.assertRaisesRegex(
                    EvidenceError,
                    "identity changed|missing|safely",
                ):
                    if case == "candidate":
                        validate_candidate_snapshot(
                            root,
                            bundle.parent
                            / payload["candidate"]["snapshot"]["path"],
                            evidence_root=bundle.parent,
                        )
                    elif case == "review":
                        validate_review_manifest(
                            root,
                            bundle.parent
                            / payload["review_manifest"]["path"],
                            expected_code_fingerprint=candidate[
                                "code_fingerprint"
                            ],
                            expected_head=candidate["reviewed_head"],
                            changed_candidate_paths=[
                                item["path"] for item in candidate["files"]
                            ],
                            evidence_root=bundle.parent,
                        )
                    elif case == "binding":
                        validate_review_binding(
                            root,
                            bundle.parent
                            / payload["review_binding"]["path"],
                            evidence_root=bundle.parent,
                        )
                    else:
                        validate_browser_evidence(
                            root,
                            bundle.parent
                            / payload["browser_evidence"][0]["path"],
                            candidate,
                            evidence_root=bundle.parent,
                        )
                self.assertTrue(swapped)

    def test_root_ancestor_and_root_replacement_cannot_escape(self) -> None:
        for case in ("ancestor", "root"):
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as temp,
            ):
                base = Path(temp)
                trusted_parent = base / "trusted-parent"
                root = trusted_parent / "root"
                root.mkdir(parents=True)
                evidence = root / "evidence.bin"
                evidence.write_bytes(b"inside\n")
                self.assertEqual(
                    sha256_file(evidence, root=root),
                    sha256_bytes(b"inside\n"),
                )

                outside_parent = base / "outside-parent"
                outside_root = outside_parent / "root"
                outside_root.mkdir(parents=True)
                (outside_root / "evidence.bin").write_bytes(
                    b"outside-sentinel\n"
                )
                if case == "ancestor":
                    trusted_parent.rename(base / "trusted-parent-original")
                    trusted_parent.symlink_to(
                        outside_parent,
                        target_is_directory=True,
                    )
                else:
                    root.rename(base / "root-original")
                    root.symlink_to(
                        outside_root,
                        target_is_directory=True,
                    )

                observed_reads: list[bytes] = []
                real_read = os.read

                def observe_read(
                    file_descriptor: int,
                    size: int,
                ) -> bytes:
                    data = real_read(file_descriptor, size)
                    observed_reads.append(data)
                    return data

                with patch(
                    "tests.task_control_center_evidence.os.read",
                    side_effect=observe_read,
                ), self.assertRaisesRegex(
                    EvidenceError,
                    "symbolic link|safely",
                ):
                    sha256_file(evidence, root=root)
                self.assertNotIn(
                    b"outside-sentinel\n",
                    b"".join(observed_reads),
                )

    def test_failed_browser_results_cannot_be_declared_valid(self) -> None:
        cases = {
            "all_failed": {
                "active_lease": {"ui_verified": False},
                "expired_lease": {
                    "ui_verified": False,
                    "forged_post_http_status": 200,
                },
                "released_lease": {"ui_verified": False},
                "preview_reads": {
                    "task_event_artifact_state_unchanged": False,
                },
                "mobile_layout": {"horizontal_overflow": True},
            },
            "one_required_failed": {
                "active_lease": {"ui_verified": False},
                "expired_lease": {"ui_verified": True},
                "released_lease": {"ui_verified": True},
                "preview_reads": {
                    "task_event_artifact_state_unchanged": True,
                },
                "mobile_layout": {"horizontal_overflow": False},
            },
            "required_scene_missing": {
                "active_lease": {"ui_verified": True},
                "expired_lease": {"ui_verified": True},
                "preview_reads": {
                    "task_event_artifact_state_unchanged": True,
                },
                "mobile_layout": {"horizontal_overflow": False},
            },
            "ambiguous_result_scheme": {
                "active_lease": {
                    "lease_state": "active",
                    "ui_verified": True,
                    "actions": [],
                    "reason": "task_claim_active",
                },
                "expired_lease": {
                    "lease_state": "expired",
                    "ui_verified": True,
                    "actions": [],
                    "reason": "task_claim_recovery_required",
                    "forged_post_http_status": 409,
                    "forged_post_error": "task_claim_recovery_required",
                    "tool_started": 0,
                    "task_status_after": "queued",
                },
                "released_lease": {
                    "lease_state": "none",
                    "ui_verified": True,
                    "actions": [
                        "revise_unstarted_task",
                        "add_task_note",
                    ],
                    "start_visible": False,
                    "cancel_visible": False,
                },
                "preview_reads": {
                    "task_event_artifact_state_unchanged": True,
                    "database_sha256_before": "a" * 64,
                    "database_sha256_after": "a" * 64,
                },
                "mobile_layout": {
                    "viewport_width": 375,
                    "horizontal_overflow": False,
                },
                "initial_browser_outcome": {
                    "request_id_retained": True,
                    "manual_query_visible": True,
                    "manual_abandon_visible": True,
                    "task_reloaded_from_authoritative_state": True,
                },
                "query_after_response_loss": {
                    "state": "failed",
                    "same_request_id": False,
                    "tool_started_events": 2,
                    "duplicate_execution_observed": True,
                },
            },
        }
        for name, results in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                with tempfile.TemporaryDirectory() as bundle_temp:
                    bundle_path = fixture.write_evidence_bundle(
                        Path(bundle_temp) / "bundle"
                    )
                    payload = json.loads(
                        bundle_path.read_text(encoding="utf-8")
                    )
                    browser_path = (
                        bundle_path.parent
                        / payload["browser_evidence"][0]["path"]
                    )
                    browser = json.loads(
                        browser_path.read_text(encoding="utf-8")
                    )
                    browser["results"] = results
                    _write_json(browser_path, browser)
                    browser_sha = sha256_file(browser_path)
                    payload["browser_evidence"][0]["sha256"] = browser_sha
                    for item in payload["files"]:
                        if item["path"] == payload["browser_evidence"][0]["path"]:
                            item["sha256"] = browser_sha
                    _write_json(bundle_path, payload)
                    with self.assertRaises(EvidenceError):
                        _validate_bundle(fixture, root, bundle_path)

    def test_browser_console_errors_and_declared_status_conflicts_fail(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            self._rewrite_bundle_browser(
                bundle_path,
                console_errors=["console failure"],
            )
            with self.assertRaisesRegex(EvidenceError, "console|browser"):
                _validate_bundle(
                    self.fixture,
                    self.root,
                    bundle_path,
                )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = EvidenceFixture(root)
            with tempfile.TemporaryDirectory() as bundle_temp:
                bundle_path = fixture.write_evidence_bundle(
                    Path(bundle_temp) / "bundle"
                )
                payload = json.loads(
                    bundle_path.read_text(encoding="utf-8")
                )
                payload["browser_evidence"][0]["status"] = "NOT_VERIFIED"
                payload["browser_evidence"][0]["reason"] = "not verified"
                _write_json(bundle_path, payload)
                with self.assertRaisesRegex(
                    EvidenceError,
                    "runtime VERIFIED",
                ):
                    _validate_bundle(fixture, root, bundle_path)

    def test_runtime_verified_requires_every_declared_browser_valid(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            self._append_bundle_browser(bundle_path)
            validated = _validate_bundle(
                self.fixture,
                self.root,
                bundle_path,
            )
            self.assertEqual(
                validated["runtime_verification"]["valid_evidence_count"],
                2,
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = EvidenceFixture(root)
            with tempfile.TemporaryDirectory() as bundle_temp:
                bundle_path = fixture.write_evidence_bundle(
                    Path(bundle_temp) / "bundle"
                )
                self._append_bundle_browser(
                    bundle_path,
                    status="NOT_VERIFIED",
                )
                with self.assertRaisesRegex(
                    EvidenceError,
                    "every declared browser evidence",
                ):
                    _validate_bundle(fixture, root, bundle_path)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = EvidenceFixture(root)
            with tempfile.TemporaryDirectory() as bundle_temp:
                bundle_path = fixture.write_evidence_bundle(
                    Path(bundle_temp) / "bundle"
                )

                def invalidate(browser: dict) -> None:
                    browser["results"]["active_lease"][
                        "ui_verified"
                    ] = False

                self._append_bundle_browser(
                    bundle_path,
                    mutate_browser=invalidate,
                )
                with self.assertRaisesRegex(
                    EvidenceError,
                    "declaration conflicts",
                ):
                    _validate_bundle(fixture, root, bundle_path)

    def test_runtime_verified_rejects_empty_undeclared_and_duplicate_browser(
        self,
    ) -> None:
        for case in ("empty", "undeclared", "duplicate"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                with tempfile.TemporaryDirectory() as bundle_temp:
                    bundle_path = fixture.write_evidence_bundle(
                        Path(bundle_temp) / "bundle"
                    )
                    payload = json.loads(
                        bundle_path.read_text(encoding="utf-8")
                    )
                    if case == "empty":
                        payload["browser_evidence"] = []
                        _write_json(bundle_path, payload)
                    elif case == "undeclared":
                        self._append_bundle_browser(
                            bundle_path,
                            declare=False,
                        )
                    else:
                        payload["browser_evidence"].append(
                            dict(payload["browser_evidence"][0])
                        )
                        _write_json(bundle_path, payload)
                    with self.assertRaises(EvidenceError):
                        _validate_bundle(fixture, root, bundle_path)

    def test_runtime_verified_rejects_all_declared_browser_invalid(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            payload = json.loads(bundle_path.read_text(encoding="utf-8"))
            payload["browser_evidence"][0]["status"] = "NOT_VERIFIED"
            payload["browser_evidence"][0]["reason"] = (
                "synthetic unavailable browser evidence"
            )
            _write_json(bundle_path, payload)
            self._append_bundle_browser(
                bundle_path,
                status="NOT_VERIFIED",
            )
            with self.assertRaisesRegex(
                EvidenceError,
                "every declared browser evidence",
            ):
                _validate_bundle(self.fixture, self.root, bundle_path)

    def test_browser_schema_is_required_and_strict(self) -> None:
        mutations = {
            "missing_schema": lambda payload: payload.pop("schema_version"),
            "unknown_schema": lambda payload: payload.__setitem__(
                "schema_version",
                "unknown-browser-schema",
            ),
            "unknown_top_field": lambda payload: payload.__setitem__(
                "override_status",
                "VERIFIED",
            ),
            "unknown_candidate_field": lambda payload: payload[
                "candidate"
            ].__setitem__("unexpected", True),
            "unknown_scene_field": lambda payload: payload["results"][
                "active_lease"
            ].__setitem__("unexpected", True),
        }
        for name, mutator in mutations.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                with tempfile.TemporaryDirectory() as bundle_temp:
                    bundle_path = fixture.write_evidence_bundle(
                        Path(bundle_temp) / "bundle"
                    )
                    self._rewrite_bundle_browser(
                        bundle_path,
                        mutator=mutator,
                    )
                    with self.assertRaises(EvidenceError):
                        _validate_bundle(
                            fixture,
                            root,
                            bundle_path,
                        )

    def test_browser_counts_require_exact_integers(self) -> None:
        lease_results = {
            "active_lease": {
                "lease_state": "active",
                "ui_verified": True,
                "actions": [],
                "reason": "task_claim_active",
            },
            "expired_lease": {
                "lease_state": "expired",
                "ui_verified": True,
                "actions": [],
                "reason": "task_claim_recovery_required",
                "forged_post_http_status": 409,
                "forged_post_error": "task_claim_recovery_required",
                "tool_started": 0,
                "task_status_after": "queued",
            },
            "released_lease": {
                "lease_state": "none",
                "ui_verified": True,
                "actions": [
                    "revise_unstarted_task",
                    "add_task_note",
                ],
                "start_visible": False,
                "cancel_visible": False,
            },
            "preview_reads": {
                "task_event_artifact_state_unchanged": True,
                "database_sha256_before": "a" * 64,
                "database_sha256_after": "a" * 64,
            },
            "mobile_layout": {
                "viewport_width": 375,
                "horizontal_overflow": False,
            },
        }
        bad_values = (True, False, 0.0, "0", -1, None)
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            for value in bad_values:
                with self.subTest(value=value):
                    mutated = json.loads(json.dumps(lease_results))
                    if value is None:
                        mutated["expired_lease"].pop("tool_started")
                    else:
                        mutated["expired_lease"]["tool_started"] = value
                    self._rewrite_bundle_browser(
                        bundle_path,
                        results=mutated,
                    )
                    with self.assertRaises(EvidenceError):
                        _validate_bundle(
                            self.fixture,
                            self.root,
                            bundle_path,
                        )

            response_results = {
                "initial_browser_outcome": {
                    "request_id_retained": True,
                    "manual_query_visible": True,
                    "manual_abandon_visible": True,
                    "task_reloaded_from_authoritative_state": True,
                },
                "query_after_response_loss": {
                    "state": "completed",
                    "same_request_id": True,
                    "tool_started_events": True,
                    "duplicate_execution_observed": False,
                },
            }
            self._rewrite_bundle_browser(
                bundle_path,
                results=response_results,
            )
            with self.assertRaises(EvidenceError):
                _validate_bundle(
                    self.fixture,
                    self.root,
                    bundle_path,
                )

    def test_bundle_old_browser_or_unknown_runtime_cannot_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            payload = json.loads(bundle_path.read_text(encoding="utf-8"))
            browser_relative = payload["browser_evidence"][0]["path"]
            browser_path = bundle_path.parent / browser_relative
            browser = json.loads(browser_path.read_text(encoding="utf-8"))
            browser["candidate"]["code_fingerprint"] = (
                "e044ebaec9164905f82370761a3a9406439202b882c4399494afe0abb7b923af"
            )
            _write_json(browser_path, browser)
            browser_sha = sha256_file(browser_path)
            payload["browser_evidence"][0]["sha256"] = browser_sha
            for item in payload["files"]:
                if item["path"] == browser_relative:
                    item["sha256"] = browser_sha
            _write_json(bundle_path, payload)
            with self.assertRaisesRegex(
                EvidenceError,
                "code fingerprint does not match candidate",
            ):
                _validate_bundle(self.fixture, self.root, bundle_path)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = EvidenceFixture(root)
            with tempfile.TemporaryDirectory() as bundle_temp:
                bundle_path = fixture.write_evidence_bundle(
                    Path(bundle_temp) / "bundle"
                )
                payload = json.loads(bundle_path.read_text(encoding="utf-8"))
                payload["runtime_verification"]["status"] = "UNKNOWN"
                _write_json(bundle_path, payload)
                with self.assertRaisesRegex(
                    EvidenceError,
                    "unsupported status",
                ):
                    _validate_bundle(fixture, root, bundle_path)
                payload["runtime_verification"]["status"] = "VERIFIED"
                payload["runtime_verification"]["scopes"] = ["container"]
                _write_json(bundle_path, payload)
                with self.assertRaisesRegex(
                    EvidenceError,
                    "unsupported scopes",
                ):
                    _validate_bundle(fixture, root, bundle_path)

    def test_bundle_review_changes_and_missing_runtime_stay_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = EvidenceFixture(root)
            with tempfile.TemporaryDirectory() as bundle_temp:
                bundle_path = fixture.write_evidence_bundle(
                    Path(bundle_temp) / "bundle",
                    code_status="REQUEST_CHANGES",
                )
                bundle = _validate_bundle(fixture, root, bundle_path)
                with TrustedEvidenceRoot(
                    root,
                    "bundle review changes repository",
                ) as trusted_root:
                    gate = self._evaluate_bundle_gate(
                        bundle,
                        repository_root=trusted_root,
                    )
                self.assertEqual(gate["historical_review"], BLOCKED)
                self.assertEqual(gate["acceptance_status"], BLOCKED)
                self.assertFalse(gate["merge_ready"])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = EvidenceFixture(root)
            with tempfile.TemporaryDirectory() as bundle_temp:
                bundle_path = fixture.write_evidence_bundle(
                    Path(bundle_temp) / "bundle",
                    runtime_status="NOT_VERIFIED",
                )
                bundle = _validate_bundle(fixture, root, bundle_path)
                with TrustedEvidenceRoot(
                    root,
                    "bundle runtime repository",
                ) as trusted_root:
                    gate = self._evaluate_bundle_gate(
                        bundle,
                        repository_root=trusted_root,
                    )
                self.assertEqual(gate["acceptance_status"], NOT_VERIFIED)
                self.assertFalse(gate["merge_ready"])

    def test_committed_range_rejects_wrong_current_head(self) -> None:
        self.fixture.complete_reviews()
        base, _target = self.fixture.commit_candidate()
        with self.assertRaisesRegex(
            EvidenceError,
            "target HEAD does not match",
        ):
            build_candidate_snapshot(
                self.root,
                candidate_mode="committed_range",
                base_head=base,
                target_head=base,
            )

    def test_working_tree_candidate_rejects_deletions_and_renames(self) -> None:
        for case in ("delete_test", "delete_product", "rename"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                EvidenceFixture(root)
                if case == "delete_test":
                    (root / "tests" / "test_example.py").unlink()
                elif case == "delete_product":
                    (root / "src" / "zevorinth" / "example.py").unlink()
                else:
                    (root / "tests" / "test_example.py").rename(
                        root / "tests" / "renamed_example.py"
                    )
                with self.assertRaisesRegex(
                    EvidenceError,
                    "delete|rename|type change|unsupported",
                ):
                    build_candidate_snapshot(root)

    def test_working_tree_candidate_rejects_file_and_parent_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            root = Path(temp)
            EvidenceFixture(root)
            external = Path(outside) / "secret.py"
            external.write_text("SECRET = True\n", encoding="utf-8")
            (root / "src" / "zevorinth" / "leaked.py").symlink_to(external)
            with self.assertRaisesRegex(EvidenceError, "symbolic link"):
                build_candidate_snapshot(root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            EvidenceFixture(root)
            test_path = root / "tests" / "test_example.py"
            test_path.unlink()
            test_path.symlink_to(root / "src" / "zevorinth" / "example.py")
            with self.assertRaisesRegex(
                EvidenceError,
                "type|symbolic link",
            ):
                build_candidate_snapshot(root)

        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            root = Path(temp)
            EvidenceFixture(root)
            external_dir = Path(outside) / "package"
            external_dir.mkdir()
            (external_dir / "module.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            (root / "src" / "zevorinth" / "linked").symlink_to(
                external_dir,
                target_is_directory=True,
            )
            with self.assertRaisesRegex(EvidenceError, "symbolic link"):
                build_candidate_snapshot(root)

    def test_bootstrap_identity_is_explicitly_incomplete(self) -> None:
        self.fixture.complete_reviews()
        self.fixture.commit_candidate()
        snapshot = build_candidate_snapshot(
            self.root,
            candidate_mode="bootstrap",
        )
        self.assertEqual(snapshot["candidate_mode"], "bootstrap")
        self.assertEqual(snapshot["file_count"], 0)
        self.assertEqual(
            snapshot["evidence_status"],
            BOOTSTRAP_INCOMPLETE,
        )
        self.assertEqual(
            snapshot["file_manifest_sha256"],
            file_manifest_sha256([]),
        )

    def test_committed_range_rejects_non_ancestor_base(self) -> None:
        self.fixture.complete_reviews()
        _base, target = self.fixture.commit_candidate()
        tree = subprocess.run(
            ["git", "rev-parse", f"{target}^{{tree}}"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        side = subprocess.run(
            [
                "git",
                "commit-tree",
                tree,
                "-p",
                target,
                "-m",
                "non-ancestor test commit",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with self.assertRaisesRegex(
            EvidenceError,
            "not an ancestor",
        ):
            build_candidate_snapshot(
                self.root,
                candidate_mode="committed_range",
                base_head=side,
                target_head=target,
            )

    def test_review_binding_rejects_extra_or_missing_files(self) -> None:
        with self.subTest(case="extra"):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                base, target = fixture.commit_candidate()
                (root / "extra.bin").write_bytes(b"extra\n")
                subprocess.run(
                    ["git", "add", "--", "extra.bin"],
                    cwd=root,
                    check=True,
                )
                subprocess.run(
                    ["git", "commit", "-qm", "extra candidate file"],
                    cwd=root,
                    check=True,
                )
                review = fixture.complete_reviews()
                review_payload = json.loads(review.read_text(encoding="utf-8"))
                review_payload["head"] = git_state(root)["head"]
                review_payload["code_fingerprint"] = code_fingerprint(root)
                _write_json(review, review_payload)
                with self.assertRaisesRegex(
                    EvidenceError,
                    "omitted changed files",
                ):
                    build_review_binding(
                        root,
                        review,
                        base_head=base,
                        target_head=git_state(root)["head"],
                        generated_at="2026-10-25T00:00:00Z",
                    )

        with self.subTest(case="missing"):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                review = fixture.complete_reviews()
                base, target = fixture.commit_candidate()
                payload = json.loads(review.read_text(encoding="utf-8"))
                reduced = payload["code_review"]["reviewed_files"][:-1]
                fixture.replace_reviewed_files("code_review", reduced)
                with self.assertRaisesRegex(
                    EvidenceError,
                    "omitted changed files",
                ):
                    build_review_binding(
                        root,
                        review,
                        base_head=base,
                        target_head=target,
                        generated_at="2026-10-25T00:00:00Z",
                    )

    def test_review_binding_rejects_path_or_hash_changes(self) -> None:
        for case in ("path", "sha256"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                review = fixture.complete_reviews()
                base, target = fixture.commit_candidate()
                payload = json.loads(review.read_text(encoding="utf-8"))
                changed = json.loads(json.dumps(
                    payload["code_review"]["reviewed_files"]
                ))
                if case == "path":
                    changed[0]["path"] = "tests/missing-reviewed-file.py"
                else:
                    changed[0]["sha256"] = "0" * 64
                fixture.replace_reviewed_files("code_review", changed)
                with self.assertRaises(EvidenceError) as caught:
                    build_review_binding(
                        root,
                        review,
                        base_head=base,
                        target_head=target,
                        generated_at="2026-10-25T00:00:00Z",
                    )
                self.assertIn(
                    "missing" if case == "path" else "SHA mismatch",
                    str(caught.exception),
                )

    def test_review_binding_rejects_missing_reports_and_results(self) -> None:
        for field in ("report", "result"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = EvidenceFixture(root)
                review = fixture.complete_reviews()
                base, target = fixture.commit_candidate()
                payload = json.loads(review.read_text(encoding="utf-8"))
                (root / payload["code_review"][field]).unlink()
                with self.assertRaisesRegex(
                    EvidenceError,
                    "missing",
                ):
                    build_review_binding(
                        root,
                        review,
                        base_head=base,
                        target_head=target,
                        generated_at="2026-10-25T00:00:00Z",
                    )

    def test_review_binding_rejects_conflicting_conclusions(self) -> None:
        review = self.fixture.complete_reviews()
        base, target = self.fixture.commit_candidate()
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
        with self.assertRaisesRegex(
            EvidenceError,
            "conclusion disagrees",
        ):
            build_review_binding(
                self.root,
                review,
                base_head=base,
                target_head=target,
                generated_at="2026-10-25T00:00:00Z",
            )

    def test_zero_file_candidate_is_rejected(self) -> None:
        self.fixture.complete_reviews()
        self.fixture.commit_candidate()
        with self.assertRaisesRegex(
            EvidenceError,
            "contains no files",
        ):
            build_candidate_snapshot(
                self.root,
                candidate_mode="working_tree",
            )

    def test_non_clear_review_is_not_upgraded_by_binding(self) -> None:
        review = self.fixture.complete_reviews(
            code_status="REQUEST_CHANGES",
            architecture_status="CLEAR",
        )
        base, target = self.fixture.commit_candidate()
        binding = build_review_binding(
            self.root,
            review,
            base_head=base,
            target_head=target,
            generated_at="2026-10-25T00:00:00Z",
        )
        self.assertEqual(
            binding["review_lanes"]["code_review"]["status"],
            "REQUEST_CHANGES",
        )
        self.assertFalse(binding["review_status"]["reviews_clear"])
        self.assertEqual(
            binding["binding_result"],
            "CONTENT_EQUIVALENT_REVIEW_BLOCKED",
        )
        self.assertFalse(binding["acceptance_supported"])
        self.assertFalse(binding["merge_ready"])

    def test_missing_previous_summary_is_structured_and_not_merge_ready(
        self,
    ) -> None:
        output = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "bootstrap-input-test"
        )
        with self.assertRaisesRegex(
            EvidenceError,
                            "previous summary input is required|trust anchor|"
                            "no longer reachable",
        ):
            run_evidence(
                self.root,
                output,
                self.fixture.output_path.parent / "review.json",
            )
        status = json.loads(
            (output / "previous-summary-status.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(status["candidate_mode"], "bootstrap")
        self.assertEqual(status["evidence_status"], BOOTSTRAP_INCOMPLETE)
        self.assertFalse(status["acceptance_supported"])
        self.assertFalse(status["merge_ready"])
        self.assertFalse((output / "run-metadata.json").exists())

    def test_missing_bundle_and_review_inputs_are_structured(self) -> None:
        output = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "bootstrap-all-inputs-test"
        )
        with self.assertRaisesRegex(
            EvidenceError,
            "previous summary input is required",
        ):
            run_evidence(self.root, output, None)
        status = json.loads(
            (output / "previous-summary-status.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(status["evidence_status"], BOOTSTRAP_INCOMPLETE)
        self.assertIn("previous_summary", status["missing_inputs"])
        self.assertIn(
            "review_manifest_or_evidence_bundle",
            status["missing_inputs"],
        )
        self.assertFalse(status["acceptance_supported"])
        self.assertFalse(status["merge_ready"])

    def test_missing_external_bundle_is_not_available(self) -> None:
        output = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "bootstrap-missing-bundle"
        )
        missing_bundle = self.root / "artifacts" / "missing-bundle.json"
        with self.assertRaisesRegex(
            EvidenceError,
            "input is unavailable or invalid",
        ):
            run_evidence(
                self.root,
                output,
                None,
                evidence_bundle_path=missing_bundle,
            )
        status = json.loads(
            (output / "previous-summary-status.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(status["evidence_status"], NOT_AVAILABLE)
        self.assertIn("valid_evidence_bundle", status["missing_inputs"])
        self.assertFalse(status["merge_ready"])

    def test_candidate_paths_reject_protected_and_escaping_values(self) -> None:
        for relative in (
            ".idea/workspace.xml",
            ".IDEA/workspace.xml",
            ".idea\\workspace.xml",
            ".env",
            ".env.local",
            ".ENV",
            ".Env.local",
            "../outside.py",
            "..\\..\\outside.py",
            "/tmp/outside.py",
        ):
            with self.subTest(relative=relative):
                with self.assertRaises(EvidenceError) as caught:
                    _validate_relative_candidate_path(relative, "candidate")
                if "\\" in relative:
                    expected_message = "forward slashes"
                elif relative.startswith(("../", "/")):
                    expected_message = "escapes"
                else:
                    expected_message = "protected"
                self.assertIn(
                    expected_message,
                    str(caught.exception),
                )

    def test_candidate_path_identity_normalizes_unicode_and_case(self) -> None:
        nfd = "cafe\u0301.py"
        nfc = "caf\u00e9.py"
        self.assertEqual(
            _validate_relative_candidate_path(nfd, "candidate"),
            nfc,
        )
        self.assertEqual(
            _canonical_path_identity(nfd, "candidate"),
            _canonical_path_identity("CAF\u00c9.py", "candidate"),
        )
        with self.assertRaisesRegex(EvidenceError, "aliased"):
            file_manifest_sha256(
                [
                    {"path": nfd, "sha256": "a" * 64},
                    {"path": "CAF\u00c9.py", "sha256": "b" * 64},
                ]
            )

    def test_candidate_scope_includes_configs_and_unknown_resources(self) -> None:
        files = {
            "tests/config.yaml": "policy: safe\n",
            "src/zevorinth/runtime.ini": "[runtime]\nmode=safe\n",
            "tests/settings.toml": "mode = 'safe'\n",
            "tests/Makefile": "all:\n\t@true\n",
            "docs/resource.bin": "\x00\x01resource",
            "build-config.cfg": "[build]\nstrict=true\n",
            "tests/unknown.zevorinth": "opaque resource\n",
            "logs/runtime.txt": "excluded runtime output\n",
            ".env.example": "PLACEHOLDER=not-a-secret\n",
            "deploy.key": "synthetic placeholder\n",
        }
        for relative, content in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        first = build_candidate_snapshot(self.root)
        paths = {item["path"] for item in first["files"]}
        excluded_files = {
            "logs/runtime.txt",
            ".env.example",
            "deploy.key",
        }
        included_files = set(files) - excluded_files
        for relative in included_files:
            self.assertIn(relative, paths)
        for relative in excluded_files:
            self.assertNotIn(relative, paths)
            self.assertIn(
                {"path": relative, "status": "??"},
                first["excluded_changes"],
            )
        self.assertEqual(
            first["candidate_scope"]["unknown_extension_policy"],
            "include",
        )
        (self.root / "tests/settings.toml").write_text(
            "mode = 'changed'\n",
            encoding="utf-8",
        )
        second = build_candidate_snapshot(self.root)
        self.assertNotEqual(
            first["code_fingerprint"],
            second["code_fingerprint"],
        )

    def test_missing_frozen_evidence_fails_with_specific_error(self) -> None:
        frozen_path = self.root / next(iter(self.fixture.frozen))
        frozen_path.unlink()
        with self.assertRaisesRegex(
            EvidenceError,
            "frozen evidence mismatch",
        ):
            self._build()

    def test_old_browser_fingerprint_is_not_current_candidate_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as bundle_temp:
            bundle_path = self.fixture.write_evidence_bundle(
                Path(bundle_temp) / "bundle"
            )
            bundle_root = bundle_path.parent
            payload = json.loads(bundle_path.read_text(encoding="utf-8"))
            browser_path = bundle_root / payload["browser_evidence"][0]["path"]
            browser = json.loads(browser_path.read_text(encoding="utf-8"))
            browser["candidate"]["code_fingerprint"] = (
                "e044ebaec9164905f82370761a3a9406439202b882c4399494afe0abb7b923af"
            )
            _write_json(browser_path, browser)
            candidate = json.loads(
                (bundle_root / "candidate-identity.json").read_text(
                    encoding="utf-8"
                )
            )
            with self.assertRaisesRegex(
                EvidenceError,
                "code fingerprint does not match candidate",
            ):
                validate_browser_evidence(
                    self.root,
                    browser_path,
                    candidate,
                    evidence_root=bundle_root,
                )

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

    def test_failed_summary_write_marks_current_state_and_preserves_history(
        self,
    ) -> None:
        current = build_candidate_snapshot(self.root)
        output_path = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "stable-summary.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        stale = {
            "schema_version": SCHEMA_VERSION,
            "status": PASS_EVIDENCE_INTEGRITY,
            "candidate_acceptance": ACCEPTED_FOR_MERGE_REVIEW,
            "candidate_identity": {
                **current,
                "code_fingerprint": "0" * 64,
                "file_manifest_sha256": "1" * 64,
            },
            "review_status": {"merge_ready": True},
        }
        _write_json(output_path, stale)
        stale_bytes = output_path.read_bytes()
        missing_bundle = self.root / "artifacts" / "missing-bundle.json"
        with self.assertRaises(EvidenceError):
            write_summary(
                self.root,
                self.fixture.metadata_path,
                output_path,
                replacement_reason="failure",
                evidence_bundle_path=missing_bundle,
            )
        self.assertEqual(output_path.read_bytes(), stale_bytes)
        with self.assertRaises(EvidenceError):
            validate_summary_is_current(self.root, output_path)
        status = json.loads(
            (output_path.parent / "acceptance-status.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(status["evidence_integrity_status"], BLOCKED)
        self.assertEqual(status["candidate_acceptance"], BLOCKED)
        self.assertFalse(status["authoritative"])
        self.assertNotIn("merge_ready", status)
        self.assertEqual(status["state"], BLOCKED)
        self.assertEqual(
            status["code_fingerprint"],
            current["code_fingerprint"],
        )

    def test_pre_publish_validation_failure_preserves_history(self) -> None:
        output_path = self.fixture.output_path
        original = output_path.read_bytes()
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ), patch(
            "tests.task_control_center_evidence._validate_summary_payload_current",
            side_effect=EvidenceError("simulated pre-publish validation failure"),
        ), self.assertRaisesRegex(
            EvidenceError,
            "pre-publish validation failure",
        ):
            write_summary(
                self.root,
                self.fixture.metadata_path,
                output_path,
                replacement_reason="pre-publish failure",
            )
        self.assertEqual(output_path.read_bytes(), original)
        status = json.loads(
            (output_path.parent / "acceptance-status.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(status["state"], BLOCKED)
        self.assertFalse(status["authoritative"])

    def test_final_status_failure_leaves_no_false_success(self) -> None:
        output_path = self.fixture.output_path
        real_write_status = _write_acceptance_status

        def fail_completed(*args, **kwargs):
            if kwargs.get("status") == "COMPLETED":
                raise EvidenceError("simulated final status failure")
            return real_write_status(*args, **kwargs)

        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ), patch(
            "tests.task_control_center_evidence._write_acceptance_status",
            side_effect=fail_completed,
        ), self.assertRaisesRegex(
            EvidenceError,
            "final status failure",
        ):
            write_summary(
                self.root,
                self.fixture.metadata_path,
                output_path,
                replacement_reason="final status failure",
            )
        status = json.loads(
            (output_path.parent / "acceptance-status.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(status["state"], "IN_PROGRESS")
        self.assertFalse(status["authoritative"])
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ):
            validate_summary_is_current(
                self.root,
                output_path,
                expected_replacement_reason="final status failure",
            )

    def test_acceptance_status_is_non_authoritative_and_stale_success_is_rejected(
        self,
    ) -> None:
        current = build_candidate_snapshot(self.root)
        output_path = self.fixture.output_path
        status_path = output_path.parent / "acceptance-status.json"
        stale_v1 = {
            "schema_version": "task-control-center-acceptance-status-v1",
            "merge_ready": True,
        }
        _write_json(status_path, stale_v1)
        with self.assertRaisesRegex(EvidenceError, "current schema"):
            validate_acceptance_status(self.root, status_path)

        stale_v2 = {
            "schema_version": "task-control-center-acceptance-status-v2",
            "authoritative": False,
            "repository": str(self.root.resolve()),
            "head": current["target_head"],
            "code_fingerprint": "0" * 64,
            "summary_path": output_path.name,
            "summary_sha256": None,
            "candidate_identity": None,
            "state": "COMPLETED",
            "evidence_integrity_status": PASS_EVIDENCE_INTEGRITY,
            "candidate_acceptance": ACCEPTED_FOR_MERGE_REVIEW,
            "reason": "stale success fixture",
        }
        _write_json(status_path, stale_v2)
        with self.assertRaisesRegex(EvidenceError, "fingerprint mismatch"):
            validate_acceptance_status(self.root, status_path)

        summary_bytes = output_path.read_bytes()
        with patch(
            "tests.task_control_center_evidence.os.replace",
            side_effect=OSError("simulated status commit failure"),
        ), self.assertRaisesRegex(EvidenceError, "atomically"):
            write_summary(
                self.root,
                self.fixture.metadata_path,
                output_path,
                replacement_reason="status invalidation failure",
            )
        self.assertFalse(status_path.exists())
        self.assertEqual(output_path.read_bytes(), summary_bytes)

    def test_summary_currentness_recomputes_evidence_and_acceptance(
        self,
    ) -> None:
        summary = self._build()
        output_path = self.fixture.output_path
        _write_json(output_path, summary)
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ):
            validated = validate_summary_is_current(
                self.root,
                output_path,
                expected_replacement_reason="test replacement",
            )
        self.assertEqual(canonical_json(validated), canonical_json(summary))

        forged = json.loads(json.dumps(summary))
        forged["review_status"]["merge_ready"] = True
        forged["candidate_acceptance"] = ACCEPTED_FOR_MERGE_REVIEW
        _write_json(output_path, forged)
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ), self.assertRaisesRegex(
            EvidenceError,
            "does not match recomputed",
        ):
            validate_summary_is_current(
                self.root,
                output_path,
                expected_replacement_reason="test replacement",
            )

        unbound = json.loads(json.dumps(summary))
        unbound.pop("validation_input")
        _write_json(output_path, unbound)
        with self.assertRaisesRegex(
            EvidenceError,
            "validation input",
        ):
            validate_summary_is_current(
                self.root,
                output_path,
                expected_replacement_reason="test replacement",
            )

        reason_forged = json.loads(json.dumps(summary))
        reason_forged["superseded_summary"]["replacement_reason"] = (
            "attacker-selected rationale"
        )
        _write_json(output_path, reason_forged)
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ), self.assertRaisesRegex(
            EvidenceError,
            "replacement reason mismatch",
        ):
            validate_summary_is_current(
                self.root,
                output_path,
                expected_replacement_reason="test replacement",
            )

    def test_acceptance_status_must_match_authoritative_summary(
        self,
    ) -> None:
        summary = self._build()
        output_path = self.fixture.output_path
        _write_json(output_path, summary)
        _write_acceptance_status(
            self.root.resolve(),
            output_path,
            status="COMPLETED",
            reason="status consistency test",
            summary=summary,
        )
        status_path = output_path.parent / "acceptance-status.json"
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ):
            validate_acceptance_status(
                self.root,
                status_path,
                expected_replacement_reason="test replacement",
            )
        forged = json.loads(status_path.read_text(encoding="utf-8"))
        forged["candidate_acceptance"] = ACCEPTED_FOR_MERGE_REVIEW
        _write_json(status_path, forged)
        with patch(
            "tests.task_control_center_evidence.FROZEN_EVIDENCE",
            self.fixture.frozen,
        ), self.assertRaisesRegex(
            EvidenceError,
            "candidate acceptance mismatch",
        ):
            validate_acceptance_status(
                self.root,
                status_path,
                expected_replacement_reason="test replacement",
            )
        blocked = {
            "schema_version": "task-control-center-acceptance-status-v2",
            "generator_version": EVIDENCE_TOOL_VERSION,
            "authoritative": False,
            "authority": output_path.name,
            "repository": str(self.root.resolve()),
            "head": git_state(self.root)["head"],
            "code_fingerprint": code_fingerprint(self.root),
            "summary_path": output_path.name,
            "summary_sha256": None,
            "candidate_identity": None,
            "state": BLOCKED,
            "evidence_integrity_status": BLOCKED,
            "candidate_acceptance": ACCEPTED_FOR_MERGE_REVIEW,
            "reason": "forged incomplete success",
        }
        _write_json(status_path, blocked)
        with self.assertRaisesRegex(
            EvidenceError,
            "cannot claim acceptance",
        ):
            validate_acceptance_status(self.root, status_path)

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

    def test_persistent_publication_is_unsupported_and_fails_before_write(
        self,
    ) -> None:
        review = self.fixture.complete_reviews()
        base, target = self.fixture.commit_candidate()
        output_dir = (
            self.root
            / "artifacts"
            / "audits"
            / "task-control-center-v1"
            / "custody-unsupported"
        )
        original_summary = self.fixture.output_path.read_bytes()
        status_path = self.fixture.output_path.parent / "acceptance-status.json"
        with tempfile.TemporaryDirectory() as outside_temp, patch(
            "tests.task_control_center_evidence."
            "PERSISTENT_PUBLICATION_ANCESTRY_SUPPORTED",
            False,
        ), patch(
            "tests.task_control_center_evidence.os.replace",
            wraps=os.replace,
        ) as replace, patch(
            "tests.task_control_center_evidence.os.write",
            wraps=os.write,
        ) as low_level_write:
            outside = Path(outside_temp)
            sentinel = outside / "sentinel.json"
            sentinel.write_text("outside\n", encoding="utf-8")
            with self.assertRaisesRegex(
                EvidenceError,
                "UNSUPPORTED|persistent publication",
            ):
                write_candidate_snapshot(
                    self.root,
                    output_dir / "final-candidate-snapshot-a26.json",
                )
            with self.assertRaisesRegex(
                EvidenceError,
                "UNSUPPORTED|persistent publication",
            ):
                write_review_binding(
                    self.root,
                    review,
                    output_dir / "review-binding-a26.json",
                    base_head=base,
                    target_head=target,
                )
            with self.assertRaisesRegex(
                EvidenceError,
                "UNSUPPORTED|persistent publication",
            ):
                write_summary(
                    self.root,
                    self.fixture.metadata_path,
                    self.fixture.output_path,
                    replacement_reason="unsupported publication",
                )
            with TrustedEvidenceRoot(
                self.root,
                "unsupported low-level publication",
            ) as trusted:
                with self.assertRaisesRegex(
                    EvidenceError,
                    "UNSUPPORTED|persistent publication",
                ):
                    trusted.write_bytes_atomic(
                        self.root / "low-level-final.json",
                        b"forged\n",
                        "low-level publication",
                    )
            self.assertEqual(replace.call_count, 0)
            self.assertEqual(low_level_write.call_count, 0)
            self.assertFalse(output_dir.exists())
            self.assertFalse((self.root / "low-level-final.json").exists())
            self.assertFalse(status_path.exists())
            self.assertEqual(sentinel.read_bytes(), b"outside\n")
        self.assertEqual(self.fixture.output_path.read_bytes(), original_summary)

    def test_unsupported_publication_cannot_produce_merge_ready(self) -> None:
        review = self.fixture.complete_reviews()
        candidate = build_candidate_snapshot(self.root)
        review_summary = validate_review_manifest(
            self.root,
            review,
            expected_code_fingerprint=candidate["code_fingerprint"],
        )
        with patch(
            "tests.task_control_center_evidence."
            "PERSISTENT_PUBLICATION_ANCESTRY_SUPPORTED",
            False,
        ):
            gate = _evaluate_acceptance_gate_core(
                candidate_identity=candidate,
                trusted_repository_root=self.trusted_repository_root,
                review_summary=review_summary,
                review_binding=None,
                acceptance_policy="target_head_review_required",
                acceptance_evidence_binding={"test": True},
                acceptance_prerequisites=self._acceptance_prerequisites(
                    candidate,
                    review_summary,
                    runtime_verification={
                        "status": "VERIFIED",
                        "scopes": ["browser"],
                        "reason": "candidate-bound browser evidence",
                        "valid_evidence_count": 1,
                    }
                ),
            )
        self.assertFalse(gate["merge_ready"])
        self.assertEqual(gate["acceptance_status"], NOT_VERIFIED)
        self.assertEqual(
            gate["persistent_publication_ancestry"],
            "UNSUPPORTED",
        )
        self.assertIn(
            "persistent_publication_ancestry",
            gate["unverified_scopes"],
        )
        self.assertIn(
            "persistent_publication_ancestry_unsupported",
            gate["blocking_reasons"],
        )

    def test_unsupported_execution_trust_cannot_produce_merge_ready(
        self,
    ) -> None:
        review = self.fixture.complete_reviews()
        candidate = build_candidate_snapshot(self.root)
        review_summary = validate_review_manifest(
            self.root,
            review,
            expected_code_fingerprint=candidate["code_fingerprint"],
        )
        runtime = {
            "status": "VERIFIED",
            "scopes": ["browser"],
            "reason": "candidate-bound browser evidence",
            "valid_evidence_count": 1,
        }
        for constant, scope, reason in (
            (
                "MUTATION_EXECUTION_TRUST_SUPPORTED",
                "mutation_execution_trust",
                "mutation_execution_trust_unsupported",
            ),
            (
                "TEST_EXECUTION_TRUST_SUPPORTED",
                "test_execution_trust",
                "test_execution_trust_unsupported",
            ),
        ):
            with self.subTest(constant=constant), patch(
                f"tests.task_control_center_evidence.{constant}",
                False,
            ):
                gate = _evaluate_acceptance_gate_core(
                    candidate_identity=candidate,
                    trusted_repository_root=self.trusted_repository_root,
                    review_summary=review_summary,
                    review_binding=None,
                    acceptance_policy="target_head_review_required",
                    acceptance_evidence_binding={"test": True},
                    acceptance_prerequisites=(
                        self._acceptance_prerequisites(
                            candidate,
                            review_summary,
                            runtime_verification=runtime
                        )
                    ),
                )
            self.assertFalse(gate["merge_ready"])
            self.assertEqual(gate["acceptance_status"], NOT_VERIFIED)
            self.assertEqual(gate[scope], "UNSUPPORTED")
            self.assertIn(scope, gate["unverified_scopes"])
            self.assertIn(reason, gate["blocking_reasons"])

    def test_candidate_snapshot_writer_rejects_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as outside_temp:
            outside = Path(outside_temp)
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("outside\n", encoding="utf-8")
            output_dir = (
                self.root
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / "snapshot-link"
            )
            output_dir.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(
                EvidenceError,
                "symbolic link|safely",
            ):
                write_candidate_snapshot(
                    self.root,
                    output_dir / "final-candidate-snapshot-test.json",
                )
            self.assertEqual(
                {
                    path.relative_to(outside).as_posix(): path.read_bytes()
                    for path in outside.rglob("*")
                    if path.is_file()
                },
                {"sentinel.txt": b"outside\n"},
            )

    def test_candidate_snapshot_writer_rejects_parent_moved_outside(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as outside_temp:
            outside = Path(outside_temp)
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("outside\n", encoding="utf-8")
            output_dir = (
                self.root
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / "snapshot-swap"
            )
            output_dir.mkdir(parents=True)
            detached = outside / "snapshot-detached"
            real_os_write = os.write
            swapped = False

            def write_then_move(file_descriptor: int, data: bytes) -> int:
                nonlocal swapped
                written = real_os_write(file_descriptor, data)
                if not swapped:
                    output_dir.rename(detached)
                    swapped = True
                return written

            with patch(
                "tests.task_control_center_evidence.os.write",
                side_effect=write_then_move,
            ), self.assertRaisesRegex(
                EvidenceError,
                "trust anchor|no longer reachable",
            ):
                write_candidate_snapshot(
                    self.root,
                    output_dir / "final-candidate-snapshot-test.json",
                )
            self.assertTrue(swapped)
            self.assertFalse(
                (detached / "final-candidate-snapshot-test.json").exists()
            )
            self.assertEqual(
                {
                    path.relative_to(outside).as_posix(): path.read_bytes()
                    for path in outside.rglob("*")
                    if path.is_file()
                },
                {"sentinel.txt": b"outside\n"},
            )

    def test_review_binding_writer_rejects_symlinked_parent(self) -> None:
        review = self.fixture.complete_reviews()
        base, target = self.fixture.commit_candidate()
        with tempfile.TemporaryDirectory() as outside_temp:
            outside = Path(outside_temp)
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("outside\n", encoding="utf-8")
            output_dir = (
                self.root
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / "binding-link"
            )
            output_dir.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(
                EvidenceError,
                "symbolic link|safely",
            ):
                write_review_binding(
                    self.root,
                    review,
                    output_dir / "review-binding-test.json",
                    base_head=base,
                    target_head=target,
                )
            self.assertEqual(
                {
                    path.relative_to(outside).as_posix(): path.read_bytes()
                    for path in outside.rglob("*")
                    if path.is_file()
                },
                {"sentinel.txt": b"outside\n"},
            )

    def test_review_binding_writer_rejects_parent_moved_outside(self) -> None:
        review = self.fixture.complete_reviews()
        base, target = self.fixture.commit_candidate()
        with tempfile.TemporaryDirectory() as outside_temp:
            outside = Path(outside_temp)
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("outside\n", encoding="utf-8")
            output_dir = (
                self.root
                / "artifacts"
                / "audits"
                / "task-control-center-v1"
                / "binding-swap"
            )
            output_dir.mkdir(parents=True)
            detached = outside / "binding-detached"
            real_os_write = os.write
            swapped = False

            def write_then_move(file_descriptor: int, data: bytes) -> int:
                nonlocal swapped
                written = real_os_write(file_descriptor, data)
                if not swapped:
                    output_dir.rename(detached)
                    swapped = True
                return written

            with patch(
                "tests.task_control_center_evidence.os.write",
                side_effect=write_then_move,
            ), self.assertRaisesRegex(
                EvidenceError,
                "trust anchor|no longer reachable",
            ):
                write_review_binding(
                    self.root,
                    review,
                    output_dir / "review-binding-test.json",
                    base_head=base,
                    target_head=target,
                )
            self.assertTrue(swapped)
            self.assertFalse((detached / "review-binding-test.json").exists())
            self.assertEqual(
                {
                    path.relative_to(outside).as_posix(): path.read_bytes()
                    for path in outside.rglob("*")
                    if path.is_file()
                },
                {"sentinel.txt": b"outside\n"},
            )


if __name__ == "__main__":
    unittest.main()
