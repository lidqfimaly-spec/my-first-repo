"""Phase 3.68: audit decision completeness and tamper boundaries."""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.test_lifecycle_audit_coverage import _host_test_task  # noqa: E402
from zevorinth.controlled_recovery import (  # noqa: E402
    CONTROLLED_RESTORE_ACTION,
    CONTROLLED_RESTORE_CONTENT,
    audit_controlled_recovery,
    controlled_recovery_evidence_digest,
    execute_controlled_restore,
    with_controlled_restore_action,
)
from zevorinth.recovery_preview import (  # noqa: E402
    RecoveryApprovalStore,
    approval_record_digest,
    approve_recovery_preview,
    build_recovery_preview,
    reject_recovery_preview,
)


NOW = "2026-10-20T00:00:00.000+00:00"
LATER = "2026-10-20T00:05:00.000+00:00"


class ControlledRecoveryPhase368Tests(unittest.TestCase):
    def _fixture(self):
        service, task, events = _host_test_task()
        workspace = service.project_root / "workspaces" / "demo"
        base = build_recovery_preview(task, events, now=NOW)
        preview = with_controlled_restore_action(base, workspace)
        action = next(
            item
            for item in preview.actions
            if item.action == CONTROLLED_RESTORE_ACTION
        )
        approval = approve_recovery_preview(
            preview,
            approver="operator",
            action=action.action,
            target=action.target,
            now=NOW,
        )
        store = RecoveryApprovalStore(service.project_root)
        store.save(approval)
        return service, task, workspace, preview, store, approval

    @staticmethod
    def _marker(workspace: Path) -> Path:
        return (
            workspace
            / "controlled-recovery"
            / "restored-marker.txt"
        )

    @staticmethod
    def _rewrite(store: RecoveryApprovalStore, approval):
        repaired = replace(
            approval, record_digest=approval_record_digest(approval)
        )
        store._write_record(repaired)
        return repaired

    def _consumed(self):
        fixture = self._fixture()
        service, task, workspace, preview, store, approval = fixture
        result = execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        self.assertEqual(result["status"], "succeeded")
        return fixture

    def test_approved_audit_revalidates_optional_freshness(self) -> None:
        service, task, workspace, preview, store, approval = self._fixture()
        current = audit_controlled_recovery(
            store,
            approval.approval_id,
            workspace,
            now=NOW,
            preview=preview,
        )
        self.assertEqual(current["status"], "not_claimed")
        self.assertEqual(current["approval_freshness"], "valid")

        service.store.record_event_and_update(
            task.task_id,
            "state_changed",
            LATER,
            updates={"phase": "changed_after_approval"},
        )
        report = service.recovery_preview(task.task_id, now=LATER)
        changed_base = build_recovery_preview(
            service.store.require_task(task.task_id),
            service.store.list_events(task.task_id),
            audit=report["audit"],
            now=LATER,
        )
        changed_preview = with_controlled_restore_action(
            changed_base, workspace
        )
        stale = audit_controlled_recovery(
            store,
            approval.approval_id,
            workspace,
            now=LATER,
            preview=changed_preview,
        )
        self.assertEqual(stale["status"], "unverified")
        self.assertEqual(stale["reason"], "approval_freshness_invalid")
        self.assertTrue(stale["manual_review"])
        self.assertFalse(stale["safe_to_retry"])

    def test_approval_lifecycle_shape_contradiction_is_rejected(self) -> None:
        _, _, workspace, _, store, approval = self._fixture()
        malformed = replace(
            approval,
            execution_attempt_id="attempt-without-claim",
            claimed_at=LATER,
        )
        self._rewrite(store, malformed)

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertEqual(audit["reason"], "approval_state_inconsistent")
        self.assertIn(
            "approved_has_execution_attempt_id", audit["approval_issues"]
        )
        self.assertIn("approved_has_claimed_at", audit["approval_issues"])

    def test_consuming_intent_must_match_expected_schema(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        claimed = store.claim(
            approval.approval_id,
            preview,
            action=CONTROLLED_RESTORE_ACTION,
            target=approval.target or "",
            now=LATER,
        )
        self.assertEqual(claimed["status"], "claimed")
        recorded = store.record_intent(
            approval.approval_id,
            execution_attempt_id=claimed["execution_attempt_id"],
            intent={
                "execution_attempt_id": claimed["execution_attempt_id"],
                "action": CONTROLLED_RESTORE_ACTION,
                "target": approval.target,
                "action_digest": approval.action_digest,
            },
            now=LATER,
        )
        self.assertEqual(recorded["status"], "recorded")

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertEqual(audit["reason"], "execution_intent_inconsistent")
        self.assertIn(
            "intent_intent_version_mismatch", audit["intent_issues"]
        )
        self.assertIn(
            "intent_relative_path_mismatch", audit["intent_issues"]
        )
        self.assertTrue(audit["manual_review"])
        self.assertFalse(audit["safe_to_retry"])

    def test_failed_evidence_corruption_is_not_verified_failure(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()

        def tamper(stage: str) -> None:
            if stage == "after_side_effect":
                marker = self._marker(workspace)
                data = marker.read_bytes()
                marker.write_bytes(data[:-1] + b"X")

        result = execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
            checkpoint=tamper,
        )
        self.assertEqual(result["status"], "failed")
        current = store.load(approval.approval_id)
        result_data = json.loads(json.dumps(current.result))
        result_data["evidence_digest"] = "0" * 64
        malformed = replace(current, result=result_data)
        self._rewrite(store, malformed)

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertEqual(
            audit["reason"], "stored_failure_evidence_inconsistent"
        )
        self.assertIn("evidence_digest_mismatch", audit["evidence_issues"])

    def test_priority_action_binding_precedes_other_failures(self) -> None:
        _, _, workspace, _, store, approval = self._consumed()
        current = store.load(approval.approval_id)
        result = json.loads(json.dumps(current.result))
        result["evidence_digest"] = "0" * 64
        self._rewrite(store, replace(current, result=result))
        other = workspace.parent / "phase368-other"
        other.mkdir()

        audit = audit_controlled_recovery(
            store, approval.approval_id, other, now=LATER
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertEqual(
            audit["reason"], "controlled_action_binding_mismatch"
        )
        self.assertTrue(audit["manual_review"])

    def test_priority_stored_evidence_precedes_current_divergence(self) -> None:
        _, _, workspace, _, store, approval = self._consumed()
        current = store.load(approval.approval_id)
        result = json.loads(json.dumps(current.result))
        result["evidence_digest"] = "0" * 64
        self._rewrite(store, replace(current, result=result))
        self._marker(workspace).unlink()

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertEqual(audit["reason"], "stored_evidence_inconsistent")
        self.assertIn("evidence_digest_mismatch", audit["evidence_issues"])

    def test_missing_execution_evidence_is_never_consistent(self) -> None:
        _, _, workspace, _, store, approval = self._consumed()
        current = store.load(approval.approval_id)
        result = json.loads(json.dumps(current.result))
        del result["execution"]
        self._rewrite(store, replace(current, result=result))

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertEqual(audit["reason"], "stored_evidence_inconsistent")
        self.assertIn("execution_evidence_missing", audit["evidence_issues"])

    def test_result_envelope_mismatch_is_never_consistent(self) -> None:
        _, _, workspace, _, store, approval = self._consumed()
        current = store.load(approval.approval_id)
        result = json.loads(json.dumps(current.result))
        result["operation"] = "different_operation"
        self._rewrite(store, replace(current, result=result))

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertEqual(audit["reason"], "stored_evidence_inconsistent")
        self.assertIn(
            "result_operation_mismatch", audit["evidence_issues"]
        )

    def test_duplicate_completion_is_rejected_without_changing_audit(self) -> None:
        _, _, workspace, _, store, approval = self._consumed()
        current = store.load(approval.approval_id)
        duplicate = store.complete(
            approval.approval_id,
            execution_attempt_id=current.execution_attempt_id or "",
            result=current.result or {},
            verification_passed=True,
            verification_result=current.verification_result or "",
            now=LATER,
        )
        self.assertEqual(duplicate["status"], "denied")
        self.assertEqual(duplicate["approval_status"], "consumed")
        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )
        self.assertEqual(audit["status"], "consistent")

    def test_rejected_approval_is_denied_and_read_only(self) -> None:
        _, _, workspace, preview, store, _ = self._fixture()
        rejected = reject_recovery_preview(
            preview,
            approver="operator",
            reason="not approved",
            now=LATER,
        )
        store.save(rejected)
        path = (
            store.root
            / "recovery_approvals"
            / f"{rejected.approval_id}.json"
        )
        before = path.read_bytes()
        audit = audit_controlled_recovery(
            store, rejected.approval_id, workspace
        )
        self.assertEqual(audit["status"], "denied")
        self.assertFalse(audit["safe_to_retry"])
        self.assertEqual(path.read_bytes(), before)

    def test_same_privilege_full_rewrite_is_only_consistency_not_auth(
        self,
    ) -> None:
        _, _, workspace, _, store, approval = self._consumed()
        marker = self._marker(workspace)
        marker.unlink()
        marker.write_bytes(CONTROLLED_RESTORE_CONTENT)
        marker.chmod(0o600)
        stat = marker.stat()
        current = store.load(approval.approval_id)
        result = json.loads(json.dumps(current.result))
        for evidence in (result["execution"], result["verification"]):
            evidence["device"] = stat.st_dev
            evidence["inode"] = stat.st_ino
        digest = controlled_recovery_evidence_digest(
            result["intent"], result["execution"], result["verification"]
        )
        result["evidence_digest"] = digest
        forged = replace(
            current,
            result=result,
            verification_result=(
                "rewritten self-consistent evidence "
                f"evidence_sha256={digest}"
            ),
        )
        forged = self._rewrite(store, forged)

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )

        self.assertEqual(audit["status"], "consistent")
        self.assertFalse(audit["security_certification"])
        self.assertEqual(audit["approval_record_digest"], forged.record_digest)

    def test_accidental_field_change_without_digest_is_corrupt(self) -> None:
        _, _, workspace, _, store, approval = self._consumed()
        current = store.load(approval.approval_id)
        result = json.loads(json.dumps(current.result))
        result["target"] = "file:accidental-change"
        malformed = replace(current, result=result)
        store._write_record(malformed)

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertEqual(audit["reason"], "approval_store_unreadable")
        self.assertEqual(audit["code"], "approval_corrupt")


if __name__ == "__main__":
    unittest.main()
