"""One side-effect evidence schema shared by both write and test tools."""

from __future__ import annotations

from typing import Any, Mapping

from .workspace_snapshot import (
    EffectPolicy,
    Snapshot,
    SnapshotError,
    diff_snapshots,
    validate_diff_record,
)


SCHEMA_VERSION = "1.0"
OPERATIONS = frozenset({"write_text_artifact", "run_tests"})


def build_workspace_effects(
    *,
    operation: str,
    source: str,
    workspace: str,
    task_id: str,
    attempt_id: str,
    claim_generation: int,
    before: Snapshot,
    after: Snapshot,
    policy_parameters: Mapping[str, Any],
    cleanup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if operation not in OPERATIONS:
        raise SnapshotError("invalid_operation", f"unknown operation: {operation}")
    policy = EffectPolicy.build(policy_parameters)
    diff = diff_snapshots(before, after, policy)
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "source": source,
        "workspace": workspace,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "claim_generation": claim_generation,
        "applicable": True,
        "complete": before.complete and after.complete,
        "detail_complete": diff["detail_complete"],
        "total_changes": diff["change_count"],
        "retained_changes": diff["retained_count"],
        "truncated": diff["truncated"],
        "unclassified_changes": 0,
        "policy": {
            "allowed_side_effects": list(policy.allowed_prefixes),
            "protected_paths": list(policy.protected_prefixes),
        },
        "before": before.to_dict(),
        "after": after.to_dict(),
        "diff": diff,
        "cleanup": dict(cleanup) if cleanup else None,
    }


def validate_workspace_effects(record: Mapping[str, Any]) -> None:
    """Fail closed on any internally inconsistent effect record."""
    required = (
        "schema_version",
        "operation",
        "source",
        "workspace",
        "task_id",
        "attempt_id",
        "claim_generation",
        "applicable",
        "complete",
        "detail_complete",
        "total_changes",
        "retained_changes",
        "truncated",
        "policy",
        "before",
        "after",
        "diff",
    )
    missing = [key for key in required if key not in record]
    if missing:
        raise SnapshotError(
            "invalid_effects", f"missing fields: {missing}"
        )
    if record["operation"] not in OPERATIONS:
        raise SnapshotError("invalid_effects", "unknown operation")
    diff = record["diff"]
    if not isinstance(diff, Mapping):
        raise SnapshotError("invalid_effects", "diff is not an object")
    validate_diff_record(diff)
    truncated = bool(diff.get("changes_truncated"))
    if bool(record["truncated"]) != truncated:
        raise SnapshotError("invalid_effects", "truncation flags disagree")
    if bool(record["detail_complete"]) == truncated:
        raise SnapshotError(
            "invalid_effects", "detail_complete contradicts truncation"
        )
    if int(record["total_changes"]) != int(diff["change_count"]):
        raise SnapshotError("invalid_effects", "total_changes mismatch")
    if int(record["retained_changes"]) != len(diff["changes"]):
        raise SnapshotError("invalid_effects", "retained_changes mismatch")
    before = Snapshot.from_dict(record["before"])
    after = Snapshot.from_dict(record["after"])
    if bool(record["complete"]) != (before.complete and after.complete):
        raise SnapshotError("invalid_effects", "complete flag mismatch")
    if (
        before.task_id != record["task_id"]
        or after.task_id != record["task_id"]
        or before.attempt_id != record["attempt_id"]
        or after.attempt_id != record["attempt_id"]
        or before.claim_generation != record["claim_generation"]
        or after.claim_generation != record["claim_generation"]
    ):
        raise SnapshotError("invalid_effects", "identity mismatch")
    policy = record["policy"]
    recomputed = diff_snapshots(
        before,
        after,
        EffectPolicy.build(
            {
                "allowed_side_effects": policy.get("allowed_side_effects", []),
                "protected_paths": policy.get("protected_paths", []),
            }
        ),
    )
    if (
        recomputed["counts"] != dict(diff.get("counts", {}))
        or recomputed["changes"] != list(diff.get("changes", []))
    ):
        raise SnapshotError(
            "invalid_effects", "diff does not match the snapshots"
        )
