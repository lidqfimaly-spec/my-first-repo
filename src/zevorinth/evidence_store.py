"""Persistent, integrity-checked storage for isolation evidence.

Threat model (documented, not overstated):

* **corruption detection** — canonical JSON + SHA-256 + schema check;
* **NOT** provenance authentication: there is no signing key;
* **NOT** tamper resistance: a principal who can write these files can also
  rewrite the digest.

Records live under ``<root>/evidence/`` with names derived from
``evidence_id`` only; ids are restricted to ``[A-Za-z0-9_-]``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .isolation_evidence import (
    REQUIRED_EVIDENCE_FIELDS,
    evidence_digest,
    verify_evidence,
)


EVIDENCE_DIR = "evidence"
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
INVALIDATION_SUFFIX = ".invalidated"


class EvidenceStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        + "\n"
    ).encode("utf-8")


def _validate_id(evidence_id: str) -> str:
    if not isinstance(evidence_id, str) or not _ID_PATTERN.fullmatch(
        evidence_id
    ):
        raise EvidenceStoreError(
            "invalid_evidence_id",
            "evidence id must be 1-128 chars of [A-Za-z0-9_-]",
        )
    return evidence_id


class EvidenceStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.directory = self.root / EVIDENCE_DIR

    # -- internal helpers -------------------------------------------------
    def _ensure_directory(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise EvidenceStoreError(
                "store_unavailable", type(exc).__name__
            ) from exc

    def _path(self, evidence_id: str) -> Path:
        return self.directory / f"{_validate_id(evidence_id)}.json"

    def _invalidation_path(self, evidence_id: str) -> Path:
        return self.directory / (
            f"{_validate_id(evidence_id)}{INVALIDATION_SUFFIX}"
        )

    # -- public API -------------------------------------------------------
    def save_evidence(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically persist one record; never returns an unsaved id."""
        missing = [k for k in REQUIRED_EVIDENCE_FIELDS if k not in evidence]
        if missing:
            raise EvidenceStoreError(
                "evidence_incomplete", ",".join(missing)
            )
        if evidence_digest(evidence) != evidence.get("digest"):
            raise EvidenceStoreError(
                "evidence_digest_mismatch", "record digest does not match"
            )
        evidence_id = _validate_id(str(evidence["evidence_id"]))
        self._ensure_directory()
        target = self._path(evidence_id)
        if target.exists():
            existing = self.load_evidence(evidence_id)
            if existing["digest"] == evidence["digest"]:
                return {"status": "unchanged", "evidence_id": evidence_id}
            raise EvidenceStoreError(
                "evidence_conflict",
                "a different record already uses this evidence id",
            )
        payload = _canonical(evidence)
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{evidence_id}.", dir=str(self.directory)
            )
        except OSError as exc:
            raise EvidenceStoreError(
                "store_write_failed", type(exc).__name__
            ) from exc
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            try:
                dir_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except OSError as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise EvidenceStoreError(
                "store_write_failed", type(exc).__name__
            ) from exc
        stored = self.load_evidence(evidence_id)
        if stored["digest"] != evidence["digest"]:
            raise EvidenceStoreError(
                "store_verification_failed", "reloaded digest differs"
            )
        return {"status": "saved", "evidence_id": evidence_id}

    def load_evidence(self, evidence_id: str) -> dict[str, Any]:
        path = self._path(evidence_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise EvidenceStoreError(
                "evidence_missing", f"no record for {evidence_id}"
            ) from exc
        except OSError as exc:
            raise EvidenceStoreError(
                "store_unavailable", type(exc).__name__
            ) from exc
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceStoreError(
                "evidence_corrupt", "record is not valid JSON"
            ) from exc
        if not isinstance(record, dict):
            raise EvidenceStoreError(
                "evidence_corrupt", "record is not an object"
            )
        missing = [k for k in REQUIRED_EVIDENCE_FIELDS if k not in record]
        if missing:
            raise EvidenceStoreError(
                "evidence_incomplete", ",".join(missing)
            )
        if evidence_digest(record) != record.get("digest"):
            raise EvidenceStoreError(
                "evidence_digest_mismatch", "stored digest does not match"
            )
        if self._invalidation_path(evidence_id).exists():
            reason_path = self._invalidation_path(evidence_id)
            try:
                reason = json.loads(
                    reason_path.read_text(encoding="utf-8")
                ).get("reason", "invalidated")
            except Exception:
                reason = "invalidated"
            record = dict(record)
            record["invalidated"] = True
            record["invalidation_reason"] = reason
        return record

    def invalidate_evidence(
        self, evidence_id: str, reason: str
    ) -> dict[str, Any]:
        self._path(evidence_id)  # validates the id
        self.load_evidence(evidence_id)  # must exist and be intact
        self._ensure_directory()
        payload = _canonical({"reason": str(reason)})
        target = self._invalidation_path(evidence_id)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{evidence_id}.inv.", dir=str(self.directory)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        except OSError as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise EvidenceStoreError(
                "store_write_failed", type(exc).__name__
            ) from exc
        return {"status": "invalidated", "evidence_id": evidence_id}

    def list_evidence(self) -> list[dict[str, Any]]:
        if not self.directory.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            if path.name.endswith(INVALIDATION_SUFFIX + ".json"):
                continue
            evidence_id = path.stem
            try:
                record = self.load_evidence(evidence_id)
            except EvidenceStoreError as exc:
                entries.append(
                    {
                        "evidence_id": evidence_id,
                        "status": "invalid",
                        "reason": exc.code,
                    }
                )
                continue
            entries.append(
                {
                    "evidence_id": evidence_id,
                    "status": (
                        "invalidated"
                        if record.get("invalidated")
                        else "stored"
                    ),
                    "request_id": record.get("request_id"),
                    "issued_at": record.get("issued_at"),
                    "expires_at": record.get("expires_at"),
                    "digest": record.get("digest"),
                }
            )
        return entries

    def verify_stored_evidence(
        self,
        evidence_id: str,
        *,
        expected_runtime: str,
        expected_image_digest: str,
        expected_config_digest: str,
        expected_request_id: str | None = None,
        expected_command_digest: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Load, re-check integrity, then run the Phase 3.27 verifier."""
        try:
            record = self.load_evidence(evidence_id)
        except EvidenceStoreError as exc:
            return {
                "state": "blocked",
                "reasons": [exc.code],
                "verifier_version": None,
            }
        if record.get("invalidated"):
            return {
                "state": "blocked",
                "reasons": ["evidence_invalidated"],
                "verifier_version": None,
            }
        return verify_evidence(
            record,
            expected_runtime=expected_runtime,
            expected_image_digest=expected_image_digest,
            expected_config_digest=expected_config_digest,
            expected_request_id=expected_request_id,
            expected_command_digest=expected_command_digest,
            now=now,
        )
