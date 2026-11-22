"""One-time host execution grants for controlled test runs.

Trust model
-----------
Host test execution runs workspace code with the invoking user's privileges,
so it is never an agent default. A human issues a grant out of band with
``zevorinth authorize-host-test``; the grant is:

- bound to exactly one workspace, one unittest target and one issuer,
- short lived (default 30 minutes, hard maximum 120),
- single use, consumed atomically before the process is spawned,
- stored outside the repository with 0600 permissions,
- compared in constant time,
- revocable, and never restored if execution fails.

A grant authorizes one controlled ``run_tests`` operation. It is not a
general host command permission: the consuming call site re-checks that the
grant's workspace and target equal the ones it is about to execute.

Audit
-----
Every lifecycle transition (issued / consumed / rejected / expired /
revoked) is appended to ``audit.jsonl`` in the grant directory. The in-task
record is the tool result plus the tool events, which carry the grant id.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

GRANT_VERSION = "host-test-grant-v1"
DEFAULT_TTL_MINUTES = 30
MAX_TTL_MINUTES = 120
MIN_TTL_MINUTES = 1
ISSUER_MAX_LENGTH = 128

#: Grant ids and secrets are hex, so the assembled token stays inside the
#: ``^[A-Za-z0-9._-]{1,128}$`` shape the test runner already accepted.
_TOKEN_PATTERN = re.compile(r"^([0-9a-f]{32})\.([0-9a-f]{64})$")
_GRANT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
#: Mirrors WORKSPACE_PATTERN / TARGET_PATTERN in local_test_runner; a test
#: asserts the two stay in sync instead of importing across the cycle.
_GRANT_WORKSPACE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_GRANT_TARGET_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$"
)

REQUIRED = "host_authorization_required"
INVALID = "host_authorization_invalid"
EXPIRED = "host_authorization_expired"
SCOPE_MISMATCH = "host_authorization_scope_mismatch"
ALREADY_USED = "host_authorization_already_used"
REVOKED = "host_authorization_revoked"
#: The grant store itself could not be read or written. Fail closed: an
#: authorization we cannot read is not an authorization.
STORAGE_FAILED = "host_authorization_storage_failed"
#: The audit entry could not be appended. The grant is already spent at this
#: point, and execution is refused: no run may happen without a record of it.
AUDIT_FAILED = "host_authorization_audit_failed"


class HostAuthorizationError(RuntimeError):
    """Raised when a host execution grant is missing, invalid or unusable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def default_grant_root() -> Path:
    """Return the out-of-repository directory holding host grants."""
    return Path.home() / ".zevorinth" / "host-grants"


