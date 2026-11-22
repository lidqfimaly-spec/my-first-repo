"""Untrusted code and research proposal candidates.

Proposal providers may use a model or another heuristic, but this module only
accepts bounded, schema-versioned data. Validation never authorizes execution:
code candidates are rechecked against the current workspace and research
candidates are rebuilt from exact local source quotes before materialization.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .research_workflow import (
    RESEARCH_REPORT_TOOL,
    ResearchWorkflowError,
    build_research_report,
    validate_research_parameters,
)
from .workspace_edit import (
    EDIT_AUTHORIZATION_TOKEN,
    EDIT_TEXT_FILE_TOOL,
    MAX_EDIT_PATCH_BYTES,
    WorkspaceEditError,
    validate_edit_parameters,
    validate_edit_precondition,
)
from .workspace_inspection import INSPECT_WORKSPACE_TOOL


PROPOSAL_SCHEMA_VERSION = "zevorinth-proposal-candidate-v1"
MAX_PROPOSAL_BYTES = 256 * 1024
MAX_ACTIVE_PROPOSAL_SESSIONS = 128
DEFAULT_PROPOSAL_SESSION_TTL_SECONDS = 120.0
PROPOSAL_KINDS = frozenset({"code", "research"})


class ProposalCandidateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ProposalProvider(Protocol):
    name: str

    def propose(
        self,
        *,
        kind: str,
        goal: str,
        context: Mapping[str, Any],
        session_nonce: str,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ProposalSession:
    """Opaque server-issued proposal request identity."""

    session_id: str
    provider_name: str
    provider_identity: int
    kind: str
    context_sha256: str
    expires_at_monotonic: float


class ProposalSessionIssuer:
    """Issue and consume one-use proposal sessions in a trusted process."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_PROPOSAL_SESSION_TTL_SECONDS,
        max_active: int = MAX_ACTIVE_PROPOSAL_SESSIONS,
        clock: Any = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or max_active < 1:
            raise ValueError("proposal session limits must be positive")
        self.ttl_seconds = float(ttl_seconds)
        self.max_active = int(max_active)
        self._clock = clock
        self._lock = threading.Lock()
        self._active: dict[str, ProposalSession] = {}
        self._providers: dict[str, ProposalProvider] = {}

    def issue(
        self,
        *,
        provider: ProposalProvider,
        kind: str,
        context: Mapping[str, Any],
    ) -> ProposalSession:
        if kind not in PROPOSAL_KINDS:
            raise ProposalCandidateError(
                "invalid_proposal_kind",
                "proposal kind must be code or research",
            )
        provider_name = getattr(provider, "name", None)
        if not isinstance(provider_name, str) or not provider_name:
            raise ProposalCandidateError(
                "invalid_proposal_provider",
                "proposal provider must expose a stable name",
            )
        now = float(self._clock())
        with self._lock:
            self._prune(now)
            if len(self._active) >= self.max_active:
                raise ProposalCandidateError(
                    "proposal_session_capacity",
                    "too many active proposal sessions",
                )
            session = ProposalSession(
                session_id=secrets.token_urlsafe(32),
                provider_name=provider_name,
                provider_identity=id(provider),
                kind=kind,
                context_sha256=proposal_context_sha256(context),
                expires_at_monotonic=now + self.ttl_seconds,
            )
            self._active[session.session_id] = session
            self._providers[session.session_id] = provider
            return session

    def consume(
        self,
        session: ProposalSession,
        *,
        provider: ProposalProvider,
        kind: str,
        context: Mapping[str, Any],
    ) -> None:
        if not isinstance(session, ProposalSession):
            raise ProposalCandidateError(
                "invalid_proposal_session",
                "proposal session was not issued by the trusted issuer",
            )
        now = float(self._clock())
        with self._lock:
            self._prune(now)
            current = self._active.get(session.session_id)
            if current != session:
                raise ProposalCandidateError(
                    "proposal_session_replay",
                    "proposal session is unknown, expired, or already consumed",
                )
            if current.expires_at_monotonic <= now:
                self._discard(session.session_id)
                raise ProposalCandidateError(
                    "proposal_session_expired",
                    "proposal session has expired",
                )
            bound_provider = self._providers.get(session.session_id)
            if (
                bound_provider is not provider
                or current.provider_identity != id(provider)
                or current.provider_name != getattr(provider, "name", None)
            ):
                raise ProposalCandidateError(
                    "proposal_provider_mismatch",
                    "proposal provider does not own this session",
                )
            if current.kind != kind:
                raise ProposalCandidateError(
                    "proposal_kind_mismatch",
                    "proposal session belongs to a different task kind",
                )
            if current.context_sha256 != proposal_context_sha256(context):
                raise ProposalCandidateError(
                    "proposal_context_mismatch",
                    "proposal context no longer matches the session",
                )
            self._discard(session.session_id)

    def _prune(self, now: float) -> None:
        expired = [
            session_id
            for session_id, session in self._active.items()
            if session.expires_at_monotonic <= now
        ]
        for session_id in expired:
            self._discard(session_id)

    def _discard(self, session_id: str) -> None:
        self._active.pop(session_id, None)
        self._providers.pop(session_id, None)


