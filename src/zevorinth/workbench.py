"""Read-only projections for the local Zevorinth workbench."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from .artifact_tool import ArtifactToolError, read_artifact
from .models import Event, Task, TaskStatus, VerificationResult
from .recovery_preview import (
    RecoveryApprovalStore,
    build_recovery_preview,
)
from .task_lifecycle import audit_task_lifecycle


DEFAULT_TASK_LIMIT = 40
MAX_TASK_LIMIT = 100
MAX_ATTENTION_ITEMS = 50
MAX_EVENT_PAYLOAD_FIELDS = 12
MAX_EVENT_TEXT = 240
MAX_ERROR_TEXT = 600
MAX_ARTIFACT_PREVIEW_BYTES = 64 * 1024
ATTENTION_STATUSES = frozenset(
    {
        TaskStatus.AWAITING_CLARIFICATION,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.PLANNING_FAILED,
        TaskStatus.INTERRUPTED,
    }
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|password|secret|token)"
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"
    ),
)
_SAFE_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)


class WorkbenchError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class ReadOnlyWorkbench:
    """Query authoritative task data without initializing or mutating state."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.database_path = self.project_root / "data" / "zevorinth.sqlite3"
        self.artifacts_root = self.project_root / "artifacts"

    def _connect(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise WorkbenchError(
                "task_store_unavailable",
                "the task database does not exist",
                status=503,
            )
        try:
            connection = sqlite3.connect(
                f"file:{self.database_path}?mode=ro",
                uri=True,
                timeout=2.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            return connection
        except sqlite3.Error as exc:
            raise WorkbenchError(
                "task_store_unavailable",
                f"task database could not be opened: {type(exc).__name__}",
                status=503,
            ) from exc

    def list_tasks(
        self,
        *,
        limit: int = DEFAULT_TASK_LIMIT,
        offset: int = 0,
        status: str | None = None,
        mode: str = "all",
    ) -> dict[str, Any]:
        if not isinstance(limit, int) or not 1 <= limit <= MAX_TASK_LIMIT:
            raise WorkbenchError(
                "invalid_limit",
                f"limit must be between 1 and {MAX_TASK_LIMIT}",
            )
        if not isinstance(offset, int) or offset < 0:
            raise WorkbenchError("invalid_offset", "offset must be non-negative")
        if mode not in {"all", "attention"}:
            raise WorkbenchError("invalid_mode", "mode must be all or attention")
        statuses: list[TaskStatus] | None = None
        if status is not None:
            try:
                statuses = [TaskStatus(status)]
            except ValueError as exc:
                raise WorkbenchError(
                    "invalid_status", "status is not a known task status"
                ) from exc
        elif mode == "attention":
            statuses = sorted(ATTENTION_STATUSES, key=lambda item: item.value)

        where = ""
        values: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where = f"WHERE status IN ({placeholders})"
            values.extend(item.value for item in statuses)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT task_json
                    FROM tasks
                    {where}
                    ORDER BY updated_at DESC, task_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (*values, limit + 1, offset),
                ).fetchall()
                counts = {
                    str(row["status"]): int(row["count"])
                    for row in connection.execute(
                        "SELECT status, COUNT(*) AS count "
                        "FROM tasks GROUP BY status"
                    ).fetchall()
                }
        except sqlite3.Error as exc:
            raise WorkbenchError(
                "task_store_unreadable",
                f"task list could not be read: {type(exc).__name__}",
                status=503,
            ) from exc
        has_more = len(rows) > limit
        tasks = [
            _task_summary(Task.from_dict(json.loads(row["task_json"])))
            for row in rows[:limit]
        ]
        return {
            "tasks": tasks,
            "page": {
                "limit": limit,
                "offset": offset,
                "next_offset": offset + len(tasks) if has_more else None,
                "has_more": has_more,
            },
            "counts": counts,
            "filters": {"mode": mode, "status": status},
            "read_only": True,
        }

    def task_detail(self, task_id: str) -> dict[str, Any]:
        _validate_task_id(task_id)
        try:
            with self._connect() as connection:
                task_row = connection.execute(
                    "SELECT task_json FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if task_row is None:
                    raise WorkbenchError(
                        "task_not_found",
                        "the task does not exist",
                        status=404,
                    )
                event_rows = connection.execute(
                    """
                    SELECT sequence, event_id, task_id, event_type,
                           occurred_at, payload_json
                    FROM events
                    WHERE task_id = ?
                    ORDER BY sequence ASC
                    """,
                    (task_id,),
                ).fetchall()
        except WorkbenchError:
            raise
        except sqlite3.Error as exc:
            raise WorkbenchError(
                "task_store_unreadable",
                f"task detail could not be read: {type(exc).__name__}",
                status=503,
            ) from exc
        task = Task.from_dict(json.loads(task_row["task_json"]))
        events = [
            Event(
                sequence=int(row["sequence"]),
                event_id=str(row["event_id"]),
                task_id=str(row["task_id"]),
                event_type=str(row["event_type"]),
                occurred_at=str(row["occurred_at"]),
                payload=json.loads(row["payload_json"]),
            )
            for row in event_rows
        ]
        evidence_state = self._persisted_test_evidence_state(task)
        audit = audit_task_lifecycle(
            task,
            events,
            evidence_state=evidence_state,
        )
        preview = build_recovery_preview(
            task,
            events,
            audit=audit,
        ).to_dict()
        artifact_observation = self._artifact_observation(task)
        return {
            "task": {
                **_task_detail(task),
                "artifact_observation": artifact_observation,
                "integrity_attention": (
                    {
                        "kind": "artifact_integrity",
                        "label": "Artifact changed after verification",
                        "requires_user_answer": False,
                        "view_only": True,
                    }
                    if artifact_observation
                    and artifact_observation["state"]
                    == "modified_since_registration"
                    else None
                ),
            },
            "events": [_event_projection(event) for event in events],
            "lifecycle_audit": audit,
            "recovery_preview": preview,
            "test_evidence": evidence_state,
            "read_only": True,
        }

    def attention(self, *, limit: int = MAX_ATTENTION_ITEMS) -> dict[str, Any]:
        if not isinstance(limit, int) or not 1 <= limit <= MAX_ATTENTION_ITEMS:
            raise WorkbenchError(
                "invalid_limit",
                f"limit must be between 1 and {MAX_ATTENTION_ITEMS}",
            )
        task_page = self.list_tasks(limit=limit, mode="attention")
        items = [
            {
                "kind": "task",
                "attention": task["attention"],
                "task_id": task["task_id"],
                "goal": task["goal"],
                "status": task["status"],
                "phase": task["phase"],
                "updated_at": task["updated_at"],
            }
            for task in task_page["tasks"]
        ]
        items.extend(self._approval_attention(limit=limit))
        items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return {
            "items": items[:limit],
            "count": len(items[:limit]),
            "truncated": len(items) > limit,
            "read_only": True,
        }

    def artifact_preview(self, task_id: str) -> dict[str, Any]:
        _validate_task_id(task_id)
        task = self._require_task(task_id)
        evidence = task.artifact
        if evidence is None:
            raise WorkbenchError(
                "artifact_not_registered",
                "the task has no registered artifact",
                status=404,
            )
        try:
            raw = read_artifact(
                self.artifacts_root,
                task.task_id,
                evidence.relative_path,
            )
        except FileNotFoundError as exc:
            raise WorkbenchError(
                "artifact_missing",
                "the registered artifact is missing",
                status=404,
            ) from exc
        except ArtifactToolError as exc:
            raise WorkbenchError(
                exc.code,
                exc.message,
                status=409,
            ) from exc
        except OSError as exc:
            raise WorkbenchError(
                "artifact_read_failed",
                f"the registered artifact could not be read: {type(exc).__name__}",
                status=503,
            ) from exc
        if len(raw) > MAX_ARTIFACT_PREVIEW_BYTES:
            raise WorkbenchError(
                "artifact_preview_too_large",
                "the registered artifact exceeds the preview limit",
                status=413,
            )
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkbenchError(
                "artifact_not_utf8",
                "the registered artifact is not UTF-8 text",
                status=415,
            ) from exc
        observed_sha256 = hashlib.sha256(raw).hexdigest()
        evidence_matches = (
            observed_sha256 == evidence.sha256
            and len(raw) == evidence.byte_count
        )
        verification = task.verification
        verified = bool(
            verification is not None
            and verification.passed
            and evidence_matches
        )
        if not evidence_matches:
            state = "modified_since_registration"
        elif verified:
            state = "verified"
        else:
            state = "unverified"
        return {
            "task_id": task.task_id,
            "artifact_path": evidence.relative_path,
            "content": content,
            "observed": {
                "byte_count": len(raw),
                "sha256": observed_sha256,
            },
            "registered": {
                "byte_count": evidence.byte_count,
                "sha256": evidence.sha256,
                "attempt_id": evidence.attempt_id,
                "claim_generation": evidence.claim_generation,
            },
            "verification": (
                _verification_projection(verification)
                if verification is not None
                else None
            ),
            "state": state,
            "read_only": True,
        }

    def _require_task(self, task_id: str) -> Task:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT task_json FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise WorkbenchError(
                "task_store_unreadable",
                f"task could not be read: {type(exc).__name__}",
                status=503,
            ) from exc
        if row is None:
            raise WorkbenchError(
                "task_not_found",
                "the task does not exist",
                status=404,
            )
        return Task.from_dict(json.loads(row["task_json"]))

    def _artifact_observation(self, task: Task) -> dict[str, Any] | None:
        if task.artifact is None:
            return None
        try:
            raw = read_artifact(
                self.artifacts_root,
                task.task_id,
                task.artifact.relative_path,
            )
        except FileNotFoundError:
            return {
                "state": "missing",
                "observed_byte_count": None,
                "observed_sha256": None,
                "matches_registration": False,
            }
        except ArtifactToolError as exc:
            return {
                "state": exc.code,
                "observed_byte_count": None,
                "observed_sha256": None,
                "matches_registration": False,
            }
        except OSError as exc:
            return {
                "state": f"read_failed_{type(exc).__name__}",
                "observed_byte_count": None,
                "observed_sha256": None,
                "matches_registration": False,
            }
        sha256 = hashlib.sha256(raw).hexdigest()
        matches = (
            sha256 == task.artifact.sha256
            and len(raw) == task.artifact.byte_count
        )
        return {
            "state": "current" if matches else "modified_since_registration",
            "observed_byte_count": len(raw),
            "observed_sha256": sha256,
            "matches_registration": matches,
        }

    @staticmethod
    def _persisted_test_evidence_state(task: Task) -> dict[str, Any] | None:
        evidence = (
            task.tool_result.test_evidence
            if task.tool_result is not None
            else None
        )
        if evidence is None:
            return None
        return {
            "state": "persisted_not_live_revalidated",
            "reasons": ["read_only_workbench_does_not_probe_runtime"],
            "evidence_id": evidence.evidence_id,
            "attempt_id": evidence.attempt_id,
            "claim_generation": evidence.claim_generation,
        }

    def _approval_attention(self, *, limit: int) -> list[dict[str, Any]]:
        try:
            approvals = RecoveryApprovalStore(self.project_root).list()
        except Exception:
            return [
                {
                    "kind": "approval_store",
                    "attention": {
                        "kind": "review_required",
                        "label": "Approval store unavailable",
                        "requires_user_answer": False,
                        "view_only": True,
                    },
                    "approval_id": None,
                    "updated_at": None,
                }
            ]
        items: list[dict[str, Any]] = []
        for approval in approvals[:limit]:
            state = "approved"
            label = "Recovery approval available for review"
            view_only = True
            if approval.status == "consuming":
                label = "Recovery approval has an unverified consumption attempt"
            elif approval.status == "failed":
                label = "Recovery attempt failed independent verification"
            elif approval.status in {"revoked", "rejected"}:
                continue
            try:
                detail = self.task_detail(approval.task_id)
                current_digest = detail["recovery_preview"].get("state_digest")
                if approval.status == "approved" and (
                    current_digest != approval.state_digest
                ):
                    state = "stale"
                    label = "Recovery approval no longer matches current state"
            except WorkbenchError:
                state = "unavailable"
                label = "Recovery approval task is unavailable"
            items.append(
                {
                    "kind": "approval",
                    "attention": {
                        "kind": "review_required",
                        "label": label,
                        "requires_user_answer": False,
                        "view_only": view_only,
                    },
                    "approval_id": approval.approval_id,
                    "task_id": approval.task_id,
                    "approval_status": state,
                    "status": approval.status,
                    "updated_at": approval.approved_at,
                }
            )
        return items


def _validate_task_id(task_id: str) -> str:
    if not _SAFE_ID.fullmatch(task_id):
        raise WorkbenchError(
            "invalid_task_id",
            "task_id must be a canonical lowercase UUID",
            status=400,
        )
    return task_id


def _attention_for(task: Task) -> dict[str, Any] | None:
    if task.status is TaskStatus.AWAITING_CLARIFICATION:
        return {
            "kind": "clarification",
            "label": "Needs your answer",
            "requires_user_answer": True,
            "view_only": True,
        }
    if task.status is TaskStatus.INTERRUPTED:
        return {
            "kind": "interrupted",
            "label": "Review required",
            "requires_user_answer": False,
            "view_only": True,
        }
    if task.status is TaskStatus.BLOCKED:
        return {
            "kind": "blocked",
            "label": "Blocked; review the reason",
            "requires_user_answer": False,
            "view_only": True,
        }
    if task.status in {TaskStatus.FAILED, TaskStatus.PLANNING_FAILED}:
        return {
            "kind": "terminal_failure",
            "label": "Failed; view the evidence",
            "requires_user_answer": False,
            "view_only": True,
        }
    return None


def _task_summary(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "status": task.status.value,
        "operation": task.request.operation,
        "goal": _bounded_text(task.request.goal or "", 320),
        "phase": task.phase,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "execution_mode": task.execution_mode.value,
        "planner_mode": task.planner_mode.value,
        "attention": _attention_for(task),
    }


def _task_detail(task: Task) -> dict[str, Any]:
    return {
        **_task_summary(task),
        "goal": _bounded_text(task.request.goal or "", 16 * 1024),
        "attempt_id": task.attempt_id,
        "claim_generation": task.claim_generation,
        "missing_fields": list(task.missing_fields),
        "clarification_questions": [
            _bounded_text(question, 500)
            for question in task.clarification_questions[:5]
        ],
        "error": (
            {
                "code": _bounded_text(task.error.code, 120),
                "message": _bounded_text(
                    _redact(task.error.message),
                    MAX_ERROR_TEXT,
                ),
            }
            if task.error is not None
            else None
        ),
        "plan": (
            {
                "planner_name": task.plan.planner_name,
                "deterministic": task.plan.deterministic,
                "steps": [
                    {
                        "step_id": step.step_id,
                        "tool_name": step.tool_name,
                        "expected_result": _bounded_text(
                            _redact(step.expected_result),
                            600,
                        ),
                    }
                    for step in task.plan.steps
                ],
            }
            if task.plan is not None
            else None
        ),
        "artifact": (
            {
                "relative_path": task.artifact.relative_path,
                "byte_count": task.artifact.byte_count,
                "sha256": task.artifact.sha256,
                "attempt_id": task.artifact.attempt_id,
                "claim_generation": task.artifact.claim_generation,
                "preview_url": (
                    f"/api/tasks/{quote(task.task_id)}/artifact"
                ),
            }
            if task.artifact is not None
            else None
        ),
        "verification": (
            _verification_projection(task.verification)
            if task.verification is not None
            else None
        ),
        "assurance_boundary": {
            "authenticity": "not_authenticated",
            "trust_level": "L0_L1_internal_consistency_only",
            "security_certification": False,
        },
    }


def _verification_projection(
    verification: VerificationResult,
) -> dict[str, Any]:
    return {
        "verifier_name": verification.verifier_name,
        "passed": verification.passed,
        "exists": verification.exists,
        "path_safe": verification.path_safe,
        "content_matches": verification.content_matches,
        "evidence_matches": verification.evidence_matches,
        "reason": verification.reason,
        "checked_at": verification.checked_at,
        "artifact": (
            {
                "relative_path": verification.artifact.relative_path,
                "byte_count": verification.artifact.byte_count,
                "sha256": verification.artifact.sha256,
                "attempt_id": verification.artifact.attempt_id,
                "claim_generation": verification.artifact.claim_generation,
            }
            if verification.artifact is not None
            else None
        ),
    }


def _event_projection(event: Event) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "event_id": event.event_id,
        "task_id": event.task_id,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at,
        "payload_summary": _payload_summary(event.payload),
    }


def _payload_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in sorted(payload, key=str):
        if len(summary) >= MAX_EVENT_PAYLOAD_FIELDS:
            break
        if _SENSITIVE_KEY.search(str(key)):
            continue
        value = payload[key]
        if value is None or isinstance(value, (bool, int, float)):
            summary[str(key)] = value
        elif isinstance(value, str):
            summary[str(key)] = _bounded_text(
                _redact(value),
                MAX_EVENT_TEXT,
            )
        elif isinstance(value, (list, dict)):
            summary[str(key)] = {
                "type": type(value).__name__,
                "count": len(value),
            }
    return summary


def _redact(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"
