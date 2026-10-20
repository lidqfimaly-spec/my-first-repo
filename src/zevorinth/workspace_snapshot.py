"""Bounded, deterministic snapshots and side-effect diffs for a workspace.

The scanner never follows symlinks.  Anything it cannot inspect is recorded
as unreadable or as a limit hit, and the snapshot is marked incomplete; an
incomplete snapshot is never treated as "no changes".
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping


MAX_FILES = 2000
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 5.0
MAX_REPORTED_CHANGES = 200

KIND_FILE = "file"
KIND_DIR = "dir"
KIND_SYMLINK = "symlink"
KIND_OTHER = "other"

EXPECTED = "expected"
UNEXPECTED = "unexpected"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"
CLASSIFICATIONS = frozenset({EXPECTED, UNEXPECTED, UNKNOWN, NOT_APPLICABLE})


class SnapshotError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Entry:
    relative_path: str
    kind: str
    size: int
    sha256: str | None
    link_target: str | None
    mtime_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
            "link_target": self.link_target,
            "mtime_ns": self.mtime_ns,
        }


@dataclass(slots=True)
class Snapshot:
    workspace: str
    task_id: str
    attempt_id: str
    claim_generation: int
    captured_at: str
    complete: bool
    entries: list[Entry] = field(default_factory=list)
    unreadable: list[dict[str, str]] = field(default_factory=list)
    limits_hit: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "claim_generation": self.claim_generation,
            "captured_at": self.captured_at,
            "complete": self.complete,
            "entries": [entry.to_dict() for entry in self.entries],
            "unreadable": list(self.unreadable),
            "limits_hit": list(self.limits_hit),
            "digest": digest(self.entries),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Snapshot":
        return cls(
            workspace=str(data["workspace"]),
            task_id=str(data["task_id"]),
            attempt_id=str(data["attempt_id"]),
            claim_generation=int(data["claim_generation"]),
            captured_at=str(data["captured_at"]),
            complete=bool(data["complete"]),
            entries=[
                Entry(
                    relative_path=str(item["path"]),
                    kind=str(item["kind"]),
                    size=int(item["size"]),
                    sha256=(
                        str(item["sha256"]) if item.get("sha256") else None
                    ),
                    link_target=item.get("link_target"),
                    mtime_ns=int(item["mtime_ns"]),
                )
                for item in data.get("entries", [])
            ],
            unreadable=[dict(item) for item in data.get("unreadable", [])],
            limits_hit=[str(item) for item in data.get("limits_hit", [])],
        )


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def digest(entries: list[Entry]) -> str:
    payload = canonical_json([entry.to_dict() for entry in entries])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_bytes(path: str, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def scan_workspace(
    workspace: Path,
    *,
    workspace_id: str,
    task_id: str,
    attempt_id: str,
    claim_generation: int,
    captured_at: str,
    cancel_check: Callable[[], bool] | None = None,
    max_files: int = MAX_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_seconds: float = MAX_SECONDS,
) -> Snapshot:
    """Scan ``workspace`` without following symlinks or escaping the root."""
    root = workspace.resolve()
    started = time.monotonic()
    entries: list[Entry] = []
    unreadable: list[dict[str, str]] = []
    limits: list[str] = []
    complete = True
    total_bytes = 0
    stack: list[tuple[str, str]] = [("", str(root))]

    while stack:
        relative_dir, directory = stack.pop()
        if cancel_check is not None and cancel_check():
            limits.append("cancelled")
            complete = False
            break
        if time.monotonic() - started > max_seconds:
            limits.append("time_limit")
            complete = False
            break
        try:
            iterator = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            unreadable.append(
                {"path": relative_dir or ".", "reason": type(exc).__name__}
            )
            complete = False
            continue
        for item in iterator:
            relative = (
                f"{relative_dir}/{item.name}" if relative_dir else item.name
            )
            if len(entries) >= max_files:
                limits.append("file_limit")
                complete = False
                break
            try:
                stat = item.stat(follow_symlinks=False)
            except OSError as exc:
                unreadable.append(
                    {"path": relative, "reason": type(exc).__name__}
                )
                complete = False
                continue
            if item.is_symlink():
                try:
                    target = os.readlink(item.path)
                except OSError as exc:
                    unreadable.append(
                        {"path": relative, "reason": type(exc).__name__}
                    )
                    complete = False
                    continue
                entries.append(
                    Entry(
                        relative,
                        KIND_SYMLINK,
                        0,
                        None,
                        target,
                        stat.st_mtime_ns,
                    )
                )
            elif item.is_dir(follow_symlinks=False):
                entries.append(
                    Entry(relative, KIND_DIR, 0, None, None, stat.st_mtime_ns)
                )
                stack.append((relative, item.path))
            elif item.is_file(follow_symlinks=False):
                if stat.st_size > max_file_bytes:
                    limits.append(f"file_too_large:{relative}")
                    complete = False
                    entries.append(
                        Entry(
                            relative,
                            KIND_FILE,
                            stat.st_size,
                            None,
                            None,
                            stat.st_mtime_ns,
                        )
                    )
                    continue
                try:
                    data = _read_bytes(item.path, max_file_bytes)
                except OSError as exc:
                    unreadable.append(
                        {"path": relative, "reason": type(exc).__name__}
                    )
                    complete = False
                    continue
                try:
                    after_stat = os.stat(
                        item.path, follow_symlinks=False
                    )
                except OSError:
                    limits.append(f"vanished:{relative}")
                    complete = False
                    continue
                if (after_stat.st_size, after_stat.st_mtime_ns) != (
                    stat.st_size,
                    stat.st_mtime_ns,
                ):
                    limits.append(f"unstable:{relative}")
                    complete = False
                total_bytes += len(data)
                if total_bytes > max_total_bytes:
                    limits.append("total_bytes_limit")
                    complete = False
                entries.append(
                    Entry(
                        relative,
                        KIND_FILE,
                        len(data),
                        hashlib.sha256(data).hexdigest(),
                        None,
                        stat.st_mtime_ns,
                    )
                )
            else:
                entries.append(
                    Entry(
                        relative,
                        KIND_OTHER,
                        stat.st_size,
                        None,
                        None,
                        stat.st_mtime_ns,
                    )
                )
        if not complete and "file_limit" in limits:
            break

    entries.sort(key=lambda entry: entry.relative_path)
    return Snapshot(
        workspace=workspace_id,
        task_id=task_id,
        attempt_id=attempt_id,
        claim_generation=claim_generation,
        captured_at=captured_at,
        complete=complete,
        entries=entries,
        unreadable=unreadable,
        limits_hit=limits,
    )


def _normalize_prefix(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotError("invalid_policy_path", "prefix must be a string")
    if value.startswith("/") or "\\" in value:
        raise SnapshotError(
            "invalid_policy_path", "prefix must be a relative posix path"
        )
    parts = [part for part in PurePosixPath(value).parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise SnapshotError(
            "invalid_policy_path", "prefix must not traverse upward"
        )
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class EffectPolicy:
    """Classify workspace changes for one task."""

    allowed_prefixes: tuple[str, ...] = ()
    protected_prefixes: tuple[str, ...] = ()

    @classmethod
    def build(cls, parameters: Mapping[str, Any]) -> "EffectPolicy":
        allowed = parameters.get("allowed_side_effects") or []
        protected = parameters.get("protected_paths") or []
        if not isinstance(allowed, (list, tuple)) or not isinstance(
            protected, (list, tuple)
        ):
            raise SnapshotError(
                "invalid_policy", "side-effect lists must be arrays"
            )
        return cls(
            allowed_prefixes=tuple(_normalize_prefix(v) for v in allowed),
            protected_prefixes=tuple(_normalize_prefix(v) for v in protected),
        )

    @staticmethod
    def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
        return any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in prefixes
        )

    def classify(self, path: str, change: str) -> tuple[str, str]:
        if self._matches(path, self.protected_prefixes):
            return UNEXPECTED, "protected_path"
        if not self.allowed_prefixes:
            return UNEXPECTED, "read_only_policy"
        if self._matches(path, self.allowed_prefixes):
            return EXPECTED, "allowed_prefix"
        return UNEXPECTED, "outside_allowed_scope"


def _change_kind(before: Entry | None, after: Entry | None) -> str | None:
    if before is None and after is not None:
        return "added"
    if before is not None and after is None:
        return "deleted"
    if before is None or after is None:
        return None
    if before.kind != after.kind:
        return "type_changed"
    if before.kind == KIND_SYMLINK and before.link_target != after.link_target:
        return "symlink_changed"
    if before.kind == KIND_FILE and before.sha256 != after.sha256:
        return "modified"
    if before.kind == KIND_FILE and before.size != after.size:
        return "modified"
    return None


def diff_snapshots(
    before: Snapshot,
    after: Snapshot,
    policy: EffectPolicy,
    *,
    limit: int = MAX_REPORTED_CHANGES,
) -> dict[str, Any]:
    """Compute a deterministic, policy-classified diff of two snapshots."""
    before_entries = {entry.relative_path: entry for entry in before.entries}
    after_entries = {entry.relative_path: entry for entry in after.entries}
    changes: list[dict[str, Any]] = []
    for path in sorted(set(before_entries) | set(after_entries)):
        old = before_entries.get(path)
        new = after_entries.get(path)
        change = _change_kind(old, new)
        if change is None:
            continue
        classification, reason = policy.classify(path, change)
        changes.append(
            {
                "path": path,
                "change": change,
                "classification": classification,
                "reason": reason,
                "before_sha256": old.sha256 if old else None,
                "after_sha256": new.sha256 if new else None,
                "before_size": old.size if old else None,
                "after_size": new.size if new else None,
            }
        )
    total = len(changes)
    truncated = total > limit
    reported = changes[:limit]
    counts = {
        "added": sum(1 for c in changes if c["change"] == "added"),
        "modified": sum(1 for c in changes if c["change"] == "modified"),
        "deleted": sum(1 for c in changes if c["change"] == "deleted"),
        "type_changed": sum(
            1 for c in changes if c["change"] == "type_changed"
        ),
        "symlink_changed": sum(
            1 for c in changes if c["change"] == "symlink_changed"
        ),
        "expected": sum(
            1 for c in changes if c["classification"] == EXPECTED
        ),
        "unexpected": sum(
            1 for c in changes if c["classification"] == UNEXPECTED
        ),
        "unknown": sum(
            1 for c in changes if c["classification"] == UNKNOWN
        ),
    }
    complete = before.complete and after.complete
    return {
        "complete": complete,
        "before_digest": digest(before.entries),
        "after_digest": digest(after.entries),
        "change_count": total,
        "counts": counts,
        "changes": reported,
        "changes_truncated": truncated,
        "unknown_regions": {
            "before_unreadable": list(before.unreadable),
            "after_unreadable": list(after.unreadable),
            "before_limits": list(before.limits_hit),
            "after_limits": list(after.limits_hit),
        },
    }


def validate_diff_record(diff: Mapping[str, Any]) -> None:
    """Fail closed when a diff record contradicts its own detail list."""
    counts = diff.get("counts")
    changes = diff.get("changes")
    if not isinstance(counts, dict) or not isinstance(changes, list):
        raise SnapshotError("invalid_diff", "diff lacks counts or changes")
    total = int(diff.get("change_count", -1))
    if total < 0:
        raise SnapshotError("invalid_diff", "change_count is missing")
    change_total = sum(
        int(counts.get(name, 0))
        for name in (
            "added",
            "modified",
            "deleted",
            "type_changed",
            "symlink_changed",
        )
    )
    classification_total = sum(
        int(counts.get(name, 0))
        for name in ("expected", "unexpected", "unknown")
    )
    if change_total != total or classification_total != total:
        raise SnapshotError(
            "invalid_diff", "counts do not sum to change_count"
        )
    if bool(diff.get("changes_truncated")):
        if len(changes) > total:
            raise SnapshotError(
                "invalid_diff", "truncated detail exceeds change_count"
            )
        return
    if total != len(changes):
        raise SnapshotError(
            "invalid_diff", "change_count does not match detail length"
        )
    for name in ("added", "modified", "deleted", "type_changed", "symlink_changed"):
        if counts.get(name) != sum(
            1 for item in changes if item.get("change") == name
        ):
            raise SnapshotError(
                "invalid_diff", f"count mismatch for {name}"
            )
    for name in ("expected", "unexpected", "unknown"):
        if counts.get(name) != sum(
            1 for item in changes if item.get("classification") == name
        ):
            raise SnapshotError(
                "invalid_diff", f"count mismatch for {name}"
            )
