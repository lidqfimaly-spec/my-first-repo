"""Reproducible offline evidence for the local Task Control Center."""

from __future__ import annotations

import argparse
import contextvars
import errno
import fcntl
import hashlib
import hmac
import inspect
import json
import math
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from contextlib import contextmanager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_OS_OPEN = os.open
_OS_DUP = os.dup
_OS_CLOSE = os.close
_ORIGINAL_UNITTEST_PRIMITIVES = {
    "TextTestRunner": unittest.TextTestRunner,
    "TextTestRunner.run": unittest.TextTestRunner.run,
    "TestSuite.run": unittest.TestSuite.run,
    "TestCase.run": unittest.TestCase.run,
    "TestResult.startTest": unittest.TestResult.startTest,
    "TestResult.stopTest": unittest.TestResult.stopTest,
    "TestResult.addSuccess": unittest.TestResult.addSuccess,
    "TestResult.addFailure": unittest.TestResult.addFailure,
    "TestResult.addError": unittest.TestResult.addError,
    "TestResult.addSkip": unittest.TestResult.addSkip,
    "TestResult.addExpectedFailure": unittest.TestResult.addExpectedFailure,
    "TestResult.addUnexpectedSuccess": (
        unittest.TestResult.addUnexpectedSuccess
    ),
}
SCHEMA_VERSION = "task-control-center-evidence-v2"
TEST_RESULT_SCHEMA_VERSION = "task-control-center-test-result-v3"
TEST_SELECTION_RULES_VERSION = "blocked-test-id-v1"
MUTATION_CASES_VERSION = "1.1.0"
REVIEW_SCHEMA_VERSION = "task-control-center-independent-review-v1"
REVIEW_RESULT_SCHEMA_VERSION = "task-control-center-review-result-v1"
CANDIDATE_SCHEMA_VERSION = "task-control-center-candidate-identity-v2"
REVIEW_BINDING_SCHEMA_VERSION = "task-control-center-review-binding-v1"
EVIDENCE_BUNDLE_SCHEMA_VERSION = "task-control-center-evidence-bundle-v2"
EVIDENCE_TOOL_VERSION = "phase-2.7a-36"
ACCEPTANCE_PREREQUISITE_SCHEMA_VERSION = (
    "task-control-center-acceptance-prerequisites-v1"
)
ACCEPTANCE_PROOF_RECORD_LIMIT = 1024
ACCEPTANCE_PROOF_TERMINAL_RETENTION_SECONDS = 60.0
PERSISTENT_PUBLICATION_ANCESTRY_SUPPORTED = False
PERSISTENT_PUBLICATION_ANCESTRY_REASON = (
    "this platform cannot atomically prevent a same-principal rename of a "
    "target directory during publication"
)
MUTATION_EXECUTION_TRUST_SUPPORTED = False
MUTATION_EXECUTION_TRUST_REASON = (
    "the available mutation runner is candidate-controlled and cannot "
    "independently authenticate test execution or trace observation"
)
TEST_EXECUTION_TRUST_SUPPORTED = False
TEST_EXECUTION_TRUST_REASON = (
    "candidate tests share a Python process with the unittest runner and "
    "can modify execution primitives or result observation"
)
REVIEW_SCOPE = "task-control-center-v1"
CANDIDATE_MODES = frozenset(
    {"working_tree", "committed_range", "bootstrap"}
)
CANDIDATE_IDENTITY_VERIFIED = "CANDIDATE_IDENTITY_VERIFIED"
BOOTSTRAP_INCOMPLETE = "BOOTSTRAP_INCOMPLETE"
NOT_AVAILABLE = "NOT_AVAILABLE"
NOT_VERIFIED = "NOT_VERIFIED"
BLOCKED = "BLOCKED"
PASS_EVIDENCE_INTEGRITY = "PASS_EVIDENCE_INTEGRITY"
ACCEPTED_FOR_MERGE_REVIEW = "ACCEPTED_FOR_MERGE_REVIEW"
ACCEPTANCE_POLICIES = frozenset(
    {"target_head_review_required", "content_equivalence_allowed"}
)
RUNTIME_STATUSES = frozenset(
    {"VERIFIED", "NOT_VERIFIED", "NOT_APPLICABLE"}
)
SUPPORTED_RUNTIME_SCOPES = frozenset({"browser"})
BROWSER_EVIDENCE_STATUSES = frozenset(
    {"VALID_FOR_CANDIDATE", "NOT_VERIFIED", "NOT_APPLICABLE"}
)
BROWSER_RESULT_SCHEMES = (
    frozenset(
        {
            "active_lease",
            "expired_lease",
            "released_lease",
            "preview_reads",
            "mobile_layout",
        }
    ),
    frozenset(
        {
            "query_after_response_loss",
            "initial_browser_outcome",
        }
    ),
)
BROWSER_SCHEMA_VERSION = "task-control-center-browser-validation-v1"
MAX_JSON_EVIDENCE_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_FILE_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_FILES = 512
MAX_JSON_DEPTH = 64
CANDIDATE_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".idea",
        ".venv",
        "venv",
        "artifacts",
        "logs",
        "tmp",
        "build",
        "dist",
        "htmlcov",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".omx",
        "node_modules",
        "__pycache__",
    }
)
CANDIDATE_EXCLUDED_FILES = frozenset(
    {".DS_Store", ".coverage", "coverage.xml"}
)
CANDIDATE_EXCLUDED_SUFFIXES = frozenset(
    {".pyc", ".pyo", ".swp", ".swo"}
)
SECRET_LIKE_SUFFIXES = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".token", ".secret"}
)
SECRET_LIKE_NAMES = frozenset(
    {".netrc", ".pgpass", "credentials", "credentials.json", "secrets"}
)
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
_TRUSTED_DIRECTORY_IDENTITIES: contextvars.ContextVar[
    dict[str, tuple[int, int]]
] = contextvars.ContextVar(
    "task_control_center_trusted_directories",
    default={},
)


class EvidenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class _OwnedFDRecord:
    identity: tuple[int, int]
    anchor_fd: int


class _OwnedFDCollection(list[int]):
    """List of unexposed FD handles with private ownership anchors."""

    def __init__(self, values: Iterable[int] = ()) -> None:
        super().__init__(values)
        self._records: dict[int, _OwnedFDRecord] = {}
        self._unresolved_anchor_states: dict[int, str] = {}
        self._retirement_lock = threading.RLock()

    def register(self, file_fd: int) -> None:
        with self._retirement_lock:
            with _defer_keyboard_interrupt():
                metadata = os.fstat(file_fd)
                anchor_fd = _OS_DUP(file_fd)
                try:
                    os.set_inheritable(anchor_fd, False)
                except BaseException:
                    _OS_CLOSE(anchor_fd)
                    raise
                identity = (metadata.st_dev, metadata.st_ino)
                self._records[file_fd] = _OwnedFDRecord(
                    identity=identity,
                    anchor_fd=anchor_fd,
                )
                self.append(file_fd)

    def record(self, file_fd: int) -> _OwnedFDRecord | None:
        return self._records.get(file_fd)

    def take_record(self, file_fd: int) -> _OwnedFDRecord | None:
        return self._records.pop(file_fd, None)

    def abandon_anchor(self, file_fd: int) -> None:
        with self._retirement_lock:
            self._abandon_anchor_locked(file_fd)

    def _abandon_anchor_locked(self, file_fd: int) -> None:
        record = self.record(file_fd)
        if record is None:
            raise EvidenceError(
                f"FD {file_fd} has no ownership anchor to abandon"
            )
        anchor_state, anchor_error = _observe_fd_identity_state(
            record.anchor_fd,
            record.identity,
        )
        if anchor_error is not None or anchor_state != "OPEN":
            error = EvidenceError(
                f"FD {file_fd} ownership anchor could not be verified"
            )
            error.fd_close_states = {
                file_fd: "OPEN",
                record.anchor_fd: anchor_state,
            }
            error.unresolved_fds = dict(error.fd_close_states)
            if anchor_error is not None:
                raise error from anchor_error
            raise error
        try:
            _acquire_ownership_lock(record.anchor_fd)
            fcntl.flock(
                file_fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BaseException as exc:
            error = EvidenceError(
                f"FD {file_fd} and ownership anchor could not be matched"
            )
            release_error = _release_ownership_lock(record.anchor_fd)
            if release_error is not None:
                _append_exception_note(
                    error,
                    "ownership lock release failed: "
                    f"{_safe_diagnostic_text(release_error)}",
                )
            error.fd_close_states = {
                file_fd: "UNKNOWN",
                record.anchor_fd: "UNKNOWN",
            }
            error.unresolved_fds = dict(error.fd_close_states)
            raise error from exc
        release_error = _release_ownership_lock(record.anchor_fd)
        if release_error is not None:
            error = EvidenceError(
                f"FD {file_fd} ownership lock release failed"
            )
            error.fd_close_states = {
                file_fd: "OPEN",
                record.anchor_fd: "UNKNOWN",
            }
            error.unresolved_fds = dict(error.fd_close_states)
            raise error from release_error
        try:
            _OS_CLOSE(record.anchor_fd)
        except BaseException as exc:
            state, observation_error = _observe_fd_close_state(
                record.anchor_fd,
                record.identity,
            )
            error = EvidenceError(
                f"FD {file_fd} ownership anchor close failed"
            )
            error.fd_close_states = {
                file_fd: "OPEN",
                record.anchor_fd: state,
            }
            error.unresolved_fds = dict(error.fd_close_states)
            if observation_error is not None:
                _append_exception_note(error, str(observation_error))
            raise error from exc
        self.take_record(file_fd)
        self.remove(file_fd)

    def adopt(
        self,
        source: "_OwnedFDCollection",
        file_fd: int,
    ) -> None:
        with source._retirement_lock, self._retirement_lock:
            record = source.take_record(file_fd)
            if record is None:
                raise EvidenceError(
                    f"FD {file_fd} has no transferable ownership record"
                )
            source.remove(file_fd)
            self._records[file_fd] = record
            self.append(file_fd)

    def close_all_anchors(self) -> None:
        with self._retirement_lock:
            for file_fd, record in list(self._records.items()):
                anchor_state, _ = _observe_fd_identity_state(
                    record.anchor_fd,
                    record.identity,
                )
                if anchor_state != "OPEN":
                    self._unresolved_anchor_states[
                        record.anchor_fd
                    ] = anchor_state
                    continue
                try:
                    _acquire_ownership_lock(record.anchor_fd)
                    fcntl.flock(
                        file_fd,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    fcntl.flock(
                        record.anchor_fd,
                        fcntl.LOCK_UN,
                    )
                except BaseException:
                    _release_ownership_lock(record.anchor_fd)
                    self._unresolved_anchor_states[
                        record.anchor_fd
                    ] = "UNKNOWN"
                    continue
                try:
                    _OS_CLOSE(record.anchor_fd)
                except BaseException:
                    state, _ = _observe_fd_close_state(
                        record.anchor_fd,
                        record.identity,
                    )
                    if state != "CLOSED":
                        self._unresolved_anchor_states[
                            record.anchor_fd
                        ] = state
                        continue
                self._records.pop(file_fd, None)

TRANSACTION_STATES = frozenset(
    {"COMMITTED", "UNCOMMITTED", "UNKNOWN"}
)
DURABILITY_STATES = frozenset(
    {"NOT_ATTEMPTED", "CONFIRMED", "UNKNOWN"}
)
TARGET_STATES = frozenset(
    {"ABSENT", "UNCOMMITTED", "COMMITTED", "CHANGED", "UNKNOWN"}
)
_MISSING_DIAGNOSTIC = object()


def _safe_diagnostic_text(value: Any) -> str:
    try:
        text = str(value)
    except BaseException:
        return "<unavailable diagnostic value>"
    return text.encode(
        "utf-8",
        "backslashreplace",
    ).decode("utf-8")


def _json_string_literal(value: Any) -> str:
    text = _safe_diagnostic_text(value)
    chunks = ['"']
    for character in text:
        codepoint = ord(character)
        if character == '"':
            chunks.append('\\"')
        elif character == "\\":
            chunks.append("\\\\")
        elif character == "\n":
            chunks.append("\\n")
        elif character == "\r":
            chunks.append("\\r")
        elif character == "\t":
            chunks.append("\\t")
        elif codepoint < 0x20:
            chunks.append(f"\\u{codepoint:04x}")
        else:
            chunks.append(character)
    chunks.append('"')
    return "".join(chunks)


def _fallback_error_json(
    error: BaseException,
    serialization_error: BaseException,
) -> str:
    """Build a minimal JSON object without json.dumps or complex diagnostics."""
    return (
        '{"schema_version":"task-control-center-error-v1",'
        '"error_type":'
        f"{_json_string_literal(type(error).__name__)},"
        '"message":'
        f"{_json_string_literal(error)},"
        '"diagnostic_output_status":"FALLBACK",'
        '"transaction_state":"UNKNOWN",'
        '"durability_state":"UNKNOWN",'
        '"target_state":"UNKNOWN",'
        '"residual":{"status":"UNKNOWN","paths":[]},'
        '"residual_unknown":true,'
        '"close_diagnostics":[],'
        '"fd_close_states":{},'
        '"unresolved_fds":{},'
        '"signal_mask_state":"UNKNOWN",'
        '"secondary_exceptions":[],'
        '"diagnostic_errors":['
        f"{_json_string_literal('diagnostic serialization failed: ' + _safe_diagnostic_text(serialization_error))}"
        "],"
        '"primary_exception":null,'
        '"context_exception":null,'
        '"notes":[]}'
    )


def _write_stderr_text(text: str) -> bool:
    try:
        sys.stderr.write(text)
        flush = getattr(sys.stderr, "flush", None)
        if callable(flush):
            flush()
        return True
    except BaseException:
        return False


def _diagnostic_exception_reference(
    error: BaseException,
    attribute: str,
    diagnostic_errors: list[str],
) -> dict[str, str] | None:
    value = _exception_attribute(
        error,
        attribute,
        _MISSING_DIAGNOSTIC,
        attribute_errors=diagnostic_errors,
    )
    if value is _MISSING_DIAGNOSTIC:
        return {
            "type": "UNKNOWN",
            "message": f"{attribute} unavailable",
        }
    if value is None:
        return None
    if not isinstance(value, BaseException):
        diagnostic_errors.append(
            f"{attribute} invalid value: "
            f"{_safe_diagnostic_text(value)}"
        )
        return {
            "type": "UNKNOWN",
            "message": f"{attribute} is not an exception",
        }
    return {
        "type": _safe_diagnostic_text(type(value).__name__),
        "message": _safe_diagnostic_text(value),
    }


def _exception_attribute(
    error: BaseException,
    name: str,
    default: Any,
    *,
    attribute_errors: list[str] | None = None,
) -> Any:
    try:
        inspect.getattr_static(error, name)
    except AttributeError:
        return default
    except BaseException as exc:
        if attribute_errors is not None:
            attribute_errors.append(
                f"{name}: {type(exc).__name__}: "
                f"{_safe_diagnostic_text(exc)}"
            )
        return default
    try:
        return getattr(error, name)
    except AttributeError as exc:
        if attribute_errors is not None:
            attribute_errors.append(
                f"{name}: {type(exc).__name__}: "
                f"{_safe_diagnostic_text(exc)}"
            )
        return default
    except BaseException as exc:
        if attribute_errors is not None:
            attribute_errors.append(
                f"{name}: {type(exc).__name__}: "
                f"{_safe_diagnostic_text(exc)}"
            )
        return default


def _transaction_state_from_error(
    error: BaseException,
    diagnostic_errors: list[str] | None = None,
) -> str:
    value = _exception_attribute(
        error,
        "transaction_state",
        _MISSING_DIAGNOSTIC,
        attribute_errors=diagnostic_errors,
    )
    if value is _MISSING_DIAGNOSTIC:
        return "UNKNOWN"
    if type(value) is str and value in TRANSACTION_STATES:
        return value
    if diagnostic_errors is not None:
        diagnostic_errors.append(
            "transaction_state invalid value: "
            f"{_safe_diagnostic_text(value)}"
        )
    return "UNKNOWN"


def _durability_state_from_error(
    error: BaseException,
    diagnostic_errors: list[str] | None = None,
) -> str:
    value = _exception_attribute(
        error,
        "durability_state",
        _MISSING_DIAGNOSTIC,
        attribute_errors=diagnostic_errors,
    )
    if value is _MISSING_DIAGNOSTIC:
        return "UNKNOWN"
    if type(value) is str and value in DURABILITY_STATES:
        return value
    if diagnostic_errors is not None:
        diagnostic_errors.append(
            "durability_state invalid value: "
            f"{_safe_diagnostic_text(value)}"
        )
    return "UNKNOWN"


def _target_state_from_error(
    error: BaseException,
    diagnostic_errors: list[str] | None = None,
) -> str:
    value = _exception_attribute(
        error,
        "target_state",
        _MISSING_DIAGNOSTIC,
        attribute_errors=diagnostic_errors,
    )
    if value is _MISSING_DIAGNOSTIC:
        return "UNKNOWN"
    if type(value) is str and value in TARGET_STATES:
        return value
    if diagnostic_errors is not None:
        diagnostic_errors.append(
            "target_state invalid value: "
            f"{_safe_diagnostic_text(value)}"
        )
    return "UNKNOWN"


def _append_close_diagnostics(
    error: BaseException,
    diagnostics: Iterable[str],
) -> None:
    try:
        existing = list(
            getattr(error, "close_diagnostics", []) or []
        )
    except BaseException:
        existing = []
    merged = list(dict.fromkeys(
        [_safe_diagnostic_text(item) for item in existing]
        + [_safe_diagnostic_text(item) for item in diagnostics]
    ))
    if not merged:
        return
    try:
        error.close_diagnostics = merged
    except BaseException:
        pass


def _observe_fd_close_state(
    file_fd: int,
    expected_identity: tuple[int, int] | None,
) -> tuple[str, BaseException | None]:
    try:
        metadata = os.fstat(file_fd)
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return "CLOSED", None
        return "UNKNOWN", exc
    except BaseException as exc:
        return "UNKNOWN", exc
    if expected_identity is None:
        return "UNKNOWN", None
    if (metadata.st_dev, metadata.st_ino) == expected_identity:
        # Device/inode identity describes a filesystem object, not the open
        # file description that originally occupied this numeric FD.
        return "UNKNOWN", None
    return "REUSED_OR_REPLACED", None


def _observe_fd_identity_state(
    file_fd: int,
    expected_identity: tuple[int, int],
) -> tuple[str, BaseException | None]:
    """Observe only whether the numeric FD currently names the expected object."""
    try:
        metadata = os.fstat(file_fd)
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return "CLOSED", None
        return "UNKNOWN", exc
    except BaseException as exc:
        return "UNKNOWN", exc
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        return "REUSED_OR_REPLACED", None
    return "OPEN", None


def _attach_fd_close_states(
    error: BaseException,
    close_states: dict[int, str],
) -> None:
    if not close_states:
        return
    try:
        existing = dict(getattr(error, "fd_close_states", {}))
    except BaseException:
        existing = {}
    existing.update(close_states)
    try:
        error.fd_close_states = existing
    except BaseException:
        pass
    unresolved = {
        file_fd: state
        for file_fd, state in existing.items()
        if state in {"OPEN", "UNKNOWN", "REUSED_OR_REPLACED"}
    }
    if unresolved:
        try:
            error.unresolved_fds = unresolved
        except BaseException:
            pass


def _copy_signal_metadata(
    source: BaseException,
    target: BaseException,
) -> None:
    for attribute in ("signal_mask_state", "secondary_exceptions"):
        try:
            value = getattr(source, attribute)
        except BaseException:
            continue
        try:
            setattr(target, attribute, value)
        except BaseException:
            pass


def _close_owner_anchor(
    anchor_fd: int,
    expected_identity: tuple[int, int],
    close_states: dict[int, str],
    observation_failures: list[tuple[str, BaseException]],
    label: str,
) -> bool:
    state, observation_error = _observe_fd_identity_state(
        anchor_fd,
        expected_identity,
    )
    if state != "OPEN":
        close_states[anchor_fd] = state
    if observation_error is not None:
        observation_failures.append((label, observation_error))
    if state != "OPEN":
        observation_failures.append(
            (
                label,
                EvidenceError(
                    "ownership anchor identity could not be verified; "
                    "numeric FD retained"
                ),
            )
        )
        return False
    release_error = _release_ownership_lock(anchor_fd)
    if release_error is not None:
        close_states[anchor_fd] = "UNKNOWN"
        error = EvidenceError(
            "ownership anchor lock release failed; "
            "numeric FD retained"
        )
        error.__cause__ = release_error
        observation_failures.append((label, error))
        return False
    try:
        _OS_CLOSE(anchor_fd)
    except BaseException as close_error:
        state, observation_error = _observe_fd_close_state(
            anchor_fd,
            expected_identity,
        )
        identity_state, identity_error = _observe_fd_identity_state(
            anchor_fd,
            expected_identity,
        )
        if identity_state == "OPEN":
            release_error = _release_ownership_lock(anchor_fd)
            if release_error is not None:
                observation_failures.append(
                    (label, release_error)
                )
        close_states[anchor_fd] = state
        observation_failures.append(
            (
                label,
                EvidenceError(
                    "ownership anchor close failed: "
                    f"{close_error}"
                ),
            )
        )
        if observation_error is not None:
            observation_failures.append(
                (label, observation_error)
            )
        if identity_error is not None:
            observation_failures.append(
                (label, identity_error)
            )
        return False
    return True


def _acquire_ownership_lock(anchor_fd: int) -> None:
    for attempt in range(20):
        try:
            fcntl.flock(
                anchor_fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            return
        except BlockingIOError:
            if attempt == 19:
                raise
            time.sleep(0.001)


def _release_ownership_lock(anchor_fd: int) -> BaseException | None:
    try:
        fcntl.flock(anchor_fd, fcntl.LOCK_UN)
    except BaseException as exc:
        return exc
    return None


def _close_fds_preserving_primary(
    descriptors: list[tuple[int, str]],
    *,
    attempted_fds: set[int] | None = None,
    close_states: dict[int, str] | None = None,
    ownership_records: Mapping[int, _OwnedFDRecord] | None = None,
) -> list[tuple[str, BaseException]]:
    primary = sys.exc_info()[1]
    failures: list[tuple[str, BaseException]] = []
    observation_failures: list[tuple[str, BaseException]] = []
    close_states = {} if close_states is None else close_states
    for file_fd, label in descriptors:
        ownership = (
            None
            if ownership_records is None
            else ownership_records.get(file_fd)
        )
        if ownership is None:
            state = "UNKNOWN"
            close_states[file_fd] = state
            failures.append(
                (
                    label,
                    EvidenceError(
                        "numeric FD has no registered ownership record"
                    ),
                )
            )
            continue
        expected_identity = ownership.identity
        anchor_state, anchor_observation_error = (
            _observe_fd_identity_state(
                ownership.anchor_fd,
                expected_identity,
            )
        )
        if anchor_state != "OPEN":
            close_states[ownership.anchor_fd] = anchor_state
        if anchor_observation_error is not None:
            observation_failures.append(
                (label, anchor_observation_error)
            )
        if anchor_state != "OPEN":
            close_states[file_fd] = "UNKNOWN"
            failures.append(
                (
                    label,
                    EvidenceError(
                        "ownership anchor is not current; numeric FDs "
                        "retained"
                    ),
                )
            )
            continue
        try:
            observed = os.fstat(file_fd)
        except OSError as observation_error:
            if observation_error.errno == errno.EBADF:
                close_states[file_fd] = "CLOSED"
                close_states[ownership.anchor_fd] = "UNKNOWN"
                observation_failures.append(
                    (
                        label,
                        EvidenceError(
                            "primary FD is closed; ownership anchor "
                            "relationship cannot be verified and was retained"
                        ),
                    )
                )
                failures.append(
                    (
                        label,
                        EvidenceError(
                            "primary FD was already closed before ownership "
                            "verification"
                        ),
                    )
                )
                continue
            observation_failures.append((label, observation_error))
            close_states[file_fd] = "UNKNOWN"
            close_states[ownership.anchor_fd] = "UNKNOWN"
            failures.append(
                (
                    label,
                    EvidenceError(
                        "numeric FD identity could not be observed; "
                        "numeric FDs retained"
                    ),
                )
            )
            continue
        except BaseException as observation_error:
            observation_failures.append((label, observation_error))
            close_states[file_fd] = "UNKNOWN"
            close_states[ownership.anchor_fd] = "UNKNOWN"
            failures.append(
                (
                    label,
                    EvidenceError(
                        "numeric FD identity observation failed; "
                        "numeric FDs retained"
                    ),
                )
            )
            continue
        else:
            if (
                observed.st_dev,
                observed.st_ino,
            ) != ownership.identity:
                state = "REUSED_OR_REPLACED"
                close_states[file_fd] = state
                close_states[ownership.anchor_fd] = "UNKNOWN"
                failures.append(
                    (
                        label,
                        EvidenceError(
                            "numeric FD identity changed before close"
                        ),
                    )
                )
                continue
            try:
                _acquire_ownership_lock(ownership.anchor_fd)
            except BlockingIOError:
                state = "UNKNOWN"
                close_states[file_fd] = state
                close_states[ownership.anchor_fd] = "UNKNOWN"
                failures.append(
                    (
                        label,
                        EvidenceError(
                            "ownership lock could not be acquired"
                        ),
                    )
                )
                continue
            except BaseException as observation_error:
                observation_failures.append(
                    (label, observation_error)
                )
                state = "UNKNOWN"
                close_states[file_fd] = state
                close_states[ownership.anchor_fd] = "UNKNOWN"
                failures.append(
                    (
                        label,
                        EvidenceError(
                            "numeric FD ownership could not be verified"
                        ),
                    )
                )
                continue
            try:
                fcntl.flock(
                    file_fd,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                state = "UNKNOWN"
                close_states[file_fd] = state
                close_states[ownership.anchor_fd] = "UNKNOWN"
                release_error = _release_ownership_lock(
                    ownership.anchor_fd
                )
                if release_error is not None:
                    observation_failures.append(
                        (label, release_error)
                    )
                failures.append(
                    (
                        label,
                        EvidenceError(
                            "numeric FD and ownership anchor do not share "
                            "the registered open instance; numeric FDs retained"
                        ),
                    )
                )
                continue
            except BaseException as observation_error:
                observation_failures.append(
                    (label, observation_error)
                )
                state = "UNKNOWN"
                close_states[file_fd] = state
                close_states[ownership.anchor_fd] = "UNKNOWN"
                release_error = _release_ownership_lock(
                    ownership.anchor_fd
                )
                if release_error is not None:
                    observation_failures.append(
                        (label, release_error)
                    )
                failures.append(
                    (
                        label,
                        EvidenceError(
                            "numeric FD ownership lock check failed"
                        ),
                    )
                )
                continue
        close_error: BaseException | None = None
        try:
            with _defer_keyboard_interrupt():
                os.close(file_fd)
        except BaseException as exc:
            close_error = exc
            state, observation_error = _observe_fd_close_state(
                file_fd,
                expected_identity,
            )
            if observation_error is not None:
                observation_failures.append(
                    (label, observation_error)
                )
        else:
            state = "CLOSED"
        close_states[file_fd] = state
        if close_error is not None:
            close_states[ownership.anchor_fd] = "UNKNOWN"
            release_error = _release_ownership_lock(
                ownership.anchor_fd
            )
            if release_error is not None:
                observation_failures.append(
                    (label, release_error)
                )
            failures.append((label, close_error))
            continue
        anchor_closed = _close_owner_anchor(
            ownership.anchor_fd,
            ownership.identity,
            close_states,
            observation_failures,
            label,
        )
        if anchor_closed and attempted_fds is not None:
            attempted_fds.add(file_fd)
    if observation_failures:
        if primary is not None:
            for label, observation_error in observation_failures:
                _append_exception_note(
                    primary,
                    f"{label} close identity observation failed: "
                    f"{observation_error}",
                )
        elif not failures:
            first_label, first_error = observation_failures[0]
            for label, observation_error in observation_failures[1:]:
                _append_exception_note(
                    first_error,
                    f"{label} close identity observation failed: "
                    f"{observation_error}",
                )
            _append_exception_note(
                first_error,
                f"{first_label} close identity observation failed",
            )
            _attach_fd_close_states(first_error, close_states)
            raise first_error
    if not failures:
        if primary is not None:
            _attach_fd_close_states(primary, close_states)
        return []
    if primary is not None:
        diagnostics = [
            f"{label}: {close_error}"
            for label, close_error in failures
        ]
        _append_close_diagnostics(primary, diagnostics)
        for label, close_error in failures:
            _append_exception_note(
                primary,
                f"{label} close failed: {close_error}",
            )
        _attach_fd_close_states(primary, close_states)
        return failures
    first_label, first_error = failures[0]
    if isinstance(first_error, OSError):
        failure_to_raise: BaseException = EvidenceError(
            f"{first_label} close failed: {first_error}"
        )
        failure_to_raise.__cause__ = first_error
        _copy_signal_metadata(first_error, failure_to_raise)
    else:
        failure_to_raise = first_error
    diagnostics = [
        f"{label}: {close_error}"
        for label, close_error in failures
    ]
    _append_close_diagnostics(failure_to_raise, diagnostics)
    for label, observation_error in observation_failures:
        _append_exception_note(
            failure_to_raise,
            f"{label} close identity observation failed: "
            f"{observation_error}",
        )
    for label, close_error in failures[1:]:
        _append_exception_note(
            failure_to_raise,
            f"{label} close failed: {close_error}",
        )
    _attach_fd_close_states(failure_to_raise, close_states)
    raise failure_to_raise


def _close_registered_fd(
    opened_fds: list[int],
    file_fd: int,
    label: str,
) -> None:
    if isinstance(opened_fds, _OwnedFDCollection):
        with opened_fds._retirement_lock:
            return _close_registered_fd_locked(
                opened_fds,
                file_fd,
                label,
            )
    return _close_registered_fd_locked(opened_fds, file_fd, label)


def _close_registered_fd_locked(
    opened_fds: list[int],
    file_fd: int,
    label: str,
) -> None:
    close_states: dict[int, str] = {}
    ownership_records = {}
    if isinstance(opened_fds, _OwnedFDCollection):
        record = opened_fds.record(file_fd)
        if record is not None:
            ownership_records[file_fd] = record
    try:
        _close_fds_preserving_primary(
            [(file_fd, label)],
            close_states=close_states,
            ownership_records=ownership_records,
        )
    except BaseException:
        raise
    else:
        if (
            isinstance(opened_fds, _OwnedFDCollection)
            and close_states.get(file_fd) == "CLOSED"
        ):
            opened_fds.take_record(file_fd)
    finally:
        try:
            opened_fds.remove(file_fd)
        except ValueError:
            pass


def _close_owned_fds(
    opened_fds: list[int],
    label: str,
) -> list[tuple[str, BaseException]]:
    if isinstance(opened_fds, _OwnedFDCollection):
        with opened_fds._retirement_lock:
            return _close_owned_fds_locked(opened_fds, label)
    return _close_owned_fds_locked(opened_fds, label)


def _close_owned_fds_locked(
    opened_fds: list[int],
    label: str,
) -> list[tuple[str, BaseException]]:
    close_states: dict[int, str] = {}
    ownership_records = {}
    if isinstance(opened_fds, _OwnedFDCollection):
        ownership_records = {
            file_fd: record
            for file_fd in opened_fds
            if (record := opened_fds.record(file_fd)) is not None
        }
    descriptors = [
        (file_fd, label)
        for file_fd in reversed(opened_fds)
    ]
    try:
        result = _close_fds_preserving_primary(
            descriptors,
            close_states=close_states,
            ownership_records=ownership_records,
        )
    except BaseException:
        raise
    else:
        if isinstance(opened_fds, _OwnedFDCollection):
            for file_fd, _label in descriptors:
                if close_states.get(file_fd) == "CLOSED":
                    opened_fds.take_record(file_fd)
        return result
    finally:
        for file_fd, _label in descriptors:
            try:
                opened_fds.remove(file_fd)
            except ValueError:
                pass


def _merge_exception_metadata(
    source: BaseException,
    target: BaseException,
) -> None:
    for note in getattr(source, "__notes__", []):
        _append_exception_note(target, note)
    for note in getattr(source, "diagnostic_notes", []):
        _append_exception_note(target, note)
    for attribute in (
        "residual_paths",
        "residual_unknown",
        "transaction_state",
        "durability_state",
        "target_state",
        "fd_close_states",
        "unresolved_fds",
        "signal_mask_state",
        "secondary_exceptions",
    ):
        try:
            value = getattr(source, attribute)
        except BaseException:
            continue
        try:
            setattr(target, attribute, value)
        except BaseException:
            pass
    try:
        close_diagnostics = getattr(source, "close_diagnostics", [])
    except BaseException:
        close_diagnostics = []
    _append_close_diagnostics(target, list(close_diagnostics or []))


def _attach_transaction_metadata(
    error: BaseException,
    *,
    transaction_state: str,
    residual_paths: Iterable[str] = (),
    residual_unknown: bool = False,
    durability_state: str | None = None,
    target_state: str | None = None,
    diagnostic: str | None = None,
) -> None:
    normalized_state = (
        transaction_state
        if transaction_state in TRANSACTION_STATES
        else "UNKNOWN"
    )
    try:
        error.transaction_state = normalized_state
    except BaseException:
        pass
    if durability_state is not None:
        normalized_durability = (
            durability_state
            if durability_state in DURABILITY_STATES
            else "UNKNOWN"
        )
        try:
            error.durability_state = normalized_durability
        except BaseException:
            pass
    if target_state is not None:
        normalized_target = (
            target_state
            if target_state in TARGET_STATES
            else "UNKNOWN"
        )
        try:
            error.target_state = normalized_target
        except BaseException:
            pass
    paths = sorted(set(residual_paths))
    if paths:
        try:
            error.residual_paths = paths
        except BaseException:
            pass
    if residual_unknown:
        try:
            error.residual_unknown = True
        except BaseException:
            pass
    if diagnostic:
        _append_exception_note(error, diagnostic)


@contextmanager
def _defer_keyboard_interrupt():
    if os.name != "posix" or not hasattr(signal, "pthread_sigmask"):
        raise EvidenceError(
            "SIGINT shielding is required but unavailable"
        )
    previous = signal.pthread_sigmask(
        signal.SIG_BLOCK,
        {signal.SIGINT},
    )
    primary: BaseException | None = None
    try:
        yield
    except BaseException as primary_error:
        primary = primary_error
        raise
    finally:
        restore_failures: list[BaseException] = []
        for _attempt in range(2):
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            except BaseException as restore_error:
                restore_failures.append(restore_error)
            else:
                break
        try:
            current_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                set(),
            )
        except BaseException as observation_error:
            restore_failures.append(observation_error)
            mask_state = "UNKNOWN"
        else:
            mask_state = (
                "BLOCKED"
                if signal.SIGINT in current_mask
                else "RESTORED"
            )
        if primary is not None:
            for restore_error in restore_failures:
                _append_exception_note(
                    primary,
                    "SIGINT mask restore diagnostic: "
                    f"{type(restore_error).__name__}: "
                    f"{_safe_diagnostic_text(restore_error)}",
                )
            try:
                primary.signal_mask_state = mask_state
            except BaseException:
                pass
            try:
                primary.secondary_exceptions = [
                    {
                        "type": type(item).__name__,
                        "message": _safe_diagnostic_text(item),
                        "phase": "sigint_mask_restore",
                    }
                    for item in restore_failures
                ]
            except BaseException:
                pass
        elif restore_failures:
            failure = restore_failures[-1]
            try:
                failure.signal_mask_state = mask_state
            except BaseException:
                pass
            try:
                failure.secondary_exceptions = [
                    {
                        "type": type(item).__name__,
                        "message": _safe_diagnostic_text(item),
                        "phase": "sigint_mask_restore",
                    }
                    for item in restore_failures[:-1]
                ]
            except BaseException:
                pass
            raise failure


def _replace_and_record_namespace_commit(
    source_name: str,
    destination_name: str,
    *,
    source_directory_fd: int,
    destination_directory_fd: int,
    transaction_state: dict[str, Any],
) -> bool:
    """Keep native replace and its local commit fact in one SIGINT region."""
    with _defer_keyboard_interrupt():
        os.replace(
            source_name,
            destination_name,
            src_dir_fd=source_directory_fd,
            dst_dir_fd=destination_directory_fd,
        )
        transaction_state["transaction_state"] = "COMMITTED"
    return True


def _open_registered_fd(
    opened_fds: list[int],
    *args: Any,
    **kwargs: Any,
) -> int:
    if not isinstance(opened_fds, _OwnedFDCollection):
        raise EvidenceError(
            "FD operations require an ownership-tracked collection"
        )
    opened_fd: int | None = None
    try:
        with _defer_keyboard_interrupt():
            opened_fd = _OS_OPEN(*args, **kwargs)
            opened_fds.register(opened_fd)
    except BaseException:
        if (
            isinstance(opened_fd, int)
            and opened_fd not in opened_fds
        ):
            try:
                _OS_CLOSE(opened_fd)
            except BaseException:
                pass
        raise
    return opened_fd


def _dup_registered_fd(
    opened_fds: list[int],
    source_fd: int,
    label: str,
) -> int:
    del label
    if not isinstance(opened_fds, _OwnedFDCollection):
        raise EvidenceError(
            "FD operations require an ownership-tracked collection"
        )
    duplicated_fd: int | None = None
    try:
        with _defer_keyboard_interrupt():
            duplicated_fd = _OS_DUP(source_fd)
            opened_fds.register(duplicated_fd)
    except BaseException:
        if (
            isinstance(duplicated_fd, int)
            and duplicated_fd not in opened_fds
        ):
            try:
                _OS_CLOSE(duplicated_fd)
            except BaseException:
                pass
        raise
    return duplicated_fd


def _observe_relative_entry(
    directory_fd: int,
    name: str,
    *,
    observation_errors: list[BaseException] | None = None,
) -> str:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "ABSENT"
    except OSError as exc:
        if observation_errors is not None:
            observation_errors.append(exc)
        return "UNKNOWN"
    except BaseException as exc:
        if observation_errors is None:
            raise
        observation_errors.append(exc)
        return "UNKNOWN"
    return "PRESENT"


def _classify_relative_file(
    directory_fd: int,
    name: str,
    expected_data: bytes,
    expected_mode: int,
    *,
    previous_identity: tuple[int, int] | None = None,
    previous_sha256: str | None = None,
    observation_errors: list[BaseException] | None = None,
) -> str:
    nofollow, _, cloexec = _open_flags()
    opened_fds: list[int] = _OwnedFDCollection()
    try:
        file_fd = _open_registered_fd(
            opened_fds,
            name,
            os.O_RDONLY | nofollow | cloexec,
            dir_fd=directory_fd,
        )
        metadata = os.fstat(file_fd)
        chunks = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    except FileNotFoundError:
        return "ABSENT"
    except OSError as exc:
        if observation_errors is not None:
            observation_errors.append(exc)
        return "UNKNOWN"
    except BaseException as exc:
        if observation_errors is None:
            raise
        observation_errors.append(exc)
        return "UNKNOWN"
    finally:
        _close_owned_fds(opened_fds, "relative file observation")
    if (
        stat.S_IMODE(metadata.st_mode) == expected_mode
        and b"".join(chunks) == expected_data
    ):
        return "COMMITTED"
    if (
        previous_identity is not None
        and previous_sha256 is not None
        and (metadata.st_dev, metadata.st_ino) == previous_identity
        and hashlib.sha256(b"".join(chunks)).hexdigest()
        == previous_sha256
    ):
        return "UNCOMMITTED"
    return "CHANGED"


def _read_relative_file_state(
    directory_fd: int,
    name: str,
) -> tuple[tuple[int, int], str] | None:
    nofollow, _, cloexec = _open_flags()
    opened_fds: list[int] = _OwnedFDCollection()
    try:
        file_fd = _open_registered_fd(
            opened_fds,
            name,
            os.O_RDONLY | nofollow | cloexec,
            dir_fd=directory_fd,
        )
        metadata = os.fstat(file_fd)
        chunks = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    except FileNotFoundError:
        return None
    finally:
        _close_owned_fds(opened_fds, "previous target observation")
    return (
        (metadata.st_dev, metadata.st_ino),
        hashlib.sha256(b"".join(chunks)).hexdigest(),
    )


def _require_persistent_publication_ancestry(operation: str) -> None:
    if not PERSISTENT_PUBLICATION_ANCESTRY_SUPPORTED:
        raise EvidenceError(
            f"{operation} is UNSUPPORTED: "
            f"{PERSISTENT_PUBLICATION_ANCESTRY_REASON}"
        )


class TrustedEvidenceRoot:
    """Own an opened evidence directory for a complete verification scope."""

    def __init__(self, path: Path, label: str) -> None:
        self.path = _canonical_absolute_path(path)
        self.label = label
        self._fd: int | None = None
        self._anchor_fd: int | None = None
        self._fd_records: dict[int, _OwnedFDRecord] = {}
        self._unresolved_fds: dict[int, str] = {}
        self._retirement_lock = threading.RLock()
        pending_fds: list[int] = _OwnedFDCollection()
        try:
            opened_fd = _open_absolute_directory_owned(
                pending_fds,
                self.path,
                label,
            )
            _assert_trusted_directory_identity(
                opened_fd,
                self.path,
                label,
            )
            identity = _directory_identity(opened_fd, label)
            anchor_fd = _dup_registered_fd(
                pending_fds,
                opened_fd,
                f"{label} anchor",
            )
            fd_record = pending_fds.record(opened_fd)
            anchor_record = pending_fds.record(anchor_fd)
            if fd_record is None or anchor_record is None:
                raise EvidenceError(
                    f"{label} FD ownership records are incomplete"
                )
            self.identity = identity
            self._fd = opened_fd
            self._anchor_fd = anchor_fd
            self._fd_records = {
                opened_fd: pending_fds.take_record(opened_fd),
                anchor_fd: pending_fds.take_record(anchor_fd),
            }
            self._anchor_relative = "."
        except BaseException:
            _close_owned_fds(pending_fds, f"{label} initialization")
            self._fd = None
            self._anchor_fd = None
            raise

    def assert_active(self) -> int:
        if self._fd is None:
            raise EvidenceError(f"{self.label} is closed")
        return self._fd

    @contextmanager
    def pinned(self):
        with _pin_directory_identity(self.path, self.assert_active()):
            yield self

    def close(self) -> None:
        with self._retirement_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        descriptors: list[tuple[int, str]] = []
        if self._fd is not None:
            descriptors.append((self._fd, f"{self.label} directory"))
        if self._anchor_fd is not None:
            descriptors.append((self._anchor_fd, f"{self.label} anchor"))
        attempted_fds: set[int] = set()
        close_states: dict[int, str] = {}
        try:
            _close_fds_preserving_primary(
                descriptors,
                attempted_fds=attempted_fds,
                close_states=close_states,
                ownership_records=self._fd_records,
            )
        finally:
            for file_fd, state in close_states.items():
                record = self._fd_records.get(file_fd)
                private_anchor_state = (
                    None
                    if record is None
                    else close_states.get(record.anchor_fd)
                )
                if file_fd == self._fd and state != "OPEN":
                    self._fd = None
                if file_fd == self._anchor_fd and state != "OPEN":
                    self._anchor_fd = None
                if state in {"UNKNOWN", "REUSED_OR_REPLACED"}:
                    self._unresolved_fds[file_fd] = state
                if private_anchor_state in {
                    "UNKNOWN",
                    "REUSED_OR_REPLACED",
                }:
                    self._unresolved_fds[
                        record.anchor_fd
                    ] = private_anchor_state
                if (
                    state == "CLOSED"
                    and private_anchor_state not in {
                        "UNKNOWN",
                        "REUSED_OR_REPLACED",
                    }
                ):
                    self._fd_records.pop(file_fd, None)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass

    def relative_path(self, path: Path, label: str) -> str:
        canonical = _canonical_absolute_path(path)
        try:
            relative = canonical.relative_to(self.path)
        except ValueError as exc:
            raise EvidenceError(f"{label} escapes its trusted root") from exc
        return _validate_relative_candidate_path(
            relative.as_posix(),
            label,
        )

    @classmethod
    def _from_open_fd(
        cls,
        path: Path,
        directory_fd: int,
        label: str,
        *,
        anchor_fd: int,
        anchor_relative: str,
        directory_record: _OwnedFDRecord,
    ) -> "TrustedEvidenceRoot":
        root = object.__new__(cls)
        root.path = _canonical_absolute_path(path)
        root.label = label
        root._fd = directory_fd
        root._anchor_fd = None
        root._fd_records = {}
        root._unresolved_fds = {}
        root._retirement_lock = threading.RLock()
        pending_fds: list[int] = _OwnedFDCollection()
        pending_fds._records[directory_fd] = directory_record
        pending_fds.append(directory_fd)
        try:
            root._anchor_relative = _validate_relative_candidate_path(
                anchor_relative,
                f"{label} anchor path",
            )
            anchor_copy = _dup_registered_fd(
                pending_fds,
                anchor_fd,
                f"{label} anchor",
            )
            root.identity = _directory_identity(directory_fd, label)
            root._anchor_fd = anchor_copy
            root._fd_records = {
                directory_fd: pending_fds.take_record(directory_fd),
                anchor_copy: pending_fds.take_record(anchor_copy),
            }
            return root
        except BaseException:
            _close_owned_fds(pending_fds, f"{label} initialization")
            root._fd = None
            root._anchor_fd = None
            raise

    def ensure_directory(
        self,
        path: Path,
        label: str,
    ) -> "TrustedEvidenceRoot":
        relative = self.relative_path(path, label)
        parts = Path(relative).parts
        nofollow, directory, cloexec = _open_flags()
        pending_fds: list[int] = _OwnedFDCollection()
        root = None
        try:
            _open_registered_fd(
                pending_fds,
                ".",
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=self.assert_active(),
            )
            for part in parts:
                try:
                    _open_registered_fd(
                        pending_fds,
                        part,
                        os.O_RDONLY | directory | nofollow | cloexec,
                        dir_fd=pending_fds[-1],
                    )
                except FileNotFoundError:
                    os.mkdir(
                        part,
                        mode=0o755,
                        dir_fd=pending_fds[-1],
                    )
                    _open_registered_fd(
                        pending_fds,
                        part,
                        os.O_RDONLY | directory | nofollow | cloexec,
                        dir_fd=pending_fds[-1],
                    )
                previous_fd = pending_fds[-2]
                _close_registered_fd(
                    pending_fds,
                    previous_fd,
                    f"{label} traversal",
                )
            with _defer_keyboard_interrupt():
                result_record = pending_fds.take_record(
                    pending_fds[-1]
                )
                if result_record is None:
                    raise EvidenceError(
                        f"{label} result FD has no ownership record"
                    )
                result_fd = pending_fds.pop()
                root = self._from_open_fd(
                    self.path / relative,
                    result_fd,
                    label,
                    anchor_fd=self.assert_anchor_active(),
                    anchor_relative=(
                        relative
                        if self._anchor_relative == "."
                        else f"{self._anchor_relative}/{relative}"
                    ),
                    directory_record=result_record,
                )
            return root
        except OSError as exc:
            if root is not None:
                try:
                    root.close()
                except BaseException as close_error:
                    _append_exception_note(exc, str(close_error))
            _close_owned_fds(pending_fds, f"{label} traversal")
            raise EvidenceError(
                f"{label} could not be opened safely; symbolic link or "
                f"non-directory path component rejected: {path}"
            ) from exc
        except BaseException as exc:
            if root is not None:
                try:
                    root.close()
                except BaseException as close_error:
                    _append_exception_note(exc, str(close_error))
            _close_owned_fds(pending_fds, f"{label} traversal")
            raise

    def directory_entries(self) -> list[str]:
        try:
            return sorted(os.listdir(self.assert_active()))
        except OSError as exc:
            raise EvidenceError(
                f"{self.label} could not be enumerated safely"
            ) from exc

    @contextmanager
    def _relative_directory_scope(
        self,
        relative: str,
        label: str,
        transaction_state: dict[str, Any] | None = None,
    ):
        nofollow, directory, cloexec = _open_flags()
        parts = Path(relative).parts
        pending_fds: list[int] = _OwnedFDCollection()
        try:
            _open_registered_fd(
                pending_fds,
                ".",
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=self.assert_active(),
            )
            for part in parts[:-1]:
                try:
                    _open_registered_fd(
                        pending_fds,
                        part,
                        os.O_RDONLY | directory | nofollow | cloexec,
                        dir_fd=pending_fds[-1],
                    )
                except FileNotFoundError:
                    os.mkdir(
                        part,
                        mode=0o755,
                        dir_fd=pending_fds[-1],
                    )
                    _open_registered_fd(
                        pending_fds,
                        part,
                        os.O_RDONLY | directory | nofollow | cloexec,
                        dir_fd=pending_fds[-1],
                    )
                previous_fd = pending_fds[-2]
                _close_registered_fd(
                    pending_fds,
                    previous_fd,
                    f"{label} traversal",
                )
            yield pending_fds[-1]
        except OSError as exc:
            raise EvidenceError(
                f"{label} directory could not be opened safely: {relative}"
            ) from exc
        finally:
            try:
                close_failures = _close_owned_fds(
                    pending_fds,
                    f"{label} traversal",
                )
            except BaseException as close_error:
                if transaction_state is not None:
                    _attach_transaction_metadata(
                        close_error,
                        transaction_state=str(
                            transaction_state.get(
                                "transaction_state",
                                "UNKNOWN",
                            )
                        ),
                        residual_paths=transaction_state.get(
                            "residual_paths",
                            [],
                        ),
                        residual_unknown=bool(
                            transaction_state.get(
                                "residual_unknown",
                                False,
                            )
                        ),
                        durability_state=str(
                            transaction_state.get(
                                "durability_state",
                                "UNKNOWN",
                            )
                        ),
                        target_state=str(
                            transaction_state.get(
                                "target_state",
                                "UNKNOWN",
                            )
                        ),
                        diagnostic=(
                            f"{label} directory close failed"
                        ),
                    )
                raise
            if close_failures and transaction_state is not None:
                primary = sys.exc_info()[1]
                if primary is not None:
                    state = str(
                        transaction_state.get(
                            "transaction_state",
                            "UNKNOWN",
                        )
                    )
                    paths = list(
                        transaction_state.get(
                            "residual_paths",
                            [],
                        )
                    )
                    unknown = bool(
                        transaction_state.get(
                            "residual_unknown",
                            False,
                        )
                    )
                    durability = str(
                        transaction_state.get(
                            "durability_state",
                            "UNKNOWN",
                        )
                    )
                    target = str(
                        transaction_state.get(
                            "target_state",
                            "UNKNOWN",
                        )
                    )
                    _attach_transaction_metadata(
                        primary,
                        transaction_state=state,
                        residual_paths=paths,
                        residual_unknown=unknown,
                        durability_state=durability,
                        target_state=target,
                        diagnostic=(
                            f"{label} directory close failed after "
                            f"transaction state {state}"
                        ),
                    )

    def _write_bytes_staging(
        self,
        path: Path,
        data: bytes,
        label: str,
    ) -> None:
        relative = self.relative_path(path, label)
        parts = Path(relative).parts
        self.assert_within_anchor()
        temporary_name = (
            f".{parts[-1]}.tmp-{os.getpid()}-{os.urandom(8).hex()}"
        )
        temporary_fds: list[int] = _OwnedFDCollection()
        temporary_pending = True
        replace_attempted = False
        namespace_committed = False
        replacement_state = "ABSENT"
        previous_identity: tuple[int, int] | None = None
        previous_sha256: str | None = None
        observation_errors: list[BaseException] = []
        transaction_state: dict[str, Any] = {
            "transaction_state": "UNCOMMITTED",
            "residual_paths": [],
            "residual_unknown": False,
            "durability_state": "NOT_ATTEMPTED",
            "target_state": "UNKNOWN",
        }
        nofollow, _, cloexec = _open_flags()
        with self._relative_directory_scope(
            relative,
            label,
            transaction_state,
        ) as directory_fd:
            try:
                temporary_fd = _open_registered_fd(
                    temporary_fds,
                    temporary_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | nofollow
                    | cloexec,
                    0o600,
                    dir_fd=directory_fd,
                )
                view = memoryview(data)
                while view:
                    written = os.write(temporary_fd, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
                os.fsync(temporary_fd)
                _close_registered_fd(
                    temporary_fds,
                    temporary_fd,
                    f"{label} temporary file",
                )
                self.assert_within_anchor()
                previous_state = _read_relative_file_state(
                    directory_fd,
                    parts[-1],
                )
                if previous_state is not None:
                    previous_identity, previous_sha256 = previous_state
                replace_attempted = True
                namespace_committed = _replace_and_record_namespace_commit(
                    temporary_name,
                    parts[-1],
                    source_directory_fd=directory_fd,
                    destination_directory_fd=directory_fd,
                    transaction_state=transaction_state,
                )
                replacement_state = _classify_relative_file(
                    directory_fd,
                    parts[-1],
                    data,
                    0o600,
                    previous_identity=previous_identity,
                    previous_sha256=previous_sha256,
                )
                transaction_state["transaction_state"] = (
                    "UNKNOWN"
                    if replacement_state == "UNKNOWN"
                    else (
                        "COMMITTED"
                        if replacement_state == "COMMITTED"
                        else "UNCOMMITTED"
                    )
                )
                transaction_state["target_state"] = replacement_state
                if replacement_state == "UNKNOWN":
                    transaction_state["residual_unknown"] = True
                    raise EvidenceError(
                        f"{label} committed state could not be verified"
                    )
                if replacement_state == "CHANGED":
                    transaction_state["residual_paths"] = [relative]
                    raise EvidenceError(
                        f"{label} final target contract was not satisfied"
                    )
                if replacement_state != "COMMITTED":
                    raise EvidenceError(
                        f"{label} replacement did not commit"
                    )
                transaction_state["residual_paths"] = [relative]
                temporary_pending = False
                os.fsync(directory_fd)
                transaction_state["durability_state"] = "CONFIRMED"
            except BaseException as exc:
                if temporary_fds:
                    _close_owned_fds(
                        temporary_fds,
                        f"{label} temporary file",
                    )
                if replace_attempted:
                    replacement_state = _classify_relative_file(
                        directory_fd,
                        parts[-1],
                        data,
                        0o600,
                        previous_identity=previous_identity,
                        previous_sha256=previous_sha256,
                        observation_errors=observation_errors,
                    )
                recorded_commit = (
                    transaction_state["transaction_state"] == "COMMITTED"
                )
                if namespace_committed or recorded_commit:
                    committed_state = "COMMITTED"
                elif replacement_state == "COMMITTED":
                    namespace_committed = True
                    committed_state = "COMMITTED"
                elif replacement_state in {"ABSENT", "UNCOMMITTED"}:
                    committed_state = "UNCOMMITTED"
                else:
                    committed_state = "UNKNOWN"
                durability_state = str(
                    transaction_state["durability_state"]
                )
                if (
                    committed_state == "COMMITTED"
                    and durability_state != "CONFIRMED"
                ):
                    durability_state = "UNKNOWN"
                transaction_state["transaction_state"] = committed_state
                transaction_state["durability_state"] = durability_state
                transaction_state["target_state"] = replacement_state
                if committed_state == "UNKNOWN":
                    transaction_state["residual_paths"] = [relative]
                    transaction_state["residual_unknown"] = True
                elif replacement_state == "UNKNOWN":
                    transaction_state["residual_paths"] = [relative]
                    transaction_state["residual_unknown"] = True
                elif replacement_state in {
                    "COMMITTED",
                    "CHANGED",
                    "UNCOMMITTED",
                }:
                    transaction_state["residual_paths"] = [relative]
                else:
                    transaction_state["residual_paths"] = []
                _attach_transaction_metadata(
                    exc,
                    transaction_state=committed_state,
                    residual_paths=transaction_state["residual_paths"],
                    residual_unknown=bool(
                        transaction_state["residual_unknown"]
                    ),
                    durability_state=durability_state,
                    target_state=replacement_state,
                    diagnostic=(
                        f"{label} namespace={committed_state}; "
                        f"durability={durability_state}; "
                        f"target={replacement_state}"
                    ),
                )
                for observation_error in observation_errors:
                    _append_exception_note(
                        exc,
                        f"{label} observation failed: "
                        f"{observation_error}",
                    )
                if temporary_pending:
                    entry_state = _observe_relative_entry(
                        directory_fd,
                        temporary_name,
                        observation_errors=observation_errors,
                    )
                    if entry_state == "PRESENT":
                        try:
                            os.unlink(
                                temporary_name,
                                dir_fd=directory_fd,
                            )
                        except FileNotFoundError:
                            pass
                        except BaseException as cleanup_error:
                            observation_errors.append(cleanup_error)
                            post_state = _observe_relative_entry(
                                directory_fd,
                                temporary_name,
                                observation_errors=observation_errors,
                            )
                            if post_state == "PRESENT":
                                transaction_state[
                                    "residual_paths"
                                ] = sorted(
                                    set(
                                        transaction_state[
                                            "residual_paths"
                                        ]
                                    )
                                    | {temporary_name}
                                )
                            elif post_state == "UNKNOWN":
                                transaction_state[
                                    "residual_unknown"
                                ] = True
                            _attach_transaction_metadata(
                                exc,
                                transaction_state=str(
                                    transaction_state[
                                        "transaction_state"
                                    ]
                                ),
                                residual_paths=transaction_state[
                                    "residual_paths"
                                ],
                                residual_unknown=bool(
                                    transaction_state[
                                        "residual_unknown"
                                    ]
                                ),
                                diagnostic=(
                                    f"{label} temporary cleanup failed: "
                                    f"{temporary_name}: {cleanup_error}; "
                                    f"post-state={post_state}"
                                ),
                            )
                    elif entry_state == "UNKNOWN":
                        transaction_state["residual_unknown"] = True
                        _attach_transaction_metadata(
                            exc,
                            transaction_state=str(
                                transaction_state[
                                    "transaction_state"
                                ]
                            ),
                            residual_paths=transaction_state[
                                "residual_paths"
                            ],
                            residual_unknown=True,
                            diagnostic=(
                                f"{label} temporary state is unknown: "
                                f"{temporary_name}"
                            ),
                        )
                for observation_error in observation_errors:
                    _append_exception_note(
                        exc,
                        f"{label} cleanup observation failed: "
                        f"{observation_error}",
                    )
                if isinstance(exc, OSError):
                    wrapped = EvidenceError(
                        f"{label} could not be committed atomically: {path}"
                    )
                    _merge_exception_metadata(exc, wrapped)
                    raise wrapped from exc
                raise

    def write_bytes_atomic(
        self,
        path: Path,
        data: bytes,
        label: str,
    ) -> None:
        _require_persistent_publication_ancestry(label)
        self._write_bytes_staging(path, data, label)

    def remove_file_if_exists(self, path: Path, label: str) -> None:
        relative = self.relative_path(path, label)
        parts = Path(relative).parts
        self.assert_within_anchor()
        with self._relative_directory_scope(
            relative,
            label,
        ) as directory_fd:
            try:
                os.unlink(parts[-1], dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def __enter__(self) -> "TrustedEvidenceRoot":
        self.assert_active()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def assert_anchor_active(self) -> int:
        if self._anchor_fd is None:
            raise EvidenceError(f"{self.label} trusted anchor is closed")
        return self._anchor_fd

    def assert_within_anchor(self) -> None:
        anchor_fd = self.assert_anchor_active()
        nofollow, directory, cloexec = _open_flags()
        pending_fds: list[int] = _OwnedFDCollection()
        try:
            _open_registered_fd(
                pending_fds,
                ".",
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=anchor_fd,
            )
            if self._anchor_relative == ".":
                pass
            else:
                for part in Path(self._anchor_relative).parts:
                    _open_registered_fd(
                        pending_fds,
                        part,
                        os.O_RDONLY | directory | nofollow | cloexec,
                        dir_fd=pending_fds[-1],
                    )
                    previous_fd = pending_fds[-2]
                    _close_registered_fd(
                        pending_fds,
                        previous_fd,
                        f"{self.label} anchor traversal",
                    )
            if _directory_identity(
                pending_fds[-1],
                self.label,
            ) != self.identity:
                raise EvidenceError(
                    f"{self.label} moved outside its fixed trust anchor"
                )
        except EvidenceError:
            raise
        except OSError as exc:
            raise EvidenceError(
                f"{self.label} is no longer reachable under its fixed "
                "trust anchor"
            ) from exc
        finally:
            _close_owned_fds(
                pending_fds,
                f"{self.label} anchor traversal",
            )


@contextmanager
def _trusted_repository_scope(
    root: Path,
    label: str = "repository root",
):
    with TrustedEvidenceRoot(root, label) as repository_root:
        with repository_root.pinned():
            yield repository_root


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _open_flags() -> tuple[int, int, int]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory or os.open not in os.supports_dir_fd:
        raise EvidenceError(
            "filesystem paths cannot be opened safely on this platform"
        )
    return nofollow, directory, cloexec


def _canonical_absolute_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    if absolute.anchor != os.sep:
        raise EvidenceError(f"path must be absolute under the filesystem root: {path}")
    parts = list(absolute.parts)
    if (
        sys.platform == "darwin"
        and len(parts) > 1
        and parts[1] in {"etc", "tmp", "var"}
    ):
        alias = Path(os.sep) / parts[1]
        expected = Path("/private") / parts[1]
        if alias.is_symlink() and Path(os.path.realpath(alias)) == expected:
            parts[1:2] = ["private", parts[1]]
    return Path(*parts)


def _open_absolute_directory(path: Path, label: str) -> int:
    absolute = _canonical_absolute_path(path)
    nofollow, directory, cloexec = _open_flags()
    opened_fds: list[int] = _OwnedFDCollection()
    try:
        _open_registered_fd(
            opened_fds,
            absolute.anchor,
            os.O_RDONLY | directory | nofollow | cloexec,
        )
        for part in absolute.parts[1:]:
            _open_registered_fd(
                opened_fds,
                part,
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=opened_fds[-1],
            )
        opened = os.fstat(opened_fds[-1])
        if not stat.S_ISDIR(opened.st_mode):
            raise EvidenceError(f"{label} must be a directory: {absolute}")
    except BaseException as exc:
        _close_owned_fds(opened_fds, f"{label} directory")
        if isinstance(exc, OSError):
            raise EvidenceError(
                f"{label} could not be opened safely; symbolic link or "
                f"non-directory path component rejected: "
                f"{absolute}: {exc}"
            ) from exc
        raise
    directory_fd = opened_fds[-1]
    ancestor_fds = opened_fds[:-1]
    close_failures: list[tuple[int, BaseException]] = []
    for ancestor_fd in ancestor_fds:
        try:
            _close_registered_fd(
                opened_fds,
                ancestor_fd,
                f"{label} ancestor directory",
            )
        except BaseException as close_error:
            close_failures.append((ancestor_fd, close_error))
    if close_failures:
        try:
            _close_registered_fd(
                opened_fds,
                directory_fd,
                f"{label} directory",
            )
        except BaseException as close_error:
            close_failures.append((directory_fd, close_error))
        error = EvidenceError(
            f"{label} directory descriptors could not be closed safely: "
            f"{absolute}"
        )
        for failed_fd, close_error in close_failures:
            _append_exception_note(
                error,
                f"directory descriptor {failed_fd} close failed: "
                f"{close_error}",
            )
        raise error from close_failures[0][1]
    try:
        opened_fds.abandon_anchor(directory_fd)
    except BaseException as transfer_error:
        try:
            _close_owned_fds(
                opened_fds,
                f"{label} directory transfer",
            )
        except BaseException as cleanup_error:
            _append_exception_note(
                transfer_error,
                f"directory transfer cleanup failed: {cleanup_error}",
            )
        raise
    return directory_fd


def _open_absolute_directory_owned(
    opened_fds: list[int],
    path: Path,
    label: str,
) -> int:
    with _defer_keyboard_interrupt():
        directory_fd = _open_absolute_directory(path, label)
        if not isinstance(opened_fds, _OwnedFDCollection):
            raise EvidenceError(
                "FD operations require an ownership-tracked collection"
            )
        opened_fds.register(directory_fd)
    return directory_fd


def _directory_identity(directory_fd: int, label: str) -> tuple[int, int]:
    opened = os.fstat(directory_fd)
    if not stat.S_ISDIR(opened.st_mode):
        raise EvidenceError(f"{label} must be a directory")
    return opened.st_dev, opened.st_ino


def _assert_trusted_directory_identity(
    directory_fd: int,
    path: Path,
    label: str,
) -> None:
    canonical = _canonical_absolute_path(path)
    expected = _TRUSTED_DIRECTORY_IDENTITIES.get().get(str(canonical))
    if expected is None:
        return
    observed = _directory_identity(directory_fd, label)
    if observed != expected:
        raise EvidenceError(
            f"{label} directory identity changed during validation: {canonical}"
        )


def _assert_trusted_directory_path(path: Path, label: str) -> None:
    opened_fds: list[int] = _OwnedFDCollection()
    try:
        directory_fd = _open_absolute_directory_owned(
            opened_fds,
            path,
            label,
        )
        _assert_trusted_directory_identity(directory_fd, path, label)
    finally:
        _close_owned_fds(opened_fds, f"{label} directory")


def _popen_in_trusted_cwd(
    root: Path,
    arguments: list[str],
    label: str,
    **kwargs: Any,
) -> subprocess.Popen:
    """Start a child in the directory inode selected before the process."""
    if os.name != "posix" or not hasattr(os, "fchdir"):
        raise EvidenceError(
            f"{label} cannot pin a child working directory on this platform"
        )
    if "cwd" in kwargs:
        raise EvidenceError(f"{label} must not use a path-based child cwd")
    opened_fds: list[int] = _OwnedFDCollection()
    try:
        root_fd = _open_absolute_directory_owned(
            opened_fds,
            root,
            label,
        )
        _assert_trusted_directory_identity(root_fd, root, label)

        def enter_pinned_directory() -> None:
            os.fchdir(root_fd)
            os.set_inheritable(root_fd, False)

        return subprocess.Popen(
            arguments,
            preexec_fn=enter_pinned_directory,
            pass_fds=(root_fd,),
            close_fds=True,
            **kwargs,
        )
    finally:
        _close_owned_fds(opened_fds, f"{label} directory")


def _run_subprocess_in_trusted_cwd(
    root: Path,
    arguments: list[str],
    label: str,
    *,
    text: bool = False,
) -> subprocess.CompletedProcess:
    process = _popen_in_trusted_cwd(
        root,
        arguments,
        label,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    stdout, stderr = process.communicate()
    return subprocess.CompletedProcess(
        arguments,
        process.returncode,
        stdout,
        stderr,
    )


def _protected_evidence_root(path: Path, label: str) -> Path:
    canonical = _canonical_absolute_path(path)
    _assert_trusted_directory_path(canonical, label)
    return canonical


@contextmanager
def _pin_directory_identity(path: Path, directory_fd: int):
    canonical = _canonical_absolute_path(path)
    identities = dict(_TRUSTED_DIRECTORY_IDENTITIES.get())
    identities[str(canonical)] = _directory_identity(
        directory_fd,
        "trusted directory",
    )
    token = _TRUSTED_DIRECTORY_IDENTITIES.set(identities)
    try:
        yield
    finally:
        _TRUSTED_DIRECTORY_IDENTITIES.reset(token)


def _read_open_regular_file(
    file_fd: int,
    path: Path,
    label: str,
    limit: int,
) -> bytes:
    opened = os.fstat(file_fd)
    if not stat.S_ISREG(opened.st_mode):
        raise EvidenceError(f"{label} must be a regular file: {path}")
    if opened.st_size > limit:
        raise EvidenceError(f"{label} exceeds the {limit}-byte limit: {path}")
    chunks = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(file_fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > limit:
        raise EvidenceError(f"{label} exceeds the {limit}-byte limit: {path}")
    return data


def _read_relative_regular_file(
    root_fd: int,
    relative: str,
    path: Path,
    label: str,
    *,
    max_bytes: int,
) -> bytes:
    nofollow, directory, cloexec = _open_flags()
    relative = _validate_relative_candidate_path(relative, label)
    parts = Path(relative).parts
    pending_fds: list[int] = _OwnedFDCollection()
    try:
        _open_registered_fd(
            pending_fds,
            ".",
            os.O_RDONLY | directory | nofollow | cloexec,
            dir_fd=root_fd,
        )
        for part in parts[:-1]:
            _open_registered_fd(
                pending_fds,
                part,
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=pending_fds[-1],
            )
            previous_fd = pending_fds[-2]
            _close_registered_fd(
                pending_fds,
                previous_fd,
                f"{label} traversal",
            )
        _open_registered_fd(
            pending_fds,
            parts[-1],
            os.O_RDONLY | nofollow | cloexec,
            dir_fd=pending_fds[-1],
        )
        return _read_open_regular_file(
            pending_fds[-1],
            path,
            label,
            max_bytes,
        )
    except EvidenceError:
        raise
    except OSError as exc:
        raise EvidenceError(
            f"{label} could not be read safely; symbolic link or "
            f"invalid path component rejected: {path}: {exc}"
        ) from exc
    finally:
        _close_owned_fds(pending_fds, f"{label} resource")


def _stat_relative_regular_file(
    root_fd: int,
    relative: str,
    path: Path,
    label: str,
) -> os.stat_result:
    """Stat a regular file by walking every component without symlinks."""
    nofollow, directory, cloexec = _open_flags()
    relative = _validate_relative_candidate_path(relative, label)
    parts = Path(relative).parts
    pending_fds: list[int] = _OwnedFDCollection()
    try:
        _open_registered_fd(
            pending_fds,
            ".",
            os.O_RDONLY | directory | nofollow | cloexec,
            dir_fd=root_fd,
        )
        for part in parts[:-1]:
            _open_registered_fd(
                pending_fds,
                part,
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=pending_fds[-1],
            )
            previous_fd = pending_fds[-2]
            _close_registered_fd(
                pending_fds,
                previous_fd,
                f"{label} traversal",
            )
        _open_registered_fd(
            pending_fds,
            parts[-1],
            os.O_RDONLY | nofollow | cloexec,
            dir_fd=pending_fds[-1],
        )
        opened = os.fstat(pending_fds[-1])
        if not stat.S_ISREG(opened.st_mode):
            raise EvidenceError(f"{label} is not a regular file: {path}")
        return opened
    except EvidenceError:
        raise
    except OSError as exc:
        raise EvidenceError(
            f"{label} could not be inspected safely; symbolic link or "
            f"invalid path component rejected: {path}: {exc}"
        ) from exc
    finally:
        _close_owned_fds(pending_fds, f"{label} resource")


def _enumerate_bundle_files(
    root_fd: int,
    manifest_relative: str,
) -> set[str]:
    nofollow, directory, cloexec = _open_flags()
    discovered: set[str] = set()
    seen_directories: set[tuple[int, int]] = {
        _directory_identity(root_fd, "bundle root")
    }
    directory_count = 0

    def walk(directory_fd: int, prefix: str) -> None:
        nonlocal directory_count
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise EvidenceError(
                "bundle directory could not be enumerated safely"
            ) from exc
        for name in names:
            safe_name = _validate_relative_candidate_path(
                name,
                "bundle entry",
            )
            relative = f"{prefix}{safe_name}"
            try:
                opened = os.stat(
                    safe_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise EvidenceError(
                    f"bundle entry could not be inspected: {relative}"
                ) from exc
            if stat.S_ISLNK(opened.st_mode):
                raise EvidenceError(
                    f"bundle contains a symbolic link: {relative}"
                )
            if stat.S_ISDIR(opened.st_mode):
                identity = (opened.st_dev, opened.st_ino)
                if identity in seen_directories:
                    raise EvidenceError(
                        f"bundle directory alias or cycle: {relative}"
                    )
                seen_directories.add(identity)
                directory_count += 1
                if directory_count > MAX_BUNDLE_FILES:
                    raise EvidenceError(
                        f"bundle contains more than {MAX_BUNDLE_FILES} directories"
                    )
                child_fds: list[int] = _OwnedFDCollection()
                try:
                    _open_registered_fd(
                        child_fds,
                        safe_name,
                        os.O_RDONLY | directory | nofollow | cloexec,
                        dir_fd=directory_fd,
                    )
                    walk(child_fds[-1], f"{relative}/")
                except EvidenceError:
                    raise
                except OSError as exc:
                    raise EvidenceError(
                        f"bundle directory could not be opened safely: {relative}"
                    ) from exc
                finally:
                    _close_owned_fds(
                        child_fds,
                        "bundle child directory",
                    )
                continue
            if not stat.S_ISREG(opened.st_mode):
                raise EvidenceError(
                    f"bundle contains an unsupported file type: {relative}"
                )
            if relative == manifest_relative:
                continue
            identity = _canonical_path_identity(relative, "bundle file")
            if identity in discovered:
                raise EvidenceError(
                    f"bundle contains a duplicate or aliased file path: {relative}"
                )
            discovered.add(identity)
            if len(discovered) > MAX_BUNDLE_FILES:
                raise EvidenceError(
                    f"bundle contains more than {MAX_BUNDLE_FILES} files"
                )

    walk(root_fd, "")
    return discovered


def _read_regular_file(
    path: Path,
    label: str,
    *,
    max_bytes: int | None = None,
    root: Path | None = None,
) -> bytes:
    limit = MAX_EVIDENCE_FILE_BYTES if max_bytes is None else max_bytes
    if root is None:
        base = _canonical_absolute_path(path.parent)
        relative = _validate_relative_candidate_path(path.name, label)
    else:
        base = _canonical_absolute_path(root)
        absolute_path = _canonical_absolute_path(path)
        try:
            relative_path = absolute_path.relative_to(base)
        except ValueError as exc:
            raise EvidenceError(
                f"{label} path escapes its root: {path}"
            ) from exc
        relative = _validate_relative_candidate_path(
            relative_path.as_posix(),
            label,
        )
    root_fds: list[int] = _OwnedFDCollection()
    try:
        root_fd = _open_absolute_directory_owned(
            root_fds,
            base,
            f"{label} root",
        )
        _assert_trusted_directory_identity(root_fd, base, f"{label} root")
        return _read_relative_regular_file(
            root_fd,
            relative,
            path,
            label,
            max_bytes=limit,
        )
    finally:
        _close_owned_fds(root_fds, f"{label} root")


def sha256_file(path: Path, *, root: Path | None = None) -> str:
    return sha256_bytes(
        _read_regular_file(path, "evidence file", root=root)
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"


def _parse_json_object_bytes(raw: bytes, path: Path) -> dict[str, Any]:
    class DuplicateKeyError(ValueError):
        pass

    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateKeyError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def validate_depth(value: Any) -> None:
        stack = [(value, 1)]
        while stack:
            item, depth = stack.pop()
            if depth > MAX_JSON_DEPTH:
                raise DuplicateKeyError(
                    f"JSON nesting exceeds {MAX_JSON_DEPTH}"
                )
            if isinstance(item, dict):
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)

    def reject_constant(value: str) -> Any:
        raise DuplicateKeyError(f"non-finite JSON number: {value}")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise DuplicateKeyError(f"non-finite JSON number: {value}")
        return parsed

    try:
        text = raw.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=no_duplicate_keys,
            parse_float=finite_float,
            parse_constant=reject_constant,
        )
        validate_depth(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateKeyError) as exc:
        raise EvidenceError(f"invalid JSON evidence: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceError(f"JSON evidence must be an object: {path}")
    return payload


def read_json(path: Path, *, root: Path | None = None) -> dict[str, Any]:
    raw = _read_regular_file(
        path,
        "JSON evidence",
        max_bytes=MAX_JSON_EVIDENCE_BYTES,
        root=root,
    )
    return _parse_json_object_bytes(raw, path)


def _is_excluded_candidate_path(relative: str) -> bool:
    path = Path(relative)
    folded_parts = [part.casefold() for part in path.parts]
    if any(part in CANDIDATE_EXCLUDED_DIRS for part in folded_parts):
        return True
    if any(
        part == ".env" or part.startswith(".env.")
        for part in folded_parts
    ):
        return True
    if path.name.casefold() in CANDIDATE_EXCLUDED_FILES:
        return True
    if path.name.casefold() in SECRET_LIKE_NAMES:
        return True
    return path.suffix.casefold() in (
        CANDIDATE_EXCLUDED_SUFFIXES | SECRET_LIKE_SUFFIXES
    )


def _run_git_process(
    root: Path,
    arguments: list[str],
) -> subprocess.CompletedProcess[bytes]:
    completed = _run_subprocess_in_trusted_cwd(
        root,
        ["git", *arguments],
        "Git repository root",
    )
    _assert_trusted_directory_path(root, "Git repository root")
    return completed


def _candidate_file_paths(root: Path) -> list[str]:
    tracked_process = _run_git_process(
        root,
        [
            "ls-files",
            "-z",
            "--",
            ".",
            ":(exclude).idea",
        ],
    )
    if tracked_process.returncode != 0:
        raise EvidenceError(
            "Git candidate enumeration failed: "
            + tracked_process.stderr.decode("utf-8", errors="replace").strip()
        )
    untracked_process = _run_git_process(
        root,
        [
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ".",
            ":(exclude).idea",
        ],
    )
    if untracked_process.returncode != 0:
        raise EvidenceError(
            "Git candidate enumeration failed: "
            + untracked_process.stderr.decode(
                "utf-8",
                errors="replace",
            ).strip()
        )
    tracked = tracked_process.stdout.decode("utf-8").split("\0")
    untracked = untracked_process.stdout.decode("utf-8").split("\0")
    paths: set[str] = set()
    for raw in [*tracked, *untracked]:
        if not raw:
            continue
        relative = _validate_relative_candidate_path(
            raw,
            "candidate",
            allow_protected=True,
        )
        if _is_excluded_candidate_path(relative):
            continue
        if relative in paths:
            raise EvidenceError(
                f"Git candidate enumeration repeated a path: {relative}"
            )
        paths.add(relative)
    return sorted(paths)


def _relevant_code_files(root: Path) -> Iterable[Path]:
    for relative in _candidate_file_paths(root):
        yield root / relative


def candidate_scope_description() -> dict[str, Any]:
    return {
        "policy": (
            "all tracked and untracked non-ignored repository files, "
            "excluding explicit runtime/cache/secret-like paths"
        ),
        "excluded_directories": sorted(CANDIDATE_EXCLUDED_DIRS),
        "excluded_files": sorted(CANDIDATE_EXCLUDED_FILES),
        "excluded_suffixes": sorted(CANDIDATE_EXCLUDED_SUFFIXES),
        "protected_dotenv_names": [".env", ".env.*"],
        "protected_secret_suffixes": sorted(SECRET_LIKE_SUFFIXES),
        "protected_secret_names": sorted(SECRET_LIKE_NAMES),
        "unknown_extension_policy": "include",
    }


def code_fingerprint(root: Path) -> str:
    root = _canonical_absolute_path(root)
    root_fds: list[int] = _OwnedFDCollection()
    digest = hashlib.sha256()
    try:
        root_fd = _open_absolute_directory_owned(
            root_fds,
            root,
            "candidate root",
        )
        _assert_trusted_directory_identity(root_fd, root, "candidate root")
        with _pin_directory_identity(
            root,
            root_fd,
        ):
            for path in _relevant_code_files(root):
                relative_text = path.relative_to(root).as_posix()
                data = _read_relative_regular_file(
                    root_fd,
                    relative_text,
                    path,
                    "candidate source",
                    max_bytes=MAX_EVIDENCE_FILE_BYTES,
                )
                digest.update(relative_text.encode("utf-8"))
                digest.update(b"\0")
                digest.update(str(len(data)).encode("ascii"))
                digest.update(b"\0")
                digest.update(data)
            _assert_trusted_directory_identity(
                root_fd,
                root,
                "candidate root",
            )
        return digest.hexdigest()
    finally:
        _close_owned_fds(root_fds, "candidate root")


def _capture_candidate_file_bytes(
    root: Path,
    paths: Iterable[str],
    *,
    captured_metadata: dict[
        str,
        tuple[int, int, int, int, int, int],
    ] | None = None,
) -> dict[str, bytes]:
    root = _canonical_absolute_path(root)
    captured: dict[str, bytes] = {}
    root_fds: list[int] = _OwnedFDCollection()
    try:
        root_fd = _open_absolute_directory_owned(
            root_fds,
            root,
            "candidate capture root",
        )
        _assert_trusted_directory_identity(
            root_fd,
            root,
            "candidate capture root",
        )
        with _pin_directory_identity(root, root_fd):
            for relative in sorted(set(paths)):
                safe_relative = _validate_relative_candidate_path(
                    relative,
                    "candidate capture",
                )
                before = _stat_relative_regular_file(
                    root_fd,
                    safe_relative,
                    root / safe_relative,
                    "candidate capture",
                )
                if stat.S_ISLNK(before.st_mode):
                    raise EvidenceError(
                        f"candidate capture contains a symbolic link: "
                        f"{safe_relative}"
                    )
                if not stat.S_ISREG(before.st_mode):
                    raise EvidenceError(
                        f"candidate capture is not a regular file: "
                        f"{safe_relative}"
                    )
                data = _read_relative_regular_file(
                    root_fd,
                    safe_relative,
                    root / safe_relative,
                    "candidate capture",
                    max_bytes=MAX_EVIDENCE_FILE_BYTES,
                )
                after = _stat_relative_regular_file(
                    root_fd,
                    safe_relative,
                    root / safe_relative,
                    "candidate capture",
                )
                if stat.S_ISLNK(after.st_mode):
                    raise EvidenceError(
                        "candidate file became a symbolic link during "
                        f"capture: {safe_relative}"
                    )
                before_identity = (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                after_identity = (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                if before_identity != after_identity:
                    raise EvidenceError(
                        "candidate file changed during capture: "
                        f"{safe_relative}"
                    )
                captured[safe_relative] = data
                if captured_metadata is not None:
                    captured_metadata[safe_relative] = after_identity
            _assert_trusted_directory_identity(
                root_fd,
                root,
                "candidate capture root",
            )
    finally:
        _close_owned_fds(root_fds, "candidate capture root")
    return captured


def _verify_captured_candidate_generation(
    root: Path,
    captured: Mapping[str, bytes],
    captured_metadata: Mapping[
        str,
        tuple[int, int, int, int, int, int],
    ],
) -> None:
    """Reject captured bytes that are not stable through a final full pass."""
    root = _canonical_absolute_path(root)
    if set(captured) != set(captured_metadata):
        raise EvidenceError(
            "candidate capture metadata does not match captured files"
        )
    root_fds: list[int] = _OwnedFDCollection()
    try:
        root_fd = _open_absolute_directory_owned(
            root_fds,
            root,
            "candidate generation root",
        )
        _assert_trusted_directory_identity(
            root_fd,
            root,
            "candidate generation root",
        )
        with _pin_directory_identity(root, root_fd):
            for relative in sorted(captured):
                before = _stat_relative_regular_file(
                    root_fd,
                    relative,
                    root / relative,
                    "candidate generation verification",
                )
                before_identity = (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before_identity != captured_metadata[relative]
                ):
                    raise EvidenceError(
                        "candidate capture generation changed before "
                        f"verification: {relative}"
                    )
                data = _read_relative_regular_file(
                    root_fd,
                    relative,
                    root / relative,
                    "candidate generation verification",
                    max_bytes=MAX_EVIDENCE_FILE_BYTES,
                )
                if data != captured[relative]:
                    raise EvidenceError(
                        "candidate capture bytes changed before "
                        f"verification: {relative}"
                    )
                after = _stat_relative_regular_file(
                    root_fd,
                    relative,
                    root / relative,
                    "candidate generation verification",
                )
                after_identity = (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                if (
                    not stat.S_ISREG(after.st_mode)
                    or after_identity != before_identity
                ):
                    raise EvidenceError(
                        "candidate file changed during final generation "
                        f"verification: {relative}"
                    )
            for relative in sorted(captured):
                current = _stat_relative_regular_file(
                    root_fd,
                    relative,
                    root / relative,
                    "candidate generation verification",
                )
                current_identity = (
                    current.st_dev,
                    current.st_ino,
                    current.st_mode,
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                )
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current_identity != captured_metadata[relative]
                ):
                    raise EvidenceError(
                        "candidate capture generation changed before "
                        f"final verification completed: {relative}"
                    )
            _assert_trusted_directory_identity(
                root_fd,
                root,
                "candidate generation root",
            )
    except OSError as exc:
        raise EvidenceError(
            "candidate generation verification failed safely because a "
            "path changed or became unavailable"
        ) from exc
    finally:
        _close_owned_fds(root_fds, "candidate generation root")


def _code_fingerprint_from_captured(
    captured: Mapping[str, bytes],
) -> str:
    digest = hashlib.sha256()
    for relative in sorted(captured):
        data = captured[relative]
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
    return digest.hexdigest()


def _file_entries_from_captured(
    paths: Iterable[str],
    captured: Mapping[str, bytes],
) -> list[dict[str, str]]:
    entries = []
    identities: dict[str, str] = {}
    for relative in sorted(set(paths)):
        safe_relative = _validate_relative_candidate_path(
            relative,
            "candidate",
        )
        if safe_relative not in captured:
            raise EvidenceError(
                f"candidate capture is missing: {safe_relative}"
            )
        identity = _canonical_path_identity(safe_relative, "candidate")
        if identity in identities:
            raise EvidenceError(
                "candidate path alias is duplicated: "
                f"{identities[identity]} and {safe_relative}"
            )
        identities[identity] = safe_relative
        entries.append(
            {
                "path": safe_relative,
                "sha256": sha256_bytes(captured[safe_relative]),
            }
        )
    return entries


def _candidate_change_records(
    root: Path,
) -> tuple[list[str], list[dict[str, str]]]:
    status_process = _run_git_process(
        root,
        [
            "diff",
            "--name-status",
            "-z",
            "--no-renames",
            "HEAD",
            "--",
            ".",
            ":(exclude).idea",
            ":(exclude)artifacts",
        ],
    )
    if status_process.returncode != 0:
        raise EvidenceError(
            "Git working-tree candidate status failed: "
            + status_process.stderr.decode("utf-8", errors="replace").strip()
        )
    status_output = status_process.stdout.decode("utf-8")
    tokens = status_output.split("\0")
    tracked: set[str] = set()
    excluded_changes: list[dict[str, str]] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        if not status:
            index += 1
            continue
        if index + 1 >= len(tokens):
            raise EvidenceError("Git diff status is truncated")
        path = tokens[index + 1]
        index += 2
        code = status[0]
        safe_path = _validate_relative_candidate_path(
            path,
            "working-tree candidate",
            allow_protected=True,
        )
        if _is_excluded_candidate_path(safe_path):
            excluded_changes.append({"path": safe_path, "status": code})
            continue
        if code == "D":
            raise EvidenceError(
                f"working-tree candidate deletes unsupported file: {safe_path}"
            )
        if code == "R":
            raise EvidenceError(
                f"working-tree candidate renames unsupported file: {safe_path}"
            )
        if code == "T":
            raise EvidenceError(
                f"working-tree candidate changes file type: {safe_path}"
            )
        if code not in {"A", "M"}:
            raise EvidenceError(
                f"working-tree candidate has unsupported Git status "
                f"{status}: {safe_path}"
            )
        tracked.add(safe_path)
    untracked_process = _run_git_process(
        root,
        [
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ".",
            ":(exclude).idea",
            ":(exclude)artifacts",
        ],
    )
    if untracked_process.returncode != 0:
        raise EvidenceError(
            "Git untracked candidate enumeration failed: "
            + untracked_process.stderr.decode(
                "utf-8",
                errors="replace",
            ).strip()
        )
    untracked = untracked_process.stdout.decode("utf-8").split("\0")
    for path in untracked:
        if not path:
            continue
        safe_path = _validate_relative_candidate_path(
            path,
            "untracked candidate",
            allow_protected=True,
        )
        if _is_excluded_candidate_path(safe_path):
            excluded_changes.append({"path": safe_path, "status": "??"})
        else:
            tracked.add(safe_path)
    return (
        sorted(tracked),
        sorted(excluded_changes, key=lambda item: item["path"]),
    )


def changed_relevant_files(root: Path) -> list[str]:
    files, _excluded = _candidate_change_records(root)
    return files


def _git_text(root: Path, arguments: list[str], *, check: bool = True) -> str:
    completed_bytes = _run_git_process(root, arguments)
    completed = subprocess.CompletedProcess(
        completed_bytes.args,
        completed_bytes.returncode,
        completed_bytes.stdout.decode("utf-8"),
        completed_bytes.stderr.decode("utf-8"),
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise EvidenceError(
            f"Git command failed ({' '.join(arguments)}): {detail}"
        )
    return completed.stdout


def _resolve_commit(root: Path, value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{label} must be a non-empty Git revision")
    resolved = _git_text(
        root,
        ["rev-parse", "--verify", f"{value.strip()}^{{commit}}"],
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise EvidenceError(f"{label} did not resolve to a Git commit")
    return resolved


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    completed = _run_git_process(
        root,
        ["merge-base", "--is-ancestor", ancestor, descendant],
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    detail = (
        completed.stderr.decode("utf-8", errors="replace").strip()
        or completed.stdout.decode("utf-8", errors="replace").strip()
    )
    raise EvidenceError(f"could not verify Git ancestry: {detail}")


def _validate_relative_candidate_path(
    relative: Any,
    label: str,
    *,
    allow_protected: bool = False,
) -> str:
    if not isinstance(relative, str) or not relative:
        raise EvidenceError(f"{label} path must be a non-empty string")
    if "\\" in relative:
        raise EvidenceError(
            f"{label} path must use forward slashes: {relative}"
        )
    if "\x00" in relative or relative in {".", ".."}:
        raise EvidenceError(f"{label} path escapes the repository: {relative}")
    relative = unicodedata.normalize("NFC", relative)
    path = Path(relative)
    if path.is_absolute() or any(part in {"..", ""} for part in path.parts):
        raise EvidenceError(f"{label} path escapes the repository: {relative}")
    parts = path.parts
    folded = [unicodedata.normalize("NFC", part).casefold() for part in parts]
    if not allow_protected and (
        ".idea" in folded
        or any(
            part == ".env" or part.startswith(".env.")
            for part in folded
        )
    ):
        raise EvidenceError(
            f"{label} path is protected from candidate evidence: {relative}"
        )
    file_name = folded[-1]
    if not allow_protected and (
        file_name in SECRET_LIKE_NAMES
        or path.suffix.casefold() in SECRET_LIKE_SUFFIXES
    ):
        raise EvidenceError(
            f"{label} path is secret-like and cannot be read: {relative}"
        )
    return path.as_posix()


def _canonical_path_identity(relative: Any, label: str) -> str:
    normalized = _validate_relative_candidate_path(relative, label)
    return unicodedata.normalize("NFC", normalized).casefold()


def _assert_no_symlink_components(
    root: Path,
    path: Path,
    label: str,
) -> None:
    root = _canonical_absolute_path(root)
    absolute_path = _canonical_absolute_path(path)
    try:
        relative = absolute_path.relative_to(root)
    except ValueError as exc:
        raise EvidenceError(f"{label} path escapes its root: {path}") from exc
    _assert_trusted_directory_path(root, f"{label} root")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise EvidenceError(
                f"{label} path traverses a symbolic link: "
                f"{relative.as_posix()}"
            )


def _reject_unknown_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    label: str,
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise EvidenceError(
            f"{label} contains unknown fields: {sorted(unknown)}"
        )


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise EvidenceError(f"{label} must be a lowercase SHA-256")
    return value


def _require_git_oid(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise EvidenceError(f"{label} must be a lowercase Git commit ID")
    return value


def _validate_previous_history(
    summary: Mapping[str, Any],
    label: str,
) -> list[dict[str, str]]:
    history = summary.get("evidence_history")
    if not isinstance(history, list) or not history:
        raise EvidenceError(f"{label} has no historical evidence chain")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(history):
        if not isinstance(item, Mapping):
            raise EvidenceError(f"{label}.evidence_history[{index}] is invalid")
        _reject_unknown_fields(
            item,
            {"previous_sha256", "replacement_reason"},
            f"{label}.evidence_history[{index}]",
        )
        previous_sha256 = _require_sha256(
            item.get("previous_sha256"),
            f"{label}.evidence_history[{index}].previous_sha256",
        )
        reason = item.get("replacement_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise EvidenceError(
                f"{label}.evidence_history[{index}] has no replacement reason"
            )
        if previous_sha256 in seen:
            raise EvidenceError(
                f"{label}.evidence_history contains duplicate history"
            )
        seen.add(previous_sha256)
        normalized.append(
            {
                "previous_sha256": previous_sha256,
                "replacement_reason": reason,
            }
        )

    superseded = summary.get("superseded_summary")
    if not isinstance(superseded, Mapping):
        raise EvidenceError(f"{label} has no superseded summary record")
    _reject_unknown_fields(
        superseded,
        {"previous_sha256", "replacement_reason"},
        f"{label}.superseded_summary",
    )
    previous_sha256 = _require_sha256(
        superseded.get("previous_sha256"),
        f"{label}.superseded_summary.previous_sha256",
    )
    replacement_reason = superseded.get("replacement_reason")
    if not isinstance(replacement_reason, str) or not replacement_reason.strip():
        raise EvidenceError(
            f"{label}.superseded_summary has no replacement reason"
        )
    if previous_sha256 not in seen:
        raise EvidenceError(
            f"{label} superseded summary is absent from evidence history"
        )
    if normalized[-1] != {
        "previous_sha256": previous_sha256,
        "replacement_reason": replacement_reason,
    }:
        raise EvidenceError(
            f"{label} evidence history is not in superseded order"
        )
    return normalized


def _validate_previous_tests(
    tests: Any,
    label: str,
    *,
    require_discovery: bool,
) -> None:
    if not isinstance(tests, Mapping):
        raise EvidenceError(f"{label} tests must be an object")
    _reject_unknown_fields(
        tests,
        {"selection", "system_python", "venv_python"},
        f"{label}.tests",
    )
    selection = tests.get("selection")
    if not isinstance(selection, Mapping):
        raise EvidenceError(f"{label} has no test selection policy")
    _reject_unknown_fields(
        selection,
        {"blocked_terms", "blocked_modules"},
        f"{label}.tests.selection",
    )
    if selection.get("blocked_terms") != list(BLOCKED_TEST_TERMS):
        raise EvidenceError(f"{label} blocked test terms have changed")
    if selection.get("blocked_modules") != list(BLOCKED_TEST_MODULES):
        raise EvidenceError(f"{label} blocked test modules have changed")
    count_fields = {
        "discovered",
        "selected",
        "filtered",
        "passed",
        "failed",
        "errors",
        "skipped",
        "expected_failures",
        "unexpected_successes",
    }
    for name in ("system_python", "venv_python"):
        evidence = tests.get(name)
        if not isinstance(evidence, Mapping):
            raise EvidenceError(f"{label} has no {name} test evidence")
        allowed = count_fields | {
            "python",
            "result",
            "result_sha256",
            "log",
            "log_sha256",
            "discovery_sha256",
            "selection_rules_version",
            "selection_rules_sha256",
        }
        _reject_unknown_fields(evidence, allowed, f"{label}.tests.{name}")
        counts = {field: evidence.get(field) for field in count_fields}
        _validate_counts(counts, f"{label}.tests.{name}")
        for field in ("python", "result", "log"):
            if not isinstance(evidence.get(field), str) or not evidence[field]:
                raise EvidenceError(
                    f"{label}.tests.{name}.{field} is invalid"
                )
        for field in ("result_sha256", "log_sha256"):
            _require_sha256(
                evidence.get(field),
                f"{label}.tests.{name}.{field}",
            )
        if require_discovery:
            if evidence.get("selection_rules_version") != (
                TEST_SELECTION_RULES_VERSION
            ):
                raise EvidenceError(
                    f"{label}.tests.{name} selection rules are unsupported"
                )
            if evidence.get("selection_rules_sha256") != (
                _test_selection_rules_sha256()
            ):
                raise EvidenceError(
                    f"{label}.tests.{name} selection rules hash is invalid"
                )
            _require_sha256(
                evidence.get("discovery_sha256"),
                f"{label}.tests.{name}.discovery_sha256",
            )


def _validate_previous_mutations(
    mutations: Any,
    label: str,
) -> None:
    if not isinstance(mutations, Mapping):
        raise EvidenceError(f"{label} mutations must be an object")
    allowed = {
        "declared",
        "executed",
        "filtered",
        "killed",
        "survived",
        "invalid",
        "error",
        "evidence_scopes",
        "cases",
        "cases_sha256",
        "report",
        "report_sha256",
    }
    _reject_unknown_fields(mutations, allowed, f"{label}.mutations")
    integer_fields = (
        "declared",
        "executed",
        "filtered",
        "killed",
        "survived",
        "invalid",
        "error",
    )
    for field in integer_fields:
        if type(mutations.get(field)) is not int or mutations[field] < 0:
            raise EvidenceError(
                f"{label}.mutations.{field} must be a non-negative integer"
            )
    if mutations["executed"] + mutations["filtered"] != mutations["declared"]:
        raise EvidenceError(f"{label} mutation declared/executed counts disagree")
    if sum(
        mutations[field]
        for field in ("killed", "survived", "invalid", "error")
    ) != mutations["executed"]:
        raise EvidenceError(f"{label} mutation result counts disagree")
    if (
        mutations["killed"] != mutations["executed"]
        or any(mutations[field] for field in ("survived", "invalid", "error"))
    ):
        raise EvidenceError(f"{label} is not a successful mutation record")
    scopes = mutations.get("evidence_scopes")
    if not isinstance(scopes, Mapping):
        raise EvidenceError(f"{label} has no mutation evidence scopes")
    _reject_unknown_fields(
        scopes,
        {"in_process", "static_source_check", "unverified"},
        f"{label}.mutations.evidence_scopes",
    )
    for field in ("in_process", "static_source_check", "unverified"):
        if type(scopes.get(field)) is not int or scopes[field] < 0:
            raise EvidenceError(
                f"{label} mutation evidence scope is invalid: {field}"
            )
    if scopes["unverified"] != 0:
        raise EvidenceError(f"{label} mutation evidence has unverified kills")
    if sum(scopes.values()) != mutations["executed"]:
        raise EvidenceError(f"{label} mutation evidence scope counts disagree")
    for field in ("cases", "report"):
        if not isinstance(mutations.get(field), str) or not mutations[field]:
            raise EvidenceError(
                f"{label}.mutations.{field} is invalid"
            )
    for field in ("cases_sha256", "report_sha256"):
        _require_sha256(
            mutations.get(field),
            f"{label}.mutations.{field}",
        )


def _validate_previous_frozen(
    frozen: Any,
    label: str,
) -> None:
    if not isinstance(frozen, Mapping):
        raise EvidenceError(f"{label} frozen evidence must be an object")
    if set(frozen) != set(FROZEN_EVIDENCE):
        raise EvidenceError(f"{label} frozen evidence set is incomplete")
    for relative, expected in FROZEN_EVIDENCE.items():
        record = frozen.get(relative)
        if not isinstance(record, Mapping):
            raise EvidenceError(
                f"{label} frozen evidence is invalid: {relative}"
            )
        _reject_unknown_fields(
            record,
            {"expected_sha256", "observed_sha256"},
            f"{label}.frozen_evidence[{relative}]",
        )
        if record.get("expected_sha256") != expected:
            raise EvidenceError(
                f"{label} frozen expected hash is invalid: {relative}"
            )
        if record.get("observed_sha256") != expected:
            raise EvidenceError(
                f"{label} frozen observed hash is invalid: {relative}"
            )


def _validate_previous_code(code: Any, label: str) -> None:
    if not isinstance(code, Mapping):
        raise EvidenceError(f"{label} code evidence must be an object")
    _reject_unknown_fields(
        code,
        {"head", "worktree_clean", "status_sha256", "code_fingerprint"},
        f"{label}.code",
    )
    _require_git_oid(code.get("head"), f"{label}.code.head")
    if not isinstance(code.get("worktree_clean"), bool):
        raise EvidenceError(f"{label}.code.worktree_clean must be boolean")
    _require_sha256(code.get("status_sha256"), f"{label}.code.status_sha256")
    _require_sha256(
        code.get("code_fingerprint"),
        f"{label}.code.code_fingerprint",
    )


def _validate_previous_candidate(candidate: Any, label: str) -> None:
    if not isinstance(candidate, Mapping):
        raise EvidenceError(f"{label} candidate identity must be an object")
    allowed = {
        "schema_version",
        "repository",
        "branch",
        "candidate_mode",
        "base_head",
        "target_head",
        "reviewed_head",
        "worktree_clean",
        "status_sha256",
        "code_fingerprint",
        "file_count",
        "file_manifest_sha256",
        "evidence_status",
        "candidate_scope",
        "excluded_changes",
        "files",
        "excluded",
    }
    _reject_unknown_fields(candidate, allowed, f"{label}.candidate_identity")
    if candidate.get("schema_version") != CANDIDATE_SCHEMA_VERSION:
        raise EvidenceError(
            f"{label} historical candidate schema is unsupported"
        )
    if not isinstance(candidate.get("repository"), str) or not candidate[
        "repository"
    ]:
        raise EvidenceError(
            f"{label} historical candidate repository is invalid"
        )
    if not isinstance(candidate.get("branch"), str):
        raise EvidenceError(f"{label} historical candidate branch is invalid")
    if candidate.get("candidate_mode") not in CANDIDATE_MODES:
        raise EvidenceError(f"{label} historical candidate mode is invalid")
    for field in ("target_head", "reviewed_head"):
        if candidate.get(field) is not None:
            _require_git_oid(
                candidate.get(field),
                f"{label}.candidate_identity.{field}",
            )
    if candidate.get("base_head") is not None:
        _require_git_oid(
            candidate.get("base_head"),
            f"{label}.candidate_identity.base_head",
        )
    if candidate.get("evidence_status") != CANDIDATE_IDENTITY_VERIFIED:
        raise EvidenceError(f"{label} historical candidate is not verified")
    if not isinstance(candidate.get("worktree_clean"), bool):
        raise EvidenceError(f"{label} historical candidate clean flag is invalid")
    _require_sha256(
        candidate.get("status_sha256"),
        f"{label}.candidate_identity.status_sha256",
    )
    _require_sha256(
        candidate.get("code_fingerprint"),
        f"{label}.candidate_identity.code_fingerprint",
    )
    if (
        type(candidate.get("file_count")) is not int
        or candidate["file_count"] < 0
    ):
        raise EvidenceError(f"{label} historical candidate file count is invalid")
    files = candidate.get("files")
    if not isinstance(files, list) or len(files) != candidate["file_count"]:
        raise EvidenceError(f"{label} historical candidate files are invalid")
    for index, item in enumerate(files):
        if not isinstance(item, Mapping):
            raise EvidenceError(
                f"{label} historical candidate file[{index}] is invalid"
            )
        _reject_unknown_fields(
            item,
            {"path", "sha256"},
            f"{label}.candidate_identity.files[{index}]",
        )
    if candidate.get("file_manifest_sha256") != file_manifest_sha256(files):
        raise EvidenceError(
            f"{label} historical candidate manifest hash is invalid"
        )
    scope = candidate.get("candidate_scope")
    if not isinstance(scope, Mapping):
        raise EvidenceError(
            f"{label} historical candidate scope is invalid"
        )
    _reject_unknown_fields(
        scope,
        {
            "policy",
            "excluded_directories",
            "excluded_files",
            "excluded_suffixes",
            "protected_dotenv_names",
            "protected_secret_suffixes",
            "protected_secret_names",
            "unknown_extension_policy",
        },
        f"{label}.candidate_identity.candidate_scope",
    )
    if not isinstance(candidate.get("excluded"), list) or not all(
        isinstance(item, str) for item in candidate["excluded"]
    ):
        raise EvidenceError(
            f"{label} historical excluded path policy is invalid"
        )


def _validate_candidate_identity_semantics(
    candidate: Any,
    trusted_root: TrustedEvidenceRoot,
    label: str,
) -> None:
    _validate_previous_candidate(candidate, label)
    if not isinstance(trusted_root, TrustedEvidenceRoot):
        raise EvidenceError(
            f"{label} has no trusted repository root"
        )
    trusted_root.assert_active()
    if candidate.get("repository") != str(trusted_root.path):
        raise EvidenceError(
            f"{label} candidate repository disagrees with trusted root"
        )
    with trusted_root.pinned():
        actual = build_candidate_snapshot(
            trusted_root.path,
            candidate_mode=candidate.get("candidate_mode"),
            base_head=candidate.get("base_head"),
            target_head=candidate.get("target_head"),
            reviewed_head=candidate.get("reviewed_head"),
        )
    if canonical_json(actual) != canonical_json(candidate):
        for field in (
            "repository",
            "target_head",
            "reviewed_head",
            "code_fingerprint",
            "file_count",
            "file_manifest_sha256",
            "status_sha256",
            "files",
        ):
            if actual.get(field) != candidate.get(field):
                raise EvidenceError(
                    f"{label} candidate {field} disagrees with trusted root"
                )
        raise EvidenceError(
            f"{label} candidate identity disagrees with trusted root"
        )
    if candidate.get("file_count", 0) <= 0:
        raise EvidenceError(
            f"{label} candidate identity has no files"
        )


def _validate_previous_review_lane(
    lane: Any,
    key: str,
    expected_status: str,
    required_scope: frozenset[str],
    expected_repository: str | None,
    expected_heads: set[str] | None,
    expected_fingerprint: str | None,
    expected_files: list[Mapping[str, Any]] | None,
    label: str,
) -> list[str]:
    lane_label = f"{label}.special_review.{key}"
    if not isinstance(lane, Mapping):
        raise EvidenceError(f"{label} special {key} lane is missing")
    _reject_unknown_fields(
        lane,
        {
            "status",
            "completed",
            "conclusion",
            "files_reviewed",
            "reviewed_files",
            "reason",
            "report",
            "report_sha256",
            "result",
            "result_sha256",
            "target_repository",
            "target_head",
            "target_code_fingerprint",
            "covered_scope",
            "uncovered_scope",
        },
        lane_label,
    )
    if lane.get("status") != expected_status:
        raise EvidenceError(
            f"{label} special {key} status disagrees with rollup"
        )
    expected_completed = expected_status != "UNAVAILABLE"
    if lane.get("completed") is not expected_completed:
        raise EvidenceError(
            f"{label} special {key} completed state contradicts its status"
        )
    if lane.get("conclusion") != expected_status:
        raise EvidenceError(
            f"{label} special {key} conclusion contradicts its status"
        )
    files_reviewed = lane.get("files_reviewed")
    if type(files_reviewed) is not int or files_reviewed < 0:
        raise EvidenceError(
            f"{label} special {key} files_reviewed is invalid"
        )
    if expected_status == "UNAVAILABLE":
        if files_reviewed != 0:
            raise EvidenceError(
                f"{label} special {key} unavailable lane reviewed files"
            )
        return []
    if files_reviewed <= 0:
        raise EvidenceError(
            f"{label} special {key} completed without reviewed files"
        )
    reviewed_files = lane.get("reviewed_files")
    if (
        not isinstance(reviewed_files, list)
        or len(reviewed_files) != files_reviewed
    ):
        raise EvidenceError(
            f"{label} special {key} reviewed file manifest is invalid"
        )
    normalized_reviewed_files: list[dict[str, str]] = []
    reviewed_identities: set[str] = set()
    for index, item in enumerate(reviewed_files):
        if not isinstance(item, Mapping):
            raise EvidenceError(
                f"{label} special {key} reviewed_files[{index}] is invalid"
            )
        _reject_unknown_fields(
            item,
            {"path", "sha256"},
            f"{lane_label}.reviewed_files[{index}]",
        )
        relative = _validate_relative_candidate_path(
            item.get("path"),
            f"{lane_label}.reviewed_files[{index}].path",
        )
        digest = item.get("sha256")
        _require_sha256(
            digest,
            f"{lane_label}.reviewed_files[{index}].sha256",
        )
        identity = _canonical_path_identity(
            relative,
            f"{lane_label}.reviewed_files[{index}].path",
        )
        if identity in reviewed_identities:
            raise EvidenceError(
                f"{label} special {key} reviewed files contain a path alias"
            )
        reviewed_identities.add(identity)
        normalized_reviewed_files.append(
            {"path": relative, "sha256": str(digest)}
        )
    normalized_reviewed_files.sort(key=lambda item: item["path"])
    if expected_files is not None:
        candidate_files = sorted(
            (
                {"path": item["path"], "sha256": item["sha256"]}
                for item in expected_files
            ),
            key=lambda item: item["path"],
        )
        if normalized_reviewed_files != candidate_files:
            raise EvidenceError(
                f"{label} special {key} reviewed file identity "
                "disagrees with candidate identity"
            )
    report = lane.get("report")
    result = lane.get("result")
    for field, value in (("report", report), ("result", result)):
        _validate_relative_candidate_path(
            value,
            f"{lane_label}.{field}",
        )
        _require_sha256(
            lane.get(f"{field}_sha256"),
            f"{lane_label}.{field}_sha256",
        )
    if report == result:
        raise EvidenceError(
            f"{label} special {key} report and result identities collide"
        )
    target_head = lane.get("target_head")
    _require_git_oid(target_head, f"{lane_label}.target_head")
    if expected_heads is not None and target_head not in expected_heads:
        raise EvidenceError(
            f"{label} special {key} target HEAD is not the reviewed candidate"
        )
    target_fingerprint = lane.get("target_code_fingerprint")
    _require_sha256(
        target_fingerprint,
        f"{lane_label}.target_code_fingerprint",
    )
    if expected_fingerprint is not None and (
        target_fingerprint != expected_fingerprint
    ):
        raise EvidenceError(
            f"{label} special {key} target fingerprint disagrees with candidate"
        )
    target_repository = lane.get("target_repository")
    if not isinstance(target_repository, str) or not target_repository:
        raise EvidenceError(
            f"{label} special {key} target repository is missing"
        )
    if (
        expected_repository is None
        or target_repository != expected_repository
    ):
        raise EvidenceError(
            f"{label} special {key} target repository disagrees with candidate"
        )
    covered_scope = _validate_review_scope_list(
        lane.get("covered_scope"),
        f"{lane_label}.covered_scope",
    )
    uncovered_scope = _validate_review_scope_list(
        lane.get("uncovered_scope"),
        f"{lane_label}.uncovered_scope",
    )
    missing_scope = required_scope - set(covered_scope)
    if missing_scope:
        raise EvidenceError(
            f"{label} special {key} omitted required scope: "
            f"{sorted(missing_scope)}"
        )
    overlap = set(covered_scope) & set(uncovered_scope)
    if overlap:
        raise EvidenceError(
            f"{label} special {key} scope is both covered and uncovered: "
            f"{sorted(overlap)}"
        )
    return uncovered_scope


def _validate_review_summary_semantics(
    review_summary: Mapping[str, Any],
    candidate: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    """Validate the normalized review object consumed by the final gate."""
    if not isinstance(review_summary, Mapping):
        raise EvidenceError(f"{label} review summary must be an object")
    _reject_unknown_fields(
        review_summary,
        {
            "independent_code_review",
            "independent_architecture_review",
            "code_review",
            "architecture_review",
            "uncovered_scope",
            "merge_ready",
            "blocking_reasons",
            "candidate_acceptance",
            "path",
            "sha256",
            "review_clear",
        },
        f"{label}.review_summary",
    )
    code_status = review_summary.get("independent_code_review")
    architecture_status = review_summary.get(
        "independent_architecture_review"
    )
    if code_status not in CODE_REVIEW_STATUSES:
        raise EvidenceError(f"{label} code review status is invalid")
    if architecture_status not in ARCHITECTURE_REVIEW_STATUSES:
        raise EvidenceError(f"{label} architecture review status is invalid")
    expected_heads = {candidate["target_head"]}
    if isinstance(candidate.get("reviewed_head"), str):
        expected_heads.add(candidate["reviewed_head"])
    code_uncovered = _validate_previous_review_lane(
        review_summary.get("code_review"),
        "code_review",
        code_status,
        REQUIRED_CODE_REVIEW_SCOPE,
        candidate.get("repository"),
        expected_heads,
        candidate.get("code_fingerprint"),
        candidate.get("files"),
        label,
    )
    architecture_uncovered = _validate_previous_review_lane(
        review_summary.get("architecture_review"),
        "architecture_review",
        architecture_status,
        REQUIRED_ARCHITECTURE_REVIEW_SCOPE,
        candidate.get("repository"),
        expected_heads,
        candidate.get("code_fingerprint"),
        candidate.get("files"),
        label,
    )
    uncovered_scope = review_summary.get("uncovered_scope")
    expected_uncovered = sorted([
        *(f"code_review:{scope}" for scope in code_uncovered),
        *(
            f"architecture_review:{scope}"
            for scope in architecture_uncovered
        ),
    ])
    if uncovered_scope != expected_uncovered:
        raise EvidenceError(
            f"{label} review summary uncovered scope is inconsistent"
        )
    blocking_reasons = review_summary.get("blocking_reasons")
    if (
        not isinstance(blocking_reasons, list)
        or len(blocking_reasons) != len(set(blocking_reasons))
        or not all(
            isinstance(item, str) and item for item in blocking_reasons
        )
    ):
        raise EvidenceError(
            f"{label} review summary blocking reasons are invalid"
        )
    expected_blocking_reasons = []
    if code_status != "CLEAR":
        expected_blocking_reasons.append(f"code_review:{code_status}")
    if architecture_status != "CLEAR":
        expected_blocking_reasons.append(
            f"architecture_review:{architecture_status}"
        )
    if expected_uncovered:
        expected_blocking_reasons.append("review_uncovered_scope")
    if blocking_reasons != expected_blocking_reasons:
        raise EvidenceError(
            f"{label} review summary blocking reasons are inconsistent"
        )
    if review_summary.get("merge_ready") is not False:
        raise EvidenceError(
            f"{label} review summary cannot self-assert merge readiness"
        )
    review_clear = (
        code_status == "CLEAR"
        and architecture_status == "CLEAR"
        and not expected_uncovered
    )
    if review_summary.get("review_clear") is not review_clear:
        raise EvidenceError(
            f"{label} review summary clear flag is inconsistent"
        )
    acceptance = review_summary.get("candidate_acceptance")
    blocking_review = (
        code_status == "REQUEST_CHANGES"
        or architecture_status in {"WATCH", "BLOCK"}
    )
    allowed_acceptance = (
        {
            "INDEPENDENT_REVIEW_CLEAR",
            "CONTENT_EQUIVALENT_REVIEW_CLEAR",
        }
        if review_clear
        else {
            "INDEPENDENT_REVIEW_BLOCKED",
            "CONTENT_EQUIVALENT_REVIEW_BLOCKED",
        }
        if blocking_review
        else {"INDEPENDENT_REVIEW_PENDING"}
        if "UNAVAILABLE" in {code_status, architecture_status}
        else {
            "INDEPENDENT_REVIEW_BLOCKED",
            "CONTENT_EQUIVALENT_REVIEW_BLOCKED",
        }
    )
    if acceptance not in allowed_acceptance:
        raise EvidenceError(
            f"{label} review summary candidate acceptance is inconsistent"
        )
    _validate_relative_candidate_path(
        review_summary.get("path"),
        f"{label}.review_summary.path",
    )
    _require_sha256(
        review_summary.get("sha256"),
        f"{label}.review_summary.sha256",
    )
    return {
        "code_status": code_status,
        "architecture_status": architecture_status,
        "uncovered_scope": expected_uncovered,
        "review_clear": review_clear,
        "candidate_acceptance": acceptance,
    }


def _validate_review_binding_semantics(
    binding: Mapping[str, Any],
    candidate: Mapping[str, Any],
    provided_review_summary: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    if not isinstance(binding, Mapping):
        raise EvidenceError(f"{label} review binding must be an object")
    _reject_unknown_fields(
        binding,
        {
            "schema_version",
            "generator_version",
            "generated_at",
            "scope",
            "repository",
            "candidate_mode",
            "base_head",
            "reviewed_head",
            "target_head",
            "code_fingerprint",
            "file_count",
            "reviewed_file_manifest_sha256",
            "target_file_manifest_sha256",
            "reviewed_files",
            "target_files",
            "review_manifest",
            "review_lanes",
            "review_status",
            "binding_result",
            "evidence_status",
            "acceptance_supported",
            "merge_ready",
            "unverified_scope",
            "limitations",
        },
        f"{label}.review_binding",
    )
    if binding.get("schema_version") != REVIEW_BINDING_SCHEMA_VERSION:
        raise EvidenceError(f"{label} review binding schema is unsupported")
    if not isinstance(binding.get("generator_version"), str) or not binding[
        "generator_version"
    ]:
        raise EvidenceError(f"{label} review binding generator is invalid")
    _binding_timestamp(binding.get("generated_at"))
    if binding.get("scope") != "content_equivalence_only":
        raise EvidenceError(f"{label} review binding scope is invalid")
    if binding.get("repository") != candidate.get("repository"):
        raise EvidenceError(
            f"{label} review binding repository disagrees with candidate"
        )
    if binding.get("candidate_mode") != candidate.get("candidate_mode"):
        raise EvidenceError(
            f"{label} review binding candidate mode disagrees with candidate"
        )
    _require_git_oid(
        binding.get("base_head"),
        f"{label}.review_binding.base_head",
    )
    if binding.get("base_head") != candidate.get("base_head"):
        raise EvidenceError(
            f"{label} review binding base HEAD disagrees with candidate"
        )
    _require_git_oid(
        binding.get("reviewed_head"),
        f"{label}.review_binding.reviewed_head",
    )
    _require_git_oid(
        binding.get("target_head"),
        f"{label}.review_binding.target_head",
    )
    if (
        binding.get("reviewed_head") != candidate.get("reviewed_head")
        or binding.get("target_head") != candidate.get("target_head")
        or binding.get("code_fingerprint")
        != candidate.get("code_fingerprint")
    ):
        raise EvidenceError(
            f"{label} review binding identity disagrees with candidate"
        )
    if (
        type(binding.get("file_count")) is not int
        or binding.get("file_count") != candidate.get("file_count")
    ):
        raise EvidenceError(
            f"{label} review binding file count disagrees with candidate"
        )
    for field in (
        "reviewed_file_manifest_sha256",
        "target_file_manifest_sha256",
    ):
        _require_sha256(
            binding.get(field),
            f"{label}.review_binding.{field}",
        )
        if binding.get(field) != candidate.get("file_manifest_sha256"):
            raise EvidenceError(
                f"{label} review binding {field} disagrees with candidate"
            )
    for field in ("reviewed_files", "target_files"):
        records = binding.get(field)
        if not isinstance(records, list):
            raise EvidenceError(
                f"{label} review binding {field} is missing"
            )
        normalized = sorted(
            (
                {
                    "path": _validate_relative_candidate_path(
                        item.get("path"),
                        f"{label}.review_binding.{field}",
                    ),
                    "sha256": item.get("sha256"),
                }
                for item in records
                if isinstance(item, Mapping)
            ),
            key=lambda item: item["path"],
        )
        if len(normalized) != len(records):
            raise EvidenceError(
                f"{label} review binding {field} has invalid entries"
            )
        for item in normalized:
            _require_sha256(
                item["sha256"],
                f"{label}.review_binding.{field}[{item['path']}]",
            )
        expected = sorted(
            (
                {"path": item["path"], "sha256": item["sha256"]}
                for item in candidate.get("files", [])
            ),
            key=lambda item: item["path"],
        )
        if normalized != expected:
            raise EvidenceError(
                f"{label} review binding {field} disagrees with candidate"
            )
    review_manifest = binding.get("review_manifest")
    if not isinstance(review_manifest, Mapping):
        raise EvidenceError(
            f"{label} review binding has no review manifest reference"
        )
    _reject_unknown_fields(
        review_manifest,
        {"path", "sha256"},
        f"{label}.review_binding.review_manifest",
    )
    _validate_relative_candidate_path(
        review_manifest.get("path"),
        f"{label}.review_binding.review_manifest.path",
    )
    _require_sha256(
        review_manifest.get("sha256"),
        f"{label}.review_binding.review_manifest.sha256",
    )
    derived_review_summary = _review_summary_from_binding(binding)
    validated_summary = _validate_review_summary_semantics(
        derived_review_summary,
        candidate,
        f"{label}.review_binding",
    )
    expected_manifest_path = review_manifest.get("path")
    provided_path = provided_review_summary.get("path")
    if provided_path not in {
        expected_manifest_path,
        f"bundle/{expected_manifest_path}",
    }:
        raise EvidenceError(
            f"{label} review binding manifest reference is inconsistent"
        )
    normalized_provided_summary = json.loads(
        canonical_json(provided_review_summary)
    )
    normalized_provided_summary["path"] = expected_manifest_path
    acceptance_labels = {
        normalized_provided_summary.get("candidate_acceptance"),
        derived_review_summary.get("candidate_acceptance"),
    }
    if acceptance_labels in (
        {
            "INDEPENDENT_REVIEW_CLEAR",
            "CONTENT_EQUIVALENT_REVIEW_CLEAR",
        },
        {
            "INDEPENDENT_REVIEW_BLOCKED",
            "CONTENT_EQUIVALENT_REVIEW_BLOCKED",
        },
        {"INDEPENDENT_REVIEW_PENDING"},
    ):
        normalized_provided_summary[
            "candidate_acceptance"
        ] = derived_review_summary["candidate_acceptance"]
    if canonical_json(normalized_provided_summary) != canonical_json(
        derived_review_summary
    ):
        raise EvidenceError(
            f"{label} review summary does not match "
            "binding-derived evidence"
        )
    binding_review_status = binding.get("review_status")
    if not isinstance(binding_review_status, Mapping):
        raise EvidenceError(
            f"{label} review binding has no review status rollup"
        )
    _reject_unknown_fields(
        binding_review_status,
        {
            "independent_code_review",
            "independent_architecture_review",
            "reviews_clear",
            "uncovered_scope",
        },
        f"{label}.review_binding.review_status",
    )
    binding_uncovered = binding_review_status.get("uncovered_scope")
    if (
        not isinstance(binding_uncovered, list)
        or len(binding_uncovered) != len(set(binding_uncovered))
        or not all(
            isinstance(item, str) and item for item in binding_uncovered
        )
    ):
        raise EvidenceError(
            f"{label} review binding uncovered scope is invalid"
        )
    if (
        binding_review_status.get("independent_code_review")
        != validated_summary["code_status"]
        or binding_review_status.get("independent_architecture_review")
        != validated_summary["architecture_status"]
        or sorted(binding_uncovered)
        != validated_summary["uncovered_scope"]
        or binding_review_status.get("reviews_clear")
        is not validated_summary["review_clear"]
    ):
        raise EvidenceError(
            f"{label} review binding status rollup is inconsistent"
        )
    expected_result = (
        "CONTENT_EQUIVALENT_REVIEW_CLEAR"
        if validated_summary["review_clear"]
        else "CONTENT_EQUIVALENT_REVIEW_BLOCKED"
    )
    if (
        binding.get("binding_result") != expected_result
        or binding.get("evidence_status") != expected_result
    ):
        raise EvidenceError(
            f"{label} review binding result disagrees with its lanes"
        )
    if (
        binding.get("acceptance_supported") is not False
        or binding.get("merge_ready") is not False
    ):
        raise EvidenceError(
            f"{label} review binding cannot self-assert acceptance"
        )
    for field in ("unverified_scope", "limitations"):
        values = binding.get(field)
        if (
            not isinstance(values, list)
            or len(values) != len(set(values))
            or not all(
                isinstance(item, str) and item for item in values
            )
        ):
            raise EvidenceError(
                f"{label} review binding {field} is invalid"
            )
    known_unverified_scopes = {
        "browser_interaction",
        "container_or_live_backend_execution",
        "real_model_planner_calls",
        "test_and_mutation_evidence_currentness",
    }
    unexpected_unverified = (
        set(binding.get("unverified_scope", []))
        - known_unverified_scopes
    )
    if unexpected_unverified:
        raise EvidenceError(
            f"{label} review binding has unpropagated scopes: "
            f"{sorted(unexpected_unverified)}"
        )
    return {
        "review_clear": validated_summary["review_clear"],
        "unverified_scope": sorted(binding["unverified_scope"]),
    }


def _validate_previous_review(
    summary: Mapping[str, Any],
    label: str,
    candidate_acceptance: str,
    *,
    candidate: Mapping[str, Any] | None,
    gate: Mapping[str, Any] | None,
    schema: str,
    legacy_code: Mapping[str, Any] | None,
    trusted_repository: str | None,
) -> None:
    special = summary.get("special_review")
    if not isinstance(special, Mapping):
        raise EvidenceError(f"{label} has no special review record")
    _reject_unknown_fields(
        special,
        {
            "independent_code_review",
            "independent_architecture_review",
            "code_review",
            "architecture_review",
            "merge_ready",
            "blocking_reasons",
            "candidate_acceptance",
            "path",
            "sha256",
            "review_clear",
            "uncovered_scope",
        },
        f"{label}.special_review",
    )
    accepted = candidate_acceptance in {
        ACCEPTED_FOR_MERGE_REVIEW,
        "INDEPENDENT_REVIEW_CLEAR",
    }
    if not accepted:
        raise EvidenceError(
            f"{label} historical review is not accepted"
        )
    special_code = special.get("independent_code_review")
    special_architecture = special.get("independent_architecture_review")
    if special_code not in CODE_REVIEW_STATUSES:
        raise EvidenceError(
            f"{label} special code review status is invalid"
        )
    if special_architecture not in ARCHITECTURE_REVIEW_STATUSES:
        raise EvidenceError(
            f"{label} special architecture review status is invalid"
        )
    if (
        special_code != "CLEAR"
        or special_architecture != "CLEAR"
        or special.get("merge_ready") is not True
        or special.get("review_clear") is not True
    ):
        raise EvidenceError(
            f"{label} historical accepted review is not clear"
        )
    special_blocking = special.get("blocking_reasons")
    if (
        not isinstance(special_blocking, list)
        or special_blocking
        or not all(
            isinstance(item, str) and item for item in special_blocking
        )
    ):
        raise EvidenceError(
            f"{label} special review blocking reasons are inconsistent"
        )
    special_uncovered = special.get("uncovered_scope")
    if (
        not isinstance(special_uncovered, list)
        or special_uncovered
        or not all(isinstance(item, str) and item for item in special_uncovered)
    ):
        raise EvidenceError(
            f"{label} special review uncovered scope is inconsistent"
        )
    _validate_relative_candidate_path(
        special.get("path"),
        f"{label}.special_review.path",
    )
    _require_sha256(
        special.get("sha256"),
        f"{label}.special_review.sha256",
    )
    resolution = (
        gate.get("current_target_review_resolution")
        if gate is not None
        else "DIRECT_CLEAR"
    )
    expected_special_acceptance = (
        "INDEPENDENT_REVIEW_CLEAR"
        if resolution == "DIRECT_CLEAR"
        else "CONTENT_EQUIVALENT_REVIEW_CLEAR"
        if resolution == "CONTENT_EQUIVALENT_CLEAR"
        else None
    )
    if (
        expected_special_acceptance is None
        or special.get("candidate_acceptance")
        != expected_special_acceptance
    ):
        raise EvidenceError(
            f"{label} special review acceptance disagrees with resolution"
        )
    expected_repository = None
    expected_heads = None
    expected_fingerprint = None
    expected_files = None
    if candidate is not None:
        expected_repository = candidate.get("repository")
        expected_head = (
            candidate.get("target_head")
            if resolution == "DIRECT_CLEAR"
            else candidate.get("reviewed_head")
        )
        if not isinstance(expected_head, str):
            raise EvidenceError(
                f"{label} candidate identity has no reviewed HEAD"
            )
        expected_heads = {expected_head}
        expected_fingerprint = candidate.get("code_fingerprint")
        expected_files = candidate.get("files")
    elif schema == "task-control-center-evidence-v1":
        if (
            trusted_repository is None
            or not isinstance(legacy_code, Mapping)
        ):
            raise EvidenceError(
                f"{label} legacy review has no trusted candidate identity"
            )
        expected_heads = {legacy_code.get("head")}
        expected_fingerprint = legacy_code.get("code_fingerprint")
        _require_git_oid(
            next(iter(expected_heads), None),
            f"{label}.code.head",
        )
        _require_sha256(
            expected_fingerprint,
            f"{label}.code.code_fingerprint",
        )
        expected_repository = str(
            _canonical_absolute_path(Path(trusted_repository))
        )
    code_uncovered = _validate_previous_review_lane(
        special.get("code_review"),
        "code_review",
        special_code,
        REQUIRED_CODE_REVIEW_SCOPE,
        expected_repository,
        expected_heads,
        expected_fingerprint,
        expected_files,
        label,
    )
    architecture_uncovered = _validate_previous_review_lane(
        special.get("architecture_review"),
        "architecture_review",
        special_architecture,
        REQUIRED_ARCHITECTURE_REVIEW_SCOPE,
        expected_repository,
        expected_heads,
        expected_fingerprint,
        expected_files,
        label,
    )
    expected_uncovered = [
        *(
            f"code_review:{scope}"
            for scope in code_uncovered
        ),
        *(
            f"architecture_review:{scope}"
            for scope in architecture_uncovered
        ),
    ]
    if special_uncovered != expected_uncovered:
        raise EvidenceError(
            f"{label} special review uncovered scope is not propagated"
        )
    review = summary.get("review_status")
    if not isinstance(review, Mapping):
        raise EvidenceError(f"{label} has no review status")
    review_fields = {
        "independent_code_review",
        "independent_architecture_review",
        "merge_ready",
        "blocking_reasons",
    }
    if schema == SCHEMA_VERSION:
        review_fields.add("review_gate")
    elif "review_gate" in review:
        review_fields.add("review_gate")
    _reject_unknown_fields(
        review,
        review_fields,
        f"{label}.review_status",
    )
    code = review.get("independent_code_review")
    architecture = review.get("independent_architecture_review")
    if (
        code != special_code
        or architecture != special_architecture
        or code != "CLEAR"
        or architecture != "CLEAR"
    ):
        raise EvidenceError(
            f"{label} special review statuses disagree with review status"
        )
    if review.get("merge_ready") is not True:
        raise EvidenceError(f"{label} historical merge flag is not clear")
    blocking = review.get("blocking_reasons")
    if (
        not isinstance(blocking, list)
        or blocking
        or not all(isinstance(item, str) and item for item in blocking)
    ):
        raise EvidenceError(
            f"{label} historical blocking reasons are inconsistent"
        )
    if schema == SCHEMA_VERSION:
        if "review_gate" not in review:
            raise EvidenceError(
                f"{label} v2 accepted history has no review_status.review_gate"
            )
        if gate is None or review.get("review_gate") != gate.get("review_gate"):
            raise EvidenceError(
                f"{label} review status gate disagrees with acceptance gate"
            )


def _validate_acceptance_gate_semantics(
    gate: Mapping[str, Any],
    label: str,
) -> None:
    """Validate the review/acceptance state machine as one unit."""
    policy = gate.get("acceptance_policy")
    resolution = gate.get("current_target_review_resolution")
    current_review = gate.get("current_target_independent_review")
    review_gate = gate.get("review_gate")
    binding = gate.get("content_equivalence_binding")
    acceptance = gate.get("acceptance_status")
    merge_ready = gate.get("merge_ready")
    unverified = gate.get("unverified_scopes")
    blocking = gate.get("blocking_reasons")
    if policy not in ACCEPTANCE_POLICIES:
        raise EvidenceError(f"{label} has an unsupported acceptance policy")
    if current_review not in {"CLEAR", "NOT_AVAILABLE", "BLOCKED"}:
        raise EvidenceError(f"{label} has an invalid current review status")
    if resolution not in {
        "DIRECT_CLEAR",
        "CONTENT_EQUIVALENT_CLEAR",
        "UNVERIFIED",
    }:
        raise EvidenceError(f"{label} has an invalid review resolution")
    if review_gate not in {
        "CLEAR",
        "CONTENT_EQUIVALENT_CLEAR",
        "NOT_AVAILABLE",
        "BLOCKED",
    }:
        raise EvidenceError(f"{label} has an invalid review gate")
    if binding not in {
        "NOT_AVAILABLE",
        "CONTENT_EQUIVALENT_CLEAR",
        "CONTENT_EQUIVALENT_BLOCKED",
        "BLOCKED",
    }:
        raise EvidenceError(f"{label} has an invalid binding status")
    if not isinstance(unverified, list) or not all(
        isinstance(item, str) and item for item in unverified
    ):
        raise EvidenceError(f"{label} unverified scopes are invalid")
    if not isinstance(blocking, list) or not all(
        isinstance(item, str) and item for item in blocking
    ):
        raise EvidenceError(f"{label} blocking reasons are invalid")
    if not isinstance(merge_ready, bool):
        raise EvidenceError(f"{label} merge flag is invalid")
    publication_ancestry = gate.get("persistent_publication_ancestry")
    if publication_ancestry not in {"VERIFIED", "UNSUPPORTED"}:
        raise EvidenceError(
            f"{label} persistent publication ancestry is invalid"
        )
    mutation_execution_trust = gate.get("mutation_execution_trust")
    test_execution_trust = gate.get("test_execution_trust")
    if mutation_execution_trust not in {"VERIFIED", "UNSUPPORTED"}:
        raise EvidenceError(
            f"{label} mutation execution trust is invalid"
        )
    if test_execution_trust not in {"VERIFIED", "UNSUPPORTED"}:
        raise EvidenceError(
            f"{label} test execution trust is invalid"
        )

    if resolution == "DIRECT_CLEAR":
        if current_review != "CLEAR" or review_gate != "CLEAR":
            raise EvidenceError(
                f"{label} DIRECT_CLEAR contradicts the review state"
            )
    elif resolution == "CONTENT_EQUIVALENT_CLEAR":
        if (
            current_review != "NOT_AVAILABLE"
            or policy != "content_equivalence_allowed"
            or binding != "CONTENT_EQUIVALENT_CLEAR"
            or review_gate != "CONTENT_EQUIVALENT_CLEAR"
        ):
            raise EvidenceError(
                f"{label} CONTENT_EQUIVALENT_CLEAR contradicts its bindings"
            )
    elif acceptance == ACCEPTED_FOR_MERGE_REVIEW:
        raise EvidenceError(
            f"{label} accepted state has no resolved current review"
        )
    if review_gate == "CONTENT_EQUIVALENT_CLEAR" and (
        policy != "content_equivalence_allowed"
        or binding != "CONTENT_EQUIVALENT_CLEAR"
        or resolution != "CONTENT_EQUIVALENT_CLEAR"
    ):
        raise EvidenceError(
            f"{label} content-equivalence gate is not fully bound"
        )

    accepted = acceptance == ACCEPTED_FOR_MERGE_REVIEW
    if merge_ready is not accepted:
        raise EvidenceError(
            f"{label} merge flag disagrees with acceptance status"
        )
    if accepted:
        if unverified:
            raise EvidenceError(
                f"{label} accepted state contains unverified scopes"
            )
        if blocking:
            raise EvidenceError(
                f"{label} accepted state contains blocking reasons"
            )
        if (
            gate.get("candidate_identity") != CANDIDATE_IDENTITY_VERIFIED
            or gate.get("tests") != "VERIFIED"
            or gate.get("frozen_evidence") != "VERIFIED"
            or gate.get("previous_summary") != "AVAILABLE"
            or gate.get("previous_summary_trusted") is not True
            or gate.get("historical_review") != "CLEAR"
            or publication_ancestry != "VERIFIED"
            or mutation_execution_trust != "VERIFIED"
            or test_execution_trust != "VERIFIED"
            or resolution == "UNVERIFIED"
        ):
            raise EvidenceError(
                f"{label} accepted state has incomplete prerequisites"
            )
        runtime = gate.get("runtime_verification")
        if not isinstance(runtime, Mapping) or runtime.get("status") != "VERIFIED":
            raise EvidenceError(
                f"{label} accepted state has no verified runtime evidence"
            )


def _validate_previous_review_binding_record(
    review_binding: Any,
    candidate: Mapping[str, Any],
    gate: Mapping[str, Any],
    label: str,
) -> None:
    resolution = gate.get("current_target_review_resolution")
    if review_binding is None:
        if resolution == "CONTENT_EQUIVALENT_CLEAR":
            raise EvidenceError(
                f"{label} content-equivalent history has no review binding"
            )
        return
    if not isinstance(review_binding, Mapping):
        raise EvidenceError(
            f"{label} historical review binding is invalid"
        )
    _reject_unknown_fields(
        review_binding,
        {
            "path",
            "sha256",
            "scope",
            "binding_result",
            "evidence_status",
            "unverified_scope",
            "reviewed_head",
            "target_head",
            "acceptance_supported",
            "merge_ready",
        },
        f"{label}.review_binding",
    )
    if (
        not isinstance(review_binding.get("path"), str)
        or not review_binding["path"]
    ):
        raise EvidenceError(
            f"{label} historical review binding path is invalid"
        )
    _validate_relative_candidate_path(
        review_binding["path"],
        f"{label}.review_binding.path",
    )
    _require_sha256(
        review_binding.get("sha256"),
        f"{label}.review_binding.sha256",
    )
    binding_unverified = review_binding.get("unverified_scope")
    if (
        not isinstance(binding_unverified, list)
        or len(binding_unverified) != len(set(binding_unverified))
        or not all(
            isinstance(item, str) and item
            for item in binding_unverified
        )
    ):
        raise EvidenceError(
            f"{label} historical review binding unverified scope is invalid"
        )
    gate_unverified = set(gate.get("unverified_scopes", []))
    if not set(binding_unverified).issubset(gate_unverified):
        raise EvidenceError(
            f"{label} historical review binding unverified scope "
            "is not propagated"
        )
    if (
        review_binding.get("scope") != "content_equivalence_only"
        or review_binding.get("binding_result")
        != "CONTENT_EQUIVALENT_REVIEW_CLEAR"
        or review_binding.get("evidence_status")
        != "CONTENT_EQUIVALENT_REVIEW_CLEAR"
        or review_binding.get("acceptance_supported") is not False
        or review_binding.get("merge_ready") is not False
    ):
        raise EvidenceError(
            f"{label} historical review binding state is inconsistent"
        )
    _require_git_oid(
        review_binding.get("reviewed_head"),
        f"{label}.review_binding.reviewed_head",
    )
    candidate_reviewed_head = candidate.get("reviewed_head")
    if (
        not isinstance(candidate_reviewed_head, str)
        or review_binding.get("reviewed_head") != candidate_reviewed_head
    ):
        raise EvidenceError(
            f"{label} historical review binding reviewed HEAD "
            "disagrees with candidate"
        )
    if (
        review_binding.get("target_head") != candidate.get("target_head")
        or gate.get("content_equivalence_binding")
        != "CONTENT_EQUIVALENT_CLEAR"
        or gate.get("review_gate") != "CONTENT_EQUIVALENT_CLEAR"
        or resolution != "CONTENT_EQUIVALENT_CLEAR"
    ):
        raise EvidenceError(
            f"{label} historical review binding disagrees with gate"
        )


def _validate_trusted_identity_shape(
    trusted_identity: Any,
    label: str,
    *,
    require_manifest: bool,
) -> Mapping[str, Any]:
    if not isinstance(trusted_identity, Mapping):
        raise EvidenceError(
            f"{label} has no external trusted candidate identity"
        )
    allowed = {
        "repository",
        "target_head",
        "code_fingerprint",
        "status_sha256",
        "file_manifest_sha256",
    }
    _reject_unknown_fields(
        trusted_identity,
        allowed,
        f"{label}.trusted_identity",
    )
    if (
        not isinstance(trusted_identity.get("repository"), str)
        or not trusted_identity["repository"]
    ):
        raise EvidenceError(f"{label} trusted repository is invalid")
    _require_git_oid(
        trusted_identity.get("target_head"),
        f"{label}.trusted_identity.target_head",
    )
    for field in ("code_fingerprint", "status_sha256"):
        _require_sha256(
            trusted_identity.get(field),
            f"{label}.trusted_identity.{field}",
        )
    if require_manifest:
        _require_sha256(
            trusted_identity.get("file_manifest_sha256"),
            f"{label}.trusted_identity.file_manifest_sha256",
        )
    elif trusted_identity.get("file_manifest_sha256") is not None:
        _require_sha256(
            trusted_identity.get("file_manifest_sha256"),
            f"{label}.trusted_identity.file_manifest_sha256",
        )
    return trusted_identity


def _load_trusted_candidate_snapshot(
    path: Path,
    repository_root: Path,
    label: str,
) -> tuple[dict[str, Any], str]:
    path = _canonical_absolute_path(path)
    repository_root = _canonical_absolute_path(repository_root)
    with TrustedEvidenceRoot(
        path.parent,
        f"{label} root",
    ) as root:
        with root.pinned():
            data = _read_regular_file(
                path,
                label,
                root=root.path,
                max_bytes=MAX_JSON_EVIDENCE_BYTES,
            )
            snapshot = _parse_json_object_bytes(data, path)
            validated = _validate_candidate_snapshot_payload(
                repository_root,
                snapshot,
                label,
            )
    return validated, sha256_bytes(data)


def _validate_previous_identity_context(
    summary: Mapping[str, Any],
    trusted_snapshot: Mapping[str, Any],
    label: str,
) -> None:
    if not isinstance(trusted_snapshot, Mapping):
        raise EvidenceError(
            f"{label} has no external trusted candidate snapshot"
        )
    trusted_identity = {
        "repository": trusted_snapshot.get("repository"),
        "target_head": trusted_snapshot.get("target_head"),
        "code_fingerprint": trusted_snapshot.get("code_fingerprint"),
        "status_sha256": trusted_snapshot.get("status_sha256"),
        "file_manifest_sha256": trusted_snapshot.get(
            "file_manifest_sha256"
        ),
    }
    candidate = summary.get("candidate_identity")
    _validate_trusted_identity_shape(
        trusted_identity,
        label,
        require_manifest=candidate is not None,
    )
    code = summary.get("code")
    if (
        not isinstance(code, Mapping)
        or code.get("head") != trusted_identity.get("target_head")
        or code.get("code_fingerprint")
        != trusted_identity.get("code_fingerprint")
        or code.get("status_sha256")
        != trusted_identity.get("status_sha256")
    ):
        raise EvidenceError(
            f"{label} historical code identity disagrees with trusted context"
        )
    if candidate is not None:
        if canonical_json(candidate) != canonical_json(trusted_snapshot):
            raise EvidenceError(
                f"{label} candidate identity disagrees with trusted snapshot"
            )
    special = summary.get("special_review")
    if isinstance(special, Mapping):
        for key in ("code_review", "architecture_review"):
            lane = special.get(key)
            if isinstance(lane, Mapping) and (
                lane.get("target_repository")
                != trusted_identity.get("repository")
            ):
                raise EvidenceError(
                    f"{label} {key} repository disagrees with trusted context"
                )


def _validate_previous_summary(
    summary: Any,
    label: str,
    *,
    trusted_repository: str | None = None,
    trusted_previous_candidate_snapshot: Mapping[str, Any] | None = None,
    require_trusted_identity: bool = False,
) -> tuple[list[str], list[dict[str, str]]]:
    if not isinstance(summary, Mapping):
        raise EvidenceError(f"{label} must be an object")
    if require_trusted_identity:
        _validate_previous_identity_context(
            summary,
            trusted_previous_candidate_snapshot,
            label,
        )
    schema = summary.get("schema_version")
    previous_candidate: Mapping[str, Any] | None = None
    previous_gate: Mapping[str, Any] | None = None
    previous_code: Mapping[str, Any] | None = None
    if schema == "task-control-center-evidence-v1":
        allowed = {
            "schema_version",
            "scope",
            "status",
            "validation_mode",
            "candidate_acceptance",
            "code",
            "tests",
            "mutations",
            "review_status",
            "special_review",
            "constraint_incidents",
            "evidence_history",
            "constraint_scope",
            "frozen_evidence",
            "superseded_summary",
        }
        _reject_unknown_fields(summary, allowed, label)
        if summary.get("scope") != "task-control-center-v1":
            raise EvidenceError(f"{label} legacy scope is invalid")
        if summary.get("status") != PASS_EVIDENCE_INTEGRITY:
            raise EvidenceError(f"{label} legacy status is not PASS")
        if summary.get("validation_mode") != "offline_noncontainer":
            raise EvidenceError(f"{label} legacy validation mode is invalid")
        acceptance = summary.get("candidate_acceptance")
        if acceptance != "INDEPENDENT_REVIEW_CLEAR":
            raise EvidenceError(f"{label} legacy acceptance is invalid")
    elif schema == SCHEMA_VERSION:
        allowed = {
            "schema_version",
            "scope",
            "validation_input",
            "status",
            "validation_mode",
            "candidate_identity",
            "evidence_bundle",
            "review_binding",
            "code",
            "tests",
            "mutations",
            "special_review",
            "review_status",
            "acceptance_gate",
            "constraint_incidents",
            "evidence_history",
            "constraint_scope",
            "frozen_evidence",
            "superseded_summary",
            "candidate_acceptance",
        }
        _reject_unknown_fields(summary, allowed, label)
        if summary.get("scope") != "task-control-center-v1":
            raise EvidenceError(f"{label} scope is invalid")
        if summary.get("status") != PASS_EVIDENCE_INTEGRITY:
            raise EvidenceError(f"{label} status is not PASS")
        if summary.get("validation_mode") != "offline_noncontainer":
            raise EvidenceError(f"{label} validation mode is invalid")
        acceptance = summary.get("candidate_acceptance")
        if acceptance != ACCEPTED_FOR_MERGE_REVIEW:
            raise EvidenceError(f"{label} acceptance is not accepted")
        candidate_record = summary.get("candidate_identity")
        _validate_previous_candidate(candidate_record, label)
        previous_candidate = candidate_record
        code = summary.get("code")
        _validate_previous_code(code, label)
        if (
            candidate_record["target_head"] != code["head"]
            or candidate_record["code_fingerprint"]
            != code["code_fingerprint"]
            or candidate_record["status_sha256"] != code["status_sha256"]
        ):
            raise EvidenceError(
                f"{label} historical candidate/code binding is inconsistent"
            )
        validation_input = summary.get("validation_input")
        if not isinstance(validation_input, Mapping):
            raise EvidenceError(f"{label} has no validation input")
        _reject_unknown_fields(
            validation_input,
            {
                "run_metadata",
                "run_metadata_sha256",
                "evidence_bundle_path",
                "replacement_reason_sha256",
                "trusted_previous_summary_sha256",
                "trusted_previous_candidate_snapshot_path",
                "trusted_previous_candidate_snapshot_sha256",
                "generator_version",
            },
            f"{label}.validation_input",
        )
        if not isinstance(validation_input.get("run_metadata"), str) or not (
            validation_input["run_metadata"]
        ):
            raise EvidenceError(f"{label} historical metadata path is invalid")
        _require_sha256(
            validation_input.get("run_metadata_sha256"),
            f"{label}.validation_input.run_metadata_sha256",
        )
        bundle_path = validation_input.get("evidence_bundle_path")
        if bundle_path is not None and (
            not isinstance(bundle_path, str) or not bundle_path
        ):
            raise EvidenceError(f"{label} historical bundle path is invalid")
        _require_sha256(
            validation_input.get("replacement_reason_sha256"),
            f"{label}.validation_input.replacement_reason_sha256",
        )
        trusted_previous = validation_input.get(
            "trusted_previous_summary_sha256"
        )
        if trusted_previous is not None:
            _require_sha256(
                trusted_previous,
                f"{label}.validation_input.trusted_previous_summary_sha256",
            )
        trusted_snapshot_path = validation_input.get(
            "trusted_previous_candidate_snapshot_path"
        )
        trusted_snapshot_sha256 = validation_input.get(
            "trusted_previous_candidate_snapshot_sha256"
        )
        if (trusted_snapshot_path is None) != (
            trusted_snapshot_sha256 is None
        ):
            raise EvidenceError(
                f"{label} trusted candidate snapshot binding is incomplete"
            )
        if trusted_snapshot_path is not None:
            if (
                not isinstance(trusted_snapshot_path, str)
                or not trusted_snapshot_path
            ):
                raise EvidenceError(
                    f"{label} trusted candidate snapshot path is invalid"
                )
            _require_sha256(
                trusted_snapshot_sha256,
                f"{label}.validation_input."
                "trusted_previous_candidate_snapshot_sha256",
            )
        if not isinstance(
            validation_input.get("generator_version"),
            str,
        ) or not validation_input["generator_version"]:
            raise EvidenceError(f"{label} historical generator is invalid")
        gate_record = summary.get("acceptance_gate")
        if not isinstance(gate_record, Mapping):
            raise EvidenceError(f"{label} has no historical acceptance gate")
        previous_gate = gate_record
        _reject_unknown_fields(
            gate_record,
            {
                "candidate_identity",
                "historical_review",
                "content_equivalence_binding",
                "current_target_independent_review",
                "current_target_review_resolution",
                "review_gate",
                "acceptance_policy",
                "tests",
                "frozen_evidence",
                "previous_summary",
                "previous_summary_trusted",
                "runtime_verification",
                "persistent_publication_ancestry",
                "mutation_execution_trust",
                "test_execution_trust",
                "unverified_scopes",
                "blocking_reasons",
                "evidence_integrity_status",
                "acceptance_status",
                "merge_ready",
            },
            f"{label}.acceptance_gate",
        )
        if gate_record.get("acceptance_policy") not in ACCEPTANCE_POLICIES:
            raise EvidenceError(f"{label} historical policy is invalid")
        if gate_record.get("evidence_integrity_status") != PASS_EVIDENCE_INTEGRITY:
            raise EvidenceError(
                f"{label} historical evidence status is inconsistent"
            )
        if gate_record.get("acceptance_status") != acceptance:
            raise EvidenceError(
                f"{label} historical acceptance status is inconsistent"
            )
        if gate_record.get("merge_ready") is not True:
            raise EvidenceError(
                f"{label} historical acceptance gate is not merge-ready"
            )
        if gate_record.get("historical_review") != "CLEAR":
            raise EvidenceError(
                f"{label} historical review gate is not clear"
            )
        policy = gate_record.get("acceptance_policy")
        review_gate = gate_record.get("review_gate")
        if review_gate not in {"CLEAR", "CONTENT_EQUIVALENT_CLEAR"}:
            raise EvidenceError(
                f"{label} historical review gate is not clear"
            )
        if review_gate == "CONTENT_EQUIVALENT_CLEAR" and (
            policy != "content_equivalence_allowed"
            or gate_record.get("content_equivalence_binding")
            != "CONTENT_EQUIVALENT_CLEAR"
        ):
            raise EvidenceError(
                f"{label} historical content-equivalence gate is invalid"
            )
        allowed_current_review = (
            {"CLEAR"}
            if review_gate == "CLEAR"
            else {"CLEAR", "NOT_AVAILABLE"}
        )
        if gate_record.get("current_target_independent_review") not in (
            allowed_current_review
        ):
            raise EvidenceError(
                f"{label} historical current review is not clear"
            )
        if gate_record.get("current_target_review_resolution") not in {
            "DIRECT_CLEAR",
            "CONTENT_EQUIVALENT_CLEAR",
        }:
            raise EvidenceError(
                f"{label} historical current review is unresolved"
            )
        if gate_record.get("candidate_identity") != CANDIDATE_IDENTITY_VERIFIED:
            raise EvidenceError(
                f"{label} historical candidate gate is inconsistent"
            )
        if gate_record.get("tests") != "VERIFIED":
            raise EvidenceError(f"{label} historical tests gate is inconsistent")
        if gate_record.get("frozen_evidence") != "VERIFIED":
            raise EvidenceError(
                f"{label} historical frozen gate is inconsistent"
            )
        if gate_record.get("previous_summary") != "AVAILABLE":
            raise EvidenceError(
                f"{label} historical previous-summary gate is inconsistent"
            )
        if gate_record.get("previous_summary_trusted") is not True:
            raise EvidenceError(
                f"{label} historical previous-summary is not trusted"
            )
        if gate_record.get("persistent_publication_ancestry") != "VERIFIED":
            raise EvidenceError(
                f"{label} historical publication ancestry is not verified"
            )
        if gate_record.get("mutation_execution_trust") != "VERIFIED":
            raise EvidenceError(
                f"{label} historical mutation execution trust is not verified"
            )
        if gate_record.get("test_execution_trust") != "VERIFIED":
            raise EvidenceError(
                f"{label} historical test execution trust is not verified"
            )
        if trusted_previous is None:
            raise EvidenceError(
                f"{label} historical trust anchor is not recorded"
            )
        if gate_record.get("unverified_scopes") != []:
            raise EvidenceError(
                f"{label} historical gate has unverified scopes"
            )
        if gate_record.get("blocking_reasons") != []:
            raise EvidenceError(
                f"{label} historical gate has blocking reasons"
            )
        runtime = gate_record.get("runtime_verification")
        if not isinstance(runtime, Mapping):
            raise EvidenceError(
                f"{label} historical runtime verification is missing"
            )
        _reject_unknown_fields(
            runtime,
            {"status", "scopes", "reason", "valid_evidence_count"},
            f"{label}.acceptance_gate.runtime_verification",
        )
        if runtime.get("status") != "VERIFIED":
            raise EvidenceError(
                f"{label} historical runtime is not verified"
            )
        if runtime.get("scopes") != ["browser"]:
            raise EvidenceError(
                f"{label} historical runtime scopes are invalid"
            )
        if not isinstance(runtime.get("reason"), str) or not runtime[
            "reason"
        ].strip():
            raise EvidenceError(
                f"{label} historical runtime reason is invalid"
            )
        if (
            type(runtime.get("valid_evidence_count")) is not int
            or runtime["valid_evidence_count"] < 1
        ):
            raise EvidenceError(
                f"{label} historical runtime evidence count is invalid"
            )
        _validate_acceptance_gate_semantics(
            gate_record,
            f"{label}.acceptance_gate",
        )
        _validate_previous_review_binding_record(
            summary.get("review_binding"),
            candidate_record,
            gate_record,
            label,
        )
    else:
        raise EvidenceError(f"{label} schema is unsupported")

    acceptance = summary["candidate_acceptance"]
    previous_code = summary.get("code")
    _validate_previous_code(previous_code, label)
    _validate_previous_tests(
        summary.get("tests"),
        label,
        require_discovery=schema == SCHEMA_VERSION,
    )
    _validate_previous_mutations(summary.get("mutations"), label)
    _validate_previous_review(
        summary,
        label,
        acceptance,
        candidate=previous_candidate,
        gate=previous_gate,
        schema=schema,
        legacy_code=previous_code,
        trusted_repository=trusted_repository,
    )
    _validate_previous_frozen(summary.get("frozen_evidence"), label)
    incidents = summary.get("constraint_incidents")
    if not isinstance(incidents, list) or not incidents or not all(
        isinstance(item, str) and item for item in incidents
    ):
        raise EvidenceError(f"{label} constraint incidents are invalid")
    constraint_scope = summary.get("constraint_scope")
    required_scope = {
        "runtime_side_effect_observation",
        "selection_basis",
        "container_tests_executed_this_run",
        "docker_backed_tests_executed_this_run",
        "live_tests_executed_this_run",
        "online_tests_executed_this_run",
        "real_model_requests",
        "external_network_requests",
        "dependency_installs",
    }
    if not isinstance(constraint_scope, Mapping) or set(
        constraint_scope
    ) != required_scope:
        raise EvidenceError(f"{label} constraint scope is incomplete")
    if constraint_scope.get("runtime_side_effect_observation") != (
        "NOT_INSTRUMENTED"
    ):
        raise EvidenceError(f"{label} constraint scope is inconsistent")
    history = _validate_previous_history(summary, label)
    if schema == SCHEMA_VERSION:
        superseded_reason = summary["superseded_summary"][
            "replacement_reason"
        ]
        if summary["validation_input"]["replacement_reason_sha256"] != (
            sha256_bytes(superseded_reason.encode("utf-8"))
        ):
            raise EvidenceError(
                f"{label} historical replacement binding is inconsistent"
            )
    return incidents, history


def _committed_range_files(
    root: Path,
    base_head: str,
    target_head: str,
) -> list[str]:
    def changed_paths(diff_filter: str) -> list[str]:
        completed = _run_git_process(
            root,
            [
                "diff",
                "--no-renames",
                "-z",
                "--name-only",
                f"--diff-filter={diff_filter}",
                base_head,
                target_head,
                "--",
            ],
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode(
                "utf-8",
                errors="replace",
            ).strip()
            raise EvidenceError(
                f"committed candidate Git diff failed: {detail}"
            )
        try:
            decoded = completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvidenceError(
                "committed candidate path is not valid UTF-8"
            ) from exc
        return [path for path in decoded.split("\0") if path]

    deleted = changed_paths("D")
    if deleted:
        raise EvidenceError(
            "committed candidate deletes files unsupported by review binding: "
            f"{sorted(deleted)}"
        )
    type_changes = changed_paths("T")
    if type_changes:
        raise EvidenceError(
            "committed candidate changes file types: "
            f"{sorted(type_changes)}"
        )
    paths = changed_paths("ACMRT")
    validated = {
        _validate_relative_candidate_path(
            path,
            "candidate",
            allow_protected=True,
        )
        for path in paths
        if path
    }
    if not validated:
        raise EvidenceError("committed candidate contains no files")
    return sorted(validated)


def file_manifest_sha256(files: Iterable[Mapping[str, Any]]) -> str:
    normalized = []
    seen: dict[str, str] = {}
    for item in files:
        if not isinstance(item, Mapping):
            raise EvidenceError("file manifest entries must be objects")
        path = _validate_relative_candidate_path(
            item.get("path"),
            "file manifest",
        )
        digest = item.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}",
            digest,
        ):
            raise EvidenceError(
                f"file manifest SHA-256 is invalid for {path}"
            )
        identity = _canonical_path_identity(path, "file manifest")
        if identity in seen:
            raise EvidenceError(
                "file manifest has duplicate or aliased path: "
                f"{seen[identity]} and {path}"
            )
        seen[identity] = path
        normalized.append({"path": path, "sha256": digest})
    normalized.sort(key=lambda item: item["path"])
    return sha256_bytes(canonical_json(normalized).encode("utf-8"))


def _worktree_file_entries(root: Path, paths: Iterable[str]) -> list[dict[str, str]]:
    entries = []
    identities: dict[str, str] = {}
    for relative in sorted(set(paths)):
        safe_relative = _validate_relative_candidate_path(
            relative,
            "candidate",
        )
        path = root / safe_relative
        _assert_no_symlink_components(root, path, "candidate")
        if not path.is_file():
            raise EvidenceError(f"candidate file is missing: {safe_relative}")
        identity = _canonical_path_identity(safe_relative, "candidate")
        if identity in identities:
            raise EvidenceError(
                "candidate path alias is duplicated: "
                f"{identities[identity]} and {safe_relative}"
            )
        identities[identity] = safe_relative
        entries.append(
            {
                "path": safe_relative,
                "sha256": sha256_file(path, root=root),
            }
        )
    return entries


def _commit_file_entries(
    root: Path,
    commit: str,
    paths: Iterable[str],
) -> list[dict[str, str]]:
    entries = []
    for relative in sorted(set(paths)):
        safe_relative = _validate_relative_candidate_path(
            relative,
            "target commit",
        )
        tree_record = _git_text(
            root,
            ["ls-tree", "-z", commit, "--", safe_relative],
        )
        if not tree_record:
            raise EvidenceError(
                f"target commit file is unavailable: {safe_relative}"
            )
        record = tree_record.split("\0", 1)[0]
        mode = record.split(" ", 1)[0]
        if mode not in {"100644", "100755"}:
            raise EvidenceError(
                f"target commit file is not a regular file: {safe_relative}"
            )
        completed = _run_git_process(
            root,
            ["show", f"{commit}:{safe_relative}"],
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise EvidenceError(
                f"target commit file is unavailable: {safe_relative}: {detail}"
            )
        entries.append(
            {
                "path": safe_relative,
                "sha256": sha256_bytes(completed.stdout),
            }
        )
    return entries


def git_state(root: Path) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    with _trusted_repository_scope(root) as repository_root:
        head_process = _run_git_process(root, ["rev-parse", "HEAD"])
        if head_process.returncode != 0:
            raise EvidenceError(
                "Git HEAD lookup failed: "
                + head_process.stderr.decode(
                    "utf-8",
                    errors="replace",
                ).strip()
            )
        head = head_process.stdout.decode("utf-8").strip()
        status_process = _run_git_process(
            root,
            [
                "status",
                "--short",
                "--untracked-files=all",
                "--",
                ".",
                ":(exclude).idea",
            ],
        )
        if status_process.returncode != 0:
            raise EvidenceError(
                "Git status failed: "
                + status_process.stderr.decode(
                    "utf-8",
                    errors="replace",
                ).strip()
            )
        status = status_process.stdout.decode("utf-8")
        return {
            "head": head,
            "worktree_clean": not bool(status.strip()),
            "status_sha256": sha256_bytes(status.encode("utf-8")),
        }


def build_candidate_snapshot(
    root: Path,
    *,
    candidate_mode: str = "working_tree",
    base_head: str | None = None,
    target_head: str | None = None,
    reviewed_head: str | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    with _trusted_repository_scope(root) as repository_root:
        return _build_candidate_snapshot_impl(
            root,
            candidate_mode=candidate_mode,
            base_head=base_head,
            target_head=target_head,
            reviewed_head=reviewed_head,
        )


def _build_candidate_snapshot_impl(
    root: Path,
    *,
    candidate_mode: str = "working_tree",
    base_head: str | None = None,
    target_head: str | None = None,
    reviewed_head: str | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    if candidate_mode not in CANDIDATE_MODES:
        raise EvidenceError(f"unsupported candidate mode: {candidate_mode}")
    state_before = git_state(root)
    branch_before = _git_text(root, ["branch", "--show-current"]).strip()
    current_head = _resolve_commit(
        root,
        state_before["head"],
        "current HEAD",
    )
    target = (
        current_head
        if target_head is None
        else _resolve_commit(root, target_head, "target HEAD")
    )
    base = (
        None
        if base_head is None
        else _resolve_commit(root, base_head, "base HEAD")
    )
    reviewed = (
        None
        if reviewed_head is None
        else _resolve_commit(root, reviewed_head, "reviewed HEAD")
    )
    if target != current_head:
        raise EvidenceError(
            "candidate target HEAD does not match the current repository HEAD"
        )

    if candidate_mode == "working_tree":
        if base is not None and base != current_head:
            raise EvidenceError(
                "working_tree base_head must equal the current HEAD"
            )
        base = current_head
        paths, excluded_changes = _candidate_change_records(root)
        if not paths:
            raise EvidenceError("working-tree candidate contains no files")
        evidence_status = CANDIDATE_IDENTITY_VERIFIED
    elif candidate_mode == "committed_range":
        if base is None:
            raise EvidenceError("committed_range requires base_head")
        if not _is_ancestor(root, base, target):
            raise EvidenceError("base_head is not an ancestor of target_head")
        if not state_before["worktree_clean"]:
            raise EvidenceError(
                "committed_range requires a clean worktree outside .idea"
            )
        committed_paths = _committed_range_files(root, base, target)
        paths = [
            path
            for path in committed_paths
            if not _is_excluded_candidate_path(path)
        ]
        excluded_changes = [
            {"path": path, "status": "committed-range-excluded"}
            for path in committed_paths
            if _is_excluded_candidate_path(path)
        ]
        if not paths:
            raise EvidenceError("committed candidate contains no in-scope files")
        evidence_status = CANDIDATE_IDENTITY_VERIFIED
    else:
        paths, excluded_changes = _candidate_change_records(root)
        evidence_status = BOOTSTRAP_INCOMPLETE

    capture_paths = _candidate_file_paths(root)
    captured_metadata: dict[
        str,
        tuple[int, int, int, int, int, int],
    ] = {}
    captured = _capture_candidate_file_bytes(
        root,
        capture_paths,
        captured_metadata=captured_metadata,
    )
    state_after = git_state(root)
    branch_after = _git_text(root, ["branch", "--show-current"]).strip()
    capture_paths_after = _candidate_file_paths(root)
    if (
        state_before != state_after
        or branch_before != branch_after
        or capture_paths != capture_paths_after
    ):
        raise EvidenceError(
            "repository state changed during candidate capture"
        )
    if candidate_mode != "committed_range":
        post_paths, post_excluded_changes = _candidate_change_records(root)
        if post_paths != paths or post_excluded_changes != excluded_changes:
            raise EvidenceError(
                "candidate change set changed during capture"
            )
    _verify_captured_candidate_generation(
        root,
        captured,
        captured_metadata,
    )
    final_state = git_state(root)
    final_branch = _git_text(root, ["branch", "--show-current"]).strip()
    final_paths = _candidate_file_paths(root)
    if (
        state_before != final_state
        or branch_before != final_branch
        or capture_paths != final_paths
    ):
        raise EvidenceError(
            "repository generation changed before final candidate verification"
        )
    if candidate_mode != "committed_range":
        final_changed_paths, final_excluded_changes = (
            _candidate_change_records(root)
        )
        if (
            final_changed_paths != paths
            or final_excluded_changes != excluded_changes
        ):
            raise EvidenceError(
                "candidate change set changed before final verification"
            )
    _verify_captured_candidate_generation(
        root,
        captured,
        captured_metadata,
    )
    settled_state = git_state(root)
    settled_branch = _git_text(root, ["branch", "--show-current"]).strip()
    settled_paths = _candidate_file_paths(root)
    if (
        final_state != settled_state
        or final_branch != settled_branch
        or final_paths != settled_paths
    ):
        raise EvidenceError(
            "repository generation changed during final candidate verification"
        )
    if candidate_mode != "committed_range":
        settled_changed_paths, settled_excluded_changes = (
            _candidate_change_records(root)
        )
        if (
            settled_changed_paths != paths
            or settled_excluded_changes != excluded_changes
        ):
            raise EvidenceError(
                "candidate change set changed during final verification"
            )
    files = _file_entries_from_captured(paths, captured)
    file_count = len(files)
    if candidate_mode != "bootstrap" and file_count <= 0:
        raise EvidenceError("candidate identity requires at least one file")
    return {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "repository": str(root),
        "branch": branch_before,
        "candidate_mode": candidate_mode,
        "base_head": base,
        "target_head": target,
        "reviewed_head": reviewed,
        "worktree_clean": state_before["worktree_clean"],
        "status_sha256": state_before["status_sha256"],
        "code_fingerprint": _code_fingerprint_from_captured(captured),
        "file_count": file_count,
        "file_manifest_sha256": file_manifest_sha256(files),
        "evidence_status": evidence_status,
        "candidate_scope": candidate_scope_description(),
        "excluded_changes": excluded_changes,
        "files": files,
        "excluded": [
            ".idea/**",
            ".env*",
            "secret configuration",
            "runtime temporary files",
        ],
    }


def validate_candidate_snapshot(
    root: Path,
    snapshot_path: Path,
    *,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    with _trusted_repository_scope(root) as repository_root:
        evidence_base = _canonical_absolute_path(evidence_root or root)
        with TrustedEvidenceRoot(
            evidence_base,
            "candidate snapshot evidence root",
        ) as trusted_root:
            with trusted_root.pinned():
                return _validate_candidate_snapshot_impl(
                    root,
                    snapshot_path,
                    evidence_root=evidence_base,
                )


def _validate_candidate_snapshot_impl(
    root: Path,
    snapshot_path: Path,
    *,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    evidence_base = _protected_evidence_root(
        evidence_root or root,
        "candidate snapshot evidence root",
    )
    resolved_snapshot_path = _evidence_root_file(
        evidence_base,
        evidence_path(snapshot_path, evidence_base),
        "candidate snapshot",
    )
    snapshot = read_json(resolved_snapshot_path, root=evidence_base)
    return _validate_candidate_snapshot_payload(
        root,
        snapshot,
        "candidate snapshot",
    )


def _validate_candidate_snapshot_payload(
    root: Path,
    snapshot: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    """Rebuild a candidate snapshot from the trusted current repository."""
    root = _canonical_absolute_path(root)
    if not isinstance(snapshot, Mapping):
        raise EvidenceError(f"{label} must be an object")
    _reject_unknown_fields(
        snapshot,
        {
            "schema_version",
            "repository",
            "branch",
            "candidate_mode",
            "base_head",
            "target_head",
            "reviewed_head",
            "worktree_clean",
            "status_sha256",
            "code_fingerprint",
            "file_count",
            "file_manifest_sha256",
            "evidence_status",
            "candidate_scope",
            "excluded_changes",
            "files",
            "excluded",
        },
        label,
    )
    if snapshot.get("schema_version") != CANDIDATE_SCHEMA_VERSION:
        raise EvidenceError(f"{label} has an unexpected identity schema")
    if snapshot.get("repository") != str(root):
        raise EvidenceError(f"{label} repository does not match root")
    if snapshot.get("candidate_mode") not in CANDIDATE_MODES:
        raise EvidenceError(
            f"{label} candidate mode is missing or unsupported"
        )
    if not isinstance(snapshot.get("files"), list):
        raise EvidenceError(f"{label} file manifest must be an array")
    if (
        type(snapshot.get("file_count")) is not int
        or snapshot["file_count"] != len(snapshot["files"])
    ):
        raise EvidenceError(
            f"{label} file count does not match file manifest"
        )
    if (
        snapshot.get("file_manifest_sha256")
        != file_manifest_sha256(snapshot["files"])
    ):
        raise EvidenceError(
            f"{label} file manifest SHA-256 does not match"
        )
    expected = build_candidate_snapshot(
        root,
        candidate_mode=snapshot["candidate_mode"],
        base_head=snapshot.get("base_head"),
        target_head=snapshot.get("target_head"),
        reviewed_head=snapshot.get("reviewed_head"),
    )
    for field in (
        "branch",
        "candidate_mode",
        "base_head",
        "target_head",
        "reviewed_head",
        "worktree_clean",
        "status_sha256",
        "code_fingerprint",
        "file_count",
        "file_manifest_sha256",
        "evidence_status",
        "candidate_scope",
        "excluded_changes",
        "files",
    ):
        if snapshot.get(field) != expected.get(field):
            raise EvidenceError(
                f"{label} does not match repository: {field}"
            )
    if snapshot != expected:
        raise EvidenceError(f"{label} contains unexpected fields")
    return expected


def write_candidate_snapshot(
    root: Path,
    output_path: Path,
    *,
    candidate_mode: str = "working_tree",
    base_head: str | None = None,
    target_head: str | None = None,
    reviewed_head: str | None = None,
) -> None:
    _require_persistent_publication_ancestry(
        "candidate snapshot publication"
    )
    root = _canonical_absolute_path(root)
    output_path = _canonical_absolute_path(output_path)
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
    with _trusted_repository_scope(root) as repository_root:
        output_root = repository_root.ensure_directory(
            output_path.parent,
            "candidate snapshot output directory",
        )
        try:
            if output_path.name in output_root.directory_entries():
                raise EvidenceError(
                    f"candidate snapshot output already exists: {output_path}"
                )
            snapshot = build_candidate_snapshot(
                root,
                candidate_mode=candidate_mode,
                base_head=base_head,
                target_head=target_head,
                reviewed_head=reviewed_head,
            )
            output_root.write_bytes_atomic(
                output_path,
                canonical_json(snapshot).encode("utf-8"),
                "candidate snapshot",
            )
        finally:
            output_root.close()


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


def _initialize_test_environment(root: Path) -> None:
    for path in (root, root / "src"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def discover_tests(root: Path) -> tuple[list[unittest.TestCase], list[unittest.TestCase]]:
    _initialize_test_environment(root)
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


def _test_selection_rules_sha256() -> str:
    return sha256_bytes(
        canonical_json(
            {
                "version": TEST_SELECTION_RULES_VERSION,
                "blocked_terms": list(BLOCKED_TEST_TERMS),
                "blocked_modules": list(BLOCKED_TEST_MODULES),
            }
        ).encode("utf-8")
    )


def _test_discovery_manifest(
    root: Path,
    *,
    selected: list[unittest.TestCase] | None = None,
    filtered: list[unittest.TestCase] | None = None,
) -> dict[str, Any]:
    if (selected is None) != (filtered is None):
        raise EvidenceError(
            "test discovery manifest requires both selected and filtered tests"
        )
    if selected is None or filtered is None:
        selected, filtered = discover_tests(root)
    failed_tests = [
        test.id()
        for test in [*selected, *filtered]
        if test.__class__.__name__ == "_FailedTest"
    ]
    if failed_tests:
        raise EvidenceError(
            "test discovery encountered import failures: "
            f"{sorted(failed_tests)}"
        )
    selected_ids = sorted(test.id() for test in selected)
    filtered_ids = sorted(test.id() for test in filtered)
    if len(selected_ids) != len(set(selected_ids)):
        raise EvidenceError("selected test discovery contains duplicate IDs")
    if len(filtered_ids) != len(set(filtered_ids)):
        raise EvidenceError("filtered test discovery contains duplicate IDs")
    overlap = set(selected_ids) & set(filtered_ids)
    if overlap:
        raise EvidenceError(
            f"selected and filtered tests overlap: {sorted(overlap)}"
        )
    if any(is_blocked_test_id(test_id) for test_id in selected_ids):
        raise EvidenceError("test discovery selected a blocked test")
    if any(not is_blocked_test_id(test_id) for test_id in filtered_ids):
        raise EvidenceError(
            "test discovery filtered a test outside the blocked predicate"
        )
    return {
        "schema_version": "task-control-center-test-discovery-v1",
        "selection_rules_version": TEST_SELECTION_RULES_VERSION,
        "selection_rules_sha256": _test_selection_rules_sha256(),
        "selected_tests": selected_ids,
        "filtered_tests": filtered_ids,
        "discovered_tests": sorted(selected_ids + filtered_ids),
    }


class _ExecutionBindingResult(unittest.TextTestResult):
    """Track exactly which tests the runner started and stopped."""

    def __init__(
        self,
        stream: Any,
        descriptions: bool,
        verbosity: int,
        *,
        expected_ids: Iterable[str],
    ) -> None:
        super().__init__(stream, descriptions, verbosity)
        self.expected_ids = frozenset(expected_ids)
        self.started_ids: list[str] = []
        self.stopped_ids: list[str] = []
        self.unexpected_ids: list[str] = []
        self.duplicate_start_ids: list[str] = []
        self._started: set[str] = set()

    def startTest(self, test: unittest.TestCase) -> None:
        test_id = test.id()
        if test_id not in self.expected_ids:
            self.unexpected_ids.append(test_id)
        if test_id in self._started:
            self.duplicate_start_ids.append(test_id)
        self._started.add(test_id)
        self.started_ids.append(test_id)
        super().startTest(test)

    def stopTest(self, test: unittest.TestCase) -> None:
        self.stopped_ids.append(test.id())
        super().stopTest(test)


def _assert_unittest_execution_primitives_unchanged() -> None:
    current = {
        "TextTestRunner": unittest.TextTestRunner,
        "TextTestRunner.run": unittest.TextTestRunner.run,
        "TestSuite.run": unittest.TestSuite.run,
        "TestCase.run": unittest.TestCase.run,
        "TestResult.startTest": unittest.TestResult.startTest,
        "TestResult.stopTest": unittest.TestResult.stopTest,
        "TestResult.addSuccess": unittest.TestResult.addSuccess,
        "TestResult.addFailure": unittest.TestResult.addFailure,
        "TestResult.addError": unittest.TestResult.addError,
        "TestResult.addSkip": unittest.TestResult.addSkip,
        "TestResult.addExpectedFailure": (
            unittest.TestResult.addExpectedFailure
        ),
        "TestResult.addUnexpectedSuccess": (
            unittest.TestResult.addUnexpectedSuccess
        ),
    }
    changed = sorted(
        name
        for name, original in _ORIGINAL_UNITTEST_PRIMITIVES.items()
        if current[name] is not original
    )
    if changed:
        raise EvidenceError(
            "candidate tests modified unittest execution primitives: "
            f"{changed}"
        )


def _execute_test_collection(
    selected: list[unittest.TestCase],
    *,
    output: Any,
) -> tuple[unittest.TestResult, list[dict[str, str]]]:
    _assert_unittest_execution_primitives_unchanged()
    selected_order = [test.id() for test in selected]
    selected_ids = sorted(selected_order)
    blocked_tests = sorted(
        test_id for test_id in selected_ids if is_blocked_test_id(test_id)
    )
    if blocked_tests:
        raise EvidenceError(
            "test execution refused blocked IDs: "
            f"{blocked_tests}"
        )
    if len(selected_ids) != len(set(selected_ids)):
        raise EvidenceError("selected test collection contains duplicate IDs")
    modified_tests = sorted(
        test.id()
        for test in selected
        if type(test).run is not unittest.TestCase.run
    )
    if modified_tests:
        raise EvidenceError(
            "candidate tests override unittest.TestCase.run: "
            f"{modified_tests}"
        )

    result_holder: list[_ExecutionBindingResult] = []

    def result_class(
        stream: Any,
        descriptions: bool,
        verbosity: int,
    ) -> _ExecutionBindingResult:
        result = _ExecutionBindingResult(
            stream,
            descriptions,
            verbosity,
            expected_ids=selected_ids,
        )
        result_holder.append(result)
        return result

    result = unittest.TextTestRunner(
        stream=output,
        verbosity=1,
        resultclass=result_class,
    ).run(unittest.TestSuite(selected))
    _assert_unittest_execution_primitives_unchanged()
    if len(result_holder) != 1:
        raise EvidenceError("test runner did not create exactly one result tracker")
    tracker = result_holder[0]
    if tracker.unexpected_ids:
        raise EvidenceError(
            "test runner executed IDs outside the fixed collection: "
            f"{sorted(set(tracker.unexpected_ids))}"
        )
    if tracker.duplicate_start_ids:
        raise EvidenceError(
            "test runner executed a selected ID more than once: "
            f"{sorted(set(tracker.duplicate_start_ids))}"
        )
    if tracker.started_ids != selected_order:
        raise EvidenceError(
            "test runner start order/set does not match the fixed collection"
        )
    if tracker.stopped_ids != selected_order:
        raise EvidenceError(
            "test runner stop set does not match the fixed collection"
        )

    outcomes: dict[str, str] = {}

    def record_outcome(test: unittest.TestCase, outcome: str) -> None:
        parent_test = getattr(test, "test_case", None)
        attributed_test = (
            parent_test
            if isinstance(parent_test, unittest.TestCase)
            else test
        )
        if not isinstance(attributed_test, unittest.TestCase):
            raise EvidenceError(
                "test result could not be attributed to a TestCase"
            )
        test_id = attributed_test.id()
        if test_id not in tracker.expected_ids:
            raise EvidenceError(
                "test result could not be attributed to fixed collection: "
                f"{test_id}"
            )
        existing_outcome = outcomes.get(test_id)
        if existing_outcome is not None and existing_outcome != outcome:
            raise EvidenceError(
                f"test produced conflicting terminal outcomes: {test_id}"
            )
        outcomes[test_id] = outcome

    for outcome, entries in (
        ("failed", result.failures),
        ("error", result.errors),
        ("skipped", result.skipped),
        ("expected_failure", result.expectedFailures),
    ):
        for entry in entries:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise EvidenceError(
                    f"test result has malformed {outcome} entry"
                )
            test, _detail = entry
            record_outcome(test, outcome)
    for test in result.unexpectedSuccesses:
        record_outcome(test, "unexpected_success")
    for test_id in selected_ids:
        outcomes.setdefault(test_id, "passed")
    return result, [
        {"test": test_id, "outcome": outcomes[test_id]}
        for test_id in selected_ids
    ]


def _current_test_discovery(root: Path) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    script = _canonical_absolute_path(Path(__file__))
    _assert_trusted_directory_path(root, "test discovery repository root")
    completed = _run_subprocess_in_trusted_cwd(
        root,
        [
            sys.executable,
            "-B",
            str(script),
            "_discover-tests",
            "--root",
            str(root),
            "--cwd-root",
        ],
        "test discovery repository root",
    )
    _assert_trusted_directory_path(root, "test discovery repository root")
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise EvidenceError(f"current test discovery failed: {detail}")
    discovery = _parse_json_object_bytes(
        completed.stdout,
        Path("<test-discovery-stdout>"),
    )
    _validate_test_discovery_manifest(discovery, "current test discovery")
    return discovery


def _validate_test_discovery_manifest(
    discovery: Mapping[str, Any],
    label: str,
) -> None:
    _reject_unknown_fields(
        discovery,
        {
            "schema_version",
            "selection_rules_version",
            "selection_rules_sha256",
            "selected_tests",
            "filtered_tests",
            "discovered_tests",
        },
        label,
    )
    if discovery.get("schema_version") != (
        "task-control-center-test-discovery-v1"
    ):
        raise EvidenceError(f"{label} schema is unsupported")
    if discovery.get("selection_rules_version") != (
        TEST_SELECTION_RULES_VERSION
    ):
        raise EvidenceError(f"{label} selection rules are unsupported")
    if discovery.get("selection_rules_sha256") != (
        _test_selection_rules_sha256()
    ):
        raise EvidenceError(f"{label} selection rules hash mismatch")
    selected = discovery.get("selected_tests")
    filtered = discovery.get("filtered_tests")
    discovered = discovery.get("discovered_tests")
    for name, values in (
        ("selected_tests", selected),
        ("filtered_tests", filtered),
        ("discovered_tests", discovered),
    ):
        if not isinstance(values, list) or not all(
            isinstance(item, str) and item for item in values
        ):
            raise EvidenceError(f"{label}.{name} must be a string array")
        if values != sorted(values):
            raise EvidenceError(f"{label}.{name} must be sorted")
        if len(values) != len(set(values)):
            raise EvidenceError(f"{label}.{name} contains duplicate IDs")
    selected_ids = set(selected)
    filtered_ids = set(filtered)
    if selected_ids & filtered_ids:
        raise EvidenceError(f"{label} selected/filtered tests overlap")
    if set(discovered) != selected_ids | filtered_ids:
        raise EvidenceError(
            f"{label} discovered tests do not equal selected/filtered"
        )
    if len(discovered) != len(selected) + len(filtered):
        raise EvidenceError(f"{label} discovered test count mismatch")
    if any(is_blocked_test_id(item) for item in selected):
        raise EvidenceError(f"{label} selected a blocked test")
    if any(not is_blocked_test_id(item) for item in filtered):
        raise EvidenceError(
            f"{label} filtered a test outside the blocked predicate"
        )


def _validate_test_execution_binding(
    executed: Mapping[str, Any],
    discovery: Mapping[str, Any],
    counts: Mapping[str, Any],
    label: str,
) -> None:
    _reject_unknown_fields(
        executed,
        {"schema_version", "selected_tests", "results"},
        f"{label} execution",
    )
    if executed.get("schema_version") != (
        "task-control-center-test-execution-v1"
    ):
        raise EvidenceError(f"{label} execution schema is unsupported")
    selected = executed.get("selected_tests")
    results = executed.get("results")
    discovery_selected = discovery.get("selected_tests")
    if not isinstance(selected, list) or not all(
        isinstance(item, str) and item for item in selected
    ):
        raise EvidenceError(f"{label} executed selected tests are invalid")
    if selected != sorted(selected) or len(selected) != len(set(selected)):
        raise EvidenceError(f"{label} executed selected tests are not canonical")
    if selected != discovery_selected:
        raise EvidenceError(
            f"{label} executed selected tests do not match fixed discovery"
        )
    if not isinstance(results, list):
        raise EvidenceError(f"{label} execution results are missing")
    outcome_by_test: dict[str, str] = {}
    allowed_outcomes = {
        "passed",
        "failed",
        "error",
        "skipped",
        "expected_failure",
        "unexpected_success",
    }
    for index, item in enumerate(results):
        if not isinstance(item, Mapping):
            raise EvidenceError(f"{label} execution result[{index}] is invalid")
        _reject_unknown_fields(
            item,
            {"test", "outcome"},
            f"{label} execution result[{index}]",
        )
        test_id = item.get("test")
        outcome = item.get("outcome")
        if (
            not isinstance(test_id, str)
            or not test_id
            or outcome not in allowed_outcomes
        ):
            raise EvidenceError(
                f"{label} execution result[{index}] has invalid fields"
            )
        if test_id in outcome_by_test:
            raise EvidenceError(
                f"{label} execution result repeats test ID: {test_id}"
            )
        outcome_by_test[test_id] = str(outcome)
    if sorted(outcome_by_test) != selected:
        raise EvidenceError(
            f"{label} execution outcomes do not match executed test IDs"
        )
    derived = {
        "passed": sum(item == "passed" for item in outcome_by_test.values()),
        "failed": sum(item == "failed" for item in outcome_by_test.values()),
        "errors": sum(item == "error" for item in outcome_by_test.values()),
        "skipped": sum(item == "skipped" for item in outcome_by_test.values()),
        "expected_failures": sum(
            item == "expected_failure" for item in outcome_by_test.values()
        ),
        "unexpected_successes": sum(
            item == "unexpected_success" for item in outcome_by_test.values()
        ),
    }
    for field, value in derived.items():
        if counts.get(field) != value:
            raise EvidenceError(
                f"{label} counts disagree with per-test execution: {field}"
            )


def _run_tests(
    root: Path,
    *,
    result_path: Path,
    log_path: Path,
    use_cwd_root: bool = False,
) -> int:
    root = _canonical_absolute_path(
        Path.cwd() if use_cwd_root else root
    )
    with _trusted_repository_scope(root):
        return _run_tests_impl(
            root,
            result_path=result_path,
            log_path=log_path,
            use_cwd_root=use_cwd_root,
        )


def _run_tests_impl(
    root: Path,
    *,
    result_path: Path,
    log_path: Path,
    use_cwd_root: bool = False,
) -> int:
    if use_cwd_root:
        root = _canonical_absolute_path(Path.cwd())
    _initialize_test_environment(root)
    before = code_fingerprint(root)
    selected, filtered = discover_tests(root)
    discovery = _test_discovery_manifest(
        root,
        selected=selected,
        filtered=filtered,
    )
    output = _CaptureStream()
    result, executed_tests = _execute_test_collection(
        selected,
        output=output,
    )
    after = code_fingerprint(root)
    outcome_counts = {
        outcome: sum(
            1 for item in executed_tests if item["outcome"] == outcome
        )
        for outcome in (
            "passed",
            "failed",
            "error",
            "skipped",
            "expected_failure",
            "unexpected_success",
        )
    }
    counts = {
        "discovered": len(selected) + len(filtered),
        "selected": len(selected),
        "filtered": len(filtered),
        "passed": outcome_counts["passed"],
        "failed": outcome_counts["failed"],
        "errors": outcome_counts["error"],
        "skipped": outcome_counts["skipped"],
        "expected_failures": outcome_counts["expected_failure"],
        "unexpected_successes": outcome_counts["unexpected_success"],
    }
    executed_payload = {
        "schema_version": "task-control-center-test-execution-v1",
        "selected_tests": sorted(test.id() for test in selected),
        "results": executed_tests,
    }
    payload = {
        "schema_version": TEST_RESULT_SCHEMA_VERSION,
        "python": sys.executable,
        "code_fingerprint_before": before,
        "code_fingerprint_after": after,
        "counts": counts,
        "discovery": discovery,
        "discovery_sha256": sha256_bytes(
            canonical_json(discovery).encode("utf-8")
        ),
        "executed": executed_payload,
        "executed_sha256": sha256_bytes(
            canonical_json(executed_payload).encode("utf-8")
        ),
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


def _filtered_mutation_cases(
    root: Path,
) -> tuple[
    dict[str, Any],
    dict[str, int],
    list[str],
    list[str],
]:
    source = read_json(root / "tests" / "mutation_cases.json", root=root)
    return _filter_mutation_source(source)


def _filter_mutation_source(
    source: Mapping[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, int],
    list[str],
    list[str],
]:
    cases = source.get("cases")
    if not isinstance(cases, list):
        raise EvidenceError("mutation_cases.json has no cases array")
    selected = []
    filtered = []
    selected_ids: list[str] = []
    filtered_ids: list[str] = []
    seen_ids: set[str] = set()
    for case in cases:
        tests = case.get("tests") if isinstance(case, dict) else None
        if not isinstance(tests, list):
            raise EvidenceError("mutation case has invalid tests field")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise EvidenceError("mutation case has no valid ID")
        if case_id in seen_ids:
            raise EvidenceError(f"mutation case ID is duplicated: {case_id}")
        seen_ids.add(case_id)
        test_text = " ".join(str(test) for test in tests)
        blocked = any(
            term in test_text.lower()
            for term in BLOCKED_TEST_TERMS
        ) or any(module in test_text for module in BLOCKED_TEST_MODULES)
        if blocked:
            filtered.append(case)
            filtered_ids.append(case_id)
        else:
            selected.append(case)
            selected_ids.append(case_id)
    return (
        {"version": source.get("version"), "cases": selected},
        {
            "declared": len(cases),
            "selected": len(selected),
            "filtered": len(filtered),
        },
        selected_ids,
        filtered_ids,
    )


def _write_bootstrap_input_status(
    output_dir: Path,
    *,
    reason: str,
    missing_inputs: list[str] | None = None,
    evidence_status: str = BOOTSTRAP_INCOMPLETE,
    input_identity: Mapping[str, Any] | None = None,
    output_root: TrustedEvidenceRoot,
) -> Path:
    path = output_dir / "previous-summary-status.json"
    data = canonical_json(
        {
            "schema_version": "task-control-center-bootstrap-input-v1",
            "generator_version": EVIDENCE_TOOL_VERSION,
            "candidate_mode": "bootstrap",
            "evidence_status": evidence_status,
            "previous_summary": None,
            "missing_inputs": list(missing_inputs or []),
            "input_identity": dict(input_identity or {}),
            "reason": reason,
            "acceptance_supported": False,
            "merge_ready": False,
        }
    ).encode("utf-8")
    output_root._write_bytes_staging(
        path,
        data,
        "bootstrap input status",
    )
    return path


def _prepare_mutation_project_snapshot(
    root: Path,
    child_root: TrustedEvidenceRoot,
    child_output_dir: Path,
    *,
    expected_code_fingerprint: str | None = None,
) -> TrustedEvidenceRoot:
    """Copy the mutation inputs from the pinned repository into a private tree."""
    root = _canonical_absolute_path(root)
    root_fds: list[int] = _OwnedFDCollection()
    try:
        root_fd = _open_absolute_directory_owned(
            root_fds,
            root,
            "mutation source root",
        )
        _assert_trusted_directory_identity(
            root_fd,
            root,
            "mutation source root",
        )
        relative_paths = _candidate_file_paths(root)
        captured_metadata: dict[
            str,
            tuple[int, int, int, int, int, int],
        ] = {}
        captured = _capture_candidate_file_bytes(
            root,
            relative_paths,
            captured_metadata=captured_metadata,
        )
        _verify_captured_candidate_generation(
            root,
            captured,
            captured_metadata,
        )
        if _candidate_file_paths(root) != relative_paths:
            raise EvidenceError(
                "mutation project source path set changed during capture"
            )
        observed_fingerprint = _code_fingerprint_from_captured(captured)
        if (
            expected_code_fingerprint is not None
            and observed_fingerprint != expected_code_fingerprint
        ):
            raise EvidenceError(
                "mutation project source fingerprint changed during capture"
            )
        selected = [
            relative
            for relative in relative_paths
            if relative == "pyproject.toml"
            or relative.startswith("src/")
            or relative.startswith("tests/")
        ]
        if "tests/mutation_runner.py" not in selected:
            raise EvidenceError("mutation runner is missing from the candidate")
        from tests import mutation_runner as mutation_runner_module

        project_root = child_output_dir / "mutation-project"
        staging_root = child_root.ensure_directory(
            project_root,
            "mutation staging root",
        )
        try:
            os.fchmod(staging_root.assert_active(), 0o700)
            try:
                mutation_runner_module._assert_exclusive_private_directory(
                    staging_root.assert_active(),
                    "mutation staging root",
                )
            except mutation_runner_module.UnsafePatchError as exc:
                raise EvidenceError(
                    "mutation staging root is not exclusively private"
                ) from exc
            for relative in selected:
                safe_relative = _validate_relative_candidate_path(
                    relative,
                    "mutation project",
                )
                data = captured[safe_relative]
                staging_root._write_bytes_staging(
                    staging_root.path / safe_relative,
                    data,
                    f"mutation snapshot {safe_relative}",
                )
            _verify_captured_candidate_generation(
                root,
                captured,
                captured_metadata,
            )
            if _candidate_file_paths(root) != relative_paths:
                raise EvidenceError(
                    "mutation project source path set changed during staging"
                )
            _assert_trusted_directory_identity(
                root_fd,
                root,
                "mutation source root",
            )
            return staging_root
        except BaseException:
            staging_root.close()
            raise
    finally:
        _close_owned_fds(root_fds, "mutation source root")


@contextmanager
def _owned_mutation_project_snapshot(
    root: Path,
    child_root: TrustedEvidenceRoot,
    child_output_dir: Path,
    *,
    expected_code_fingerprint: str | None = None,
):
    project_root_owner = None
    try:
        with _defer_keyboard_interrupt():
            project_root_owner = _prepare_mutation_project_snapshot(
                root,
                child_root,
                child_output_dir,
                expected_code_fingerprint=expected_code_fingerprint,
            )
        yield project_root_owner
    finally:
        if project_root_owner is not None:
            project_root_owner.close()


def _collect_run_evidence_outputs(
    root: Path,
    output_dir: Path,
    *,
    code_before: str,
    output_root: TrustedEvidenceRoot,
) -> tuple[dict[str, Any], dict[str, int], Path, Path]:
    script_bytes = _read_regular_file(
        _canonical_absolute_path(Path(__file__)),
        "evidence tool script",
        max_bytes=MAX_EVIDENCE_FILE_BYTES,
    )
    system_python = Path(shutil.which("python3") or "/usr/bin/python3")
    venv_python = root / ".venv" / "bin" / "python"
    if system_python.absolute() == venv_python.absolute():
        raise EvidenceError(
            "system and virtual-environment Python must be distinct"
        )
    with tempfile.TemporaryDirectory(
        prefix="zevorinth-evidence-child-"
    ) as child_temp:
        child_output_dir = _canonical_absolute_path(Path(child_temp))
        with TrustedEvidenceRoot(
            child_output_dir,
            "evidence child output root",
        ) as child_root:
            with child_root.pinned():
                child_script_path = child_output_dir / "evidence-tool.py"
                child_root._write_bytes_staging(
                    child_script_path,
                    script_bytes,
                    "child evidence tool",
                )
                test_evidence: dict[str, Any] = {}
                for name, python in (
                    ("system_python", system_python),
                    ("venv_python", venv_python),
                ):
                    if not python.is_file():
                        raise EvidenceError(
                            f"{name} interpreter is missing: {python}"
                        )
                    result_path = output_dir / f"{name}-result.json"
                    log_path = output_dir / f"{name}.log"
                    child_result_path = child_output_dir / result_path.name
                    child_log_path = child_output_dir / log_path.name
                    _assert_trusted_directory_path(
                        root,
                        "repository root",
                    )
                    completed = _run_subprocess_in_trusted_cwd(
                        root,
                        [
                            str(python),
                            str(child_script_path),
                            "_run-tests",
                            "--root",
                            str(root),
                            "--result",
                            str(child_result_path),
                            "--log",
                            str(child_log_path),
                            "--cwd-root",
                        ],
                        "test child repository root",
                    )
                    _assert_trusted_directory_path(
                        root,
                        "repository root",
                    )
                    if completed.returncode != 0:
                        raise EvidenceError(
                            f"{name} tests failed with exit "
                            f"{completed.returncode}: "
                            f"{completed.stderr.decode('utf-8', errors='replace').strip()}"
                        )
                    result_bytes = _read_regular_file(
                        child_result_path,
                        f"{name} child result",
                        root=child_output_dir,
                    )
                    log_bytes = _read_regular_file(
                        child_log_path,
                        f"{name} child log",
                        root=child_output_dir,
                    )
                    output_root._write_bytes_staging(
                        result_path,
                        result_bytes,
                        f"{name} result",
                    )
                    output_root._write_bytes_staging(
                        log_path,
                        log_bytes,
                        f"{name} log",
                    )
                    result = _parse_json_object_bytes(
                        result_bytes,
                        child_result_path,
                    )
                    if result.get("successful") is not True:
                        raise EvidenceError(
                            f"{name} tests did not report success"
                        )
                    if (
                        result.get("code_fingerprint_before")
                        != code_before
                        or result.get("code_fingerprint_after")
                        != code_before
                    ):
                        raise EvidenceError(
                            f"{name} tests did not preserve candidate identity"
                        )
                    test_evidence[name] = {
                        "result": str(result_path.relative_to(root)),
                        "result_sha256": sha256_bytes(result_bytes),
                        "log": str(log_path.relative_to(root)),
                        "log_sha256": sha256_bytes(log_bytes),
                    }

                cases_path = output_dir / "mutation-cases.json"
                (
                    cases_payload,
                    mutation_filters,
                    _selected_ids,
                    _filtered_ids,
                ) = _filtered_mutation_cases(root)
                cases_bytes = canonical_json(cases_payload).encode("utf-8")
                output_root._write_bytes_staging(
                    cases_path,
                    cases_bytes,
                    "mutation cases",
                )

                mutations_path = output_dir / "mutations.json"
                child_mutations_path = (
                    child_output_dir / mutations_path.name
                )
                with _owned_mutation_project_snapshot(
                    root,
                    child_root,
                    child_output_dir,
                    expected_code_fingerprint=code_before,
                ) as project_root_owner:
                    project_root_path = project_root_owner.path
                    child_cases_path = (
                        project_root_path / "mutation-cases.json"
                    )
                    project_root_owner._write_bytes_staging(
                        child_cases_path,
                        cases_bytes,
                        "child mutation cases",
                    )
                    with project_root_owner.pinned():
                        mutation_run = _run_subprocess_in_trusted_cwd(
                            project_root_path,
                            [
                                str(venv_python),
                                "tests/mutation_runner.py",
                                "--cases",
                                "mutation-cases.json",
                                "--output",
                                str(child_mutations_path),
                            ],
                            "mutation project snapshot",
                        )
                _assert_trusted_directory_path(
                    root,
                    "repository root",
                )
                if mutation_run.returncode != 0:
                    raise EvidenceError(
                        "mutation testing failed: "
                        + mutation_run.stderr.decode(
                            "utf-8",
                            errors="replace",
                        ).strip()
                    )
                mutations_bytes = _read_regular_file(
                    child_mutations_path,
                    "child mutation report",
                    root=child_output_dir,
                )
                output_root._write_bytes_staging(
                    mutations_path,
                    mutations_bytes,
                    "mutation report",
                )
    return (
        test_evidence,
        mutation_filters,
        cases_path,
        mutations_path,
    )


def run_evidence(
    root: Path,
    output_dir: Path,
    review_manifest_path: Path | None,
    *,
    previous_summary_path: Path | None = None,
    evidence_bundle_path: Path | None = None,
    review_binding_path: Path | None = None,
    acceptance_policy: str = "target_head_review_required",
) -> int:
    root = _canonical_absolute_path(root)
    with _trusted_repository_scope(root) as repository_root:
        output_dir = _validate_output_path(
            root,
            output_dir,
            directory=True,
        )
        output_root = repository_root.ensure_directory(
            output_dir,
            "evidence output directory",
        )
        try:
            if output_root.directory_entries():
                raise EvidenceError(
                    f"evidence output directory is not empty: {output_dir}"
                )
            if evidence_bundle_path is None:
                return _run_evidence_impl(
                    root,
                    output_dir,
                    review_manifest_path,
                    previous_summary_path=previous_summary_path,
                    evidence_bundle_path=None,
                    review_binding_path=review_binding_path,
                    acceptance_policy=acceptance_policy,
                    trusted_bundle_root=None,
                    repository_root=repository_root,
                    output_root=output_root,
                )
            bundle_root = _canonical_absolute_path(evidence_bundle_path).parent
            try:
                trusted = TrustedEvidenceRoot(
                    bundle_root,
                    "evidence run bundle root",
                )
            except EvidenceError:
                return _run_evidence_impl(
                    root,
                    output_dir,
                    review_manifest_path,
                    previous_summary_path=previous_summary_path,
                    evidence_bundle_path=evidence_bundle_path,
                    review_binding_path=review_binding_path,
                    acceptance_policy=acceptance_policy,
                    trusted_bundle_root=None,
                    repository_root=repository_root,
                    output_root=output_root,
                )
            with trusted:
                with trusted.pinned():
                    return _run_evidence_impl(
                        root,
                        output_dir,
                        review_manifest_path,
                        previous_summary_path=previous_summary_path,
                        evidence_bundle_path=evidence_bundle_path,
                        review_binding_path=review_binding_path,
                        acceptance_policy=acceptance_policy,
                        trusted_bundle_root=trusted,
                        repository_root=repository_root,
                        output_root=output_root,
                    )
        finally:
            output_root.close()


def _run_evidence_impl(
    root: Path,
    output_dir: Path,
    review_manifest_path: Path | None,
    *,
    previous_summary_path: Path | None = None,
    evidence_bundle_path: Path | None = None,
    review_binding_path: Path | None = None,
    acceptance_policy: str = "target_head_review_required",
    trusted_bundle_root: TrustedEvidenceRoot | None = None,
    repository_root: TrustedEvidenceRoot | None = None,
    output_root: TrustedEvidenceRoot | None = None,
) -> int:
    root = _canonical_absolute_path(root)
    if repository_root is None or output_root is None:
        raise EvidenceError("run evidence requires trusted repository roots")
    metadata_path = output_dir / "run-metadata.json"
    if metadata_path.name in output_root.directory_entries():
        raise EvidenceError(
            f"output directory already contains evidence: {metadata_path}"
        )
    if acceptance_policy not in ACCEPTANCE_POLICIES:
        raise EvidenceError("unsupported acceptance policy")
    bundle = None
    bundle_manifest_sha_before = None
    bundle_file_hashes_before: dict[str, str] = {}
    candidate_identity_record = None
    review_binding_record = None
    if evidence_bundle_path is not None:
        if review_manifest_path is not None or review_binding_path is not None:
            raise EvidenceError(
                "evidence bundle cannot be combined with legacy review inputs"
            )
        try:
            if trusted_bundle_root is None:
                raise EvidenceError("evidence bundle is NOT_AVAILABLE")
            bundle = _validate_evidence_bundle_with_root(
                root,
                evidence_bundle_path,
                trusted_bundle_root,
            )
            bundle_root = Path(bundle["bundle_root"])
            bundle_path = Path(bundle["bundle_manifest_path"])
            bundle_manifest_sha_before = sha256_file(
                bundle_path,
                root=bundle_root,
            )
            bundle_file_hashes_before = {
                relative: sha256_file(
                    bundle_root / relative,
                    root=bundle_root,
                )
                for relative in bundle["file_paths"]
            }
            previous_source = _evidence_root_file(
                bundle_root,
                bundle["previous_summary"]["path"],
                "bundle previous summary",
            )
            review_manifest = _evidence_root_file(
                bundle_root,
                bundle["review_manifest_reference"]["path"],
                "bundle review manifest",
            )
            previous_source_bytes = _read_regular_file(
                previous_source,
                "bundle previous summary",
                root=bundle_root,
            )
            acceptance_policy = resolve_acceptance_policy(
                acceptance_policy,
                bundle["acceptance_policy"],
            )
        except EvidenceError as exc:
            _write_bootstrap_input_status(
                output_dir,
                reason=str(exc),
                missing_inputs=["valid_evidence_bundle"],
                evidence_status=(
                    NOT_AVAILABLE
                    if (
                        "NOT_AVAILABLE" in str(exc)
                        or "No such file" in str(exc)
                    )
                    else BLOCKED
                ),
                input_identity={
                    "evidence_bundle": str(evidence_bundle_path),
                },
                output_root=output_root,
            )
            raise EvidenceError(
                "evidence bundle input is unavailable or invalid; acceptance is closed"
            ) from exc
    else:
        missing_inputs = []
        if previous_summary_path is None:
            missing_inputs.append("previous_summary")
        if review_manifest_path is None and review_binding_path is None:
            missing_inputs.append("review_manifest_or_evidence_bundle")
        if missing_inputs:
            _write_bootstrap_input_status(
                output_dir,
                reason=(
                    "required historical inputs were not provided: "
                    + ", ".join(missing_inputs)
                ),
                missing_inputs=missing_inputs,
                input_identity={
                    "previous_summary": (
                        str(previous_summary_path)
                        if previous_summary_path is not None
                        else None
                    ),
                    "review_manifest": (
                        str(review_manifest_path)
                        if review_manifest_path is not None
                        else None
                    ),
                    "review_binding": (
                        str(review_binding_path)
                        if review_binding_path is not None
                        else None
                    ),
                },
                output_root=output_root,
            )
            raise EvidenceError(
                "required historical inputs are missing; previous summary "
                "input is required; bootstrap evidence is "
                f"{BOOTSTRAP_INCOMPLETE}"
            )
        try:
            previous_source = _evidence_file(
                root,
                evidence_path(previous_summary_path, root),
                "previous summary input",
            )
            previous_source_bytes = _read_regular_file(
                previous_source,
                "previous summary input",
                root=root,
            )
        except EvidenceError as exc:
            _write_bootstrap_input_status(
                output_dir,
                reason=str(exc),
                missing_inputs=["valid_previous_summary"],
                input_identity={
                    "previous_summary": str(previous_summary_path),
                    "review_manifest": (
                        str(review_manifest_path)
                        if review_manifest_path is not None
                        else None
                    ),
                    "review_binding": (
                        str(review_binding_path)
                        if review_binding_path is not None
                        else None
                    ),
                },
                output_root=output_root,
            )
            raise EvidenceError(
                "previous summary input is unavailable; bootstrap evidence is "
                f"{BOOTSTRAP_INCOMPLETE}"
            ) from exc

    previous_path = output_dir / "previous-summary.json"
    output_root._write_bytes_staging(
        previous_path,
        previous_source_bytes,
        "previous summary snapshot",
    )
    code_before = code_fingerprint(root)
    git_before = git_state(root)
    if bundle is None:
        if review_binding_path is not None:
            binding_path = _evidence_file(
                root,
                evidence_path(review_binding_path, root),
                "review binding",
            )
            binding = validate_review_binding(root, binding_path)
            review_binding_record = {
                "path": evidence_path(binding_path, root),
                "sha256": sha256_file(binding_path, root=root),
            }
            candidate = build_candidate_snapshot(
                root,
                candidate_mode="committed_range",
                base_head=binding["base_head"],
                target_head=binding["target_head"],
                reviewed_head=binding["reviewed_head"],
            )
            review_manifest = _evidence_file(
                root,
                binding["review_manifest"]["path"],
                "review manifest",
            )
        else:
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
            candidate = build_candidate_snapshot(root)
        candidate_path = output_dir / "candidate-identity.json"
        output_root._write_bytes_staging(
            candidate_path,
            canonical_json(candidate).encode("utf-8"),
            "candidate identity",
        )
        candidate_identity_record = {
            "path": str(candidate_path.relative_to(root)),
            "sha256": sha256_file(candidate_path, root=root),
        }
    (
        test_evidence,
        mutation_filters,
        cases_path,
        mutations_path,
    ) = _collect_run_evidence_outputs(
        root,
        output_dir,
        code_before=code_before,
        output_root=output_root,
    )
    code_after = code_fingerprint(root)
    if code_before != code_after:
        raise EvidenceError("code changed while evidence was being generated")
    git_after = git_state(root)
    if git_before["head"] != git_after["head"]:
        raise EvidenceError("Git HEAD changed while evidence was being generated")
    if (
        bundle is not None
        and sha256_file(bundle_path, root=bundle_root)
        != bundle_manifest_sha_before
    ):
        raise EvidenceError("evidence bundle changed during generation")
    for relative, expected in bundle_file_hashes_before.items():
        observed = sha256_file(bundle_root / relative, root=bundle_root)
        if observed != expected:
            raise EvidenceError(
                f"evidence bundle file changed during generation: {relative}"
            )
    frozen = {}
    for relative, expected in FROZEN_EVIDENCE.items():
        path = root / relative
        try:
            _validate_hash(
                path,
                expected,
                f"frozen evidence {relative}",
                root=root,
            )
        except EvidenceError as exc:
            raise EvidenceError(f"frozen evidence mismatch: {relative}") from exc
        observed = sha256_file(path, root=root)
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
            "source_sha256": sha256_file(
                root / "tests" / "mutation_cases.json",
                root=root,
            ),
            "cases": str(cases_path.relative_to(root)),
            "cases_sha256": sha256_file(cases_path, root=root),
            "report": str(mutations_path.relative_to(root)),
            "report_sha256": sha256_file(mutations_path, root=root),
            "filters": mutation_filters,
        },
        "candidate_identity": candidate_identity_record,
        "evidence_bundle": (
            {
                "path": str(bundle_path),
                "sha256": bundle["manifest_sha256"],
            }
            if bundle is not None
            else None
        ),
        "review_binding": review_binding_record,
        "acceptance_policy": acceptance_policy,
        "review": {
            "path": evidence_path(review_manifest, root),
            "sha256": sha256_file(
                review_manifest,
                root=(bundle_root if bundle is not None else root),
            ),
        },
        "previous_summary": {
            "path": str(previous_path.relative_to(root)),
            "sha256": sha256_file(previous_path, root=root),
        },
        "frozen_evidence": frozen,
    }
    output_root._write_bytes_staging(
        metadata_path,
        canonical_json(metadata).encode("utf-8"),
        "run metadata",
    )
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
    _reject_unknown_fields(counts, set(required), f"{label} counts")
    for field in required:
        if type(counts.get(field)) is not int or counts[field] < 0:
            raise EvidenceError(f"{label}.{field} must be a non-negative integer")
    if any(
        counts[field] != 0
        for field in (
            "failed",
            "errors",
            "expected_failures",
            "unexpected_successes",
        )
    ):
        raise EvidenceError(
            f"{label} contains non-successful test outcomes"
        )
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


def _validate_hash(
    path: Path,
    expected: str,
    label: str,
    *,
    root: Path | None = None,
) -> None:
    observed = sha256_file(path, root=root)
    if observed != expected:
        raise EvidenceError(
            f"{label} SHA mismatch: expected {expected}, observed {observed}"
        )


def _evidence_file(root: Path, value: Any, label: str) -> Path:
    root = _canonical_absolute_path(root)
    relative = _validate_relative_candidate_path(value, label)
    path = root / relative
    _assert_no_symlink_components(root, path, label)
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise EvidenceError(f"{label} missing: {path}") from exc
    if not stat.S_ISREG(mode):
        raise EvidenceError(f"{label} missing: {path}")
    return path


def _evidence_root_file(
    evidence_root: Path,
    value: Any,
    label: str,
    *,
    reject_symlinks: bool = True,
) -> Path:
    evidence_root = _canonical_absolute_path(evidence_root)
    relative = _validate_relative_candidate_path(value, label)
    path = evidence_root / relative
    if reject_symlinks:
        _assert_no_symlink_components(evidence_root, path, label)
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise EvidenceError(f"{label} missing: {path}") from exc
    if not stat.S_ISREG(mode):
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
        _canonical_absolute_path(root)
        / "artifacts"
        / "audits"
        / "task-control-center-v1"
    )


def _validate_output_path(root: Path, value: Path, *, directory: bool) -> Path:
    resolved = _canonical_absolute_path(value)
    audit_root = _audit_output_root(root)
    _assert_trusted_directory_path(root, "repository root")
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


def _validate_review_repository_path(
    value: Any,
    root: Path,
    label: str,
) -> None:
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{label} must be a non-empty path string")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise EvidenceError(f"{label} must be an absolute path")
    if value != str(root):
        raise EvidenceError(
            f"{label} must use the exact canonical repository path"
        )


def validate_review_manifest(
    root: Path,
    review_manifest_path: Path,
    *,
    expected_code_fingerprint: str | None = None,
    expected_head: str | None = None,
    changed_candidate_paths: Iterable[str] | None = None,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    with _trusted_repository_scope(root):
        evidence_base = _canonical_absolute_path(evidence_root or root)
        with TrustedEvidenceRoot(
            evidence_base,
            "review manifest evidence root",
        ) as trusted_root:
            with trusted_root.pinned():
                return _validate_review_manifest_impl(
                    root,
                    review_manifest_path,
                    expected_code_fingerprint=expected_code_fingerprint,
                    expected_head=expected_head,
                    changed_candidate_paths=changed_candidate_paths,
                    evidence_root=evidence_base,
                )


@dataclass(frozen=True)
class _ReviewEvidenceSnapshot:
    evidence_json: str

    def summary(self) -> dict[str, Any]:
        return _parse_json_object_bytes(
            self.evidence_json.encode("utf-8"),
            Path("<review-evidence-snapshot>"),
        )


def _capture_review_evidence_snapshot(
    root: Path,
    review_manifest_path: Path,
    trusted_evidence_root: TrustedEvidenceRoot,
    *,
    expected_code_fingerprint: str | None = None,
    expected_head: str | None = None,
    changed_candidate_paths: Iterable[str] | None = None,
) -> _ReviewEvidenceSnapshot:
    with trusted_evidence_root.pinned():
        validated = _validate_review_manifest_impl(
            root,
            review_manifest_path,
            expected_code_fingerprint=expected_code_fingerprint,
            expected_head=expected_head,
            changed_candidate_paths=changed_candidate_paths,
            evidence_root=trusted_evidence_root.path,
        )
    return _ReviewEvidenceSnapshot(canonical_json(validated))


def _validate_review_scope_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise EvidenceError(f"{label} must be a non-empty-string array")
    if len(value) != len(set(value)):
        raise EvidenceError(f"{label} contains duplicate items")
    return list(value)


def _validate_review_manifest_impl(
    root: Path,
    review_manifest_path: Path,
    *,
    expected_code_fingerprint: str | None = None,
    expected_head: str | None = None,
    changed_candidate_paths: Iterable[str] | None = None,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    """Validate both independent review lanes and preserve their risk state."""
    root = _canonical_absolute_path(root)
    evidence_base = _protected_evidence_root(
        evidence_root or root,
        "review manifest evidence root",
    )
    manifest_path = _evidence_root_file(
        evidence_base,
        evidence_path(review_manifest_path, evidence_base),
        "review manifest",
    )
    manifest = read_json(manifest_path, root=evidence_base)
    if manifest.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise EvidenceError("unexpected independent review manifest schema")
    _reject_unknown_fields(
        manifest,
        {
            "schema_version",
            "scope",
            "repository",
            "head",
            "code_fingerprint",
            "code_review",
            "architecture_review",
        },
        "independent review manifest",
    )
    if manifest.get("scope") != REVIEW_SCOPE:
        raise EvidenceError("independent review manifest scope mismatch")
    repository = manifest.get("repository")
    if not isinstance(repository, str):
        raise EvidenceError("review manifest has no repository")
    _validate_review_repository_path(
        repository,
        root,
        "review manifest repository",
    )
    repository_git = git_state(root)
    review_head = (
        _resolve_commit(root, repository_git["head"], "current HEAD")
        if expected_head is None
        else _resolve_commit(root, expected_head, "expected review HEAD")
    )
    current_fingerprint = (
        expected_code_fingerprint
        if expected_code_fingerprint is not None
        else code_fingerprint(root)
    )
    if manifest.get("head") != review_head:
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
        _reject_unknown_fields(
            lane,
            {
                "status",
                "completed",
                "conclusion",
                "files_reviewed",
                "reason",
                "reviewed_files",
                "report",
                "report_sha256",
                "result",
                "result_sha256",
                "target_repository",
                "target_head",
                "target_code_fingerprint",
                "covered_scope",
                "uncovered_scope",
            },
            key,
        )
        status = _normalize_review_status(lane.get("status"), key)
        if status not in allowed_statuses:
            raise EvidenceError(f"{key} has unsupported status: {status}")
        completed = lane.get("completed")
        files_reviewed = lane.get("files_reviewed")
        if type(files_reviewed) is not int or files_reviewed < 0:
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
        lane_summary["conclusion"] = conclusion
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
            _reject_unknown_fields(
                item,
                {"path", "sha256"},
                f"{key}.reviewed_files",
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
                root=root,
            )
            reviewed_paths.add(relative)
        changed_paths = set(
            changed_relevant_files(root)
            if changed_candidate_paths is None
            else changed_candidate_paths
        )
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
        report_path = _evidence_root_file(
            evidence_base,
            lane.get("report"),
            f"{key}.report",
        )
        report_bytes = _read_regular_file(
            report_path,
            f"{key} report",
            root=evidence_base,
        )
        try:
            report_text = report_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvidenceError(f"{key} report is not UTF-8") from exc
        if not report_text.strip():
            raise EvidenceError(f"{key} report is empty")
        _validate_hash(
            report_path,
            str(lane.get("report_sha256")),
            f"{key} report",
            root=evidence_base,
        )
        if review_head not in report_text:
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
        result_path = _evidence_root_file(
            evidence_base,
            lane.get("result"),
            f"{key}.result",
        )
        _validate_hash(
            result_path,
            str(lane.get("result_sha256")),
            f"{key} result",
            root=evidence_base,
        )
        result = read_json(result_path, root=evidence_base)
        if result.get("schema_version") != REVIEW_RESULT_SCHEMA_VERSION:
            raise EvidenceError(f"{key} result has an unexpected schema")
        _reject_unknown_fields(
            result,
            {
                "schema_version",
                "lane",
                "status",
                "conclusion",
                "files_reviewed",
                "reviewed_files",
                "target_repository",
                "target_head",
                "target_code_fingerprint",
                "covered_scope",
                "uncovered_scope",
                "report",
                "report_sha256",
            },
            f"{key} result",
        )
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
        _validate_review_repository_path(
            lane["target_repository"],
            root,
            f"{key}.target_repository",
        )
        if lane.get("target_head") != review_head:
            raise EvidenceError(f"{key} reviewed a different HEAD")
        if lane.get("target_code_fingerprint") != current_fingerprint:
            raise EvidenceError(
                f"{key} reviewed a different code fingerprint"
            )
        covered_scope = _validate_review_scope_list(
            lane.get("covered_scope"),
            f"{key}.covered_scope",
        )
        if "uncovered_scope" not in lane:
            raise EvidenceError(f"{key}.uncovered_scope is required")
        uncovered_scope = _validate_review_scope_list(
            lane.get("uncovered_scope"),
            f"{key}.uncovered_scope",
        )
        overlap_scope = set(covered_scope) & set(uncovered_scope)
        if overlap_scope:
            raise EvidenceError(
                f"{key} scope is both covered and uncovered: "
                f"{sorted(overlap_scope)}"
            )
        if result.get("uncovered_scope") is None:
            raise EvidenceError(f"{key} result uncovered scope is missing")
        if result.get("uncovered_scope") != lane.get("uncovered_scope"):
            raise EvidenceError(f"{key} result uncovered scope mismatch")
        missing_scope = required_scope - set(covered_scope)
        if missing_scope:
            raise EvidenceError(
                f"{key} omitted required review scope: "
                f"{sorted(missing_scope)}"
            )
        lane_summary.update(
            {
                "conclusion": conclusion,
                "reviewed_files": reviewed_files,
                "report": lane["report"],
                "report_sha256": sha256_file(
                    report_path,
                    root=evidence_base,
                ),
                "result": lane["result"],
                "result_sha256": sha256_file(
                    result_path,
                    root=evidence_base,
                ),
                "target_repository": lane["target_repository"],
                "target_head": review_head,
                "target_code_fingerprint": current_fingerprint,
                "covered_scope": sorted(covered_scope),
                "uncovered_scope": sorted(uncovered_scope),
            }
        )
        validated[output_key] = status
        validated[key] = lane_summary

    code_status = validated["independent_code_review"]
    architecture_status = validated["independent_architecture_review"]
    uncovered_by_lane = {
        key: list(validated[key].get("uncovered_scope", []))
        for key in ("code_review", "architecture_review")
        if isinstance(validated.get(key), Mapping)
    }
    uncovered_review_scopes = [
        f"{key}:{scope}"
        for key, scopes in sorted(uncovered_by_lane.items())
        for scope in scopes
    ]
    merge_ready = (
        code_status == "CLEAR"
        and architecture_status == "CLEAR"
        and not uncovered_review_scopes
    )
    blocking_reasons = []
    if code_status != "CLEAR":
        blocking_reasons.append(f"code_review:{code_status}")
    if architecture_status != "CLEAR":
        blocking_reasons.append(
            f"architecture_review:{architecture_status}"
        )
    if uncovered_review_scopes:
        blocking_reasons.append("review_uncovered_scope")
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
            "path": _evidence_reference(manifest_path, root, evidence_base),
            "sha256": sha256_file(manifest_path, root=evidence_base),
            "review_clear": merge_ready,
            "merge_ready": False,
            "blocking_reasons": blocking_reasons,
            "uncovered_scope": uncovered_review_scopes,
            "candidate_acceptance": candidate_acceptance,
        }
    )
    reviewed_files_for_validation = validated["code_review"].get(
        "reviewed_files",
        validated["architecture_review"].get("reviewed_files", []),
    )
    _validate_review_summary_semantics(
        validated,
        {
            "repository": str(root),
            "target_head": review_head,
            "reviewed_head": review_head,
            "code_fingerprint": current_fingerprint,
            "files": reviewed_files_for_validation,
        },
        "independent review manifest",
    )
    return validated


def _binding_timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError("review binding timestamp must be a non-empty string")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("review binding timestamp is not ISO-8601") from exc
    return value


def build_review_binding(
    root: Path,
    review_manifest_path: Path,
    *,
    base_head: str,
    target_head: str,
    generated_at: str | None = None,
    evidence_root: Path | None = None,
    manifest_reference: str | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    with _trusted_repository_scope(root):
        evidence_base = _canonical_absolute_path(evidence_root or root)
        with TrustedEvidenceRoot(
            evidence_base,
            "review binding source root",
        ) as trusted_root:
            with trusted_root.pinned():
                return _build_review_binding_impl(
                    root,
                    review_manifest_path,
                    base_head=base_head,
                    target_head=target_head,
                    generated_at=generated_at,
                    evidence_root=evidence_base,
                    manifest_reference=manifest_reference,
                )


def _build_review_binding_impl(
    root: Path,
    review_manifest_path: Path,
    *,
    base_head: str,
    target_head: str,
    generated_at: str | None = None,
    evidence_root: Path | None = None,
    manifest_reference: str | None = None,
) -> dict[str, Any]:
    """Bind an unchanged reviewed file set to a newer committed candidate."""
    root = _canonical_absolute_path(root)
    evidence_base = _protected_evidence_root(
        evidence_root or root,
        "review binding source root",
    )
    manifest_path = _evidence_root_file(
        evidence_base,
        evidence_path(review_manifest_path, evidence_base),
        "review manifest",
    )
    manifest = read_json(manifest_path, root=evidence_base)
    if manifest.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise EvidenceError("unexpected independent review manifest schema")
    if manifest.get("scope") != REVIEW_SCOPE:
        raise EvidenceError("independent review manifest scope mismatch")
    if manifest.get("repository") != str(root):
        raise EvidenceError("review manifest repository does not match root")
    reviewed_head = _resolve_commit(
        root,
        manifest.get("head"),
        "reviewed HEAD",
    )
    target = _resolve_commit(root, target_head, "target HEAD")
    base = _resolve_commit(root, base_head, "base HEAD")
    candidate = build_candidate_snapshot(
        root,
        candidate_mode="committed_range",
        base_head=base,
        target_head=target,
        reviewed_head=reviewed_head,
    )
    candidate_paths = [item["path"] for item in candidate["files"]]
    review_summary = validate_review_manifest(
        root,
        manifest_path,
        expected_code_fingerprint=candidate["code_fingerprint"],
        expected_head=reviewed_head,
        changed_candidate_paths=candidate_paths,
        evidence_root=evidence_base,
    )
    for key in ("code_review", "architecture_review"):
        lane = review_summary.get(key)
        if not isinstance(lane, dict) or not lane.get("completed"):
            raise EvidenceError(
                f"{key} is unavailable; content-equivalence binding is impossible"
            )

    target_files = _commit_file_entries(root, target, candidate_paths)
    candidate_manifest = file_manifest_sha256(candidate["files"])
    target_manifest = file_manifest_sha256(target_files)
    if candidate_manifest != target_manifest:
        raise EvidenceError(
            "target commit file set or SHA-256 differs from the candidate worktree"
        )

    reviewed_files = review_summary["code_review"].get("reviewed_files")
    if not isinstance(reviewed_files, list) or not reviewed_files:
        raise EvidenceError("reviewed file manifest is empty or missing")
    reviewed_manifest = file_manifest_sha256(reviewed_files)
    if reviewed_manifest != target_manifest:
        reviewed_by_path = {
            item["path"]: item["sha256"]
            for item in reviewed_files
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("sha256"), str)
        }
        target_by_path = {
            item["path"]: item["sha256"]
            for item in target_files
        }
        if set(reviewed_by_path) != set(target_by_path):
            raise EvidenceError(
                "reviewed and target commit file sets are not identical"
            )
        changed = sorted(
            path
            for path in target_by_path
            if reviewed_by_path.get(path) != target_by_path[path]
        )
        raise EvidenceError(
            f"reviewed and target commit file hashes differ: {changed}"
        )

    code_status = review_summary["independent_code_review"]
    architecture_status = review_summary[
        "independent_architecture_review"
    ]
    uncovered_scopes = [
        f"{key}:{scope}"
        for key in ("code_review", "architecture_review")
        for scope in review_summary[key]["uncovered_scope"]
    ]
    reviews_clear = (
        code_status == "CLEAR"
        and architecture_status == "CLEAR"
        and review_summary.get("review_clear") is True
        and not uncovered_scopes
    )
    if reviews_clear:
        binding_result = "CONTENT_EQUIVALENT_REVIEW_CLEAR"
        evidence_status = "CONTENT_EQUIVALENT_REVIEW_CLEAR"
    else:
        binding_result = "CONTENT_EQUIVALENT_REVIEW_BLOCKED"
        evidence_status = "CONTENT_EQUIVALENT_REVIEW_BLOCKED"

    lane_records = {}
    for key in ("code_review", "architecture_review"):
        lane = review_summary[key]
        lane_records[key] = {
            "status": lane["status"],
            "completed": lane["completed"],
            "files_reviewed": lane["files_reviewed"],
            "report": lane["report"],
            "report_sha256": lane["report_sha256"],
            "result": lane["result"],
            "result_sha256": lane["result_sha256"],
            "target_head": lane["target_head"],
            "target_code_fingerprint": lane["target_code_fingerprint"],
            "covered_scope": lane["covered_scope"],
            "uncovered_scope": lane["uncovered_scope"],
        }

    return {
        "schema_version": REVIEW_BINDING_SCHEMA_VERSION,
        "generator_version": EVIDENCE_TOOL_VERSION,
        "generated_at": _binding_timestamp(generated_at),
        "scope": "content_equivalence_only",
        "repository": str(root),
        "candidate_mode": "committed_range",
        "base_head": base,
        "reviewed_head": reviewed_head,
        "target_head": target,
        "code_fingerprint": candidate["code_fingerprint"],
        "file_count": len(candidate["files"]),
        "reviewed_file_manifest_sha256": reviewed_manifest,
        "target_file_manifest_sha256": target_manifest,
        "reviewed_files": reviewed_files,
        "target_files": target_files,
        "review_manifest": {
            "path": (
                manifest_reference
                if manifest_reference is not None
                else _evidence_reference(manifest_path, root, evidence_base)
            ),
            "sha256": sha256_file(manifest_path, root=evidence_base),
        },
        "review_lanes": lane_records,
        "review_status": {
            "independent_code_review": code_status,
            "independent_architecture_review": architecture_status,
            "reviews_clear": reviews_clear,
            "uncovered_scope": uncovered_scopes,
        },
        "binding_result": binding_result,
        "evidence_status": evidence_status,
        "acceptance_supported": False,
        "merge_ready": False,
        "unverified_scope": [
            "browser_interaction",
            "container_or_live_backend_execution",
            "real_model_planner_calls",
            "test_and_mutation_evidence_currentness",
        ],
        "limitations": [
            "This record proves reviewed file content and path equivalence only.",
            "It does not create a new review or claim the original review occurred at the target HEAD.",
            "It cannot by itself establish merge readiness.",
        ],
    }


def validate_review_binding(
    root: Path,
    binding_path: Path,
    *,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    with _trusted_repository_scope(root):
        evidence_base = _canonical_absolute_path(evidence_root or root)
        with TrustedEvidenceRoot(
            evidence_base,
            "review binding evidence root",
        ) as trusted_root:
            with trusted_root.pinned():
                return _validate_review_binding_impl(
                    root,
                    binding_path,
                    evidence_root=evidence_base,
                )


def _validate_review_binding_impl(
    root: Path,
    binding_path: Path,
    *,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    evidence_base = _protected_evidence_root(
        evidence_root or root,
        "review binding evidence root",
    )
    resolved_binding_path = _evidence_root_file(
        evidence_base,
        evidence_path(binding_path, evidence_base),
        "review binding",
    )
    binding = read_json(resolved_binding_path, root=evidence_base)
    if binding.get("schema_version") != REVIEW_BINDING_SCHEMA_VERSION:
        raise EvidenceError("unexpected review binding schema")
    if binding.get("repository") != str(root):
        raise EvidenceError("review binding repository does not match root")
    review_manifest = binding.get("review_manifest")
    if not isinstance(review_manifest, dict):
        raise EvidenceError("review binding has no original review manifest")
    expected = build_review_binding(
        root,
        _evidence_root_file(
            evidence_base,
            review_manifest.get("path"),
            "review binding manifest",
        ),
        base_head=str(binding.get("base_head")),
        target_head=str(binding.get("target_head")),
        generated_at=str(binding.get("generated_at")),
        evidence_root=evidence_base,
        manifest_reference=review_manifest.get("path"),
    )
    if not isinstance(binding.get("review_manifest"), dict) or (
        binding["review_manifest"].get("sha256")
        != expected["review_manifest"]["sha256"]
    ):
        raise EvidenceError("review binding references a different manifest")
    if binding != expected:
        raise EvidenceError("review binding does not match recomputed content")
    return expected


def _browser_result_failure(
    key: str,
    condition: bool,
    failures: list[str],
) -> None:
    if not condition:
        failures.append(key)


def _is_exact_integer(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected


def _validate_browser_schema(browser: Mapping[str, Any]) -> None:
    if browser.get("schema_version") != BROWSER_SCHEMA_VERSION:
        raise EvidenceError("unsupported browser evidence schema")
    _reject_unknown_fields(
        browser,
        {
            "schema_version",
            "candidate",
            "browser",
            "results",
            "fixtures",
            "screenshot",
            "limits",
            "browser_console_errors",
        },
        "browser evidence",
    )
    candidate = browser.get("candidate")
    if not isinstance(candidate, Mapping):
        raise EvidenceError("browser evidence does not bind a candidate identity")
    _reject_unknown_fields(
        candidate,
        {"head", "code_fingerprint", "snapshot", "snapshot_sha256"},
        "browser evidence candidate",
    )
    browser_meta = browser.get("browser")
    if browser_meta is not None:
        if not isinstance(browser_meta, Mapping):
            raise EvidenceError("browser metadata must be an object")
        _reject_unknown_fields(
            browser_meta,
            {"name", "url", "runtime", "console_warning_error_count"},
            "browser evidence browser",
        )
    if "browser_console_errors" in browser:
        errors = browser.get("browser_console_errors")
        if not isinstance(errors, list) or not all(
            isinstance(item, str) for item in errors
        ):
            raise EvidenceError(
                "browser_console_errors must be a string array"
            )
    if "fixtures" in browser and not isinstance(browser.get("fixtures"), Mapping):
        raise EvidenceError("browser fixtures must be an object")
    if "limits" in browser:
        limits = browser.get("limits")
        if not isinstance(limits, list) or not all(
            isinstance(item, str) for item in limits
        ):
            raise EvidenceError("browser limits must be a string array")
    if "screenshot" in browser:
        screenshot = browser.get("screenshot")
        if not isinstance(screenshot, Mapping):
            raise EvidenceError("browser screenshot must be an object")
        _reject_unknown_fields(
            screenshot,
            {"path", "sha256", "bytes"},
            "browser screenshot",
        )
        if not isinstance(screenshot.get("path"), str):
            raise EvidenceError("browser screenshot path is invalid")
        _validate_relative_candidate_path(
            screenshot.get("path"),
            "browser screenshot",
        )
        if not isinstance(screenshot.get("sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}",
            screenshot["sha256"],
        ):
            raise EvidenceError("browser screenshot SHA-256 is invalid")
        if type(screenshot.get("bytes")) is not int or screenshot["bytes"] < 0:
            raise EvidenceError("browser screenshot byte count is invalid")

    results = browser.get("results")
    if not isinstance(results, Mapping):
        raise EvidenceError("browser evidence has no recorded results")
    lease_scheme, response_scheme = BROWSER_RESULT_SCHEMES
    result_keys = set(results)
    lease_present = bool(lease_scheme & result_keys)
    response_present = bool(response_scheme & result_keys)
    if lease_present and not lease_scheme.issubset(result_keys):
        raise EvidenceError("browser lease result set is incomplete")
    if response_present and not response_scheme.issubset(result_keys):
        raise EvidenceError("browser response-loss result set is incomplete")
    if not lease_present and not response_present:
        raise EvidenceError("browser evidence has no recognized result scheme")
    allowed_results = (
        set(lease_scheme if lease_present else set())
        | set(response_scheme if response_present else set())
    )
    _reject_unknown_fields(results, allowed_results, "browser evidence results")
    scene_fields = {
        "active_lease": {
            "lease_state",
            "ui_verified",
            "actions",
            "reason",
        },
        "expired_lease": {
            "lease_state",
            "ui_verified",
            "actions",
            "reason",
            "forged_post_http_status",
            "forged_post_error",
            "tool_started",
            "task_status_after",
        },
        "released_lease": {
            "lease_state",
            "ui_verified",
            "actions",
            "start_visible",
            "cancel_visible",
        },
        "preview_reads": {
            "task_event_artifact_state_unchanged",
            "database_sha256_before",
            "database_sha256_after",
        },
        "mobile_layout": {"viewport_width", "horizontal_overflow"},
        "initial_browser_outcome": {
            "request_id_retained",
            "manual_query_visible",
            "manual_abandon_visible",
            "task_reloaded_from_authoritative_state",
        },
        "query_after_response_loss": {
            "state",
            "same_request_id",
            "tool_started_events",
            "duplicate_execution_observed",
        },
    }
    for key, allowed in scene_fields.items():
        if key not in results:
            continue
        scene = results.get(key)
        if not isinstance(scene, Mapping):
            raise EvidenceError(f"browser evidence {key} must be an object")
        _reject_unknown_fields(scene, allowed, f"browser evidence {key}")


def _evaluate_browser_result(browser: Mapping[str, Any]) -> dict[str, Any]:
    results = browser.get("results")
    if not isinstance(results, Mapping) or not results:
        return {
            "valid": False,
            "scheme": None,
            "failed_checks": ["results_missing"],
        }
    failures: list[str] = []
    browser_meta = browser.get("browser")
    has_console_evidence = False
    if isinstance(browser_meta, Mapping) and "console_warning_error_count" in browser_meta:
        has_console_evidence = True
        _browser_result_failure(
            "browser.console_warning_error_count",
            type(browser_meta.get("console_warning_error_count")) is int
            and browser_meta["console_warning_error_count"] == 0,
            failures,
        )
    if "browser_console_errors" in browser:
        has_console_evidence = True
        errors = browser.get("browser_console_errors")
        _browser_result_failure(
            "browser_console_errors",
            isinstance(errors, list) and not errors,
            failures,
        )
    _browser_result_failure(
        "console_evidence",
        has_console_evidence,
        failures,
    )

    if BROWSER_RESULT_SCHEMES[0].issubset(results):
        if BROWSER_RESULT_SCHEMES[1].issubset(results):
            return {
                "valid": False,
                "scheme": None,
                "failed_checks": ["ambiguous_result_scheme"],
            }
        active = results.get("active_lease")
        expired = results.get("expired_lease")
        released = results.get("released_lease")
        preview = results.get("preview_reads")
        mobile = results.get("mobile_layout")
        _browser_result_failure(
            "active_lease",
            isinstance(active, Mapping)
            and active.get("lease_state") == "active"
            and active.get("ui_verified") is True
            and active.get("actions") == []
            and active.get("reason") == "task_claim_active",
            failures,
        )
        _browser_result_failure(
            "expired_lease",
            isinstance(expired, Mapping)
            and expired.get("lease_state") == "expired"
            and expired.get("ui_verified") is True
            and expired.get("actions") == []
            and expired.get("reason") == "task_claim_recovery_required"
            and _is_exact_integer(
                expired.get("forged_post_http_status"),
                409,
            )
            and expired.get("forged_post_error")
            == "task_claim_recovery_required"
            and _is_exact_integer(expired.get("tool_started"), 0)
            and expired.get("task_status_after") == "queued",
            failures,
        )
        _browser_result_failure(
            "released_lease",
            isinstance(released, Mapping)
            and released.get("lease_state") == "none"
            and released.get("ui_verified") is True
            and isinstance(released.get("actions"), list)
            and set(released.get("actions") or [])
            == {"revise_unstarted_task", "add_task_note"}
            and released.get("start_visible") is False
            and released.get("cancel_visible") is False,
            failures,
        )
        _browser_result_failure(
            "preview_reads",
            isinstance(preview, Mapping)
            and preview.get("task_event_artifact_state_unchanged")
            is True
            and isinstance(preview.get("database_sha256_before"), str)
            and re.fullmatch(
                r"[0-9a-f]{64}",
                preview.get("database_sha256_before"),
            )
            and preview.get("database_sha256_before")
            == preview.get("database_sha256_after"),
            failures,
        )
        _browser_result_failure(
            "mobile_layout",
            isinstance(mobile, Mapping)
            and _is_exact_integer(mobile.get("viewport_width"), 375)
            and mobile.get("horizontal_overflow") is False,
            failures,
        )
        return {
            "valid": not failures,
            "scheme": "lease_control_center_v1",
            "failed_checks": failures,
        }

    if BROWSER_RESULT_SCHEMES[1].issubset(results):
        initial = results.get("initial_browser_outcome")
        query = results.get("query_after_response_loss")
        _browser_result_failure(
            "initial_browser_outcome",
            isinstance(initial, Mapping)
            and initial.get("request_id_retained") is True
            and initial.get("manual_query_visible") is True
            and initial.get("manual_abandon_visible") is True
            and initial.get("task_reloaded_from_authoritative_state") is True,
            failures,
        )
        _browser_result_failure(
            "query_after_response_loss",
            isinstance(query, Mapping)
            and query.get("state") == "completed"
            and query.get("same_request_id") is True
            and _is_exact_integer(
                query.get("tool_started_events"),
                1,
            )
            and query.get("duplicate_execution_observed") is False,
            failures,
        )
        return {
            "valid": not failures,
            "scheme": "response_loss_recovery_v1",
            "failed_checks": failures,
        }

    return {
        "valid": False,
        "scheme": None,
        "failed_checks": ["required_scene_missing"],
    }


def validate_browser_evidence(
    root: Path,
    browser_path: Path,
    candidate: Mapping[str, Any],
    *,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    with _trusted_repository_scope(root):
        evidence_base = _canonical_absolute_path(evidence_root or root)
        with TrustedEvidenceRoot(
            evidence_base,
            "browser evidence root",
        ) as trusted_root:
            with trusted_root.pinned():
                return _validate_browser_evidence_impl(
                    root,
                    browser_path,
                    candidate,
                    evidence_root=evidence_base,
                )


def _validate_browser_evidence_impl(
    root: Path,
    browser_path: Path,
    candidate: Mapping[str, Any],
    *,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    evidence_base = _protected_evidence_root(
        evidence_root or root,
        "browser evidence root",
    )
    resolved_browser_path = _evidence_root_file(
        evidence_base,
        evidence_path(browser_path, evidence_base),
        "browser evidence",
    )
    browser = read_json(resolved_browser_path, root=evidence_base)
    _validate_browser_schema(browser)
    record = browser.get("candidate")
    if not isinstance(record, dict):
        raise EvidenceError(
            "browser evidence does not bind a candidate identity"
        )
    if record.get("head") != candidate.get("target_head"):
        raise EvidenceError(
            "browser evidence target HEAD does not match candidate"
        )
    if record.get("code_fingerprint") != candidate.get("code_fingerprint"):
        raise EvidenceError(
            "browser evidence code fingerprint does not match candidate"
        )
    snapshot_value = record.get("snapshot")
    if not isinstance(snapshot_value, str):
        raise EvidenceError("browser evidence has no candidate snapshot")
    snapshot_path = _evidence_root_file(
        evidence_base,
        snapshot_value,
        "browser candidate snapshot",
    )
    _validate_hash(
        snapshot_path,
        str(record.get("snapshot_sha256")),
        "browser candidate snapshot",
        root=evidence_base,
    )
    validate_candidate_snapshot(
        root,
        snapshot_path,
        evidence_root=evidence_base,
    )
    screenshot = browser.get("screenshot")
    if isinstance(screenshot, Mapping):
        screenshot_path = _evidence_root_file(
            evidence_base,
            screenshot.get("path"),
            "browser screenshot",
        )
        screenshot_bytes = _read_regular_file(
            screenshot_path,
            "browser screenshot",
            root=evidence_base,
        )
        if sha256_bytes(screenshot_bytes) != screenshot.get("sha256"):
            raise EvidenceError("browser screenshot SHA-256 mismatch")
        if len(screenshot_bytes) != screenshot.get("bytes"):
            raise EvidenceError("browser screenshot byte count mismatch")
    evaluation = _evaluate_browser_result(browser)
    return {
        "payload": browser,
        "valid": evaluation["valid"],
        "scheme": evaluation["scheme"],
        "failed_checks": evaluation["failed_checks"],
    }


def _bundle_reference(
    value: Any,
    label: str,
    *,
    allowed_fields: set[str] | None = None,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{label} must be an object")
    _reject_unknown_fields(
        value,
        allowed_fields or {"path", "sha256"},
        label,
    )
    path = _validate_relative_candidate_path(value.get("path"), label)
    digest = value.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise EvidenceError(f"{label}.sha256 is invalid")
    return {"path": path, "sha256": digest}


def _bundle_status(
    value: Any,
    allowed: frozenset[str],
    label: str,
) -> str:
    status = _normalize_review_status(value, label)
    if status not in allowed:
        raise EvidenceError(f"{label} has unsupported status: {status}")
    return status


def _validate_bundle_files(
    bundle_root: Path,
    bundle_root_fd: int,
    bundle_manifest: Path,
    declared: Any,
    required_roles: Mapping[str, str],
) -> dict[str, str]:
    _assert_trusted_directory_path(bundle_root, "bundle root")
    if not isinstance(declared, list):
        raise EvidenceError("bundle files must be an array")
    if len(declared) > MAX_BUNDLE_FILES:
        raise EvidenceError(
            f"bundle files exceeds the {MAX_BUNDLE_FILES}-entry limit"
        )
    actual_by_identity: dict[str, str] = {}
    seen_roles: set[str] = set()
    for index, item in enumerate(declared):
        if not isinstance(item, Mapping):
            raise EvidenceError(f"bundle files[{index}] must be an object")
        _reject_unknown_fields(
            item,
            {"path", "sha256", "role"},
            f"bundle files[{index}]",
        )
        reference = _bundle_reference(
            item,
            f"bundle files[{index}]",
            allowed_fields={"path", "sha256", "role"},
        )
        role = item.get("role")
        if not isinstance(role, str) or not role.strip():
            raise EvidenceError(f"bundle files[{index}] has no role")
        if role in seen_roles:
            raise EvidenceError(f"bundle files has duplicate role: {role}")
        seen_roles.add(role)
        path = reference["path"]
        identity = _canonical_path_identity(path, "bundle files")
        if identity in actual_by_identity:
            raise EvidenceError(
                "bundle files has duplicate or aliased path: "
                f"{actual_by_identity[identity]} and {path}"
            )
        expected_role = required_roles.get(identity)
        if expected_role is None:
            raise EvidenceError(
                f"bundle files contains undeclared evidence: {path}"
            )
        if role != expected_role:
            raise EvidenceError(
                "bundle file role does not match path: "
                f"{path}: expected {expected_role}, observed {role}"
            )
        file_path = _evidence_root_file(
            bundle_root,
            path,
            f"bundle files[{index}]",
        )
        _validate_hash(
            file_path,
            reference["sha256"],
            f"bundle files[{index}]",
            root=bundle_root,
        )
        actual_by_identity[identity] = path
    missing = set(required_roles) - set(actual_by_identity)
    if missing:
        raise EvidenceError(
            f"bundle files omits referenced inputs: {sorted(missing)}"
        )

    manifest_relative = bundle_manifest.relative_to(bundle_root).as_posix()
    discovered = _enumerate_bundle_files(
        bundle_root_fd,
        manifest_relative,
    )
    _assert_trusted_directory_path(bundle_root, "bundle root")
    undeclared = discovered - set(required_roles)
    if undeclared:
        raise EvidenceError(
            f"bundle contains undeclared files: {sorted(undeclared)}"
        )
    return actual_by_identity


def validate_evidence_bundle(
    root: Path,
    bundle_manifest_path: Path,
) -> dict[str, Any]:
    """Validate a bundle using one securely opened root directory object."""
    root = _canonical_absolute_path(root)
    original_manifest = _canonical_absolute_path(bundle_manifest_path)
    bundle_root = _canonical_absolute_path(original_manifest.parent)
    with _trusted_repository_scope(root):
        with TrustedEvidenceRoot(
            bundle_root,
            "evidence bundle root",
        ) as trusted_root:
            return _validate_evidence_bundle_with_root(
                root,
                original_manifest,
                trusted_root,
            )


def _validate_evidence_bundle_with_root(
    root: Path,
    bundle_manifest_path: Path,
    trusted_root: TrustedEvidenceRoot,
) -> dict[str, Any]:
    original_manifest = _canonical_absolute_path(bundle_manifest_path)
    bundle_root = _canonical_absolute_path(original_manifest.parent)
    if bundle_root != trusted_root.path:
        raise EvidenceError(
            "evidence bundle manifest is outside the trusted root object"
        )
    manifest_relative = _validate_relative_candidate_path(
        original_manifest.name,
        "evidence bundle manifest",
    )
    manifest_path = bundle_root / manifest_relative
    with trusted_root.pinned():
        bundle_root_fd = trusted_root.assert_active()
        manifest_bytes = _read_relative_regular_file(
            bundle_root_fd,
            manifest_relative,
            manifest_path,
            "evidence bundle manifest",
            max_bytes=MAX_JSON_EVIDENCE_BYTES,
        )
        manifest_sha256 = sha256_bytes(manifest_bytes)
        bundle = _parse_json_object_bytes(manifest_bytes, manifest_path)
        return _validate_evidence_bundle_payload(
            root,
            bundle_root,
            bundle_root_fd,
            manifest_path,
            bundle,
            manifest_sha256,
        )


def _validate_evidence_bundle_payload(
    root: Path,
    bundle_root: Path,
    bundle_root_fd: int,
    manifest_path: Path,
    bundle: Mapping[str, Any],
    manifest_sha256: str,
) -> dict[str, Any]:
    """Validate a bundle payload read from an already trusted directory."""
    if bundle.get("schema_version") != EVIDENCE_BUNDLE_SCHEMA_VERSION:
        raise EvidenceError("unexpected evidence bundle schema")
    _reject_unknown_fields(
        bundle,
        {
            "schema_version",
            "bundle_id",
            "generator_version",
            "created_at",
            "acceptance_policy",
            "candidate",
            "review_manifest",
            "review_binding",
            "review",
            "previous_summary",
            "frozen_evidence",
            "browser_evidence",
            "runtime_verification",
            "files",
        },
        "evidence bundle",
    )
    bundle_id = bundle.get("bundle_id")
    if not isinstance(bundle_id, str) or not bundle_id.strip():
        raise EvidenceError("evidence bundle has no bundle ID")
    generator_version = bundle.get("generator_version")
    if not isinstance(generator_version, str) or not generator_version.strip():
        raise EvidenceError("evidence bundle has no generator version")
    _binding_timestamp(bundle.get("created_at"))
    policy = _normalize_review_status(
        bundle.get("acceptance_policy"),
        "evidence bundle acceptance_policy",
    ).lower()
    if policy not in ACCEPTANCE_POLICIES:
        raise EvidenceError("evidence bundle acceptance policy is unsupported")

    candidate_record = bundle.get("candidate")
    if not isinstance(candidate_record, Mapping):
        raise EvidenceError("evidence bundle has no candidate identity")
    _reject_unknown_fields(
        candidate_record,
        {
            "candidate_mode",
            "base_head",
            "reviewed_head",
            "target_head",
            "code_fingerprint",
            "file_count",
            "file_manifest_sha256",
            "evidence_status",
            "snapshot",
        },
        "evidence bundle candidate",
    )
    snapshot_reference = _bundle_reference(
        candidate_record.get("snapshot"),
        "evidence bundle candidate.snapshot",
    )
    snapshot_path = _evidence_root_file(
        bundle_root,
        snapshot_reference["path"],
        "evidence bundle candidate snapshot",
    )
    _validate_hash(
        snapshot_path,
        snapshot_reference["sha256"],
        "evidence bundle candidate snapshot",
        root=bundle_root,
    )
    candidate = validate_candidate_snapshot(
        root,
        snapshot_path,
        evidence_root=bundle_root,
    )
    for field in (
        "candidate_mode",
        "base_head",
        "reviewed_head",
        "target_head",
        "code_fingerprint",
        "file_count",
        "file_manifest_sha256",
        "evidence_status",
    ):
        if candidate_record.get(field) != candidate.get(field):
            raise EvidenceError(
                f"evidence bundle candidate mismatch: {field}"
            )

    review_manifest_reference = _bundle_reference(
        bundle.get("review_manifest"),
        "evidence bundle review_manifest",
    )
    review_manifest_path = _evidence_root_file(
        bundle_root,
        review_manifest_reference["path"],
        "evidence bundle review manifest",
    )
    _validate_hash(
        review_manifest_path,
        review_manifest_reference["sha256"],
        "evidence bundle review manifest",
        root=bundle_root,
    )
    review_manifest = read_json(review_manifest_path, root=bundle_root)
    reviewed_head = _resolve_commit(
        root,
        review_manifest.get("head"),
        "evidence bundle reviewed HEAD",
    )
    if reviewed_head != candidate.get("reviewed_head"):
        raise EvidenceError(
            "evidence bundle review manifest HEAD differs from candidate"
        )
    review_summary = validate_review_manifest(
        root,
        review_manifest_path,
        expected_code_fingerprint=candidate["code_fingerprint"],
        expected_head=reviewed_head,
        changed_candidate_paths=[
            item["path"] for item in candidate["files"]
        ],
        evidence_root=bundle_root,
    )
    declared_review = bundle.get("review")
    if not isinstance(declared_review, Mapping):
        raise EvidenceError("evidence bundle has no review declaration")
    _reject_unknown_fields(
        declared_review,
        {"reviewed_head", "code_review", "architecture_review"},
        "evidence bundle review",
    )
    if declared_review.get("reviewed_head") != reviewed_head:
        raise EvidenceError("evidence bundle review HEAD declaration mismatch")
    for key in ("code_review", "architecture_review"):
        declared_lane = declared_review.get(key)
        actual_lane = review_summary.get(key)
        if not isinstance(declared_lane, Mapping) or not isinstance(
            actual_lane, Mapping
        ):
            raise EvidenceError(f"evidence bundle has no {key} declaration")
        _reject_unknown_fields(
            declared_lane,
            {
                "status",
                "conclusion",
                "files_reviewed",
                "reviewed_files",
                "report",
                "report_sha256",
                "result",
                "result_sha256",
            },
            f"evidence bundle {key}",
        )
        for field in (
            "status",
            "conclusion",
            "files_reviewed",
            "reviewed_files",
            "report",
            "report_sha256",
            "result",
            "result_sha256",
        ):
            if declared_lane.get(field) != actual_lane.get(field):
                raise EvidenceError(
                    f"evidence bundle {key} declaration mismatch: {field}"
                )

    binding_reference = bundle.get("review_binding")
    normalized_binding_reference = None
    binding = None
    if binding_reference is not None:
        normalized_binding_reference = _bundle_reference(
            binding_reference,
            "evidence bundle review_binding",
        )
        binding_path = _evidence_root_file(
            bundle_root,
            normalized_binding_reference["path"],
            "evidence bundle review binding",
        )
        _validate_hash(
            binding_path,
            normalized_binding_reference["sha256"],
            "evidence bundle review binding",
            root=bundle_root,
        )
        binding = validate_review_binding(
            root,
            binding_path,
            evidence_root=bundle_root,
        )
        if binding.get("target_head") != candidate.get("target_head"):
            raise EvidenceError("review binding target differs from bundle candidate")
        if binding.get("reviewed_head") != reviewed_head:
            raise EvidenceError(
                "review binding reviewed HEAD differs from bundle review"
            )
        if binding.get("code_fingerprint") != candidate.get("code_fingerprint"):
            raise EvidenceError(
                "review binding fingerprint differs from bundle candidate"
            )
        if (
            binding.get("reviewed_file_manifest_sha256")
            != candidate.get("file_manifest_sha256")
            or binding.get("target_file_manifest_sha256")
            != candidate.get("file_manifest_sha256")
        ):
            raise EvidenceError(
                "review binding file manifest differs from bundle candidate"
            )

    previous_declared = bundle.get("previous_summary")
    previous_reference = _bundle_reference(
        previous_declared,
        "evidence bundle previous_summary",
        allowed_fields={"path", "sha256", "status"},
    )
    previous_path = _evidence_root_file(
        bundle_root,
        previous_reference["path"],
        "evidence bundle previous summary",
    )
    _validate_hash(
        previous_path,
        previous_reference["sha256"],
        "evidence bundle previous summary",
        root=bundle_root,
    )
    previous = read_json(previous_path, root=bundle_root)
    if not isinstance(previous_declared, Mapping) or (
        previous_declared.get("status") != "AVAILABLE"
    ):
        raise EvidenceError("evidence bundle previous summary is not AVAILABLE")
    incidents, previous_history = _validate_previous_summary(
        previous,
        "evidence bundle previous summary",
        trusted_repository=str(root),
    )

    frozen_declared = bundle.get("frozen_evidence")
    if not isinstance(frozen_declared, Mapping):
        raise EvidenceError("evidence bundle has no frozen evidence declaration")
    frozen_summary: dict[str, Any] = {}
    for repo_relative, expected in FROZEN_EVIDENCE.items():
        reference = _bundle_reference(
            frozen_declared.get(repo_relative),
            f"evidence bundle frozen_evidence[{repo_relative}]",
            allowed_fields={"path", "sha256", "status"},
        )
        frozen_record = frozen_declared.get(repo_relative)
        if not isinstance(frozen_record, Mapping) or (
            frozen_record.get("status") != "VERIFIED"
        ):
            raise EvidenceError(
                f"evidence bundle frozen evidence is not VERIFIED: {repo_relative}"
            )
        if reference["sha256"] != expected:
            raise EvidenceError(
                f"evidence bundle frozen evidence hash mismatch: {repo_relative}"
            )
        frozen_path = _evidence_root_file(
            bundle_root,
            reference["path"],
            f"evidence bundle frozen_evidence[{repo_relative}]",
        )
        _validate_hash(
            frozen_path,
            expected,
            repo_relative,
            root=bundle_root,
        )
        frozen_summary[repo_relative] = {
            "path": reference["path"],
            "expected_sha256": expected,
            "observed_sha256": sha256_file(
                frozen_path,
                root=bundle_root,
            ),
        }

    browser_declared = bundle.get("browser_evidence")
    if not isinstance(browser_declared, list):
        raise EvidenceError("evidence bundle browser_evidence must be an array")
    browser_summary = []
    browser_snapshot_paths: set[str] = set()
    for index, item in enumerate(browser_declared):
        if not isinstance(item, Mapping):
            raise EvidenceError(f"browser evidence[{index}] must be an object")
        reference = _bundle_reference(
            item,
            f"browser evidence[{index}]",
            allowed_fields={"path", "sha256", "status", "reason"},
        )
        status = _bundle_status(
            item.get("status"),
            BROWSER_EVIDENCE_STATUSES,
            f"browser evidence[{index}].status",
        )
        reason = item.get("reason")
        if status != "VALID_FOR_CANDIDATE" and (
            not isinstance(reason, str) or not reason.strip()
        ):
            raise EvidenceError(
                f"browser evidence[{index}] requires a non-empty reason"
            )
        browser_path = _evidence_root_file(
            bundle_root,
            reference["path"],
            f"browser evidence[{index}]",
        )
        _validate_hash(
            browser_path,
            reference["sha256"],
            f"browser evidence[{index}]",
            root=bundle_root,
        )
        payload = read_json(browser_path, root=bundle_root)
        candidate_record = payload.get("candidate")
        if isinstance(candidate_record, Mapping):
            snapshot_value = candidate_record.get("snapshot")
            if isinstance(snapshot_value, str):
                browser_snapshot_paths.add(snapshot_value)
        screenshot = payload.get("screenshot")
        if isinstance(screenshot, Mapping):
            screenshot_path = screenshot.get("path")
            if isinstance(screenshot_path, str):
                browser_snapshot_paths.add(screenshot_path)
        browser_validation = validate_browser_evidence(
            root,
            browser_path,
            candidate,
            evidence_root=bundle_root,
        )
        if status == "VALID_FOR_CANDIDATE" and not browser_validation["valid"]:
            raise EvidenceError(
                "browser evidence declaration conflicts with recorded results: "
                f"{browser_validation['failed_checks']}"
            )
        evaluated_status = (
            "VALID_FOR_CANDIDATE"
            if status == "VALID_FOR_CANDIDATE"
            and browser_validation["valid"]
            else "NOT_VERIFIED"
        )
        browser_summary.append(
            {
                "path": reference["path"],
                "sha256": reference["sha256"],
                "status": status,
                "evaluated_status": evaluated_status,
                "validation_scheme": browser_validation["scheme"],
                "failed_checks": browser_validation["failed_checks"],
                "reason": reason,
            }
        )

    runtime = bundle.get("runtime_verification")
    if not isinstance(runtime, Mapping):
        raise EvidenceError("evidence bundle has no runtime verification")
    _reject_unknown_fields(
        runtime,
        {"status", "scopes", "reason"},
        "evidence bundle runtime",
    )
    runtime_status = _bundle_status(
        runtime.get("status"),
        RUNTIME_STATUSES,
        "evidence bundle runtime status",
    )
    scopes = runtime.get("scopes")
    if not isinstance(scopes, list) or not all(
        isinstance(item, str) and item for item in scopes
    ):
        raise EvidenceError("runtime verification scopes must be strings")
    if len(scopes) != len(set(scopes)):
        raise EvidenceError("runtime verification scopes must be unique")
    if runtime_status == "VERIFIED":
        unsupported_scopes = set(scopes) - SUPPORTED_RUNTIME_SCOPES
        if unsupported_scopes:
            raise EvidenceError(
                "runtime VERIFIED has unsupported scopes: "
                f"{sorted(unsupported_scopes)}"
            )
        if not scopes:
            raise EvidenceError(
                "runtime VERIFIED requires at least one supported scope"
            )
    runtime_reason = runtime.get("reason")
    if not isinstance(runtime_reason, str) or not runtime_reason.strip():
        raise EvidenceError("runtime verification requires a reason")
    if runtime_status == "VERIFIED":
        if not browser_summary:
            raise EvidenceError(
                "runtime VERIFIED requires candidate-bound browser evidence"
            )
        invalid_browser_evidence = [
            item["path"]
            for item in browser_summary
            if (
                item["status"] != "VALID_FOR_CANDIDATE"
                or item["evaluated_status"] != "VALID_FOR_CANDIDATE"
            )
        ]
        if invalid_browser_evidence:
            raise EvidenceError(
                "runtime VERIFIED requires every declared browser evidence "
                f"record to be valid: {invalid_browser_evidence}"
            )

    required_roles: dict[str, str] = {}

    def require_path(path: str, role: str) -> None:
        identity = _canonical_path_identity(path, "bundle role")
        existing = required_roles.get(identity)
        if existing is not None and existing != role:
            raise EvidenceError(
                "bundle path alias has conflicting roles: "
                f"{existing} and {role}"
            )
        required_roles[identity] = role

    require_path(snapshot_reference["path"], "candidate_snapshot")
    require_path(review_manifest_reference["path"], "review_manifest")
    require_path(previous_reference["path"], "previous_summary")
    for relative, reference in frozen_summary.items():
        require_path(
            reference["path"],
            f"frozen_evidence:{relative}",
        )
    for index, item in enumerate(browser_summary):
        require_path(item["path"], f"browser_evidence:{index}")
    for index, path in enumerate(sorted(browser_snapshot_paths)):
        identity = _canonical_path_identity(path, "browser snapshot")
        if identity not in required_roles:
            require_path(path, f"browser_candidate_snapshot:{index}")
    for key in ("code_review", "architecture_review"):
        lane = review_summary[key]
        require_path(lane["report"], f"{key}_report")
        require_path(lane["result"], f"{key}_result")
    if binding_reference is not None:
        require_path(
            normalized_binding_reference["path"],
            "review_binding",
        )
    _validate_bundle_files(
        bundle_root,
        bundle_root_fd,
        manifest_path,
        bundle.get("files"),
        required_roles,
    )
    valid_evidence_count = sum(
        item["evaluated_status"] == "VALID_FOR_CANDIDATE"
        for item in browser_summary
    )
    if (
        runtime_status == "VERIFIED"
        and valid_evidence_count != len(browser_summary)
    ):
        raise EvidenceError(
            "runtime VERIFIED browser evidence count is inconsistent"
        )

    return {
        "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "bundle_root": str(bundle_root),
        "bundle_manifest_path": str(manifest_path),
        "manifest_path": manifest_path.relative_to(bundle_root).as_posix(),
        "manifest_sha256": manifest_sha256,
        "generator_version": generator_version,
        "created_at": bundle["created_at"],
        "acceptance_policy": policy,
        "candidate_identity": candidate,
        "review_manifest_reference": review_manifest_reference,
        "review_summary": review_summary,
        "review_binding": binding,
        "review_binding_reference": normalized_binding_reference,
        "previous_summary": {
            "path": previous_reference["path"],
            "sha256": previous_reference["sha256"],
            "status": "AVAILABLE",
            "constraint_incidents": incidents,
            "evidence_history": previous_history,
            "superseded_summary": previous.get("superseded_summary"),
        },
        "frozen_evidence": frozen_summary,
        "browser_evidence": browser_summary,
        "runtime_verification": {
            "status": runtime_status,
            "scopes": scopes,
            "reason": runtime_reason,
            "valid_evidence_count": valid_evidence_count,
        },
        "file_paths": sorted(
            {
                _evidence_root_file(
                    bundle_root,
                    later_path,
                    "bundle evidence",
                ).relative_to(bundle_root).as_posix()
                for later_path in [
                    snapshot_reference["path"],
                    review_manifest_reference["path"],
                    previous_reference["path"],
                    *[ref["path"] for ref in frozen_summary.values()],
                    *[item["path"] for item in browser_summary],
                    *sorted(browser_snapshot_paths),
                    *[
                        review_summary[key][field]
                        for key in ("code_review", "architecture_review")
                        for field in ("report", "result")
                    ],
                    *(
                        [normalized_binding_reference["path"]]
                        if normalized_binding_reference is not None
                        else []
                    ),
                ]
            }
        ),
    }


def _review_summary_from_binding(
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    code = binding.get("review_lanes", {}).get("code_review")
    architecture = binding.get("review_lanes", {}).get("architecture_review")
    if not isinstance(code, Mapping) or not isinstance(architecture, Mapping):
        raise EvidenceError("review binding has no complete review lanes")
    code_status = _normalize_review_status(code.get("status"), "code review")
    architecture_status = _normalize_review_status(
        architecture.get("status"),
        "architecture review",
    )
    blocking_reasons = []
    if code_status != "CLEAR":
        blocking_reasons.append(f"code_review:{code_status}")
    if architecture_status != "CLEAR":
        blocking_reasons.append(
            f"architecture_review:{architecture_status}"
        )
    uncovered_scopes = []
    for key in ("code_review", "architecture_review"):
        lane = binding.get("review_lanes", {}).get(key)
        scopes = lane.get("uncovered_scope") if isinstance(lane, Mapping) else None
        uncovered_scopes.extend(
            f"{key}:{scope}"
            for scope in scopes
            if isinstance(scope, str)
        )
    if uncovered_scopes:
        blocking_reasons.append("review_uncovered_scope")
    uncovered_scopes.sort()
    reviewed_files = binding.get("reviewed_files")
    if not isinstance(reviewed_files, list) or not reviewed_files:
        raise EvidenceError("review binding has no reviewed file manifest")
    lane_records = {}
    for key, lane in (
        ("code_review", code),
        ("architecture_review", architecture),
    ):
        lane_record = dict(lane)
        lane_record["conclusion"] = lane_record["status"]
        lane_record["reviewed_files"] = list(reviewed_files)
        lane_record["target_repository"] = binding.get("repository")
        lane_records[key] = lane_record
    return {
        "independent_code_review": code_status,
        "independent_architecture_review": architecture_status,
        "code_review": lane_records["code_review"],
        "architecture_review": lane_records["architecture_review"],
        "uncovered_scope": uncovered_scopes,
        "merge_ready": False,
        "blocking_reasons": blocking_reasons,
        "review_clear": not blocking_reasons,
        "candidate_acceptance": (
            "CONTENT_EQUIVALENT_REVIEW_CLEAR"
            if not blocking_reasons
            else "CONTENT_EQUIVALENT_REVIEW_BLOCKED"
        ),
        "path": binding["review_manifest"]["path"],
        "sha256": binding["review_manifest"]["sha256"],
    }


def resolve_acceptance_policy(
    caller_policy: str,
    declared_policy: str | None,
) -> str:
    if caller_policy not in ACCEPTANCE_POLICIES:
        raise EvidenceError(
            f"unsupported caller acceptance policy: {caller_policy}"
        )
    if declared_policy is None:
        raise EvidenceError("bundle acceptance policy is missing")
    normalized = _normalize_review_status(
        declared_policy,
        "bundle acceptance_policy",
    ).lower()
    if normalized not in ACCEPTANCE_POLICIES:
        raise EvidenceError(
            f"unsupported bundle acceptance policy: {normalized}"
        )
    if normalized != caller_policy:
        raise EvidenceError(
            "acceptance policy conflict: caller requested "
            f"{caller_policy}, bundle declares {normalized}"
        )
    return caller_policy


@dataclass(frozen=True)
class _AcceptancePrerequisiteProof:
    issuance_id: str
    evidence_json: str
    signature: str


@dataclass(frozen=True)
class _AcceptancePrerequisiteOwner:
    token: str


def _new_acceptance_prerequisite_owner() -> _AcceptancePrerequisiteOwner:
    return _AcceptancePrerequisiteOwner(secrets.token_hex(32))


def _make_acceptance_prerequisite_authority(
    *,
    clock=time.monotonic,
    max_records: int = ACCEPTANCE_PROOF_RECORD_LIMIT,
    terminal_retention_seconds: float = (
        ACCEPTANCE_PROOF_TERMINAL_RETENTION_SECONDS
    ),
):
    if type(max_records) is not int or max_records <= 0:
        raise EvidenceError(
            "acceptance proof record limit must be a positive integer"
        )
    if (
        type(terminal_retention_seconds) not in {int, float}
        or not math.isfinite(terminal_retention_seconds)
        or terminal_retention_seconds < 0
    ):
        raise EvidenceError(
            "acceptance proof retention must be finite and non-negative"
        )
    secret = os.urandom(32)
    records: dict[str, dict[str, Any]] = {}
    owners: dict[str, str] = {}
    records_lock = threading.Lock()

    def now() -> float:
        value = clock()
        if type(value) not in {int, float} or not math.isfinite(value):
            raise EvidenceError(
                "acceptance proof clock returned an invalid value"
            )
        return float(value)

    def evidence_digest(evidence_binding: Mapping[str, Any]) -> str:
        if not isinstance(evidence_binding, Mapping):
            raise EvidenceError(
                "acceptance proof evidence binding must be an object"
            )
        return sha256_bytes(
            canonical_json(
                {
                    "schema_version": (
                        ACCEPTANCE_PREREQUISITE_SCHEMA_VERSION
                    ),
                    "evidence": evidence_binding,
                }
            ).encode("utf-8")
        )

    def terminal(record: Mapping[str, Any]) -> bool:
        return record.get("status") in {
            "CONSUMED",
            "REVOKED",
            "EXPIRED",
        }

    def expire_if_needed(record: dict[str, Any]) -> None:
        current = now()
        if (
            record["status"] == "ACTIVE"
            and current >= record["expires_at"]
        ):
            record["status"] = "EXPIRED"
            record["terminal_at"] = current

    def purge_locked(*, force_terminal: bool = False) -> int:
        current = now()
        removable = []
        for issuance_id, record in records.items():
            expire_if_needed(record)
            if terminal(record):
                terminal_at = record.get("terminal_at", current)
                if (
                    force_terminal
                    or current - terminal_at >= terminal_retention_seconds
                ):
                    removable.append(issuance_id)
        for issuance_id in removable:
            removed = records.pop(issuance_id, None)
            if removed is not None:
                owner_token = removed.get("owner_token")
                if isinstance(owner_token, str):
                    owners.pop(owner_token, None)
        return len(removable)

    def transition_revoked_locked(record: dict[str, Any]) -> str | None:
        if record["status"] != "ACTIVE":
            return record.get("cleanup_error")
        record["status"] = "REVOKED"
        try:
            record["terminal_at"] = now()
        except BaseException as exc:
            record["cleanup_error"] = str(exc)
            return str(exc)
        return None

    def mark_revoked(proof: _AcceptancePrerequisiteProof) -> None:
        with records_lock:
            record = records.get(proof.issuance_id)
            if record is not None and record["proof"] is proof:
                transition_revoked_locked(record)

    def issue(
        *,
        candidate_identity: Mapping[str, Any],
        review_summary: Mapping[str, Any],
        evidence_binding: Mapping[str, Any],
        tests_verified: bool,
        frozen_verified: bool,
        previous_summary_available: bool,
        previous_summary_trusted: bool,
        runtime_verification: Mapping[str, Any] | None,
        owner: _AcceptancePrerequisiteOwner,
        ttl_seconds: float = 300.0,
    ) -> _AcceptancePrerequisiteProof:
        for name, value in (
            ("tests_verified", tests_verified),
            ("frozen_verified", frozen_verified),
            ("previous_summary_available", previous_summary_available),
            ("previous_summary_trusted", previous_summary_trusted),
        ):
            if type(value) is not bool:
                raise EvidenceError(
                    f"acceptance proof {name} must be boolean"
                )
        if runtime_verification is not None and not isinstance(
            runtime_verification,
            Mapping,
        ):
            raise EvidenceError(
                "acceptance proof runtime verification must be an object"
            )
        if (
            type(ttl_seconds) not in {int, float}
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise EvidenceError(
                "acceptance proof lifetime must be positive and finite"
            )
        if (
            not isinstance(owner, _AcceptancePrerequisiteOwner)
            or not re.fullmatch(r"[0-9a-f]{64}", owner.token)
        ):
            raise EvidenceError(
                "acceptance proof owner must be an authority-created owner"
            )
        issuance_id = secrets.token_hex(32)
        payload = {
            "issuance_id": issuance_id,
            "issued_at": time.time(),
            "candidate_identity_sha256": sha256_bytes(
                canonical_json(candidate_identity).encode("utf-8")
            ),
            "review_summary_sha256": sha256_bytes(
                canonical_json(review_summary).encode("utf-8")
            ),
            "evidence_binding_sha256": evidence_digest(evidence_binding),
            "tests_verified": tests_verified,
            "frozen_verified": frozen_verified,
            "previous_summary_available": previous_summary_available,
            "previous_summary_trusted": previous_summary_trusted,
            "runtime_verification": runtime_verification,
        }
        evidence_json = canonical_json(payload)
        signature = hmac.new(
            secret,
            evidence_json.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        proof = _AcceptancePrerequisiteProof(
            issuance_id,
            evidence_json,
            signature,
        )
        with records_lock:
            purge_locked()
            if len(records) >= max_records:
                purge_locked(force_terminal=True)
            if len(records) >= max_records:
                raise EvidenceError(
                    "acceptance proof record capacity exhausted"
                )
            if owner.token in owners:
                raise EvidenceError(
                    "acceptance proof owner is already bound"
                )
            records[issuance_id] = {
                "proof": proof,
                "owner_token": owner.token,
                "status": "ACTIVE",
                "expires_at": now() + float(ttl_seconds),
            }
            owners[owner.token] = issuance_id
        return proof

    def prepare(
        proof: Any,
        *,
        candidate_identity: Mapping[str, Any],
        review_summary: Mapping[str, Any],
        evidence_binding: Mapping[str, Any],
    ) -> tuple[
        dict[str, Any],
        str,
        str,
        str,
    ]:
        if not isinstance(proof, _AcceptancePrerequisiteProof):
            raise EvidenceError(
                "acceptance gate requires a validator-issued "
                "prerequisite proof"
            )
        expected_signature = hmac.new(
            secret,
            proof.evidence_json.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(
            proof.signature,
            expected_signature,
        ):
            raise EvidenceError(
                "acceptance prerequisite proof signature is invalid"
            )
        parsed = _parse_json_object_bytes(
            proof.evidence_json.encode("utf-8"),
            Path("<acceptance-prerequisites>"),
        )
        _reject_unknown_fields(
            parsed,
            {
                "issuance_id",
                "issued_at",
                "candidate_identity_sha256",
                "review_summary_sha256",
                "evidence_binding_sha256",
                "tests_verified",
                "frozen_verified",
                "previous_summary_available",
                "previous_summary_trusted",
                "runtime_verification",
            },
            "acceptance prerequisite proof",
        )
        if parsed.get("issuance_id") != proof.issuance_id:
            raise EvidenceError(
                "acceptance prerequisite proof issuance ID is invalid"
            )
        if type(parsed.get("issued_at")) not in {int, float}:
            raise EvidenceError(
                "acceptance prerequisite issued_at is invalid"
            )
        for field in (
            "candidate_identity_sha256",
            "review_summary_sha256",
            "evidence_binding_sha256",
        ):
            _require_sha256(
                parsed.get(field),
                f"acceptance prerequisite proof {field}",
            )
        expected_candidate = sha256_bytes(
            canonical_json(candidate_identity).encode("utf-8")
        )
        expected_review = sha256_bytes(
            canonical_json(review_summary).encode("utf-8")
        )
        expected_evidence = evidence_digest(evidence_binding)
        for field in (
            "tests_verified",
            "frozen_verified",
            "previous_summary_available",
            "previous_summary_trusted",
        ):
            if type(parsed.get(field)) is not bool:
                raise EvidenceError(
                    f"acceptance prerequisite proof {field} is invalid"
                )
        runtime = parsed.get("runtime_verification")
        if runtime is not None and not isinstance(runtime, Mapping):
            raise EvidenceError(
                "acceptance prerequisite runtime proof is invalid"
            )
        return (
            parsed,
            expected_candidate,
            expected_review,
            expected_evidence,
        )

    def consume(
        proof: Any,
        *,
        candidate_identity: Mapping[str, Any],
        review_summary: Mapping[str, Any],
        evidence_binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            (
                parsed,
                expected_candidate,
                expected_review,
                expected_evidence,
            ) = prepare(
                proof,
                candidate_identity=candidate_identity,
                review_summary=review_summary,
                evidence_binding=evidence_binding,
            )
        except Exception:
            if isinstance(proof, _AcceptancePrerequisiteProof):
                mark_revoked(proof)
            raise
        with records_lock:
            record = records.get(proof.issuance_id)
            if record is None or record["proof"] is not proof:
                raise EvidenceError(
                    "acceptance prerequisite proof is not an active issuance"
                )
            expire_if_needed(record)
            status = record["status"]
            if status == "CONSUMED":
                raise EvidenceError(
                    "acceptance prerequisite proof is already consumed"
                )
            if status == "REVOKED":
                raise EvidenceError(
                    "acceptance prerequisite proof is revoked"
                )
            if status == "EXPIRED":
                raise EvidenceError(
                    "acceptance prerequisite proof is expired"
                )
            if status != "ACTIVE":
                raise EvidenceError(
                    "acceptance prerequisite proof state is invalid"
                )
            if parsed["candidate_identity_sha256"] != expected_candidate:
                record["status"] = "REVOKED"
                record["terminal_at"] = now()
                raise EvidenceError(
                    "acceptance prerequisite proof is bound to a "
                    "different candidate"
                )
            if parsed["review_summary_sha256"] != expected_review:
                record["status"] = "REVOKED"
                record["terminal_at"] = now()
                raise EvidenceError(
                    "acceptance prerequisite proof is bound to different "
                    "review evidence"
                )
            if parsed["evidence_binding_sha256"] != expected_evidence:
                record["status"] = "REVOKED"
                record["terminal_at"] = now()
                raise EvidenceError(
                    "acceptance prerequisite proof evidence binding "
                    "does not match current evidence"
                )
            record["status"] = "CONSUMED"
            record["terminal_at"] = now()
            return parsed

    def revoke(proof: _AcceptancePrerequisiteProof) -> None:
        with records_lock:
            record = records.get(proof.issuance_id)
            if record is None or record["proof"] is not proof:
                raise EvidenceError(
                    "acceptance prerequisite proof is not an active issuance"
                )
            cleanup_errors = []
            transition_error = transition_revoked_locked(record)
            if transition_error is not None:
                cleanup_errors.append(transition_error)
            try:
                purge_locked()
            except BaseException as exc:
                cleanup_errors.append(str(exc))
            if cleanup_errors:
                raise EvidenceError(
                    "acceptance prerequisite was revoked but cleanup is "
                    f"incomplete: {cleanup_errors[0]}"
                )

    def revoke_owner(
        owner: _AcceptancePrerequisiteOwner,
    ) -> tuple[str, str | None]:
        if not isinstance(owner, _AcceptancePrerequisiteOwner):
            raise EvidenceError(
                "acceptance proof owner is invalid"
            )
        with records_lock:
            issuance_id = owners.get(owner.token)
            if issuance_id is None:
                issuance_id = next(
                    (
                        candidate_id
                        for candidate_id, record in records.items()
                        if record.get("owner_token") == owner.token
                    ),
                    None,
                )
            if issuance_id is None:
                return "NOT_ACTIVE", None
            record = records.get(issuance_id)
            if record is None:
                owners.pop(owner.token, None)
                return "NOT_ACTIVE", None
            cleanup_error = transition_revoked_locked(record)
            if cleanup_error is None:
                try:
                    purge_locked()
                except BaseException as exc:
                    cleanup_error = str(exc)
                    record["cleanup_error"] = cleanup_error
            return str(record["status"]), cleanup_error

    def status(proof: _AcceptancePrerequisiteProof) -> str:
        with records_lock:
            record = records.get(proof.issuance_id)
            if record is None or record["proof"] is not proof:
                return "NOT_ACTIVE"
            expire_if_needed(record)
            return str(record["status"])

    def purge() -> int:
        with records_lock:
            return purge_locked()

    def record_count() -> int:
        with records_lock:
            purge_locked()
            return len(records)

    return (
        issue,
        consume,
        revoke,
        revoke_owner,
        status,
        purge,
        record_count,
    )


(
    _issue_acceptance_prerequisite_proof,
    _consume_acceptance_prerequisite_proof,
    _revoke_acceptance_prerequisite_proof,
    _revoke_acceptance_prerequisite_owner,
    _acceptance_prerequisite_status,
    _purge_acceptance_prerequisite_proofs,
    _acceptance_prerequisite_record_count,
) = _make_acceptance_prerequisite_authority()


def _append_exception_note(
    error: BaseException,
    note: str,
) -> None:
    try:
        error.add_note(note)
        return
    except BaseException:
        pass
    try:
        notes = list(getattr(error, "diagnostic_notes", []))
        notes.append(note)
        error.diagnostic_notes = notes
    except BaseException:
        pass


@contextmanager
def _acceptance_prerequisite_transaction(
    *,
    candidate_identity: Mapping[str, Any],
    review_summary: Mapping[str, Any],
    evidence_binding: Mapping[str, Any],
    tests_verified: bool,
    frozen_verified: bool,
    previous_summary_available: bool,
    previous_summary_trusted: bool,
    runtime_verification: Mapping[str, Any] | None,
):
    """Own one issued proof until it is consumed or safely revoked."""
    owner = _new_acceptance_prerequisite_owner()
    try:
        proof = _issue_acceptance_prerequisite_proof(
            candidate_identity=candidate_identity,
            review_summary=review_summary,
            evidence_binding=evidence_binding,
            tests_verified=tests_verified,
            frozen_verified=frozen_verified,
            previous_summary_available=previous_summary_available,
            previous_summary_trusted=previous_summary_trusted,
            runtime_verification=runtime_verification,
            owner=owner,
        )
        yield proof
    except BaseException as original_error:
        try:
            _status, cleanup_error = (
                _revoke_acceptance_prerequisite_owner(owner)
            )
            if cleanup_error is not None:
                _append_exception_note(
                    original_error,
                    "acceptance prerequisite was revoked but cleanup is "
                    f"incomplete: {cleanup_error}",
                )
        except BaseException as cleanup_error:
            _append_exception_note(
                original_error,
                "acceptance prerequisite cleanup also failed: "
                f"{cleanup_error}",
            )
        raise
    else:
        status_after_exit, cleanup_error = (
            _revoke_acceptance_prerequisite_owner(owner)
        )
        if cleanup_error is not None:
            raise EvidenceError(
                "acceptance prerequisite owner cleanup is incomplete "
                f"after normal context exit: {cleanup_error}"
            )
        if status_after_exit == "ACTIVE":
            raise EvidenceError(
                "acceptance prerequisite remained ACTIVE after owner cleanup"
            )


def _evaluate_acceptance_gate_core(
    *,
    candidate_identity: Mapping[str, Any],
    trusted_repository_root: TrustedEvidenceRoot,
    trusted_review_evidence_root: TrustedEvidenceRoot | None = None,
    trusted_review_manifest_path: Path | None = None,
    trusted_binding_evidence_root: TrustedEvidenceRoot | None = None,
    trusted_review_binding_path: Path | None = None,
    review_summary: Mapping[str, Any],
    review_binding: Mapping[str, Any] | None,
    acceptance_policy: str,
    acceptance_evidence_binding: Mapping[str, Any],
    acceptance_prerequisites: _AcceptancePrerequisiteProof,
) -> dict[str, Any]:
    """Return a fail-closed acceptance decision without collapsing evidence states."""
    candidate_identity = json.loads(canonical_json(candidate_identity))
    provided_review_summary = json.loads(canonical_json(review_summary))
    acceptance_evidence_binding = json.loads(
        canonical_json(acceptance_evidence_binding)
    )
    if review_binding is not None:
        review_binding = json.loads(canonical_json(review_binding))
    prerequisite = _consume_acceptance_prerequisite_proof(
        acceptance_prerequisites,
        candidate_identity=candidate_identity,
        review_summary=provided_review_summary,
        evidence_binding=acceptance_evidence_binding,
    )
    previous_summary_available = prerequisite[
        "previous_summary_available"
    ]
    previous_summary_trusted = prerequisite[
        "previous_summary_trusted"
    ]
    tests_verified = prerequisite["tests_verified"]
    frozen_verified = prerequisite["frozen_verified"]
    runtime_verification = prerequisite["runtime_verification"]
    if acceptance_policy not in ACCEPTANCE_POLICIES:
        raise EvidenceError("unsupported acceptance policy")
    _validate_candidate_identity_semantics(
        candidate_identity,
        trusted_repository_root,
        "acceptance gate",
    )
    _validate_review_summary_semantics(
        provided_review_summary,
        candidate_identity,
        "acceptance gate input",
    )
    authoritative_review_summary: dict[str, Any]
    binding_status = "NOT_AVAILABLE"
    binding_unverified_scopes: list[str] = []
    if review_binding is None:
        review_root = (
            trusted_review_evidence_root
            if trusted_review_evidence_root is not None
            else trusted_repository_root
        )
        if not isinstance(review_root, TrustedEvidenceRoot):
            raise EvidenceError(
                "acceptance gate direct review has no trusted evidence root"
            )
        review_path = trusted_review_manifest_path
        if review_path is None:
            review_reference = Path(
                str(provided_review_summary.get("path"))
            )
            if review_reference.is_absolute():
                review_path = review_reference
            elif (
                review_reference.parts
                and review_reference.parts[0] == "bundle"
                and review_root.path != trusted_repository_root.path
            ):
                review_path = review_root.path.joinpath(
                    *review_reference.parts[1:]
                )
            else:
                review_path = review_root.path / review_reference
        code_review = provided_review_summary.get("code_review")
        if not isinstance(code_review, Mapping):
            raise EvidenceError(
                "acceptance gate direct review has no code review"
            )
        review_snapshot = _capture_review_evidence_snapshot(
            trusted_repository_root.path,
            review_path,
            review_root,
            expected_code_fingerprint=candidate_identity.get(
                "code_fingerprint"
            ),
            expected_head=code_review.get("target_head"),
            changed_candidate_paths=[
                item["path"]
                for item in candidate_identity.get("files", [])
            ],
        )
        actual_review = review_snapshot.summary()
        if canonical_json(actual_review) != canonical_json(
            provided_review_summary
        ):
            raise EvidenceError(
                "acceptance gate direct review does not match "
                "independently validated evidence"
            )
        authoritative_review_summary = actual_review
    else:
        if (
            trusted_binding_evidence_root is None
            or trusted_review_binding_path is None
        ):
            raise EvidenceError(
                "acceptance gate review binding has no trusted evidence root"
            )
        actual_binding = json.loads(
            canonical_json(
                validate_review_binding(
                    trusted_repository_root.path,
                    trusted_review_binding_path,
                    evidence_root=trusted_binding_evidence_root.path,
                )
            )
        )
        if canonical_json(actual_binding) != canonical_json(
            review_binding
        ):
            raise EvidenceError(
                "acceptance gate review binding does not match "
                "independently validated evidence"
            )
        binding_validation = _validate_review_binding_semantics(
            actual_binding,
            candidate_identity,
            provided_review_summary,
            "acceptance gate",
        )
        binding_unverified_scopes = list(
            binding_validation["unverified_scope"]
        )
        if binding_validation["review_clear"]:
            binding_status = "CONTENT_EQUIVALENT_CLEAR"
        else:
            binding_status = "CONTENT_EQUIVALENT_BLOCKED"
        authoritative_review_summary = _review_summary_from_binding(
            actual_binding
        )
    validated_review = _validate_review_summary_semantics(
        authoritative_review_summary,
        candidate_identity,
        "acceptance gate",
    )
    candidate_status = (
        CANDIDATE_IDENTITY_VERIFIED
        if candidate_identity.get("evidence_status") == CANDIDATE_IDENTITY_VERIFIED
        else BLOCKED
    )

    code_status = validated_review["code_status"]
    architecture_status = validated_review["architecture_status"]
    blocking_review_states = {
        "REQUEST_CHANGES",
        "WATCH",
        "BLOCK",
    }
    review_blocked = (
        code_status in blocking_review_states
        or architecture_status in blocking_review_states
    )
    review_unavailable = (
        code_status == "UNAVAILABLE"
        or architecture_status == "UNAVAILABLE"
    )
    code_lane = authoritative_review_summary.get("code_review")
    architecture_lane = authoritative_review_summary.get(
        "architecture_review"
    )
    review_uncovered_scopes = validated_review["uncovered_scope"]
    review_coverage_incomplete = bool(review_uncovered_scopes)
    historical_review_status = (
        "BLOCKED"
        if review_blocked or review_coverage_incomplete
        else "NOT_AVAILABLE"
        if review_unavailable
        else "CLEAR"
    )

    target_head = candidate_identity.get("target_head")
    direct_target_match = (
        isinstance(code_lane, Mapping)
        and isinstance(architecture_lane, Mapping)
        and code_lane.get("target_head") == target_head
        and architecture_lane.get("target_head") == target_head
        and not review_coverage_incomplete
    )
    if review_blocked or review_coverage_incomplete:
        current_review_status = "BLOCKED"
    elif review_unavailable:
        current_review_status = "NOT_AVAILABLE"
    elif direct_target_match:
        current_review_status = "CLEAR"
    else:
        current_review_status = "NOT_AVAILABLE"

    if acceptance_policy == "target_head_review_required":
        review_gate_status = current_review_status
    elif current_review_status == "CLEAR":
        review_gate_status = "CLEAR"
    elif binding_status == "CONTENT_EQUIVALENT_CLEAR":
        review_gate_status = "CONTENT_EQUIVALENT_CLEAR"
    elif binding_status in {"BLOCKED", "CONTENT_EQUIVALENT_BLOCKED"}:
        review_gate_status = "BLOCKED"
    else:
        review_gate_status = "NOT_AVAILABLE"

    if runtime_verification is None:
        runtime_status = NOT_VERIFIED
        runtime_reason = "no current candidate runtime evidence was provided"
        runtime_scopes: list[str] = []
    else:
        runtime_status = _bundle_status(
            runtime_verification.get("status"),
            RUNTIME_STATUSES,
            "runtime verification status",
        )
        runtime_reason = runtime_verification.get("reason")
        if not isinstance(runtime_reason, str) or not runtime_reason.strip():
            raise EvidenceError("runtime verification requires a reason")
        scopes = runtime_verification.get("scopes")
        if not isinstance(scopes, list) or not all(
            isinstance(item, str) and item for item in scopes
        ):
            raise EvidenceError("runtime verification scopes must be strings")
        runtime_scopes = list(scopes)
        if runtime_status == "VERIFIED":
            unsupported_scopes = (
                set(runtime_scopes) - SUPPORTED_RUNTIME_SCOPES
            )
            if unsupported_scopes:
                raise EvidenceError(
                    "runtime VERIFIED has unsupported scopes: "
                    f"{sorted(unsupported_scopes)}"
                )
            if not runtime_scopes:
                raise EvidenceError(
                    "runtime VERIFIED requires at least one supported scope"
                )
            valid_evidence_count = runtime_verification.get(
                "valid_evidence_count"
            )
            if (
                type(valid_evidence_count) is not int
                or valid_evidence_count <= 0
            ):
                raise EvidenceError(
                    "runtime VERIFIED requires current evidence"
                )

    blocking_reasons: list[str] = []
    if candidate_status != CANDIDATE_IDENTITY_VERIFIED:
        blocking_reasons.append("candidate_identity_not_verified")
    if not tests_verified:
        blocking_reasons.append("tests_not_verified")
    if not frozen_verified:
        blocking_reasons.append("frozen_evidence_not_verified")
    if historical_review_status == "BLOCKED":
        blocking_reasons.append("historical_review_blocked")
    if binding_status in {"BLOCKED", "CONTENT_EQUIVALENT_BLOCKED"}:
        blocking_reasons.append("review_binding_blocked")
    if review_coverage_incomplete:
        blocking_reasons.append("review_uncovered_scope")
    if current_review_status == "BLOCKED" and acceptance_policy == (
        "target_head_review_required"
    ):
        blocking_reasons.append("current_review_blocked")

    if blocking_reasons:
        evidence_integrity_status = BLOCKED
        acceptance_status = BLOCKED
    elif not previous_summary_available:
        evidence_integrity_status = BOOTSTRAP_INCOMPLETE
        acceptance_status = BOOTSTRAP_INCOMPLETE
        blocking_reasons.append("previous_summary_not_available")
    elif review_gate_status != "CLEAR" and review_gate_status != (
        "CONTENT_EQUIVALENT_CLEAR"
    ):
        evidence_integrity_status = NOT_AVAILABLE
        acceptance_status = NOT_AVAILABLE
        blocking_reasons.append(f"review_gate:{review_gate_status}")
    else:
        evidence_integrity_status = PASS_EVIDENCE_INTEGRITY
        if runtime_status != "VERIFIED":
            acceptance_status = NOT_VERIFIED
            blocking_reasons.append("current_runtime_not_verified")
        else:
            trust_blockers = []
            if not PERSISTENT_PUBLICATION_ANCESTRY_SUPPORTED:
                trust_blockers.append(
                    "persistent_publication_ancestry_unsupported"
                )
            if not MUTATION_EXECUTION_TRUST_SUPPORTED:
                trust_blockers.append(
                    "mutation_execution_trust_unsupported"
                )
            if not TEST_EXECUTION_TRUST_SUPPORTED:
                trust_blockers.append(
                    "test_execution_trust_unsupported"
                )
            if trust_blockers:
                acceptance_status = NOT_VERIFIED
                blocking_reasons.extend(trust_blockers)
            elif not previous_summary_trusted:
                acceptance_status = NOT_VERIFIED
                blocking_reasons.append("previous_summary_not_trusted")
            else:
                acceptance_status = ACCEPTED_FOR_MERGE_REVIEW

    unverified_scopes = []
    current_review_resolved = (
        current_review_status == "CLEAR"
        or (
            acceptance_policy == "content_equivalence_allowed"
            and review_gate_status == "CONTENT_EQUIVALENT_CLEAR"
        )
    )
    current_review_resolution = (
        "DIRECT_CLEAR"
        if current_review_status == "CLEAR"
        else "CONTENT_EQUIVALENT_CLEAR"
        if current_review_resolved
        else "UNVERIFIED"
    )
    if (
        current_review_resolution == "DIRECT_CLEAR"
        and validated_review["candidate_acceptance"]
        != "INDEPENDENT_REVIEW_CLEAR"
    ):
        raise EvidenceError(
            "review summary candidate acceptance disagrees "
            "with direct review resolution"
        )
    if (
        current_review_resolution == "CONTENT_EQUIVALENT_CLEAR"
        and validated_review["candidate_acceptance"]
        not in {
            "INDEPENDENT_REVIEW_CLEAR",
            "CONTENT_EQUIVALENT_REVIEW_CLEAR",
        }
    ):
        raise EvidenceError(
            "review summary candidate acceptance disagrees "
            "with content-equivalent resolution"
        )
    if not current_review_resolved:
        unverified_scopes.append("current_target_independent_review")
    if binding_status not in {
        "CONTENT_EQUIVALENT_CLEAR",
        "NOT_AVAILABLE",
    }:
        unverified_scopes.append("content_equivalence_binding")
    if runtime_status != "VERIFIED":
        unverified_scopes.append("current_runtime_verification")
    unverified_scopes.extend(review_uncovered_scopes)
    unverified_scopes.extend(binding_unverified_scopes)
    if not PERSISTENT_PUBLICATION_ANCESTRY_SUPPORTED:
        unverified_scopes.append("persistent_publication_ancestry")
    if not MUTATION_EXECUTION_TRUST_SUPPORTED:
        unverified_scopes.append("mutation_execution_trust")
    if not TEST_EXECUTION_TRUST_SUPPORTED:
        unverified_scopes.append("test_execution_trust")
    if binding_unverified_scopes:
        blocking_reasons.append("review_binding_unverified_scope")
        if acceptance_status == ACCEPTED_FOR_MERGE_REVIEW:
            acceptance_status = NOT_VERIFIED

    acceptance_gate = {
        "candidate_identity": candidate_status,
        "historical_review": historical_review_status,
        "content_equivalence_binding": binding_status,
        "current_target_independent_review": current_review_status,
        "current_target_review_resolution": current_review_resolution,
        "review_gate": review_gate_status,
        "acceptance_policy": acceptance_policy,
        "tests": "VERIFIED" if tests_verified else "NOT_VERIFIED",
        "frozen_evidence": (
            "VERIFIED" if frozen_verified else "NOT_VERIFIED"
        ),
        "previous_summary": (
            (
                "AVAILABLE"
                if previous_summary_trusted
                else "AVAILABLE_UNTRUSTED"
            )
            if previous_summary_available
            else NOT_AVAILABLE
        ),
        "previous_summary_trusted": (
            previous_summary_available and previous_summary_trusted
        ),
        "runtime_verification": {
            "status": runtime_status,
            "scopes": runtime_scopes,
            "reason": runtime_reason,
            "valid_evidence_count": (
                int(runtime_verification.get("valid_evidence_count", 0))
                if runtime_verification is not None
                else 0
            ),
        },
        "persistent_publication_ancestry": (
            "VERIFIED"
            if PERSISTENT_PUBLICATION_ANCESTRY_SUPPORTED
            else "UNSUPPORTED"
        ),
        "mutation_execution_trust": (
            "VERIFIED"
            if MUTATION_EXECUTION_TRUST_SUPPORTED
            else "UNSUPPORTED"
        ),
        "test_execution_trust": (
            "VERIFIED"
            if TEST_EXECUTION_TRUST_SUPPORTED
            else "UNSUPPORTED"
        ),
        "unverified_scopes": unverified_scopes,
        "blocking_reasons": blocking_reasons,
        "evidence_integrity_status": evidence_integrity_status,
        "acceptance_status": acceptance_status,
        "merge_ready": acceptance_status == ACCEPTED_FOR_MERGE_REVIEW,
    }
    _validate_acceptance_gate_semantics(
        acceptance_gate,
        "evaluated acceptance gate",
    )
    return acceptance_gate


def evaluate_acceptance_gate(
    *,
    candidate_identity: Mapping[str, Any],
    trusted_repository_root: TrustedEvidenceRoot,
    trusted_review_evidence_root: TrustedEvidenceRoot | None = None,
    trusted_review_manifest_path: Path | None = None,
    trusted_binding_evidence_root: TrustedEvidenceRoot | None = None,
    trusted_review_binding_path: Path | None = None,
    review_summary: Mapping[str, Any],
    review_binding: Mapping[str, Any] | None,
    acceptance_policy: str,
) -> dict[str, Any]:
    """Diagnostic public gate that cannot self-assert acceptance."""
    evidence_binding = {"diagnostic": True}
    with _acceptance_prerequisite_transaction(
        candidate_identity=candidate_identity,
        review_summary=review_summary,
        evidence_binding=evidence_binding,
        tests_verified=False,
        frozen_verified=False,
        previous_summary_available=False,
        previous_summary_trusted=False,
        runtime_verification=None,
    ) as proof:
        return _evaluate_acceptance_gate_core(
            candidate_identity=candidate_identity,
            trusted_repository_root=trusted_repository_root,
            trusted_review_evidence_root=trusted_review_evidence_root,
            trusted_review_manifest_path=trusted_review_manifest_path,
            trusted_binding_evidence_root=trusted_binding_evidence_root,
            trusted_review_binding_path=trusted_review_binding_path,
            review_summary=review_summary,
            review_binding=review_binding,
            acceptance_policy=acceptance_policy,
            acceptance_evidence_binding=evidence_binding,
            acceptance_prerequisites=proof,
        )


def _selected_bundle_path(
    root: Path,
    metadata_path: Path,
    evidence_bundle_path: Path | None,
) -> Path | None:
    metadata = read_json(metadata_path, root=root)
    bundle_record = metadata.get("evidence_bundle")
    selected = evidence_bundle_path
    if selected is None and isinstance(bundle_record, dict):
        path_value = bundle_record.get("path")
        if not isinstance(path_value, str) or not path_value:
            raise EvidenceError("run metadata has an invalid evidence bundle path")
        selected = Path(path_value)
    if selected is not None and not selected.is_absolute():
        selected = root / selected
    return selected


_MUTATION_REEXECUTION_CACHE: dict[tuple[str, ...], dict[str, Any]] = {}


def _validate_mutation_result_schema(
    result: Mapping[str, Any],
    case_id: str,
    root: Path,
) -> None:
    _reject_unknown_fields(
        result,
        {
            "id",
            "property",
            "path",
            "patches",
            "patch_count",
            "tests",
            "expected",
            "result",
            "reason",
            "mutated_lines_executed",
            "execution_evidence",
            "test",
        },
        f"mutation result {case_id}",
    )
    if result.get("id") != case_id:
        raise EvidenceError(f"mutation result ID mismatch: {case_id}")
    if result.get("expected") != "killed" or result.get("result") != "killed":
        raise EvidenceError(f"mutation case was not killed: {case_id}")
    if result.get("reason") is not None:
        raise EvidenceError(
            f"killed mutation must not carry a failure reason: {case_id}"
        )
    if not isinstance(result.get("property"), str) or not result["property"]:
        raise EvidenceError(f"mutation property is invalid: {case_id}")
    path = result.get("path")
    patches = result.get("patches")
    patch_count = result.get("patch_count")
    if not isinstance(path, str) or not path:
        raise EvidenceError(f"mutation path is invalid: {case_id}")
    if not isinstance(patches, list) or not patches:
        raise EvidenceError(f"mutation patches are missing: {case_id}")
    if type(patch_count) is not int or patch_count != len(patches):
        raise EvidenceError(f"mutation patch count is invalid: {case_id}")
    normalized_patches: list[dict[str, Any]] = []
    for patch in patches:
        if not isinstance(patch, Mapping):
            raise EvidenceError(f"mutation patch is invalid: {case_id}")
        _reject_unknown_fields(
            patch,
            {"path", "start_line", "end_line"},
            f"mutation patch {case_id}",
        )
        start = patch.get("start_line")
        end = patch.get("end_line")
        if (
            not isinstance(patch.get("path"), str)
            or not patch["path"]
            or type(start) is not int
            or type(end) is not int
            or start < 1
            or end < start
        ):
            raise EvidenceError(f"mutation patch fields are invalid: {case_id}")
        normalized_patches.append(dict(patch))
    if normalized_patches[0]["path"] != path:
        raise EvidenceError(f"mutation primary path mismatch: {case_id}")
    tests = result.get("tests")
    if (
        not isinstance(tests, list)
        or not tests
        or not all(isinstance(item, str) and item for item in tests)
        or len(tests) != len(set(tests))
    ):
        raise EvidenceError(f"mutation tests are invalid: {case_id}")

    mutated = result.get("mutated_lines_executed")
    if not isinstance(mutated, Mapping):
        raise EvidenceError(f"mutation trace evidence is missing: {case_id}")
    _reject_unknown_fields(
        mutated,
        {"executed", "segments", "trace_missing"},
        f"mutation trace {case_id}",
    )
    if (
        not isinstance(mutated.get("executed"), bool)
        or not isinstance(mutated.get("trace_missing"), bool)
    ):
        raise EvidenceError(f"mutation trace flags are invalid: {case_id}")
    segments = mutated.get("segments")
    if not isinstance(segments, list) or len(segments) != len(patches):
        raise EvidenceError(f"mutation trace segments are incomplete: {case_id}")
    normalized_segments: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, Mapping):
            raise EvidenceError(
                f"mutation trace segment is invalid: {case_id}"
            )
        _reject_unknown_fields(
            segment,
            {"path", "start_line", "end_line", "hit"},
            f"mutation trace segment {case_id}[{index}]",
        )
        expected_segment = {
            **patches[index],
            "hit": segment.get("hit"),
        }
        if dict(segment) != expected_segment or not isinstance(
            segment.get("hit"), bool
        ):
            raise EvidenceError(
                f"mutation trace segment disagrees with patch: {case_id}"
            )
        normalized_segments.append(dict(segment))
    expected_executed = all(item["hit"] for item in normalized_segments)
    if mutated["executed"] is not expected_executed:
        raise EvidenceError(
            f"mutation executed flag disagrees with trace: {case_id}"
        )

    execution = result.get("execution_evidence")
    if not isinstance(execution, Mapping):
        raise EvidenceError(
            f"mutation execution evidence is missing: {case_id}"
        )
    _reject_unknown_fields(
        execution,
        {
            "scope",
            "executed_in_process",
            "static_source_check",
            "executed_in_child_process",
            "child_trace_supported",
            "trace_missing",
        },
        f"mutation execution {case_id}",
    )
    for field in (
        "executed_in_process",
        "static_source_check",
        "executed_in_child_process",
        "child_trace_supported",
        "trace_missing",
    ):
        if not isinstance(execution.get(field), bool):
            raise EvidenceError(
                f"mutation execution.{field} must be boolean: {case_id}"
            )
    if execution["trace_missing"] is not mutated["trace_missing"]:
        raise EvidenceError(
            f"mutation trace_missing evidence is contradictory: {case_id}"
        )
    scope = execution.get("scope")
    if scope == "in_process":
        if (
            execution["executed_in_process"] is not True
            or execution["static_source_check"] is not False
            or execution["executed_in_child_process"] is not False
            or execution["child_trace_supported"] is not False
            or execution["trace_missing"] is not False
            or mutated["executed"] is not True
        ):
            raise EvidenceError(
                f"in_process mutation lacks verified execution: {case_id}"
            )
    elif scope == "static_source_check":
        if (
            execution["executed_in_process"] is not False
            or execution["static_source_check"] is not True
            or execution["executed_in_child_process"] is not False
            or execution["child_trace_supported"] is not False
        ):
            raise EvidenceError(
                f"static mutation evidence is contradictory: {case_id}"
            )
    elif scope == "child_process":
        if (
            execution["executed_in_process"] is not False
            or execution["static_source_check"] is not False
            or execution["executed_in_child_process"] is not True
            or execution["child_trace_supported"] is not True
            or execution["trace_missing"] is not False
        ):
            raise EvidenceError(
                f"child-process mutation lacks verified execution: {case_id}"
            )
    else:
        raise EvidenceError(
            f"mutation execution scope is unsupported: {case_id}"
        )

    test_record = result.get("test")
    if not isinstance(test_record, Mapping):
        raise EvidenceError(f"mutation test record is missing: {case_id}")
    _reject_unknown_fields(
        test_record,
        {"command", "exit_code"},
        f"mutation test {case_id}",
    )
    command = test_record.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise EvidenceError(f"mutation test command is invalid: {case_id}")
    active_python = Path(sys.executable).absolute()
    system_python = Path(
        shutil.which("python3") or "/usr/bin/python3"
    ).absolute()
    allowed_interpreters = {
        str(active_python),
        str(active_python.resolve()),
        str(system_python),
        str(system_python.resolve()),
    }
    candidate_interpreter = root / ".venv" / "bin" / "python"
    if candidate_interpreter.is_file():
        allowed_interpreters.add(str(candidate_interpreter.absolute()))
    command_interpreter = Path(command[0]).absolute()
    if (
        str(command_interpreter) not in allowed_interpreters
        and str(command_interpreter.resolve()) not in allowed_interpreters
    ):
        raise EvidenceError(
            f"mutation test interpreter is not the active runner: {case_id}"
        )
    if type(test_record.get("exit_code")) is not int or test_record[
        "exit_code"
    ] != 1:
        raise EvidenceError(
            f"mutation test did not fail as required: {case_id}"
        )


def _reexecute_mutation_report(
    root: Path,
    *,
    source_sha256: str,
    cases_sha256: str,
    code_fingerprint_value: str,
    cases_payload: Mapping[str, Any],
) -> dict[str, Any]:
    interpreter = Path(shutil.which("python3") or "/usr/bin/python3")
    try:
        interpreter.relative_to(root)
    except ValueError:
        pass
    else:
        raise EvidenceError(
            "independent mutation interpreter is inside the candidate root"
        )
    cache_key = (
        str(interpreter),
        sys.version,
        source_sha256,
        cases_sha256,
        code_fingerprint_value,
    )
    cached = _MUTATION_REEXECUTION_CACHE.get(cache_key)
    if cached is not None:
        return json.loads(canonical_json(cached))

    with tempfile.TemporaryDirectory(
        prefix="zevorinth-mutation-revalidation-"
    ) as temp:
        temp_root = Path(temp)
        with TrustedEvidenceRoot(
            temp_root,
            "mutation revalidation root",
        ) as child_root:
            with _owned_mutation_project_snapshot(
                root,
                child_root,
                temp_root,
                expected_code_fingerprint=code_fingerprint_value,
            ) as project_root_owner:
                project_root = project_root_owner.path
                cases_path = project_root / "mutation-cases.json"
                project_root_owner._write_bytes_staging(
                    cases_path,
                    canonical_json(cases_payload).encode("utf-8"),
                    "mutation revalidation cases",
                )
                output_path = temp_root / "mutations.json"
                with project_root_owner.pinned():
                    completed = _run_subprocess_in_trusted_cwd(
                        project_root,
                        [
                            str(interpreter),
                            "tests/mutation_runner.py",
                            "--cases",
                            "mutation-cases.json",
                            "--output",
                            str(output_path),
                        ],
                        "mutation revalidation project",
                    )
            if not output_path.is_file():
                raise EvidenceError(
                    "independent mutation re-execution failed: "
                    + completed.stderr.decode(
                        "utf-8",
                        errors="replace",
                    ).strip()
                )
            report_bytes = _read_regular_file(
                output_path,
                "independent mutation report",
                root=temp_root,
            )
            report = _parse_json_object_bytes(
                report_bytes,
                output_path,
            )
    _MUTATION_REEXECUTION_CACHE[cache_key] = json.loads(canonical_json(report))
    return report


def _normalized_mutation_report(report: Mapping[str, Any]) -> dict[str, Any]:
    normalized = json.loads(canonical_json(report))
    for result in normalized.get("results", []):
        if isinstance(result, Mapping):
            test = result.get("test")
            if isinstance(test, Mapping):
                command = test.get("command")
                if isinstance(command, list) and command:
                    command[0] = "<active-python>"
    return normalized


def _validate_mutation_evidence(
    root: Path,
    mutation: Mapping[str, Any],
) -> tuple[dict[str, int], dict[str, int]]:
    if mutation.get("source") != "tests/mutation_cases.json":
        raise EvidenceError("mutation source is not the current candidate source")
    source_relative = "tests/mutation_cases.json"
    source_bytes = _read_regular_file(
        root / source_relative,
        "mutation source",
        root=root,
    )
    source_sha256 = sha256_bytes(source_bytes)
    if mutation.get("source_sha256") != source_sha256:
        raise EvidenceError("mutation source hash does not match metadata")
    source = _parse_json_object_bytes(
        source_bytes,
        root / source_relative,
    )
    if source.get("version") != MUTATION_CASES_VERSION:
        raise EvidenceError("mutation source version is unsupported")
    cases_payload, filters, selected_ids, filtered_ids = (
        _filter_mutation_source(source)
    )
    metadata_filters = mutation.get("filters")
    if not isinstance(metadata_filters, Mapping) or dict(metadata_filters) != filters:
        raise EvidenceError("mutation filter counts do not match the source")

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
        root=root,
    )
    _validate_hash(
        report_path,
        str(mutation.get("report_sha256")),
        "mutation report",
        root=root,
    )
    cases = read_json(cases_path, root=root)
    if canonical_json(cases) != canonical_json(cases_payload):
        raise EvidenceError(
            "mutation cases were omitted, added, reordered, or misclassified"
        )
    report = read_json(report_path, root=root)
    _reject_unknown_fields(
        report,
        {
            "status",
            "case_count",
            "counts",
            "evidence_scope_counts",
            "execution_evidence_counts",
            "execution_unverified",
            "results",
            "unexpected_results",
            "unverified_survivals",
            "version",
        },
        "mutation report",
    )
    if report.get("version") != MUTATION_CASES_VERSION:
        raise EvidenceError("mutation report version is unsupported")
    if report.get("case_count") != len(selected_ids):
        raise EvidenceError("mutation report/input case count mismatch")
    counts = report.get("counts")
    if not isinstance(counts, Mapping):
        raise EvidenceError("mutation report has no counts")
    count_names = ("killed", "survived", "invalid", "error")
    _reject_unknown_fields(counts, set(count_names), "mutation counts")
    normalized_counts: dict[str, int] = {}
    for field in count_names:
        value = counts.get(field)
        if type(value) is not int or value < 0:
            raise EvidenceError(f"mutation count is invalid: {field}")
        normalized_counts[field] = value
    if sum(normalized_counts.values()) != len(selected_ids):
        raise EvidenceError("mutation result count mismatch")
    if (
        report.get("status") != "PASS"
        or normalized_counts["killed"] != len(selected_ids)
        or any(
            normalized_counts[field] != 0
            for field in ("survived", "invalid", "error")
        )
    ):
        raise EvidenceError("mutation evidence is not PASS")
    if report.get("unexpected_results") != []:
        raise EvidenceError("mutation report has unexpected results")
    if report.get("unverified_survivals") != []:
        raise EvidenceError("mutation report has unverified survivors")
    if report.get("execution_unverified") != []:
        raise EvidenceError("mutation execution contains unverified cases")

    results = report.get("results")
    if not isinstance(results, list):
        raise EvidenceError("mutation report has no results array")
    result_ids: list[str] = []
    derived_scope_counts = {
        "in_process": 0,
        "child_process": 0,
        "static_source_check": 0,
        "unverified": 0,
    }
    for index, result in enumerate(results):
        if not isinstance(result, Mapping):
            raise EvidenceError(f"mutation result[{index}] is invalid")
        case_id = result.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise EvidenceError(f"mutation result[{index}] has no case ID")
        result_ids.append(case_id)
        _validate_mutation_result_schema(result, case_id, root)
        scope = str(result["execution_evidence"]["scope"])
        derived_scope_counts[scope] += 1
    if len(result_ids) != len(set(result_ids)):
        raise EvidenceError("mutation report contains duplicate case IDs")
    if sorted(result_ids) != sorted(selected_ids):
        raise EvidenceError("mutation report case IDs do not match the source")

    scopes = report.get("evidence_scope_counts")
    if not isinstance(scopes, Mapping):
        raise EvidenceError("mutation report has no evidence scope counts")
    _reject_unknown_fields(
        scopes,
        {"in_process", "static_source_check", "unverified"},
        "mutation evidence scopes",
    )
    normalized_scopes: dict[str, int] = {}
    for field in ("in_process", "static_source_check", "unverified"):
        value = scopes.get(field)
        if type(value) is not int or value < 0:
            raise EvidenceError(f"mutation evidence scope is invalid: {field}")
        normalized_scopes[field] = value
    if normalized_scopes["unverified"] != 0:
        raise EvidenceError("mutation evidence contains unverified kills")
    if sum(normalized_scopes.values()) != len(selected_ids):
        raise EvidenceError("mutation evidence scope count mismatch")
    expected_evidence_scopes = {
        "in_process": derived_scope_counts["in_process"],
        "static_source_check": derived_scope_counts["static_source_check"],
        "unverified": derived_scope_counts["unverified"],
    }
    if normalized_scopes != expected_evidence_scopes:
        raise EvidenceError(
            "mutation evidence scope counts do not match per-result evidence"
        )

    execution_scopes = report.get("execution_evidence_counts")
    if not isinstance(execution_scopes, Mapping):
        raise EvidenceError("mutation report has no execution evidence counts")
    _reject_unknown_fields(
        execution_scopes,
        {"in_process", "child_process", "static_source_check", "unverified"},
        "mutation execution evidence counts",
    )
    normalized_execution_scopes: dict[str, int] = {}
    for field in ("in_process", "child_process", "static_source_check", "unverified"):
        value = execution_scopes.get(field)
        if type(value) is not int or value < 0:
            raise EvidenceError(
                f"mutation execution evidence count is invalid: {field}"
            )
        normalized_execution_scopes[field] = value
    if normalized_execution_scopes != derived_scope_counts:
        raise EvidenceError(
            "mutation execution evidence counts do not match per-result evidence"
        )

    independently_executed = _reexecute_mutation_report(
        root,
        source_sha256=source_sha256,
        cases_sha256=sha256_bytes(
            canonical_json(cases_payload).encode("utf-8")
        ),
        code_fingerprint_value=code_fingerprint(root),
        cases_payload=cases_payload,
    )
    if canonical_json(_normalized_mutation_report(report)) != canonical_json(
        _normalized_mutation_report(independently_executed)
    ):
        raise EvidenceError(
            "mutation report does not match independent current re-execution"
        )

    final_source = _read_regular_file(
        root / source_relative,
        "mutation source recheck",
        root=root,
    )
    if sha256_bytes(final_source) != source_sha256:
        raise EvidenceError("mutation source changed during validation")
    return normalized_counts, normalized_scopes


def build_summary(
    root: Path,
    metadata_path: Path,
    *,
    replacement_reason: str,
    evidence_bundle_path: Path | None = None,
    acceptance_policy: str = "target_head_review_required",
    trusted_previous_summary_sha256: str | None = None,
    trusted_previous_candidate_snapshot_path: Path | None = None,
) -> dict[str, Any]:
    if trusted_previous_summary_sha256 is not None:
        _require_sha256(
            trusted_previous_summary_sha256,
            "trusted previous summary SHA-256",
        )
    root = _canonical_absolute_path(root)
    with _trusted_repository_scope(root) as repository_root:
        selected = _selected_bundle_path(
            root,
            metadata_path,
            evidence_bundle_path,
        )
        if selected is None:
            return _build_summary_impl(
                root,
                metadata_path,
                replacement_reason=replacement_reason,
                evidence_bundle_path=None,
                acceptance_policy=acceptance_policy,
                trusted_previous_summary_sha256=(
                    trusted_previous_summary_sha256
                ),
                trusted_previous_candidate_snapshot_path=(
                    trusted_previous_candidate_snapshot_path
                ),
                trusted_repository_root=repository_root,
            )
        bundle_root = _canonical_absolute_path(
            _canonical_absolute_path(selected).parent
        )
        with TrustedEvidenceRoot(
            bundle_root,
            "bundle acceptance lifecycle root",
        ) as trusted_root:
            with trusted_root.pinned():
                return _build_summary_impl(
                    root,
                    metadata_path,
                    replacement_reason=replacement_reason,
                    evidence_bundle_path=selected,
                    acceptance_policy=acceptance_policy,
                    trusted_bundle_root=trusted_root,
                    trusted_previous_summary_sha256=(
                        trusted_previous_summary_sha256
                    ),
                    trusted_previous_candidate_snapshot_path=(
                        trusted_previous_candidate_snapshot_path
                    ),
                    trusted_repository_root=repository_root,
                )


def _build_summary_impl(
    root: Path,
    metadata_path: Path,
    *,
    replacement_reason: str,
    evidence_bundle_path: Path | None = None,
    acceptance_policy: str = "target_head_review_required",
    trusted_bundle_root: TrustedEvidenceRoot | None = None,
    trusted_previous_summary_sha256: str | None = None,
    trusted_previous_candidate_snapshot_path: Path | None = None,
    trusted_repository_root: TrustedEvidenceRoot | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    if (
        not isinstance(trusted_repository_root, TrustedEvidenceRoot)
        or trusted_repository_root.path != root
    ):
        raise EvidenceError(
            "summary build has no matching trusted repository root"
        )
    metadata_bytes = _read_regular_file(
        metadata_path,
        "run metadata",
        root=root,
    )
    metadata_sha256 = sha256_bytes(metadata_bytes)
    metadata = _parse_json_object_bytes(metadata_bytes, metadata_path)
    metadata_path = _canonical_absolute_path(metadata_path)
    run_dir = metadata_path.parent
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("unexpected run metadata schema")
    recorded_policy = metadata.get("acceptance_policy")
    if recorded_policy is not None:
        acceptance_policy = resolve_acceptance_policy(
            acceptance_policy,
            recorded_policy,
        )
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
    bundle_record = metadata.get("evidence_bundle")
    selected_bundle_path = evidence_bundle_path
    if selected_bundle_path is None and isinstance(bundle_record, dict):
        path_value = bundle_record.get("path")
        if not isinstance(path_value, str) or not path_value:
            raise EvidenceError("run metadata has an invalid evidence bundle path")
        selected_bundle_path = Path(path_value)
    if selected_bundle_path is not None and not selected_bundle_path.is_absolute():
        selected_bundle_path = root / selected_bundle_path
    bundle = None
    bundle_root = None
    bundle_path = None
    if selected_bundle_path is not None:
        if trusted_bundle_root is None:
            with TrustedEvidenceRoot(
                _canonical_absolute_path(selected_bundle_path).parent,
                "bundle acceptance lifecycle root",
            ) as local_trusted_root:
                with local_trusted_root.pinned():
                    return _build_summary_impl(
                        root,
                        metadata_path,
                        replacement_reason=replacement_reason,
                        evidence_bundle_path=evidence_bundle_path,
                        acceptance_policy=acceptance_policy,
                        trusted_bundle_root=local_trusted_root,
                        trusted_previous_summary_sha256=(
                            trusted_previous_summary_sha256
                        ),
                        trusted_previous_candidate_snapshot_path=(
                            trusted_previous_candidate_snapshot_path
                        ),
                        trusted_repository_root=trusted_repository_root,
                    )
        bundle = _validate_evidence_bundle_with_root(
            root,
            selected_bundle_path,
            trusted_bundle_root,
        )
        bundle_root = Path(bundle["bundle_root"])
        bundle_path = Path(bundle["bundle_manifest_path"])
        if isinstance(bundle_record, dict):
            _validate_hash(
                bundle_path,
                str(bundle_record.get("sha256")),
                "evidence bundle",
                root=bundle_root,
            )
        if (
            bundle["candidate_identity"]["target_head"] != current_git["head"]
            or bundle["candidate_identity"]["code_fingerprint"] != current_code
        ):
            raise EvidenceError(
                "evidence bundle candidate does not match current repository"
            )
    tests = metadata.get("tests")
    if not isinstance(tests, dict):
        raise EvidenceError("run metadata has no tests object")
    current_test_discovery = _current_test_discovery(root)
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
            root=root,
        )
        _validate_hash(
            log_path,
            str(evidence.get("log_sha256")),
            f"{name} log",
            root=root,
        )
        result = read_json(result_path, root=root)
        if result.get("schema_version") != TEST_RESULT_SCHEMA_VERSION:
            raise EvidenceError(f"{name} result schema is unsupported")
        _reject_unknown_fields(
            result,
            {
                "schema_version",
                "python",
                "code_fingerprint_before",
                "code_fingerprint_after",
                "counts",
                "discovery",
                "discovery_sha256",
                "executed",
                "executed_sha256",
                "skipped_tests",
                "successful",
            },
            f"{name} result",
        )
        interpreter = result.get("python")
        if not isinstance(interpreter, str) or not interpreter:
            raise EvidenceError(f"{name} result has no interpreter path")
        interpreter_paths.add(str(Path(interpreter).absolute()))
        if result.get("successful") is not True:
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
        discovery = result.get("discovery")
        if not isinstance(discovery, Mapping):
            raise EvidenceError(f"{name} test discovery manifest is missing")
        _validate_test_discovery_manifest(
            discovery,
            f"{name} test discovery",
        )
        if canonical_json(discovery) != canonical_json(
            current_test_discovery
        ):
            raise EvidenceError(
                f"{name} test discovery does not match current discovery"
            )
        if result.get("discovery_sha256") != sha256_bytes(
            canonical_json(discovery).encode("utf-8")
        ):
            raise EvidenceError(f"{name} test discovery hash mismatch")
        executed = result.get("executed")
        if not isinstance(executed, Mapping):
            raise EvidenceError(f"{name} executed test evidence is missing")
        _validate_test_execution_binding(
            executed,
            discovery,
            counts,
            name,
        )
        if result.get("executed_sha256") != sha256_bytes(
            canonical_json(executed).encode("utf-8")
        ):
            raise EvidenceError(f"{name} executed test evidence hash mismatch")
        selected = discovery["selected_tests"]
        filtered = discovery["filtered_tests"]
        if counts["discovered"] != len(discovery["discovered_tests"]):
            raise EvidenceError(f"{name} discovered test count mismatch")
        if (
            len(selected) != counts["selected"]
            or len(filtered) != counts["filtered"]
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
        skipped_tests = result.get("skipped_tests")
        if skipped_tests is None:
            if counts["skipped"]:
                raise EvidenceError(
                    f"{name} skipped test details are required"
                )
        else:
            if not isinstance(skipped_tests, list) or len(
                skipped_tests
            ) != counts["skipped"]:
                raise EvidenceError(
                    f"{name} skipped test details/count mismatch"
                )
            skipped_ids = [
                item.get("test")
                for item in skipped_tests
                if isinstance(item, Mapping)
            ]
            if (
                len(skipped_ids) != len(skipped_tests)
                or len(skipped_ids) != len(set(skipped_ids))
            ):
                raise EvidenceError(
                    f"{name} skipped test IDs must be unique"
                )
            for item in skipped_tests:
                if not isinstance(item, Mapping):
                    raise EvidenceError(
                        f"{name} skipped test detail must be an object"
                    )
                test_id = item.get("test")
                reason = item.get("reason")
                if (
                    not isinstance(test_id, str)
                    or not test_id
                    or not isinstance(reason, str)
                    or not reason
                ):
                    raise EvidenceError(
                        f"{name} skipped test detail is invalid"
                    )
                if test_id in REQUIRED_CONTROL_TESTS:
                    raise EvidenceError(
                        f"{name} skipped a required control test: {test_id}"
                    )
                if test_id not in selected_ids:
                    raise EvidenceError(
                        f"{name} skipped a test outside the selected set: "
                        f"{test_id}"
                    )
        test_summary[name] = {
            **counts,
            "python": interpreter,
            "result": summary_evidence_path(result_path, root, run_dir),
            "result_sha256": evidence["result_sha256"],
            "log": summary_evidence_path(log_path, root, run_dir),
            "log_sha256": evidence["log_sha256"],
            "discovery_sha256": result["discovery_sha256"],
            "selection_rules_version": TEST_SELECTION_RULES_VERSION,
            "selection_rules_sha256": discovery["selection_rules_sha256"],
        }
    if len(interpreter_paths) != 2:
        raise EvidenceError(
            "system and virtual-environment test runs used the same interpreter"
        )
    mutation = metadata.get("mutation")
    if not isinstance(mutation, dict):
        raise EvidenceError("run metadata has no mutation object")
    counts, evidence_scopes = _validate_mutation_evidence(root, mutation)
    declared = counts["killed"]
    excluded = mutation["filters"]["filtered"]
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
    review_binding = None
    review_binding_path = None
    review_binding_evidence_root = None
    review_binding_summary = None
    review_evidence_root = None
    review_manifest_path = None
    evidence_bundle_summary = None
    runtime_verification = None
    if bundle is not None:
        candidate_identity = bundle["candidate_identity"]
        review_summary = bundle["review_summary"]
        review_binding = bundle["review_binding"]
        review_evidence_root = trusted_bundle_root
        review_manifest_path = _evidence_root_file(
            bundle_root,
            bundle["review_manifest_reference"]["path"],
            "bundle review manifest",
        )
        if review_binding is not None:
            reference = bundle["review_binding_reference"]
            if not isinstance(trusted_bundle_root, TrustedEvidenceRoot):
                raise EvidenceError(
                    "bundle review binding has no trusted root"
                )
            review_binding_path = _evidence_root_file(
                bundle_root,
                reference["path"],
                "bundle review binding",
            )
            review_binding_evidence_root = trusted_bundle_root
            review_binding_summary = {
                **reference,
                "scope": review_binding["scope"],
                "binding_result": review_binding["binding_result"],
                "evidence_status": review_binding["evidence_status"],
                "unverified_scope": list(
                    review_binding["unverified_scope"]
                ),
                "reviewed_head": review_binding["reviewed_head"],
                "target_head": review_binding["target_head"],
                "acceptance_supported": False,
                "merge_ready": False,
            }
        acceptance_policy = resolve_acceptance_policy(
            acceptance_policy,
            bundle["acceptance_policy"],
        )
        runtime_verification = bundle["runtime_verification"]
        previous_record = bundle["previous_summary"]
        previous_summary = _evidence_root_file(
            bundle_root,
            previous_record["path"],
            "bundle previous summary",
        )
        _validate_hash(
            previous_summary,
            previous_record["sha256"],
            "bundle previous summary",
            root=bundle_root,
        )
        evidence_bundle_summary = {
            "bundle_id": bundle["bundle_id"],
            "path": _evidence_reference(
                bundle_path,
                root,
                Path(bundle["bundle_root"]),
            ),
            "sha256": bundle["manifest_sha256"],
            "schema_version": bundle["schema_version"],
            "generator_version": bundle["generator_version"],
            "created_at": bundle["created_at"],
            "candidate_mode": candidate_identity["candidate_mode"],
            "target_head": candidate_identity["target_head"],
            "code_fingerprint": candidate_identity["code_fingerprint"],
        }
    else:
        candidate_record = metadata.get("candidate_identity")
        if isinstance(candidate_record, dict):
            candidate_path = _evidence_file(
                root,
                candidate_record.get("path"),
                "candidate identity",
            )
            _validate_hash(
                candidate_path,
                str(candidate_record.get("sha256")),
                "candidate identity",
                root=root,
            )
            candidate_identity = validate_candidate_snapshot(
                root,
                candidate_path,
            )
        else:
            candidate_identity = build_candidate_snapshot(root)

        binding_record = metadata.get("review_binding")
        if isinstance(binding_record, dict):
            binding_path = _evidence_file(
                root,
                binding_record.get("path"),
                "review binding",
            )
            _validate_hash(
                binding_path,
                str(binding_record.get("sha256")),
                "review binding",
                root=root,
            )
            review_binding = validate_review_binding(root, binding_path)
            review_binding_path = binding_path
            review_binding_evidence_root = trusted_repository_root
            review_summary = _review_summary_from_binding(review_binding)
            review_binding_summary = {
                "path": str(binding_record["path"]),
                "sha256": sha256_file(binding_path, root=root),
                "scope": review_binding["scope"],
                "binding_result": review_binding["binding_result"],
                "evidence_status": review_binding["evidence_status"],
                "unverified_scope": list(
                    review_binding["unverified_scope"]
                ),
                "reviewed_head": review_binding["reviewed_head"],
                "target_head": review_binding["target_head"],
                "acceptance_supported": False,
                "merge_ready": False,
            }
        else:
            review = metadata.get("review")
            if not isinstance(review, dict):
                raise EvidenceError("run metadata has no review evidence")
            review_path = _evidence_file(root, review.get("path"), "review")
            _validate_hash(
                review_path,
                str(review.get("sha256")),
                "review",
                root=root,
            )
            review_summary = validate_review_manifest(
                root,
                review_path,
                expected_code_fingerprint=current_code,
            )
            review_evidence_root = trusted_repository_root
            review_manifest_path = review_path
        if acceptance_policy not in ACCEPTANCE_POLICIES:
            raise EvidenceError("unsupported acceptance policy")
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
            root=root,
        )

    if candidate_identity.get("target_head") != current_git["head"]:
        raise EvidenceError("candidate target HEAD does not match current HEAD")
    if candidate_identity.get("code_fingerprint") != current_code:
        raise EvidenceError("candidate fingerprint does not match current code")
    frozen_summary = {}
    for relative, expected in FROZEN_EVIDENCE.items():
        path = root / relative
        try:
            _validate_hash(
                path,
                expected,
                f"frozen evidence {relative}",
                root=root,
            )
        except EvidenceError as exc:
            raise EvidenceError(f"frozen evidence mismatch: {relative}") from exc
        observed = sha256_file(path, root=root)
        if bundle is None:
            frozen = metadata.get("frozen_evidence")
            if not isinstance(frozen, dict):
                raise EvidenceError("run metadata has no frozen evidence")
            evidence = frozen.get(relative)
            if not isinstance(evidence, dict):
                raise EvidenceError(
                    f"frozen evidence missing from run: {relative}"
                )
            if evidence.get("observed_sha256") != observed:
                raise EvidenceError(
                    f"frozen evidence run record mismatch: {relative}"
                )
        frozen_summary[relative] = {
            "expected_sha256": expected,
            "observed_sha256": observed,
        }
    previous_evidence_root = bundle_root if bundle is not None else root
    previous_sha = sha256_file(
        previous_summary,
        root=previous_evidence_root,
    )
    if trusted_previous_summary_sha256 is not None and (
        trusted_previous_summary_sha256 != previous_sha
    ):
        raise EvidenceError(
            "previous summary does not match the caller's trusted digest"
        )
    previous = read_json(
        previous_summary,
        root=previous_evidence_root,
    )
    trusted_previous_candidate_snapshot = None
    trusted_previous_candidate_snapshot_sha256 = None
    if trusted_previous_candidate_snapshot_path is not None:
        (
            trusted_previous_candidate_snapshot,
            trusted_previous_candidate_snapshot_sha256,
        ) = _load_trusted_candidate_snapshot(
            trusted_previous_candidate_snapshot_path,
            root,
            "trusted previous candidate snapshot",
        )
    incidents, history = _validate_previous_summary(
        previous,
        "previous summary",
        trusted_repository=str(root),
        trusted_previous_candidate_snapshot=(
            trusted_previous_candidate_snapshot
        ),
        require_trusted_identity=(
            trusted_previous_summary_sha256 is not None
        ),
    )
    if previous_sha in {
        item["previous_sha256"] for item in history
    }:
        raise EvidenceError(
            "previous summary digest is already present in evidence history"
        )
    history = [
        *history,
        {
            "previous_sha256": previous_sha,
            "replacement_reason": replacement_reason,
        },
    ]
    acceptance_evidence_binding = {
        "tests": test_summary,
        "mutations": {
            "declared": int(mutation["filters"]["declared"]),
            "selected": int(declared),
            "filtered": int(excluded),
            "killed": int(counts["killed"]),
            "cases_sha256": sha256_file(cases_path, root=root),
            "report_sha256": sha256_file(report_path, root=root),
        },
        "frozen_evidence": frozen_summary,
        "previous_summary_sha256": previous_sha,
        "review_binding_sha256": (
            sha256_bytes(canonical_json(review_binding).encode("utf-8"))
            if review_binding is not None
            else None
        ),
        "runtime_verification": runtime_verification,
    }
    with _acceptance_prerequisite_transaction(
        candidate_identity=candidate_identity,
        review_summary=review_summary,
        evidence_binding=acceptance_evidence_binding,
        tests_verified=True,
        frozen_verified=True,
        previous_summary_available=True,
        previous_summary_trusted=(
            trusted_previous_summary_sha256 is not None
            and trusted_previous_candidate_snapshot is not None
        ),
        runtime_verification=runtime_verification,
    ) as proof:
        acceptance_gate = _evaluate_acceptance_gate_core(
            candidate_identity=candidate_identity,
            trusted_repository_root=trusted_repository_root,
            trusted_review_evidence_root=review_evidence_root,
            trusted_review_manifest_path=review_manifest_path,
            trusted_binding_evidence_root=review_binding_evidence_root,
            trusted_review_binding_path=review_binding_path,
            review_summary=review_summary,
            review_binding=review_binding,
            acceptance_policy=acceptance_policy,
            acceptance_evidence_binding=acceptance_evidence_binding,
            acceptance_prerequisites=proof,
        )
    combined_blocking_reasons = list(dict.fromkeys(
        [
            *review_summary.get("blocking_reasons", []),
            *acceptance_gate["blocking_reasons"],
        ]
    ))
    review_resolution = acceptance_gate[
        "current_target_review_resolution"
    ]
    special_candidate_acceptance = (
        "INDEPENDENT_REVIEW_CLEAR"
        if review_resolution == "DIRECT_CLEAR"
        else "CONTENT_EQUIVALENT_REVIEW_CLEAR"
        if review_resolution == "CONTENT_EQUIVALENT_CLEAR"
        else review_summary.get("candidate_acceptance")
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "task-control-center-v1",
        "validation_input": {
            "run_metadata": evidence_path(metadata_path, root),
            "run_metadata_sha256": metadata_sha256,
            "evidence_bundle_path": (
                str(_canonical_absolute_path(selected_bundle_path))
                if selected_bundle_path is not None
                else None
            ),
            "replacement_reason_sha256": sha256_bytes(
                replacement_reason.encode("utf-8")
            ),
            "trusted_previous_summary_sha256": (
                trusted_previous_summary_sha256
            ),
            "trusted_previous_candidate_snapshot_path": (
                str(
                    _canonical_absolute_path(
                        trusted_previous_candidate_snapshot_path
                    )
                )
                if trusted_previous_candidate_snapshot_path is not None
                else None
            ),
            "trusted_previous_candidate_snapshot_sha256": (
                trusted_previous_candidate_snapshot_sha256
            ),
            "generator_version": EVIDENCE_TOOL_VERSION,
        },
        "status": acceptance_gate["evidence_integrity_status"],
        "validation_mode": "offline_noncontainer",
        "candidate_identity": candidate_identity,
        "evidence_bundle": evidence_bundle_summary,
        "review_binding": review_binding_summary,
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
            "cases_sha256": sha256_file(cases_path, root=root),
            "report": summary_evidence_path(report_path, root, run_dir),
            "report_sha256": sha256_file(report_path, root=root),
        },
        "special_review": {
            **review_summary,
            "candidate_acceptance": special_candidate_acceptance,
            "review_clear": (
                acceptance_gate["review_gate"]
                in {"CLEAR", "CONTENT_EQUIVALENT_CLEAR"}
                and not review_summary.get("uncovered_scope")
            ),
            "merge_ready": acceptance_gate["merge_ready"],
            "blocking_reasons": list(
                review_summary.get("blocking_reasons", [])
            ),
        },
        "review_status": {
            "independent_code_review":
                review_summary["independent_code_review"],
            "independent_architecture_review":
                review_summary["independent_architecture_review"],
            "merge_ready": acceptance_gate["merge_ready"],
            "blocking_reasons": combined_blocking_reasons,
            "review_gate": acceptance_gate["review_gate"],
        },
        "acceptance_gate": acceptance_gate,
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
        "candidate_acceptance": acceptance_gate["acceptance_status"],
    }


def _evidence_reference(
    path: Path,
    root: Path,
    evidence_root: Path | None = None,
) -> str:
    resolved = _canonical_absolute_path(path)
    try:
        return resolved.relative_to(_canonical_absolute_path(root)).as_posix()
    except ValueError:
        if evidence_root is None:
            return str(resolved)
        return (
            "bundle/"
            + resolved.relative_to(
                _canonical_absolute_path(evidence_root)
            ).as_posix()
        )


def evidence_path(path: Path, root: Path) -> str:
    canonical_root = _canonical_absolute_path(root)
    canonical_path = _canonical_absolute_path(path)
    try:
        return canonical_path.relative_to(canonical_root).as_posix()
    except ValueError:
        return str(canonical_path)


def summary_evidence_path(path: Path, root: Path, run_dir: Path) -> str:
    try:
        relative = _canonical_absolute_path(path).relative_to(
            _canonical_absolute_path(run_dir)
        )
    except ValueError:
        return evidence_path(path, root)
    return f"<run>/{relative.as_posix()}"


def _write_acceptance_status(
    root: Path,
    summary_path: Path,
    *,
    status: str,
    reason: str,
    summary: Mapping[str, Any] | None = None,
    repository_root: TrustedEvidenceRoot | None = None,
) -> Path:
    status_path = summary_path.parent / "acceptance-status.json"
    state = git_state(root)
    fingerprint = None
    try:
        fingerprint = code_fingerprint(root)
    except Exception:
        pass
    candidate = summary.get("candidate_identity") if summary else None
    summary_sha256 = None
    if summary is not None:
        summary_bytes = _read_regular_file(
            summary_path,
            "acceptance authority summary",
            root=root,
        )
        summary_sha256 = sha256_bytes(summary_bytes)
    payload = {
        "schema_version": "task-control-center-acceptance-status-v2",
        "generator_version": EVIDENCE_TOOL_VERSION,
        "authoritative": False,
        "authority": summary_path.name,
        "repository": str(root),
        "head": state["head"],
        "code_fingerprint": fingerprint,
        "summary_path": summary_path.name,
        "summary_sha256": summary_sha256,
        "candidate_identity": (
            dict(candidate) if isinstance(candidate, Mapping) else None
        ),
        "state": status,
        "evidence_integrity_status": (
            summary.get("status")
            if summary is not None and status == "COMPLETED"
            else status
        ),
        "candidate_acceptance": (
            summary.get("candidate_acceptance")
            if summary is not None
            else BLOCKED
        ),
        "reason": reason,
    }
    if repository_root is not None:
        repository_root.remove_file_if_exists(
            status_path,
            "acceptance status",
        )
        repository_root.write_bytes_atomic(
            status_path,
            canonical_json(payload).encode("utf-8"),
            "acceptance status",
        )
    else:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            status_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise EvidenceError(
                f"previous acceptance status could not be invalidated: {status_path}"
            ) from exc
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=status_path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(canonical_json(payload))
        try:
            os.replace(temporary, status_path)
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise EvidenceError(
                f"acceptance status could not be committed atomically: {status_path}"
            ) from exc
    return status_path


def validate_acceptance_status(
    root: Path,
    status_path: Path,
    *,
    expected_replacement_reason: str | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    with _trusted_repository_scope(root):
        return _validate_acceptance_status_impl(
            root,
            status_path,
            expected_replacement_reason=expected_replacement_reason,
        )


def _validate_acceptance_status_impl(
    root: Path,
    status_path: Path,
    *,
    expected_replacement_reason: str | None = None,
) -> dict[str, Any]:
    """Validate the non-authoritative status marker without granting acceptance."""
    root = _canonical_absolute_path(root)
    payload = read_json(status_path, root=root)
    if payload.get("schema_version") != (
        "task-control-center-acceptance-status-v2"
    ):
        raise EvidenceError("acceptance status is not the current schema")
    _reject_unknown_fields(
        payload,
        {
            "schema_version",
            "generator_version",
            "authoritative",
            "authority",
            "repository",
            "head",
            "code_fingerprint",
            "summary_path",
            "summary_sha256",
            "candidate_identity",
            "state",
            "evidence_integrity_status",
            "candidate_acceptance",
            "reason",
        },
        "acceptance status",
    )
    if payload.get("authoritative") is not False:
        raise EvidenceError("acceptance status must be non-authoritative")
    state = payload.get("state")
    if state not in {"IN_PROGRESS", "COMPLETED", BLOCKED}:
        raise EvidenceError("acceptance status state is invalid")
    current = git_state(root)
    if payload.get("repository") != str(root):
        raise EvidenceError("acceptance status repository mismatch")
    if payload.get("head") != current["head"]:
        raise EvidenceError("acceptance status HEAD mismatch")
    if payload.get("code_fingerprint") != code_fingerprint(root):
        raise EvidenceError("acceptance status fingerprint mismatch")
    summary_name = payload.get("summary_path")
    if not isinstance(summary_name, str) or not summary_name:
        raise EvidenceError("acceptance status has no summary authority")
    if payload.get("authority") != summary_name:
        raise EvidenceError("acceptance status authority mismatch")
    if Path(summary_name).name != summary_name:
        raise EvidenceError("acceptance status summary path is invalid")
    summary_path = status_path.parent / summary_name
    if state == "COMPLETED":
        summary = validate_summary_is_current(
            root,
            summary_path,
            expected_replacement_reason=expected_replacement_reason,
        )
        summary_sha = sha256_file(summary_path, root=root)
        if payload.get("summary_sha256") != summary_sha:
            raise EvidenceError("acceptance status summary hash mismatch")
        identity = payload.get("candidate_identity")
        if not isinstance(identity, Mapping) or identity != (
            summary.get("candidate_identity")
        ):
            raise EvidenceError("acceptance status candidate mismatch")
        if payload.get("evidence_integrity_status") != summary.get("status"):
            raise EvidenceError(
                "acceptance status evidence integrity mismatch"
            )
        if payload.get("candidate_acceptance") != summary.get(
            "candidate_acceptance"
        ):
            raise EvidenceError(
                "acceptance status candidate acceptance mismatch"
            )
    else:
        if payload.get("summary_sha256") is not None:
            raise EvidenceError(
                "incomplete acceptance status must not reference a summary hash"
            )
        if payload.get("candidate_identity") is not None:
            raise EvidenceError(
                "incomplete acceptance status must not carry a candidate identity"
            )
        if payload.get("candidate_acceptance") != BLOCKED:
            raise EvidenceError(
                "incomplete acceptance status cannot claim acceptance"
            )
        if payload.get("evidence_integrity_status") != state:
            raise EvidenceError(
                "incomplete acceptance status evidence state mismatch"
            )
    return payload


def validate_summary_is_current(
    root: Path,
    summary_path: Path,
    *,
    metadata_path: Path | None = None,
    expected_replacement_reason: str | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    with _trusted_repository_scope(root):
        return _validate_summary_is_current_impl(
            root,
            summary_path,
            metadata_path=metadata_path,
            expected_replacement_reason=expected_replacement_reason,
        )


def _validate_summary_is_current_impl(
    root: Path,
    summary_path: Path,
    *,
    metadata_path: Path | None = None,
    expected_replacement_reason: str | None = None,
) -> dict[str, Any]:
    root = _canonical_absolute_path(root)
    summary = read_json(summary_path, root=root)
    return _validate_summary_payload_current(
        root,
        summary,
        metadata_path=metadata_path,
        expected_replacement_reason=expected_replacement_reason,
    )


def _validate_summary_payload_current(
    root: Path,
    summary: Mapping[str, Any],
    *,
    metadata_path: Path | None = None,
    expected_replacement_reason: str | None = None,
) -> dict[str, Any]:
    if not isinstance(summary, Mapping):
        raise EvidenceError("summary must be an object")
    if summary.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("summary schema is not current")
    identity = summary.get("candidate_identity")
    if not isinstance(identity, Mapping):
        raise EvidenceError("summary has no candidate identity")
    current = build_candidate_snapshot(
        root,
        candidate_mode=str(identity.get("candidate_mode")),
        base_head=identity.get("base_head"),
        target_head=identity.get("target_head"),
        reviewed_head=identity.get("reviewed_head"),
    )
    for field in (
        "target_head",
        "code_fingerprint",
        "file_count",
        "file_manifest_sha256",
    ):
        if identity.get(field) != current.get(field):
            raise EvidenceError(
                f"summary candidate identity mismatch: {field}"
            )
    validation_input = summary.get("validation_input")
    if not isinstance(validation_input, Mapping):
        raise EvidenceError("summary has no validation input binding")
    expected_metadata_sha = validation_input.get("run_metadata_sha256")
    if not isinstance(expected_metadata_sha, str) or not re.fullmatch(
        r"[0-9a-f]{64}",
        expected_metadata_sha,
    ):
        raise EvidenceError("summary run metadata hash is invalid")
    if validation_input.get("generator_version") != EVIDENCE_TOOL_VERSION:
        raise EvidenceError("summary generator version is not current")
    bundle_reference = validation_input.get("evidence_bundle_path")
    selected_bundle_path = None
    if bundle_reference is not None:
        if not isinstance(bundle_reference, str) or not bundle_reference:
            raise EvidenceError("summary evidence bundle reference is invalid")
        selected_bundle_path = Path(bundle_reference)
    metadata_reference = validation_input.get("run_metadata")
    if not isinstance(metadata_reference, str):
        raise EvidenceError("summary has no run metadata reference")
    selected_metadata = _evidence_file(
        root,
        metadata_reference,
        "summary run metadata",
    )
    if metadata_path is not None:
        supplied = _canonical_absolute_path(metadata_path)
        if supplied != selected_metadata:
            raise EvidenceError("summary run metadata path mismatch")
        selected_metadata = supplied
    if sha256_file(selected_metadata, root=root) != expected_metadata_sha:
        raise EvidenceError("summary run metadata hash mismatch")
    gate = summary.get("acceptance_gate")
    if not isinstance(gate, Mapping):
        raise EvidenceError("summary has no acceptance gate")
    policy = gate.get("acceptance_policy")
    if policy not in ACCEPTANCE_POLICIES:
        raise EvidenceError("summary acceptance policy is invalid")
    superseded = summary.get("superseded_summary")
    recorded_replacement_reason = (
        superseded.get("replacement_reason")
        if isinstance(superseded, Mapping)
        else None
    )
    if (
        not isinstance(recorded_replacement_reason, str)
        or not recorded_replacement_reason
    ):
        raise EvidenceError("summary has no replacement reason")
    if (
        not isinstance(expected_replacement_reason, str)
        or not expected_replacement_reason
    ):
        raise EvidenceError(
            "independent replacement reason input is required"
        )
    if recorded_replacement_reason != expected_replacement_reason:
        raise EvidenceError("summary replacement reason mismatch")
    reason_sha = validation_input.get("replacement_reason_sha256")
    if reason_sha != sha256_bytes(expected_replacement_reason.encode("utf-8")):
        raise EvidenceError("summary replacement reason hash mismatch")
    trusted_previous_summary_sha256 = validation_input.get(
        "trusted_previous_summary_sha256"
    )
    if trusted_previous_summary_sha256 is not None:
        _require_sha256(
            trusted_previous_summary_sha256,
            "summary trusted previous summary SHA-256",
        )
    trusted_snapshot_path = validation_input.get(
        "trusted_previous_candidate_snapshot_path"
    )
    trusted_snapshot_sha256 = validation_input.get(
        "trusted_previous_candidate_snapshot_sha256"
    )
    if (trusted_snapshot_path is None) != (
        trusted_snapshot_sha256 is None
    ):
        raise EvidenceError(
            "summary trusted candidate snapshot binding is incomplete"
        )
    if trusted_snapshot_path is not None:
        if (
            not isinstance(trusted_snapshot_path, str)
            or not trusted_snapshot_path
        ):
            raise EvidenceError(
                "summary trusted candidate snapshot path is invalid"
            )
        _require_sha256(
            trusted_snapshot_sha256,
            "summary trusted candidate snapshot SHA-256",
        )
        _, observed_snapshot_sha256 = _load_trusted_candidate_snapshot(
            Path(trusted_snapshot_path),
            root,
            "summary trusted previous candidate snapshot",
        )
        if observed_snapshot_sha256 != trusted_snapshot_sha256:
            raise EvidenceError(
                "summary trusted candidate snapshot hash mismatch"
            )
    recomputed = build_summary(
        root,
        selected_metadata,
        replacement_reason=expected_replacement_reason,
        evidence_bundle_path=selected_bundle_path,
        acceptance_policy=policy,
        trusted_previous_summary_sha256=trusted_previous_summary_sha256,
        trusted_previous_candidate_snapshot_path=(
            Path(trusted_snapshot_path)
            if trusted_snapshot_path is not None
            else None
        ),
    )
    if canonical_json(recomputed) != canonical_json(summary):
        raise EvidenceError(
            "summary does not match recomputed current evidence"
        )
    return recomputed


def _write_summary_impl(
    root: Path,
    metadata_path: Path,
    output_path: Path,
    *,
    replacement_reason: str,
    evidence_bundle_path: Path | None = None,
    acceptance_policy: str = "target_head_review_required",
    trusted_previous_summary_sha256: str | None = None,
    trusted_previous_candidate_snapshot_path: Path | None = None,
    repository_root: TrustedEvidenceRoot | None = None,
) -> dict[str, Any]:
    _require_persistent_publication_ancestry("summary publication")
    root = _canonical_absolute_path(root)
    metadata_path = _canonical_absolute_path(metadata_path)
    output_path = _validate_output_path(
        root,
        output_path,
        directory=False,
    )
    metadata = read_json(metadata_path, root=root)
    previous_record = metadata.get("previous_summary")
    if not isinstance(previous_record, dict):
        raise EvidenceError("run metadata has no previous summary snapshot")
    previous_summary = _evidence_file(
        root,
        previous_record.get("path"),
        "previous summary",
    )
    protected = {
        metadata_path,
        previous_summary,
        root / "tests" / "mutation_cases.json",
        *(root / relative for relative in FROZEN_EVIDENCE),
    }
    bundle_record = metadata.get("evidence_bundle")
    if evidence_bundle_path is None and isinstance(bundle_record, dict):
        path_value = bundle_record.get("path")
        if isinstance(path_value, str) and path_value:
            evidence_bundle_path = Path(path_value)
    if evidence_bundle_path is not None and not evidence_bundle_path.is_absolute():
        evidence_bundle_path = root / evidence_bundle_path
    if evidence_bundle_path is not None:
        validated_bundle = validate_evidence_bundle(
            root,
            evidence_bundle_path,
        )
        bundle_root = Path(validated_bundle["bundle_root"])
        bundle_manifest_path = Path(
            validated_bundle["bundle_manifest_path"]
        )
        protected.add(bundle_manifest_path)
        for relative in validated_bundle["file_paths"]:
            protected.add(
                _evidence_root_file(
                    bundle_root,
                    relative,
                    "protected bundle evidence",
                )
            )
    review_record = metadata.get("review")
    if evidence_bundle_path is None and isinstance(review_record, dict):
        review_manifest = _evidence_file(
            root,
            review_record.get("path"),
            "review manifest",
        )
        protected.add(review_manifest)
        review_data = read_json(review_manifest, root=root)
        for key in ("code_review", "architecture_review"):
            lane = review_data.get(key)
            if not isinstance(lane, dict):
                continue
            for field in ("report", "result"):
                if isinstance(lane.get(field), str):
                    protected.add(_evidence_file(root, lane[field], key))
    binding_record = metadata.get("review_binding")
    if isinstance(binding_record, dict):
        protected.add(
            _evidence_file(root, binding_record.get("path"), "review binding")
        )
    candidate_record = metadata.get("candidate_identity")
    if isinstance(candidate_record, dict):
        protected.add(
            _evidence_file(
                root,
                candidate_record.get("path"),
                "candidate identity",
            )
        )
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
        evidence_bundle_path=evidence_bundle_path,
        acceptance_policy=acceptance_policy,
        trusted_previous_summary_sha256=(
            trusted_previous_summary_sha256
        ),
        trusted_previous_candidate_snapshot_path=(
            trusted_previous_candidate_snapshot_path
        ),
    )
    summary = _validate_summary_payload_current(
        root,
        summary,
        metadata_path=metadata_path,
        expected_replacement_reason=replacement_reason,
    )
    summary_bytes = canonical_json(summary).encode("utf-8")
    if repository_root is not None:
        repository_root.write_bytes_atomic(
            output_path,
            summary_bytes,
            "summary output",
        )
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=output_path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(summary_bytes.decode("utf-8"))
        os.replace(temporary, output_path)
    return summary


def write_summary(
    root: Path,
    metadata_path: Path,
    output_path: Path,
    *,
    replacement_reason: str,
    evidence_bundle_path: Path | None = None,
    acceptance_policy: str = "target_head_review_required",
    trusted_previous_summary_sha256: str | None = None,
    trusted_previous_candidate_snapshot_path: Path | None = None,
) -> None:
    _require_persistent_publication_ancestry("summary publication")
    if trusted_previous_summary_sha256 is not None:
        _require_sha256(
            trusted_previous_summary_sha256,
            "trusted previous summary SHA-256",
        )
    root = _canonical_absolute_path(root)
    output_path = _validate_output_path(
        root,
        output_path,
        directory=False,
    )
    with _trusted_repository_scope(root) as repository_root:
        selected_bundle_path = _selected_bundle_path(
            root,
            metadata_path,
            evidence_bundle_path,
        )
        if selected_bundle_path is None:
            _write_summary_with_lifecycle(
                root,
                metadata_path,
                output_path,
                replacement_reason=replacement_reason,
                evidence_bundle_path=None,
                acceptance_policy=acceptance_policy,
                trusted_previous_summary_sha256=(
                    trusted_previous_summary_sha256
                ),
                trusted_previous_candidate_snapshot_path=(
                    trusted_previous_candidate_snapshot_path
                ),
                repository_root=repository_root,
            )
            return
        bundle_root = _canonical_absolute_path(selected_bundle_path).parent
        with TrustedEvidenceRoot(
            bundle_root,
            "summary acceptance lifecycle root",
        ) as trusted_root:
            with trusted_root.pinned():
                _write_summary_with_lifecycle(
                    root,
                    metadata_path,
                    output_path,
                    replacement_reason=replacement_reason,
                    evidence_bundle_path=selected_bundle_path,
                    acceptance_policy=acceptance_policy,
                    trusted_previous_summary_sha256=(
                        trusted_previous_summary_sha256
                    ),
                    trusted_previous_candidate_snapshot_path=(
                        trusted_previous_candidate_snapshot_path
                    ),
                    repository_root=repository_root,
                )


def _write_summary_with_lifecycle(
    root: Path,
    metadata_path: Path,
    output_path: Path,
    *,
    replacement_reason: str,
    evidence_bundle_path: Path | None,
    acceptance_policy: str,
    trusted_previous_summary_sha256: str | None,
    trusted_previous_candidate_snapshot_path: Path | None,
    repository_root: TrustedEvidenceRoot,
) -> None:
    _write_acceptance_status(
        root,
        output_path,
        status="IN_PROGRESS",
        reason="summary validation started; acceptance is not yet authoritative",
        repository_root=repository_root,
    )
    try:
        summary = _write_summary_impl(
            root,
            metadata_path,
            output_path,
            replacement_reason=replacement_reason,
            evidence_bundle_path=evidence_bundle_path,
            acceptance_policy=acceptance_policy,
            trusted_previous_summary_sha256=(
                trusted_previous_summary_sha256
            ),
            trusted_previous_candidate_snapshot_path=(
                trusted_previous_candidate_snapshot_path
            ),
            repository_root=repository_root,
        )
    except Exception as exc:
        try:
            _write_acceptance_status(
                root,
                output_path,
                status=BLOCKED,
                reason=f"{type(exc).__name__}: {exc}",
                repository_root=repository_root,
            )
        except Exception:
            pass
        raise
    _write_acceptance_status(
        root,
        output_path,
        status="COMPLETED",
        reason="summary written and current candidate identity verified",
        summary=summary,
        repository_root=repository_root,
    )


def write_review_binding(
    root: Path,
    review_manifest_path: Path,
    output_path: Path,
    *,
    base_head: str,
    target_head: str,
    generated_at: str | None = None,
) -> None:
    _require_persistent_publication_ancestry(
        "review binding publication"
    )
    root = _canonical_absolute_path(root)
    output_path = _canonical_absolute_path(output_path)
    try:
        output_path.relative_to(_audit_output_root(root))
    except ValueError as exc:
        raise EvidenceError(
            "review binding output must stay under "
            "artifacts/audits/task-control-center-v1"
        ) from exc
    if (
        output_path.suffix != ".json"
        or not output_path.name.startswith("review-binding-")
    ):
        raise EvidenceError(
            "review binding output must use a review-binding-*.json name"
        )
    with _trusted_repository_scope(root) as repository_root:
        output_root = repository_root.ensure_directory(
            output_path.parent,
            "review binding output directory",
        )
        try:
            if output_path.name in output_root.directory_entries():
                raise EvidenceError(
                    f"review binding output already exists: {output_path}"
                )
            binding = build_review_binding(
                root,
                review_manifest_path,
                base_head=base_head,
                target_head=target_head,
                generated_at=generated_at,
            )
            output_root.write_bytes_atomic(
                output_path,
                canonical_json(binding).encode("utf-8"),
                "review binding",
            )
        finally:
            output_root.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--json-errors",
        action="store_true",
        help="emit structured EvidenceError diagnostics as JSON on stderr",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    run_parser.add_argument("--output-dir", type=Path, required=True)
    run_parser.add_argument("--review-manifest", type=Path, default=None)
    run_parser.add_argument("--evidence-bundle", type=Path, default=None)
    run_parser.add_argument("--review-binding", type=Path, default=None)
    run_parser.add_argument("--previous-summary", type=Path, default=None)
    run_parser.add_argument(
        "--acceptance-policy",
        choices=sorted(ACCEPTANCE_POLICIES),
        default="target_head_review_required",
    )
    tests_parser = subparsers.add_parser("_run-tests")
    tests_parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    tests_parser.add_argument("--result", type=Path, required=True)
    tests_parser.add_argument("--log", type=Path, required=True)
    tests_parser.add_argument("--cwd-root", action="store_true")
    discovery_parser = subparsers.add_parser("_discover-tests")
    discovery_parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    discovery_parser.add_argument("--cwd-root", action="store_true")
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    snapshot_parser.add_argument("--output", type=Path, required=True)
    snapshot_parser.add_argument(
        "--candidate-mode",
        choices=sorted(CANDIDATE_MODES),
        default="working_tree",
    )
    snapshot_parser.add_argument("--base-head", default=None)
    snapshot_parser.add_argument("--target-head", default=None)
    snapshot_parser.add_argument("--reviewed-head", default=None)
    binding_parser = subparsers.add_parser("review-binding")
    binding_parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    binding_parser.add_argument("--review-manifest", type=Path, required=True)
    binding_parser.add_argument("--base-head", required=True)
    binding_parser.add_argument("--target-head", required=True)
    binding_parser.add_argument("--generated-at", default=None)
    binding_parser.add_argument("--output", type=Path, required=True)
    summary_parser = subparsers.add_parser("summarize")
    summary_parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    summary_parser.add_argument("--run-metadata", type=Path, required=True)
    summary_parser.add_argument("--evidence-bundle", type=Path, default=None)
    summary_parser.add_argument(
        "--acceptance-policy",
        choices=sorted(ACCEPTANCE_POLICIES),
        default="target_head_review_required",
    )
    summary_parser.add_argument("--output", type=Path, required=True)
    summary_parser.add_argument("--replace-reason", required=True)
    summary_parser.add_argument(
        "--trusted-previous-sha256",
        default=None,
    )
    summary_parser.add_argument(
        "--trusted-previous-candidate-snapshot",
        type=Path,
        default=None,
    )
    bundle_parser = subparsers.add_parser("validate-bundle")
    bundle_parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    bundle_parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_evidence(
                args.root,
                args.output_dir,
                args.review_manifest,
                previous_summary_path=args.previous_summary,
                evidence_bundle_path=args.evidence_bundle,
                review_binding_path=args.review_binding,
                acceptance_policy=args.acceptance_policy,
            )
        if args.command == "_run-tests":
            return _run_tests(
                _canonical_absolute_path(args.root),
                result_path=_canonical_absolute_path(args.result),
                log_path=_canonical_absolute_path(args.log),
                use_cwd_root=args.cwd_root,
            )
        if args.command == "_discover-tests":
            discovery_root = (
                _canonical_absolute_path(Path.cwd())
                if args.cwd_root
                else _canonical_absolute_path(args.root)
            )
            with _trusted_repository_scope(discovery_root):
                discovery = _test_discovery_manifest(discovery_root)
            print(canonical_json(discovery))
            return 0
        if args.command == "snapshot":
            write_candidate_snapshot(
                _canonical_absolute_path(args.root),
                _canonical_absolute_path(args.output),
                candidate_mode=args.candidate_mode,
                base_head=args.base_head,
                target_head=args.target_head,
                reviewed_head=args.reviewed_head,
            )
            print(_canonical_absolute_path(args.output))
            return 0
        if args.command == "review-binding":
            write_review_binding(
                _canonical_absolute_path(args.root),
                _canonical_absolute_path(args.review_manifest),
                _canonical_absolute_path(args.output),
                base_head=args.base_head,
                target_head=args.target_head,
                generated_at=args.generated_at,
            )
            print(_canonical_absolute_path(args.output))
            return 0
        if args.command == "validate-bundle":
            validated = validate_evidence_bundle(
                _canonical_absolute_path(args.root),
                args.bundle,
            )
            print(
                json.dumps(
                    {
                        "bundle_id": validated["bundle_id"],
                        "manifest_sha256": validated["manifest_sha256"],
                        "acceptance_policy": validated["acceptance_policy"],
                        "runtime_verification":
                            validated["runtime_verification"],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        write_summary(
            args.root,
            args.run_metadata,
            args.output,
            replacement_reason=args.replace_reason,
            evidence_bundle_path=args.evidence_bundle,
            acceptance_policy=args.acceptance_policy,
            trusted_previous_summary_sha256=(
                args.trusted_previous_sha256
            ),
            trusted_previous_candidate_snapshot_path=(
                args.trusted_previous_candidate_snapshot
            ),
        )
        print(_canonical_absolute_path(args.output))
        return 0
    except EvidenceError as exc:
        if args.json_errors:
            diagnostic_errors: list[str] = []
            raw_residual_paths = _exception_attribute(
                exc,
                "residual_paths",
                [],
                attribute_errors=diagnostic_errors,
            )
            try:
                residual_path_values = list(raw_residual_paths or [])
            except BaseException as conversion_error:
                residual_path_values = []
                residual_unknown = True
                diagnostic_errors.append(
                    "residual_paths conversion: "
                    f"{type(conversion_error).__name__}: "
                    f"{_safe_diagnostic_text(conversion_error)}"
                )
            else:
                raw_residual_unknown = _exception_attribute(
                    exc,
                    "residual_unknown",
                    False,
                    attribute_errors=diagnostic_errors,
                )
                try:
                    residual_unknown = bool(raw_residual_unknown)
                except BaseException as conversion_error:
                    residual_unknown = True
                    diagnostic_errors.append(
                        "residual_unknown conversion: "
                        f"{type(conversion_error).__name__}: "
                        f"{_safe_diagnostic_text(conversion_error)}"
                    )
            if any(
                error.startswith("residual_paths:")
                for error in diagnostic_errors
            ):
                residual_unknown = True
            residual_paths = sorted(
                {
                    _safe_diagnostic_text(path)
                    for path in residual_path_values
                }
            )
            close_diagnostic_values = _exception_attribute(
                exc,
                "close_diagnostics",
                [],
                attribute_errors=diagnostic_errors,
            )
            try:
                close_diagnostics = list(
                    close_diagnostic_values or []
                )
            except BaseException as conversion_error:
                close_diagnostics = []
                residual_unknown = True
                diagnostic_errors.append(
                    "close_diagnostics conversion: "
                    f"{type(conversion_error).__name__}: "
                    f"{_safe_diagnostic_text(conversion_error)}"
                )
            note_values = _exception_attribute(
                exc,
                "__notes__",
                [],
                attribute_errors=diagnostic_errors,
            )
            try:
                notes = list(note_values or [])
            except BaseException as conversion_error:
                notes = []
                diagnostic_errors.append(
                    "notes conversion: "
                    f"{type(conversion_error).__name__}: "
                    f"{_safe_diagnostic_text(conversion_error)}"
                )
            raw_fd_close_states = _exception_attribute(
                exc,
                "fd_close_states",
                {},
                attribute_errors=diagnostic_errors,
            )
            try:
                fd_close_states = {
                    str(_safe_diagnostic_text(file_fd)):
                        _safe_diagnostic_text(state)
                    for file_fd, state
                    in dict(raw_fd_close_states or {}).items()
                }
            except BaseException as conversion_error:
                fd_close_states = {}
                diagnostic_errors.append(
                    "fd_close_states conversion: "
                    f"{type(conversion_error).__name__}: "
                    f"{_safe_diagnostic_text(conversion_error)}"
                )
            raw_unresolved_fds = _exception_attribute(
                exc,
                "unresolved_fds",
                {},
                attribute_errors=diagnostic_errors,
            )
            try:
                unresolved_fds = {
                    str(_safe_diagnostic_text(file_fd)):
                        _safe_diagnostic_text(state)
                    for file_fd, state
                    in dict(raw_unresolved_fds or {}).items()
                }
            except BaseException as conversion_error:
                unresolved_fds = {}
                diagnostic_errors.append(
                    "unresolved_fds conversion: "
                    f"{type(conversion_error).__name__}: "
                    f"{_safe_diagnostic_text(conversion_error)}"
                )
            raw_signal_mask_state = _exception_attribute(
                exc,
                "signal_mask_state",
                _MISSING_DIAGNOSTIC,
                attribute_errors=diagnostic_errors,
            )
            if raw_signal_mask_state is _MISSING_DIAGNOSTIC:
                signal_mask_state = (
                    "UNKNOWN"
                    if any(
                        item.startswith("signal_mask_state:")
                        for item in diagnostic_errors
                    )
                    else "NOT_APPLICABLE"
                )
            elif (
                type(raw_signal_mask_state) is str
                and raw_signal_mask_state in (
                    "NOT_APPLICABLE",
                    "RESTORED",
                    "BLOCKED",
                    "UNKNOWN",
                )
            ):
                signal_mask_state = raw_signal_mask_state
            else:
                signal_mask_state = "UNKNOWN"
                diagnostic_errors.append(
                    "signal_mask_state invalid value: "
                    f"{_safe_diagnostic_text(raw_signal_mask_state)}"
                )
            raw_secondary_exceptions = _exception_attribute(
                exc,
                "secondary_exceptions",
                [],
                attribute_errors=diagnostic_errors,
            )
            try:
                secondary_exceptions = [
                    {
                        "type": _safe_diagnostic_text(
                            item.get("type", "UNKNOWN")
                        ),
                        "message": _safe_diagnostic_text(
                            item.get("message", "")
                        ),
                        "phase": _safe_diagnostic_text(
                            item.get("phase", "UNKNOWN")
                        ),
                    }
                    for item in list(raw_secondary_exceptions or [])
                    if isinstance(item, Mapping)
                ]
            except BaseException as conversion_error:
                secondary_exceptions = []
                diagnostic_errors.append(
                    "secondary_exceptions conversion: "
                    f"{type(conversion_error).__name__}: "
                    f"{_safe_diagnostic_text(conversion_error)}"
                )
            primary_exception = _diagnostic_exception_reference(
                exc,
                "__cause__",
                diagnostic_errors,
            )
            context_exception = _diagnostic_exception_reference(
                exc,
                "__context__",
                diagnostic_errors,
            )
            payload = {
                "schema_version": "task-control-center-error-v1",
                "error_type": _safe_diagnostic_text(type(exc).__name__),
                "message": _safe_diagnostic_text(exc),
                "transaction_state": _transaction_state_from_error(
                    exc,
                    diagnostic_errors,
                ),
                "durability_state": _durability_state_from_error(
                    exc,
                    diagnostic_errors,
                ),
                "target_state": _target_state_from_error(
                    exc,
                    diagnostic_errors,
                ),
                "residual": {
                    "status": (
                        "UNKNOWN"
                        if residual_unknown
                        else (
                            "PRESENT"
                            if residual_paths
                            else "ABSENT"
                        )
                    ),
                    "paths": residual_paths,
                },
                "residual_unknown": residual_unknown,
                "close_diagnostics": [
                    _safe_diagnostic_text(item)
                    for item in close_diagnostics
                ],
                "fd_close_states": fd_close_states,
                "unresolved_fds": unresolved_fds,
                "signal_mask_state": signal_mask_state,
                "secondary_exceptions": secondary_exceptions,
                "diagnostic_errors": diagnostic_errors,
                "primary_exception": primary_exception,
                "context_exception": context_exception,
                "notes": [
                    _safe_diagnostic_text(note)
                    for note in notes
                ],
            }
            try:
                rendered = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if type(rendered) is not str:
                    raise TypeError(
                        "json.dumps returned a non-string value"
                    )
            except BaseException as serialization_error:
                rendered = _fallback_error_json(
                    exc,
                    serialization_error,
                )
            _write_stderr_text(rendered + "\n")
        else:
            _write_stderr_text(
                "evidence error: "
                f"{_safe_diagnostic_text(exc)}\n"
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
