"""Phase 3.69: evidence provenance, trust roots and tamper boundaries."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.test_lifecycle_audit_coverage import _host_test_task  # noqa: E402
from zevorinth.controlled_recovery import (  # noqa: E402
    ASSURANCE_LEVELS,
    CONTROLLED_RESTORE_ACTION,
    CONTROLLED_RESTORE_CONTENT,
    audit_controlled_recovery,
    assurance_level,
    controlled_recovery_evidence_digest,
    execute_controlled_restore,
    with_controlled_restore_action,
)
from zevorinth.recovery_preview import (  # noqa: E402
    RecoveryApprovalStore,
    approval_record_digest,
    approve_recovery_preview,
    build_recovery_preview,
)


NOW = "2026-10-20T00:00:00.000+00:00"
LATER = "2026-10-20T00:05:00.000+00:00"


class EvidenceTrustBoundaryTests(unittest.TestCase):
    def _consumed(self):
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
        result = execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        self.assertEqual(result["status"], "succeeded")
        return service, workspace, store, approval

    @staticmethod
    def _rewrite(store: RecoveryApprovalStore, approval):
        repaired = replace(
            approval, record_digest=approval_record_digest(approval)
        )
        store._write_record(repaired)
        return repaired

    def test_audit_distinguishes_consistency_from_authenticity(self) -> None:
        _, workspace, store, approval = self._consumed()
        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace, now=NOW
        )
        self.assertEqual(audit["status"], "consistent")
        self.assertEqual(audit["assurance_level"], "L1")
        self.assertEqual(
            audit["assurance"]["label"], "internal_consistency_only"
        )
        self.assertEqual(
            audit["authenticity"], "not_authenticated"
        )
        self.assertEqual(audit["trust_root"], "none")
        self.assertFalse(audit["same_privilege_rewrite_protected"])
        self.assertEqual(
            audit["evidence_claims"],
            {
                "integrity": "digest_detects_corruption_only",
                "consistency": "audited",
                "authenticity": "not_authenticated",
                "same_privilege_tamper": "not_defended",
            },
        )
        self.assertEqual(
            set(audit["evidence_sources"]),
            {
                "approval_record",
                "intent",
                "execution",
                "verification",
                "completion",
                "current_observation",
            },
        )
        self.assertTrue(
            all(
                item["authenticated"] is False
                for item in audit["evidence_sources"].values()
            )
        )
        self.assertIn(
            "claim_authenticity",
            audit["assurance"]["prohibited_behavior"],
        )
        self.assertIn(
            "automatic_retry",
            audit["assurance"]["prohibited_behavior"],
        )

    def test_single_record_tamper_in_isolated_copy_is_corrupt(self) -> None:
        service, workspace, store, approval = self._consumed()
        self.assertTrue(
            service.project_root.resolve().is_relative_to(
                Path(tempfile.gettempdir()).resolve()
            )
        )
        current = store.load(approval.approval_id)
        result = json.loads(json.dumps(current.result))
        result["target"] = "file:tampered"
        malformed = replace(current, result=result)
        store._write_record(malformed)

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace, now=NOW
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertEqual(audit["code"], "approval_corrupt")
        self.assertEqual(audit["assurance_level"], "L0")
        self.assertFalse(audit["same_privilege_rewrite_protected"])

    def test_coordinated_partial_rewrite_is_still_detected(self) -> None:
        _, workspace, store, approval = self._consumed()
        current = store.load(approval.approval_id)
        result = json.loads(json.dumps(current.result))
        intent = json.loads(json.dumps(current.execution_intent))
        intent["workspace_root"] = "/forged/workspace"
        result["intent"] = intent
        digest = controlled_recovery_evidence_digest(
            intent, result["execution"], result["verification"]
        )
        result["evidence_digest"] = digest
        forged = replace(
            current,
            execution_intent=intent,
            result=result,
            verification_result=(
                "coordinated rewrite "
                f"evidence_sha256={digest}"
            ),
        )
        self._rewrite(store, forged)

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace, now=NOW
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertEqual(audit["reason"], "stored_evidence_inconsistent")
        self.assertIn(
            "intent_workspace_root_mismatch", audit["evidence_issues"]
        )
        self.assertEqual(audit["authenticity"], "not_authenticated")

    def test_full_same_privilege_rewrite_is_not_authenticated(self) -> None:
        _, workspace, store, approval = self._consumed()
        marker = (
            workspace
            / "controlled-recovery"
            / "restored-marker.txt"
        )
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
                "fully rewritten local evidence "
                f"evidence_sha256={digest}"
            ),
        )
        forged = self._rewrite(store, forged)

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace, now=NOW
        )

        self.assertEqual(audit["status"], "consistent")
        self.assertEqual(audit["approval_record_digest"], forged.record_digest)
        self.assertEqual(audit["authenticity"], "not_authenticated")
        self.assertFalse(audit["same_privilege_rewrite_protected"])
        self.assertFalse(audit["security_certification"])

    def test_missing_trust_root_is_reported_for_all_decisions(self) -> None:
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
        approved = audit_controlled_recovery(
            store, approval.approval_id, workspace, now=NOW
        )
        self.assertEqual(approved["status"], "not_claimed")
        self.assertEqual(approved["assurance_level"], "L1")
        self.assertEqual(approved["trust_root"], "none")

        store.revoke(
            approval.approval_id,
            reason="operator revoked",
            now=LATER,
        )
        revoked = audit_controlled_recovery(
            store, approval.approval_id, workspace, now=NOW
        )
        self.assertEqual(revoked["status"], "denied")
        self.assertEqual(revoked["assurance_level"], "L1")
        self.assertEqual(revoked["trust_root"], "none")
        self.assertEqual(revoked["authenticity"], "not_authenticated")

    def test_unimplemented_assurance_levels_cannot_claim_trust(self) -> None:
        for level in ("L2", "L3"):
            with self.subTest(level=level):
                definition = assurance_level(level)
                self.assertFalse(definition["implemented"])
                self.assertFalse(definition["evidence_authenticated"])
                self.assertFalse(definition["security_certification"])
                self.assertEqual(
                    ASSURANCE_LEVELS[level]["allowed_behavior"], []
                )

    def test_l0_and_l1_behavior_contracts_deny_automation(self) -> None:
        for level in ("L0", "L1"):
            with self.subTest(level=level):
                definition = assurance_level(level)
                self.assertTrue(definition["implemented"])
                self.assertFalse(definition["security_certification"])
                self.assertIn(
                    "automatic_retry",
                    definition["prohibited_behavior"],
                )
                self.assertIn(
                    "automatic_cleanup",
                    definition["prohibited_behavior"],
                )


if __name__ == "__main__":
    unittest.main()