def _isoformat(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse_isoformat(value: str) -> datetime:
    return datetime.fromisoformat(value)


class HostGrantStore:
    """Filesystem-backed, single-use storage for host execution grants."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_grant_root()

    # -- paths ---------------------------------------------------------
    def _ensure_root(self) -> None:
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except FileExistsError:
            # Some sandboxed interpreters intercept mkdir and ignore
            # ``exist_ok``.  If the directory really is there, carry on.
            if not self.root.is_dir():
                raise
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def _record_path(self, grant_id: str) -> Path:
        return self.root / f"{grant_id}.json"

    def _lock_path(self, grant_id: str) -> Path:
        return self.root / f"{grant_id}.lock"

    def _audit_path(self) -> Path:
        return self.root / "audit.jsonl"

    # -- io ------------------------------------------------------------
    def _write_record(self, record: Mapping[str, Any]) -> None:
        self._ensure_root()
        target = self._record_path(str(record["grant_id"]))
        try:
            descriptor, temp_name = tempfile.mkstemp(
                prefix=".grant-", dir=str(self.root)
            )
        except OSError as exc:
            raise HostAuthorizationError(
                STORAGE_FAILED,
                f"host authorization record could not be staged:"
                f" {type(exc).__name__}",
            ) from exc
        try:
            os.fchmod(descriptor, 0o600)
            payload = json.dumps(
                dict(record), ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        except OSError as exc:
            _unlink_quietly(temp_name)
            raise HostAuthorizationError(
                STORAGE_FAILED,
                f"host authorization record could not be written:"
                f" {type(exc).__name__}",
            ) from exc
        finally:
            os.close(descriptor)
        try:
            os.replace(temp_name, target)
        except OSError as exc:
            _unlink_quietly(temp_name)
            raise HostAuthorizationError(
                STORAGE_FAILED,
                f"host authorization record could not be committed:"
                f" {type(exc).__name__}",
            ) from exc

    def _read_record(self, grant_id: str) -> dict[str, Any] | None:
        """Read one record; unreadable and corrupt both mean "not usable".

        A record we cannot parse must never be interpreted as "no restriction".
        Callers treat ``None`` as a rejection, never as a grant.
        """
        path = self._record_path(grant_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        return record if isinstance(record, dict) else None

    def _append_audit(self, entry: Mapping[str, Any]) -> None:
        """Append one audit line, or fail closed.

        The audit trail is part of the consumption protocol, not a log written
        after the fact: if an entry cannot be durably appended, the caller must
        refuse to execute. ``O_APPEND`` + ``fsync`` per line keeps concurrent
        writers from interleaving partial lines.
        """
        self._ensure_root()
        try:
            descriptor = os.open(
                str(self._audit_path()),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
        except OSError as exc:
            raise HostAuthorizationError(
                AUDIT_FAILED,
                f"host authorization audit log could not be opened:"
                f" {type(exc).__name__}",
            ) from exc
        try:
            os.write(
                descriptor,
                (
                    json.dumps(
                        dict(entry), ensure_ascii=False, sort_keys=True
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            os.fsync(descriptor)
        except OSError as exc:
            raise HostAuthorizationError(
                AUDIT_FAILED,
                f"host authorization audit entry could not be written:"
                f" {type(exc).__name__}",
            ) from exc
        finally:
            os.close(descriptor)

    def _audit(
        self,
        event: str,
        *,
        grant_id: str | None,
        workspace: Any = None,
        target: Any = None,
        issuer: Any = None,
        reason: str | None = None,
        at: datetime | None = None,
    ) -> None:
        self._append_audit(
            {
                "event": event,
                "grant_id": grant_id,
                "workspace": workspace,
                "target": target,
                "issuer": issuer,
                "reason": reason,
                "recorded_at": _isoformat(
                    at or datetime.now(timezone.utc)
                ),
                "os_user": _os_user(),
                "pid": os.getpid(),
            }
        )

    def _audit_quietly(self, event: str, **kwargs: Any) -> None:
        """Best-effort audit for a path that is already refusing the request.

        Nothing will execute, so a missing rejection line cannot hide an
        execution. Suppressing the failure keeps the reported reason honest
        (``expired`` must not surface as ``audit_failed``).
        """
        try:
            self._audit(event, **kwargs)
        except HostAuthorizationError:
            pass

    def _has_audit(self, event: str, grant_id: str) -> bool:
        """True when ``audit.jsonl`` already records ``event`` for the grant."""
        path = self._audit_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return False
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(entry, dict)
                and entry.get("event") == event
                and entry.get("grant_id") == grant_id
            ):
                return True
        return False

    # -- lifecycle -----------------------------------------------------
    def issue(
        self,
        *,
        workspace: str,
        target: str,
        issuer: str,
        ttl_minutes: int = DEFAULT_TTL_MINUTES,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Create a single-use grant and return it with its token."""
        if not isinstance(workspace, str) or not _GRANT_WORKSPACE_PATTERN.fullmatch(
            workspace
        ):
            raise HostAuthorizationError(
                INVALID, "workspace name is not a valid grant scope"
            )
        if not isinstance(target, str) or not _GRANT_TARGET_PATTERN.fullmatch(
            target
        ):
            raise HostAuthorizationError(
                INVALID, "test target is not a valid grant scope"
            )
        if (
            not isinstance(issuer, str)
            or not issuer.strip()
            or len(issuer) > ISSUER_MAX_LENGTH
        ):
            raise HostAuthorizationError(
                INVALID, "issuer must be a bounded non-empty label"
            )
        if not isinstance(ttl_minutes, int) or isinstance(ttl_minutes, bool):
            raise HostAuthorizationError(
                INVALID, "ttl_minutes must be an integer"
            )
        if not MIN_TTL_MINUTES <= ttl_minutes <= MAX_TTL_MINUTES:
            raise HostAuthorizationError(
                INVALID,
                f"ttl_minutes must be between {MIN_TTL_MINUTES}"
                f" and {MAX_TTL_MINUTES}",
            )
        issued_at = now or datetime.now(timezone.utc)
        expires_at = issued_at + timedelta(minutes=ttl_minutes)
        grant_id = uuid.uuid4().hex
        secret = uuid.uuid4().hex + uuid.uuid4().hex
        record = {
            "version": GRANT_VERSION,
            "grant_id": grant_id,
            "secret_sha256": hashlib.sha256(
                secret.encode("utf-8")
            ).hexdigest(),
            "workspace": workspace,
            "target": target,
            "issuer": issuer.strip(),
            "issued_at": _isoformat(issued_at),
            "expires_at": _isoformat(expires_at),
            "ttl_minutes": ttl_minutes,
            "status": "issued",
            "consumed_at": None,
            "consumed_workspace": None,
            "consumed_target": None,
            "revoked_at": None,
        }
        self._write_record(record)
        try:
            self._audit(
                "issued",
                grant_id=grant_id,
                workspace=workspace,
                target=target,
                issuer=record["issuer"],
                at=issued_at,
            )
        except HostAuthorizationError:
            # The token is never returned on this path, so the record is
            # unusable; drop it rather than leave a phantom "issued" grant.
            _unlink_quietly(str(self._record_path(grant_id)))
            raise
        return {
            "grant": {
                key: value
                for key, value in record.items()
                if key != "secret_sha256"
            },
            "token": f"{grant_id}.{secret}",
        }

    def verify(
        self,
        token: Any,
        *,
        workspace: str,
        target: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Check a grant without consuming it.

        Used by the policy layer so a plan can be judged before execution.
        Only :meth:`consume` performs the atomic single-use transition, and a
        token that passes ``verify`` can still fail ``consume`` if it was used
        in between, which fails closed.
        """
        grant_id, record, moment = self._load_for_token(token, workspace, target)
        self._check_usable(
            record,
            workspace=workspace,
            target=target,
            moment=moment,
        )
        return {
            key: value
            for key, value in record.items()
            if key != "secret_sha256"
        }

    def _load_for_token(
        self,
        token: Any,
        workspace: str,
        target: str,
    ) -> tuple[str, dict[str, Any], datetime]:
        if token is None or (isinstance(token, str) and not token.strip()):
            raise HostAuthorizationError(
                REQUIRED, "host execution requires a one-time grant token"
            )
        match = _TOKEN_PATTERN.fullmatch(token) if isinstance(token, str) else None
        if match is None:
            raise HostAuthorizationError(
                INVALID, "host authorization token is malformed"
            )
        grant_id, secret = match.group(1), match.group(2)
        record = self._read_record(grant_id)
        if record is None:
            raise HostAuthorizationError(
                INVALID, "host authorization token is not recognised"
            )
        expected = str(record.get("secret_sha256") or "")
        actual = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        if not expected or not hmac.compare_digest(expected, actual):
            raise HostAuthorizationError(
                INVALID, "host authorization token is not recognised"
            )
        return grant_id, record, datetime.now(timezone.utc)

    @staticmethod
    def _check_usable(
        record: Mapping[str, Any],
        *,
        workspace: str,
        target: str,
        moment: datetime,
    ) -> None:
        if record.get("revoked_at"):
            raise HostAuthorizationError(
                REVOKED, "host authorization grant was revoked"
            )
        if str(record.get("status") or "") != "issued":
            raise HostAuthorizationError(
                ALREADY_USED, "host authorization grant was already consumed"
            )
        try:
            expires_at = _parse_isoformat(str(record["expires_at"]))
        except (KeyError, ValueError) as exc:
            raise HostAuthorizationError(
                INVALID, "host authorization grant has no valid expiry"
            ) from exc
        if moment >= expires_at:
            raise HostAuthorizationError(
                EXPIRED, "host authorization grant has expired"
            )
        if record.get("workspace") != workspace or record.get("target") != target:
            raise HostAuthorizationError(
                SCOPE_MISMATCH,
                "host authorization grant does not cover this"
                " workspace and target",
            )

    def consume(
        self,
        token: Any,
        *,
        workspace: str,
        target: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Verify and atomically consume a grant for this exact execution.

        Protocol, in this order, under the per-grant lock:

        1. read the record and compare the secret in constant time,
        2. check revocation, single use, expiry and scope,
        3. commit ``status = consumed`` durably (``os.replace`` + ``fsync``),
        4. append the ``consumed`` audit entry.

        Step 3 is the authoritative single-use transition; step 4 is required
        by it.  If the audit entry cannot be written the grant still counts as
        spent and this raises ``host_authorization_audit_failed``: the run is
        refused, because an execution nobody can find in the audit trail is
        worse than a burned grant.  The grant is never restored, including
        when the test run itself fails.
        """
        if token is None or (isinstance(token, str) and not token.strip()):
            self._audit_quietly(
                "rejected",
                grant_id=None,
                workspace=workspace,
                target=target,
                reason=REQUIRED,
            )
            raise HostAuthorizationError(
                REQUIRED, "host execution requires a one-time grant token"
            )
        match = _TOKEN_PATTERN.fullmatch(token) if isinstance(token, str) else None
        if match is None:
            self._audit_quietly(
                "rejected",
                grant_id=None,
                workspace=workspace,
                target=target,
                reason=INVALID,
            )
            raise HostAuthorizationError(
                INVALID, "host authorization token is malformed"
            )
        grant_id, secret = match.group(1), match.group(2)
        self._ensure_root()
        with _exclusive_lock(self._lock_path(grant_id)):
            # Taken under the lock on purpose: the recorded moment must
            # reflect the order the lock actually granted, otherwise two
            # racers (consume vs revoke) get timestamps that contradict the
            # real sequence and the audit trail lies about who went first.
            moment = now or datetime.now(timezone.utc)
            record = self._read_record(grant_id)
            if record is None:
                self._reject(grant_id, workspace, target, INVALID, at=moment)
            expected = str(record.get("secret_sha256") or "")
            actual = hashlib.sha256(secret.encode("utf-8")).hexdigest()
            if not expected or not hmac.compare_digest(expected, actual):
                self._reject(grant_id, workspace, target, INVALID, at=moment)
            try:
                self._check_usable(
                    record,
                    workspace=workspace,
                    target=target,
                    moment=moment,
                )
            except HostAuthorizationError as exc:
                self._audit_quietly(
                    "expired" if exc.code == EXPIRED else "rejected",
                    grant_id=grant_id,
                    workspace=workspace,
                    target=target,
                    issuer=record.get("issuer"),
                    reason=exc.code,
                    at=moment,
                )
                raise
            consumed = dict(record)
            consumed.update(
                {
                    "status": "consumed",
                    "consumed_at": _isoformat(moment),
                    "consumed_workspace": workspace,
                    "consumed_target": target,
                }
            )
            self._write_record(consumed)
            # Strict on purpose: no execution without a durable audit entry.
            self._audit(
                "consumed",
                grant_id=grant_id,
                workspace=workspace,
                target=target,
                issuer=record.get("issuer"),
                at=moment,
            )
        return {
            key: value
            for key, value in consumed.items()
            if key != "secret_sha256"
        }

    def _reject(
        self,
        grant_id: str,
        workspace: str,
        target: str,
        reason: str,
        *,
        at: datetime | None = None,
    ) -> None:
        # A broken audit log must never turn a rejection into a permission,
        # and must not mask the real rejection code either.
        self._audit_quietly(
            "rejected",
            grant_id=grant_id,
            workspace=workspace,
            target=target,
            reason=reason,
            at=at,
        )
        raise HostAuthorizationError(
            reason, "host authorization token is not recognised"
        )

    def revoke(
        self,
        grant_id: str,
        *,
        issuer: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Explicitly revoke a grant so it can never be consumed."""
        if not isinstance(grant_id, str) or not _GRANT_ID_PATTERN.fullmatch(
            grant_id
        ):
            raise HostAuthorizationError(
                INVALID, "grant id must be 32 lowercase hex characters"
            )
        if not isinstance(issuer, str) or not issuer.strip():
            raise HostAuthorizationError(
                INVALID, "issuer must be a bounded non-empty label"
            )
        self._ensure_root()
        with _exclusive_lock(self._lock_path(grant_id)):
            moment = now or datetime.now(timezone.utc)
            record = self._read_record(grant_id)
            if record is None:
                raise HostAuthorizationError(
                    INVALID, "host authorization grant does not exist"
                )
            updated = dict(record)
            if not updated.get("revoked_at"):
                updated["revoked_at"] = _isoformat(moment)
                updated["revoked_by"] = issuer.strip()
                self._write_record(updated)
            # The revocation record is authoritative; the audit line is
            # required by it. Writing it only when missing makes a retry
            # repair an earlier audit failure instead of silently succeeding.
            if not self._has_audit("revoked", grant_id):
                self._audit(
                    "revoked",
                    grant_id=grant_id,
                    workspace=updated.get("workspace"),
                    target=updated.get("target"),
                    issuer=issuer.strip(),
                    at=moment,
                )
        return {
            key: value
            for key, value in updated.items()
            if key != "secret_sha256"
        }

    def list_grants(self) -> list[dict[str, Any]]:
        """List grant metadata (never the secret) for operator review."""
        if not self.root.exists():
            return []
        grants: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json")):
            record = self._read_record(path.stem)
            if record is None:
                continue
            grants.append(
                {
                    key: value
                    for key, value in record.items()
                    if key != "secret_sha256"
                }
            )
        return grants


class _exclusive_lock:
    """Cross-process exclusive lock on a grant record."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> "_exclusive_lock":
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX
            raise HostAuthorizationError(
                INVALID, "host authorization requires POSIX file locking"
            ) from None
        self._descriptor = os.open(
            str(self._path), os.O_RDWR | os.O_CREAT, 0o600
        )
        fcntl.flock(self._descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._descriptor is None:
            return
        try:
            import fcntl

            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._descriptor)
            self._descriptor = None


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _os_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # pragma: no cover - environment dependent
        return "unknown"


def default_issuer() -> str:
    """Return the default issuer label (the current OS user)."""
    return _os_user()


def require_interactive_issuer() -> None:
    """Human-only guard for grant issuance.

    This is a guardrail, not a security boundary: a process with shell access
    can fake a TTY. The real constraints are single use, short expiry, scope
    binding and the audit record this module writes.
    """
    if not sys.stdin.isatty():
        raise HostAuthorizationError(
            "host_authorization_issuance_requires_tty",
            "grant issuance requires an interactive terminal"
            " (a human decision, not an agent action)",
        )


def grant_root_permissions(
    store: HostGrantStore,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Return the storage location facts an auditor needs."""
    root = store.root
    if not root.exists():
        return {"root": str(root), "exists": False}
    stat_result = root.stat()
    payload: dict[str, Any] = {
        "root": str(root),
        "exists": True,
        "mode": oct(stat_result.st_mode & 0o777),
    }
    if project_root is not None:
        payload["outside_repository"] = not _is_within(root, project_root)
    return payload


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        return candidate.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
