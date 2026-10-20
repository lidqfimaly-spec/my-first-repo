"""Guarded cleanup of files a task provably created.

Cleanup is disabled unless the caller explicitly enables and authorizes it.
Ownership comes from the persisted effect record, never from a directory
name, and every check is repeated immediately before deletion.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .workspace_effects import validate_workspace_effects
from .workspace_snapshot import EffectPolicy, SnapshotError


@dataclass(frozen=True, slots=True)
class OwnershipRecord:
    task_id: str
    attempt_id: str
    claim_generation: int
    workspace: str
    relative_path: str
    source: str
    created_sha256: str


def ownership_records(effects: Mapping[str, Any]) -> list[OwnershipRecord]:
    """Files this task created under an allowed policy."""
    validate_workspace_effects(effects)
    records: list[OwnershipRecord] = []
    for change in effects["diff"]["changes"]:
        if change["change"] != "added":
            continue
        if change["classification"] != "expected":
            continue
        if not change.get("after_sha256"):
            continue
        records.append(
            OwnershipRecord(
                task_id=str(effects["task_id"]),
                attempt_id=str(effects["attempt_id"]),
                claim_generation=int(effects["claim_generation"]),
                workspace=str(effects["workspace"]),
                relative_path=str(change["path"]),
                source=str(effects["source"]),
                created_sha256=str(change["after_sha256"]),
            )
        )
    return records


@dataclass(slots=True)
class CleanupDecision:
    allowed: bool
    code: str
    reason: str
    path: Path | None = None
    expected_sha256: str | None = None
    ownership: OwnershipRecord | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "relative_path": (
                self.ownership.relative_path if self.ownership else None
            ),
            "expected_sha256": self.expected_sha256,
        }


def plan_cleanup(
    *,
    effects: Mapping[str, Any],
    workspace_root: Path,
    relative_path: str,
    current_generation: int,
    enabled: bool,
    authorized: bool,
) -> CleanupDecision:
    """Decide whether ``relative_path`` may be deleted."""
    if not enabled:
        return CleanupDecision(False, "cleanup_disabled", "cleanup is off")
    if not authorized:
        return CleanupDecision(
            False, "cleanup_unauthorized", "cleanup needs explicit authorization"
        )
    try:
        validate_workspace_effects(effects)
    except SnapshotError as exc:
        return CleanupDecision(False, exc.code, exc.message)
    if int(effects["claim_generation"]) != int(current_generation):
        return CleanupDecision(
            False,
            "stale_generation",
            "cleanup requires the generation that created the file",
        )
    policy = EffectPolicy.build(
        {
            "allowed_side_effects": effects["policy"].get(
                "allowed_side_effects", []
            ),
            "protected_paths": effects["policy"].get("protected_paths", []),
        }
    )
    if policy._matches(relative_path, policy.protected_prefixes):
        return CleanupDecision(
            False, "protected_path", "path is protected from cleanup"
        )
    record = next(
        (
            item
            for item in ownership_records(effects)
            if item.relative_path == relative_path
        ),
        None,
    )
    if record is None:
        return CleanupDecision(
            False,
            "not_owned",
            "the task has no ownership record for this path",
        )
    root = workspace_root.resolve()
    target = root / relative_path
    if target.is_symlink():
        return CleanupDecision(
            False, "unsafe_path", "refusing to delete a symlink"
        )
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        return CleanupDecision(
            False, "missing_target", "target does not exist"
        )
    if root not in resolved.parents:
        return CleanupDecision(
            False, "unsafe_path", "target escapes the workspace"
        )
    if not resolved.is_file():
        return CleanupDecision(
            False, "not_a_file", "target is not a regular file"
        )
    actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if actual != record.created_sha256:
        return CleanupDecision(
            False,
            "hash_mismatch",
            "file changed since the task created it",
        )
    return CleanupDecision(
        True, "allowed", "owned and unchanged", resolved, actual, record
    )


def execute_cleanup(decision: CleanupDecision) -> dict[str, Any]:
    """Delete the planned target after one final identity re-check."""
    if not decision.allowed or decision.path is None:
        return {
            "status": "rejected",
            "code": decision.code,
            "reason": decision.reason,
            "removed": False,
        }
    path = decision.path
    if path.is_symlink() or not path.is_file():
        return {
            "status": "rejected",
            "code": "target_changed",
            "reason": "target changed after planning",
            "removed": False,
        }
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != decision.expected_sha256:
        return {
            "status": "rejected",
            "code": "target_changed",
            "reason": "content changed after planning",
            "removed": False,
        }
    try:
        os.unlink(path)
    except OSError as exc:
        return {
            "status": "failed",
            "code": "unlink_failed",
            "reason": type(exc).__name__,
            "removed": False,
        }
    gone = not path.exists() and not path.is_symlink()
    return {
        "status": "completed" if gone else "unverified",
        "code": "removed" if gone else "removal_unconfirmed",
        "reason": "file removed" if gone else "file still present",
        "removed": gone,
        "relative_path": decision.ownership.relative_path
        if decision.ownership
        else None,
        "sha256": decision.expected_sha256,
    }