@dataclass(frozen=True, slots=True)
class ValidatedProposal:
    kind: str
    goal: str
    body: Mapping[str, Any]
    provenance: Mapping[str, Any]


class ScriptedProposalProvider:
    """Deterministic test provider; it performs no model or network call."""

    name = "scripted-proposal-test-v1"

    def __init__(self, response: Mapping[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def propose(
        self,
        *,
        kind: str,
        goal: str,
        context: Mapping[str, Any],
        session_nonce: str,
    ) -> Mapping[str, Any]:
        self.calls.append(
            {
                "kind": kind,
                "goal": goal,
                "context_sha256": proposal_context_sha256(context),
                "session_nonce": session_nonce,
            }
        )
        return dict(self._response)


def proposal_context_sha256(context: Mapping[str, Any]) -> str:
    try:
        encoded = _canonical_json(context)
    except (TypeError, ValueError) as exc:
        raise ProposalCandidateError(
            "invalid_proposal_context",
            "proposal context must be canonical JSON data",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def validate_proposal_candidate(
    project_root: Any,
    candidate: object,
    *,
    expected_kind: str,
    provider: ProposalProvider,
    session: ProposalSession,
    issuer: ProposalSessionIssuer,
    context: Mapping[str, Any],
) -> ValidatedProposal:
    """Validate a provider candidate without executing or mutating anything."""
    if expected_kind not in PROPOSAL_KINDS:
        raise ProposalCandidateError(
            "invalid_proposal_kind",
            "expected proposal kind must be code or research",
        )
    issuer.consume(
        session,
        provider=provider,
        kind=expected_kind,
        context=context,
    )
    if not isinstance(candidate, Mapping):
        raise ProposalCandidateError(
            "invalid_proposal_candidate",
            "proposal candidate must be an object",
        )
    expected_fields = {
        "schema_version",
        "kind",
        "provider",
        "session_nonce",
        "context_sha256",
        "body",
    }
    if set(candidate) != expected_fields:
        raise ProposalCandidateError(
            "invalid_proposal_fields",
            "proposal candidate fields do not match the closed schema",
        )
    try:
        encoded = _canonical_json(candidate)
    except (TypeError, ValueError) as exc:
        raise ProposalCandidateError(
            "invalid_proposal_candidate",
            "proposal candidate must be canonical JSON data",
        ) from exc
    if len(encoded) > MAX_PROPOSAL_BYTES:
        raise ProposalCandidateError(
            "proposal_too_large",
            f"proposal exceeds {MAX_PROPOSAL_BYTES} bytes",
        )
    if candidate.get("schema_version") != PROPOSAL_SCHEMA_VERSION:
        raise ProposalCandidateError(
            "proposal_schema_mismatch",
            "proposal schema version is not supported",
        )
    if candidate.get("kind") != expected_kind:
        raise ProposalCandidateError(
            "proposal_kind_mismatch",
            "proposal kind does not match the requested task",
        )
    expected_provider = session.provider_name
    expected_session_nonce = session.session_id
    expected_context_sha256 = session.context_sha256
    if candidate.get("provider") != expected_provider:
        raise ProposalCandidateError(
            "proposal_provider_mismatch",
            "proposal provider identity does not match the trusted caller",
        )
    if candidate.get("session_nonce") != expected_session_nonce:
        raise ProposalCandidateError(
            "proposal_nonce_mismatch",
            "proposal session nonce does not match this request",
        )
    if candidate.get("context_sha256") != expected_context_sha256:
        raise ProposalCandidateError(
            "proposal_context_mismatch",
            "proposal context digest does not match the supplied context",
        )
    try:
        normalized = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProposalCandidateError(
            "invalid_proposal_candidate",
            "proposal candidate must be canonical JSON data",
        ) from exc
    if not isinstance(normalized, Mapping):
        raise ProposalCandidateError(
            "invalid_proposal_candidate",
            "proposal candidate must be an object",
        )
    body = normalized.get("body")
    if not isinstance(body, Mapping):
        raise ProposalCandidateError(
            "invalid_proposal_body",
            "proposal body must be an object",
        )
    if expected_kind == "code":
        _validate_code_body(project_root, body)
    else:
        _validate_research_body(project_root, body)
    body_snapshot = _freeze_json(body)
    provenance = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "kind": expected_kind,
        "provider": expected_provider,
        "session_id": expected_session_nonce,
        "session_nonce": expected_session_nonce,
        "context_sha256": expected_context_sha256,
        "candidate_sha256": hashlib.sha256(encoded).hexdigest(),
        "validation": "DETERMINISTIC_CANDIDATE_ONLY",
    }
    return ValidatedProposal(
        kind=expected_kind,
        goal=str(body_snapshot.get("goal") or "").strip(),
        body=body_snapshot,
        provenance=MappingProxyType(provenance),
    )


def code_task_steps(
    proposal: ValidatedProposal,
    *,
    test_backend: str = "container",
    host_authorization: str | None = None,
) -> list[dict[str, Any]]:
    if proposal.kind != "code":
        raise ProposalCandidateError(
            "proposal_kind_mismatch", "code task steps require a code proposal"
        )
    body = proposal.body
    edit = dict(body["edit"])
    steps: list[dict[str, Any]] = [
        {
            "step_id": "inspect",
            "tool_name": INSPECT_WORKSPACE_TOOL,
            "expected_result": (
                f"inspect {edit['path']} in workspace {edit['workspace']}"
            ),
            "parameters": {
                "workspace": edit["workspace"],
                "path": edit["path"],
            },
        },
        {
            "step_id": "edit",
            "tool_name": EDIT_TEXT_FILE_TOOL,
            "expected_result": f"apply candidate edit to {edit['path']}",
            "depends_on": ["inspect"],
            "requires_approval": True,
            "parameters": {
                "workspace": edit["workspace"],
                "path": edit["path"],
                "expected_sha256": "${inspect.selected_sha256}",
                "old_text": edit["old_text"],
                "new_text": edit["new_text"],
                "authorization": EDIT_AUTHORIZATION_TOKEN,
            },
        },
    ]
    test_target = body.get("test_target")
    if test_target is not None:
        if test_backend not in {"container", "host"}:
            raise ProposalCandidateError(
                "invalid_proposal_test_backend",
                "trusted test backend must be container or host",
            )
        if test_backend == "host" and host_authorization is None:
            raise ProposalCandidateError(
                "host_authorization_required",
                "host test execution requires separate trusted authorization",
            )
        test_parameters: dict[str, Any] = {
            "runner": "unittest",
            "target": test_target,
            "workspace": edit["workspace"],
            "execution_backend": test_backend,
            "timeout_seconds": 10.0,
        }
        if host_authorization is not None:
            test_parameters["host_authorization"] = host_authorization
        steps.append(
            {
                "step_id": "test",
                "tool_name": "run_tests",
                "expected_result": (
                    f"run approved test {test_target} in {edit['workspace']}"
                ),
                "depends_on": ["edit"],
                "parameters": test_parameters,
            }
        )
    return steps


def research_task_parameters(proposal: ValidatedProposal) -> dict[str, Any]:
    if proposal.kind != "research":
        raise ProposalCandidateError(
            "proposal_kind_mismatch",
            "research task parameters require a research proposal",
        )
    return {
        "workspace": proposal.body["workspace"],
        "sources": list(proposal.body["sources"]),
        "title": proposal.body["title"],
        "output_path": proposal.body["output_path"],
        "findings": _thaw_json(proposal.body["findings"]),
    }


def _validate_code_body(
    project_root: Any,
    body: Mapping[str, Any],
) -> None:
    allowed = {"goal", "workspace", "edit", "test_target", "rationale"}
    if set(body) - allowed:
        raise ProposalCandidateError(
            "invalid_code_proposal_fields",
            "code proposal body contains unknown fields",
        )
    goal = body.get("goal")
    if not _bounded_text(goal, 8_000):
        raise ProposalCandidateError(
            "invalid_code_proposal_goal", "code proposal goal is invalid"
        )
    workspace = body.get("workspace")
    edit = body.get("edit")
    if not isinstance(workspace, str) or not isinstance(edit, Mapping):
        raise ProposalCandidateError(
            "invalid_code_proposal",
            "code proposal must contain workspace and edit objects",
        )
    if set(edit) - {
        "workspace",
        "path",
        "expected_sha256",
        "old_text",
        "new_text",
    }:
        raise ProposalCandidateError(
            "invalid_code_proposal_edit",
            "code proposal edit contains unknown fields",
        )
    if edit.get("workspace") != workspace:
        raise ProposalCandidateError(
            "code_proposal_workspace_mismatch",
            "edit workspace must match the proposal workspace",
        )
    try:
        parameters = validate_edit_parameters(
            {
                **dict(edit),
                "authorization": EDIT_AUTHORIZATION_TOKEN,
            }
        )
        validate_edit_precondition(project_root, {
            **parameters,
            "authorization": EDIT_AUTHORIZATION_TOKEN,
        })
    except WorkspaceEditError as exc:
        raise ProposalCandidateError(exc.code, exc.message) from exc
    rationale = body.get("rationale")
    if rationale is not None and not _bounded_text(rationale, 8_000):
        raise ProposalCandidateError(
            "invalid_code_proposal_rationale",
            "code proposal rationale is invalid",
        )
    test_target = body.get("test_target")
    if test_target is not None and (
        not isinstance(test_target, str)
        or not test_target
        or len(test_target) > 256
        or not all(
            part.isidentifier() for part in test_target.split(".")
        )
    ):
        raise ProposalCandidateError(
            "invalid_code_proposal_test_target",
            "test_target must be a bounded dotted identifier",
        )


def _validate_research_body(
    project_root: Any,
    body: Mapping[str, Any],
) -> None:
    allowed = {
        "goal",
        "workspace",
        "sources",
        "title",
        "output_path",
        "findings",
    }
    if set(body) - allowed:
        raise ProposalCandidateError(
            "invalid_research_proposal_fields",
            "research proposal body contains unknown fields",
        )
    if not _bounded_text(body.get("goal"), 8_000):
        raise ProposalCandidateError(
            "invalid_research_proposal_goal",
            "research proposal goal is invalid",
        )
    parameters = {
        key: body[key]
        for key in (
            "workspace",
            "sources",
            "title",
            "output_path",
            "findings",
        )
    }
    try:
        validate_research_parameters(project_root, parameters)
        build_research_report(
            project_root,
            parameters,
            generated_at="1970-01-01T00:00:00.000+00:00",
        )
    except ResearchWorkflowError as exc:
        raise ProposalCandidateError(exc.code, exc.message) from exc


def _bounded_text(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and len(value) <= maximum
        and "\x00" not in value
    )


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze_json(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _thaw_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
