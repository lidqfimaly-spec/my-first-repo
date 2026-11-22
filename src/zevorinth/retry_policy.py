"""Bounded manual retry policy for idempotent tools.

Design (per PRODUCT_CHARTER and architecture.md):
- Only genuinely side-effect-free tools are retryable.
- Only transient error codes are retryable.
- Bounded attempts: max 3 (configurable).
- Retry creates a new task, never reuses a partial result.
- Non-idempotent tools (write/edit/modify/test-run) are never retryable.
- Retry is user-triggered through the control surface; there is no
  automatic retry scheduler in the execution loop.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .planner import (
    CONTROLLED_SHELL,
    INSPECT_WORKSPACE,
    READ_TEXT_ARTIFACT,
    RESEARCH_REPORT,
    WRITE_TEXT_ARTIFACT,
    MODIFY_TEXT_ARTIFACT,
    EDIT_TEXT_FILE,
)
from .local_test_runner import RUN_TESTS_TOOL


class TaskRetryError(RuntimeError):
    """Raised when a retry request is rejected by the retry contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


MAX_RETRY_ATTEMPTS = 3
DEFAULT_MAX_ATTEMPTS = 1

#: Tools that are safe to replay because the implementation performs no
#: workspace or filesystem mutation, verified by reading each tool body:
#: - controlled_shell: whitelist of 21 read-only commands, no write flags
#: - inspect_workspace: read-only bounded directory/file inspection
#: - read_text_artifact: read-only artifact read
#: - research_report: builds report evidence from supplied sources; the
#:   report body is returned in the tool result and is not written to the
#:   workspace (writing is a separate write_text_artifact step).
IDEMPOTENT_TOOLS = frozenset(
    {
        CONTROLLED_SHELL,
        INSPECT_WORKSPACE,
        READ_TEXT_ARTIFACT,
        RESEARCH_REPORT,
    }
)

#: Tools that are NOT safe to retry because a partial execution may have
#: left side effects.
#: run_tests is listed here because executing a test suite runs workspace
#: code, which can create or modify files and make network calls; the
#: nominal expectation "tests are read-only" is not an enforceable
#: guarantee, so side-effect analysis forbids replaying it.
NON_IDEMPOTENT_TOOLS = frozenset(
    {
        WRITE_TEXT_ARTIFACT,
        MODIFY_TEXT_ARTIFACT,
        EDIT_TEXT_FILE,
        RUN_TESTS_TOOL,
    }
)

#: Error codes that are transient and may succeed on retry.
RETRYABLE_ERROR_CODES = frozenset(
    {
        "timeout",
        "shell_failed",
        "timed_out",
        "transient_error",
        "lease_expired",
        "claim_lost",
    }
)

#: Error codes that are permanent and will never succeed on retry.
NON_RETRYABLE_ERROR_CODES = frozenset(
    {
        "command_not_whitelisted",
        "forbidden_flag",
        "sensitive_path_rejected",
        "absolute_path_outside_workspace",
        "path_traversal_outside_workspace",
        "invalid_command",
        "invalid_arguments",
        "invalid_message_content",
        "invalid_parameters",
        "invalid_content",
        "empty_content",
        "content_too_large",
        "unsupported_operation",
        "unsupported_tool",
        "unbounded_plan",
        "artifact_exists",
        "planning_failed",
        "planner_internal_error",
        "verification_failed",
        "shell_verification_failed",
        "test_isolation_unverified",
        "blocked_isolation",
        "blocked_test_unknown",
        # Environment and authorization conditions: retrying cannot fix them,
        # and a missing grant must never be papered over by a retry.
        "container_backend_unavailable",
        "host_authorization_required",
        "host_authorization_invalid",
        "host_authorization_expired",
        "host_authorization_scope_mismatch",
        "host_authorization_already_used",
        "host_authorization_revoked",
        "host_authorization_storage_failed",
        "host_authorization_audit_failed",
        # A process that could not be created is a local capability problem,
        # not a transient one, and retrying would spend a fresh grant on the
        # same broken spawn.
        "test_spawn_failed",
        "workspace_unreadable",
        "confirmation_required",
        "control_disabled",
        "invalid_control_token",
        "invalid_origin",
        "invalid_host",
    }
)

#: Terminal task states that cannot be retried.
NON_RETRYABLE_STATUSES = frozenset(
    {
        "succeeded",
        "cancelled",
        "simulated",
    }
)

#: Task states that may be eligible for retry.
RETRYABLE_STATUSES = frozenset(
    {
        "failed",
        "interrupted",
        "planning_failed",
    }
)


def is_idempotent(tool_name: str) -> bool:
    """Return True if the tool is safe to retry (no side effects)."""
    return tool_name in IDEMPOTENT_TOOLS


def is_retryable_error(error_code: str | None) -> bool:
    """Return True if the error code is transient and may succeed on retry."""
    if error_code is None:
        return False
    if error_code in NON_RETRYABLE_ERROR_CODES:
        return False
    if error_code in RETRYABLE_ERROR_CODES:
        return True
    # Unknown error codes are NOT retryable by default (fail safe).
    return False


def is_retryable_status(status: str) -> bool:
    """Return True if the task status is eligible for retry."""
    return status in RETRYABLE_STATUSES


def should_retry(
    *,
    tool_name: str | None,
    error_code: str | None,
    task_status: str,
    attempt_count: int,
    max_attempts: int = MAX_RETRY_ATTEMPTS,
) -> bool:
    """Determine if a failed task should be retried.

    All conditions must be true:
    1. The task status is retryable (failed/interrupted/planning_failed).
    2. The tool is idempotent (no side effects).
    3. The error code is transient (retryable).
    4. The attempt count has not exceeded the maximum.
    """
    if not is_retryable_status(task_status):
        return False
    if tool_name is None or not is_idempotent(tool_name):
        return False
    if not is_retryable_error(error_code):
        return False
    if attempt_count >= max_attempts:
        return False
    return True


def get_retry_decision(
    *,
    tool_name: str | None,
    error_code: str | None,
    task_status: str,
    attempt_count: int,
    max_attempts: int = MAX_RETRY_ATTEMPTS,
) -> dict[str, object]:
    """Return a detailed retry decision with reasoning.

    Always returns a dict with:
    - retry: bool
    - reason: str (human-readable explanation)
    - tool_name, error_code, status, attempt_count, max_attempts
    """
    reasons: list[str] = []
    retry = True

    if not is_retryable_status(task_status):
        retry = False
        reasons.append(
            f"status {task_status!r} is not retryable "
            f"(must be one of {sorted(RETRYABLE_STATUSES)})"
        )
    if tool_name is None:
        retry = False
        reasons.append("tool_name is unknown")
    elif not is_idempotent(tool_name):
        retry = False
        reasons.append(
            f"tool {tool_name!r} is not idempotent "
            f"(not in {sorted(IDEMPOTENT_TOOLS)})"
        )
    if not is_retryable_error(error_code):
        retry = False
        if error_code is None:
            reasons.append("error_code is missing")
        elif error_code in NON_RETRYABLE_ERROR_CODES:
            reasons.append(
                f"error {error_code!r} is permanent (non-retryable)"
            )
        else:
            reasons.append(
                f"error {error_code!r} is not in the retryable set"
            )
    if attempt_count >= max_attempts:
        retry = False
        reasons.append(
            f"attempt_count {attempt_count} >= max_attempts {max_attempts}"
        )

    return {
        "retry": retry,
        "reason": "; ".join(reasons) if reasons else "all retry conditions met",
        "tool_name": tool_name,
        "error_code": error_code,
        "status": task_status,
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "idempotent": (
            is_idempotent(tool_name) if tool_name else False
        ),
    }


def retry_request_digest(original_task_id: str, request: Any) -> str:
    """Bind an idempotency key to one original task and one request body.

    The digest covers the original task id plus the goal/operation/
    parameters that would be re-submitted, so a reused ``request_id``
    can never silently point at a different retry.
    """
    payload = (
        request.to_dict()
        if hasattr(request, "to_dict")
        else dict(request)
    )
    encoded = json.dumps(
        {"retry_of": str(original_task_id), "request": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
