"""Phase 3.65: atomic recovery approval consumption without execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.test_lifecycle_audit_coverage import _host_test_task  # noqa: E402
from zevorinth.recovery_preview import (  # noqa: E402
    ApprovalStoreError,
    RecoveryPreview,
    RecoveryApprovalStore,
    approval_record_digest,
    approve_recovery_preview,
    build_recovery_preview,
    reject_recovery_preview,
)


NOW = "2026-10-20T00:00:00.000+00:00"
CLAIMED_AT = "2026-10-20T00:01:00.000+00:00"
COMPLETED_AT = "2026-10-20T00:02:00.000+00:00"
WORKER = PROJECT_ROOT / "tests" / "approval_consumption_worker.py"


class RecoveryConsumptionTests(unittest.TestCase):
    def _fixture(self):
        service, task, events = _host_test_task()
        preview = build_recovery_preview(task, events, now=NOW)
        action = preview.actions[0]
        root = Path(tempfile.mkdtemp(prefix="zv-consumption-"))
        store = RecoveryApprovalStore(root)
        approval = approve_recovery_preview(
            preview,
            approver="operator",
            action=action.action,
            target=action.target,
            now=NOW,
        )
        store.save(approval)
        return service, task, preview, root, store, approval

    def _claim(self, store, preview, approval):
        return store.claim(
            approval.approval_id,
            preview,
            action=approval.action or "",
            target=approval.target or "",
            now=CLAIMED_AT,
        )

    def test_claim_requires_exact_action_and_target(self) -> None:
        _, _, preview, _, store, approval = self._fixture()
        result = self._claim(store, preview, approval)
        self.assertEqual(result["status"], "claimed")
        self.assertFalse(result["execution_supported"])
        self.assertFalse(result["safe_to_retry"])
        reloaded = RecoveryApprovalStore(store.root).load(
            approval.approval_id
        )
        self.assertEqual(reloaded.status, "consuming")
        self.assertEqual(
            reloaded.execution_attempt_id,
            result["execution_attempt_id"],
        )
        self.assertEqual(reloaded.action, approval.action)
        self.assertEqual(reloaded.target, approval.target)
        self.assertEqual(
            reloaded.preconditions, approval.preconditions
        )
        self.assertEqual(
            reloaded.expected_result, approval.expected_result
        )
        self.assertEqual(reloaded.verification, approval.verification)
        self.assertEqual(reloaded.action_digest, approval.action_digest)

    def test_approval_binding_rejects_unknown_action(self) -> None:
        _, _, preview, _, _, _ = self._fixture()
        with self.assertRaises(ValueError):
            approve_recovery_preview(
                preview,
                approver="operator",
                action="invented_action",
                target=preview.actions[0].target,
                now=NOW,
            )

    def test_unbound_phase_364_style_approval_fails_closed(self) -> None:
        _, _, _, _, store, approval = self._fixture()
        candidates = (
            replace(
                approval,
                action=None,
                target=None,
                preconditions=(),
                expected_result=None,
                verification=None,
                action_digest=None,
            ),
            replace(approval, action_digest=None),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                unbound = replace(
                    candidate,
                    record_digest=approval_record_digest(candidate),
                )
                with self.assertRaises(ApprovalStoreError) as raised:
                    store.save(unbound)
                self.assertEqual(
                    raised.exception.code,
                    "approval_action_binding_missing",
                )

    def test_wrong_action_or_target_is_denied_without_claim(self) -> None:
        _, _, preview, _, store, approval = self._fixture()
        wrong_action = store.claim(
            approval.approval_id,
            preview,
            action="manual_reconcile_cleanup",
            target=approval.target or "",
            now=CLAIMED_AT,
        )
        self.assertEqual(wrong_action["status"], "denied")
        self.assertEqual(
            wrong_action["reason"], "approved_action_mismatch"
        )
        wrong_target = store.claim(
            approval.approval_id,
            preview,
            action=approval.action or "",
            target="task:other",
            now=CLAIMED_AT,
        )
        self.assertEqual(wrong_target["status"], "denied")
        self.assertEqual(
            wrong_target["reason"], "approved_action_mismatch"
        )
        self.assertEqual(
            store.load(approval.approval_id).status, "approved"
        )

    def test_preview_action_binding_drift_is_denied(self) -> None:
        _, _, preview, _, store, approval = self._fixture()
        action = preview.actions[0]
        changes = (
            {"operation": "changed after approval"},
            {"current_state": "changed after approval"},
            {"preconditions": ("changed after approval",)},
            {"expected_result": "changed after approval"},
            {"verification": "changed after approval"},
            {"risk": "critical"},
            {"executable": True},
        )
        for change in changes:
            with self.subTest(change=change):
                changed_action = replace(action, **change)
                changed_preview = replace(
                    preview,
                    actions=(changed_action, *preview.actions[1:]),
                )
                result = self._claim(store, changed_preview, approval)
                self.assertEqual(result["status"], "denied")
                self.assertEqual(
                    result["reason"], "action_binding_mismatch"
                )
        self.assertEqual(
            store.load(approval.approval_id).status, "approved"
        )

    def test_stale_preview_is_denied(self) -> None:
        service, task, _, _, store, approval = self._fixture()
        service.store.record_event_and_update(
            task.task_id,
            "state_changed",
            CLAIMED_AT,
            updates={"phase": "changed_before_claim"},
        )
        current = service.recovery_preview(task.task_id, now=CLAIMED_AT)
        changed_preview = build_recovery_preview(
            service.store.require_task(task.task_id),
            service.store.list_events(task.task_id),
            audit=current["audit"],
            now=CLAIMED_AT,
        )
        result = self._claim(store, changed_preview, approval)
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["approval_status"], "stale")
        self.assertEqual(
            store.load(approval.approval_id).status, "approved"
        )

    def test_non_acknowledgement_scope_cannot_be_claimed(self) -> None:
        _, _, preview, _, store, _ = self._fixture()
        action = preview.actions[0]
        approval = approve_recovery_preview(
            preview,
            approver="operator",
            action=action.action,
            target=action.target,
            now=CLAIMED_AT,
        )
        widened = replace(approval, scope="recovery_execution")
        widened = replace(
            widened, record_digest=approval_record_digest(widened)
        )
        store.save(widened)
        result = store.claim(
            widened.approval_id,
            preview,
            action=action.action,
            target=action.target,
            now=COMPLETED_AT,
        )
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["reason"], "approval_scope_mismatch")
        self.assertFalse(result["execution_supported"])
        self.assertEqual(
            store.load(widened.approval_id).status, "approved"
        )

    def test_revoked_and_rejected_records_cannot_be_claimed(self) -> None:
        _, _, preview, root, store, approval = self._fixture()
        revoked = store.revoke(
            approval.approval_id,
            reason="operator revoked",
            now=CLAIMED_AT,
        )
        revoked_result = self._claim(store, preview, revoked)
        self.assertEqual(revoked_result["status"], "denied")
        self.assertEqual(revoked_result["approval_status"], "revoked")

        rejected = reject_recovery_preview(
            preview,
            approver="operator",
            reason="not safe",
            now=NOW,
        )
        RecoveryApprovalStore(root).save(rejected)
        rejected_result = store.claim(
            rejected.approval_id,
            preview,
            action=rejected.action or preview.actions[0].action,
            target=rejected.target or preview.actions[0].target,
            now=CLAIMED_AT,
        )
        self.assertEqual(rejected_result["status"], "denied")
        self.assertEqual(
            rejected_result["approval_status"], "rejected"
        )

    def test_second_claim_and_replay_after_completion_are_denied(self) -> None:
        _, _, preview, _, store, approval = self._fixture()
        claimed = self._claim(store, preview, approval)
        replay = self._claim(store, preview, approval)
        self.assertEqual(replay["status"], "denied")
        self.assertEqual(replay["approval_status"], "consuming")
        store.complete(
            approval.approval_id,
            execution_attempt_id=claimed["execution_attempt_id"],
            result={"outcome": "inspection_only"},
            verification_passed=True,
            verification_result="independent verifier accepted evidence",
            now=COMPLETED_AT,
        )
        terminal_replay = self._claim(store, preview, approval)
        self.assertEqual(terminal_replay["status"], "denied")
        self.assertEqual(
            terminal_replay["approval_status"], "consumed"
        )

    def test_two_processes_obtain_at_most_one_claim(self) -> None:
        _, _, preview, root, _, approval = self._fixture()
        preview_path = root / "claim-preview.json"
        preview_path.write_text(
            json.dumps(preview.to_dict()), encoding="utf-8"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [
            sys.executable,
            str(WORKER),
            str(root),
            approval.approval_id,
            str(preview_path),
            approval.action or "",
            approval.target or "",
        ]
        processes = [
            subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(2)
        ]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout.strip()))
        self.assertEqual(
            sorted(item["status"] for item in outputs),
            ["claimed", "denied"],
        )
        self.assertTrue(
            all(not item["execution_supported"] for item in outputs)
        )
        self.assertTrue(all(not item["safe_to_retry"] for item in outputs))
        winner = next(
            item for item in outputs if item["status"] == "claimed"
        )
        persisted = RecoveryApprovalStore(root).load(
            approval.approval_id
        )
        self.assertEqual(persisted.status, "consuming")
        self.assertEqual(
            persisted.execution_attempt_id,
            winner["execution_attempt_id"],
        )

    def test_completion_requires_attempt_result_and_verification(self) -> None:
        _, _, preview, _, store, approval = self._fixture()
        claimed = self._claim(store, preview, approval)

        wrong_attempt = store.complete(
            approval.approval_id,
            execution_attempt_id="attempt-not-the-claim",
            result={"outcome": "inspection_only"},
            verification_passed=True,
            verification_result="verified",
            now=COMPLETED_AT,
        )
        self.assertEqual(
            wrong_attempt["reason"], "execution_attempt_mismatch"
        )
        no_result = store.complete(
            approval.approval_id,
            execution_attempt_id=claimed["execution_attempt_id"],
            result=None,
            verification_passed=True,
            verification_result="verified",
            now=COMPLETED_AT,
        )
        self.assertEqual(no_result["reason"], "execution_result_required")
        no_verification = store.complete(
            approval.approval_id,
            execution_attempt_id=claimed["execution_attempt_id"],
            result={"outcome": "inspection_only"},
            verification_passed=None,
            verification_result="not independently verified",
            now=COMPLETED_AT,
        )
        self.assertEqual(
            no_verification["reason"],
            "independent_verification_required",
        )
        no_verification_detail = store.complete(
            approval.approval_id,
            execution_attempt_id=claimed["execution_attempt_id"],
            result={"outcome": "inspection_only"},
            verification_passed=True,
            verification_result=" ",
            now=COMPLETED_AT,
        )
        self.assertEqual(
            no_verification_detail["reason"],
            "verification_result_required",
        )
        self.assertEqual(
            store.load(approval.approval_id).status, "consuming"
        )

    def test_verified_completion_is_consumed_and_restart_readable(self) -> None:
        _, _, preview, _, store, approval = self._fixture()
        claimed = self._claim(store, preview, approval)
        completed = store.complete(
            approval.approval_id,
            execution_attempt_id=claimed["execution_attempt_id"],
            result={"outcome": "inspection_only", "changed": False},
            verification_passed=True,
            verification_result="independent verifier accepted evidence",
            now=COMPLETED_AT,
        )
        self.assertEqual(completed["approval_status"], "consumed")
        self.assertFalse(completed["execution_supported"])
        persisted = RecoveryApprovalStore(store.root).load(
            approval.approval_id
        )
        self.assertEqual(persisted.status, "consumed")
        self.assertEqual(
            persisted.execution_attempt_id,
            claimed["execution_attempt_id"],
        )
        self.assertEqual(
            persisted.result,
            {"outcome": "inspection_only", "changed": False},
        )
        self.assertTrue(persisted.verification_passed)
        self.assertEqual(
            persisted.verification_result,
            "independent verifier accepted evidence",
        )
        self.assertEqual(
            store.reconcile(approval.approval_id)["status"],
            "completed",
        )

    def test_failed_verification_becomes_failed_not_consumed(self) -> None:
        _, _, preview, _, store, approval = self._fixture()
        claimed = self._claim(store, preview, approval)
        completed = store.complete(
            approval.approval_id,
            execution_attempt_id=claimed["execution_attempt_id"],
            result={"outcome": "inspection_only"},
            verification_passed=False,
            verification_result="independent verifier rejected evidence",
            now=COMPLETED_AT,
        )
        self.assertEqual(completed["approval_status"], "failed")
        self.assertEqual(
            store.load(approval.approval_id).status, "failed"
        )
        reconciled = store.reconcile(approval.approval_id)
        self.assertEqual(reconciled["status"], "verified_failure")
        self.assertTrue(reconciled["manual_review"])
        self.assertFalse(reconciled["safe_to_retry"])

    def test_claim_crash_is_unverified_and_not_retried(self) -> None:
        _, _, preview, _, store, approval = self._fixture()
        claimed = self._claim(store, preview, approval)
        reconciled = RecoveryApprovalStore(store.root).reconcile(
            approval.approval_id
        )
        self.assertEqual(reconciled["status"], "unverified")
        self.assertTrue(reconciled["manual_review"])
        self.assertFalse(reconciled["safe_to_retry"])
        self.assertFalse(reconciled["execution_supported"])
        self.assertEqual(
            reconciled["execution_attempt_id"],
            claimed["execution_attempt_id"],
        )
        replay = self._claim(store, preview, approval)
        self.assertEqual(replay["status"], "denied")
        self.assertFalse(replay["safe_to_retry"])

    def test_reconcile_is_read_only(self) -> None:
        _, _, preview, root, store, approval = self._fixture()
        self._claim(store, preview, approval)
        path = root / "recovery_approvals" / f"{approval.approval_id}.json"
        before = path.read_bytes()
        first = store.reconcile(approval.approval_id)
        second = RecoveryApprovalStore(root).reconcile(approval.approval_id)
        self.assertEqual(first, second)
        self.assertEqual(path.read_bytes(), before)

    def test_claim_and_completion_write_failures_fail_closed(self) -> None:
        _, _, preview, _, store, approval = self._fixture()
        with mock.patch.object(
            store,
            "_write_record",
            side_effect=ApprovalStoreError(
                "approval_store_write_failed", "injected"
            ),
        ):
            with self.assertRaises(ApprovalStoreError):
                self._claim(store, preview, approval)
        self.assertEqual(
            store.load(approval.approval_id).status, "approved"
        )

        claimed = self._claim(store, preview, approval)
        with mock.patch.object(
            store,
            "_write_record",
            side_effect=ApprovalStoreError(
                "approval_store_write_failed", "injected"
            ),
        ):
            with self.assertRaises(ApprovalStoreError):
                store.complete(
                    approval.approval_id,
                    execution_attempt_id=claimed["execution_attempt_id"],
                    result={"outcome": "inspection_only"},
                    verification_passed=True,
                    verification_result="verified",
                    now=COMPLETED_AT,
                )
        self.assertEqual(
            store.load(approval.approval_id).status, "consuming"
        )
        reconciled = store.reconcile(approval.approval_id)
        self.assertEqual(reconciled["status"], "unverified")
        self.assertTrue(reconciled["manual_review"])

    def test_non_json_result_is_not_persisted_as_completion(self) -> None:
        _, _, preview, _, store, approval = self._fixture()
        claimed = self._claim(store, preview, approval)
        with self.assertRaises(ApprovalStoreError) as raised:
            store.complete(
                approval.approval_id,
                execution_attempt_id=claimed["execution_attempt_id"],
                result={"object": object()},
                verification_passed=True,
                verification_result="verified",
                now=COMPLETED_AT,
            )
        self.assertEqual(
            raised.exception.code, "approval_store_write_failed"
        )
        self.assertEqual(
            store.load(approval.approval_id).status, "consuming"
        )
        self.assertEqual(
            store.reconcile(approval.approval_id)["status"],
            "unverified",
        )

    def test_malformed_terminal_record_is_rejected(self) -> None:
        _, _, preview, _, store, approval = self._fixture()
        claimed = self._claim(store, preview, approval)
        malformed = replace(
            store.load(approval.approval_id),
            status="consumed",
            completed_at=COMPLETED_AT,
            verification_passed=True,
        )
        malformed = replace(
            malformed,
            record_digest=approval_record_digest(malformed),
        )
        with self.assertRaises(ApprovalStoreError) as raised:
            store.save(malformed)
        self.assertEqual(raised.exception.code, "approval_state_invalid")
        self.assertEqual(
            store.load(approval.approval_id).status, "consuming"
        )
        self.assertEqual(
            store.load(approval.approval_id).execution_attempt_id,
            claimed["execution_attempt_id"],
        )

    def test_terminal_status_must_match_verification_result(self) -> None:
        _, _, preview, _, store, approval = self._fixture()
        claimed = self._claim(store, preview, approval)
        store.complete(
            approval.approval_id,
            execution_attempt_id=claimed["execution_attempt_id"],
            result={"outcome": "inspection_only"},
            verification_passed=False,
            verification_result="independent verifier rejected evidence",
            now=COMPLETED_AT,
        )
        failed = store.load(approval.approval_id)
        malformed = replace(failed, status="consumed")
        malformed = replace(
            malformed,
            record_digest=approval_record_digest(malformed),
        )
        with self.assertRaises(ApprovalStoreError) as raised:
            store.save(malformed)
        self.assertEqual(raised.exception.code, "approval_state_invalid")
        self.assertEqual(
            store.load(approval.approval_id).status, "failed"
        )

    def test_consumption_has_no_task_workspace_or_execution_effects(self) -> None:
        service, task, preview, _, store, approval = self._fixture()
        workspace = service.project_root / "workspaces" / "demo"
        probe = workspace / "probe-written.txt"
        probe.write_text("unchanged\n", encoding="utf-8")
        before_task = task.to_dict()
        before_events = [
            event.to_dict() for event in service.store.list_events(task.task_id)
        ]
        before_probe = probe.read_bytes()

        claimed = self._claim(store, preview, approval)
        store.complete(
            approval.approval_id,
            execution_attempt_id=claimed["execution_attempt_id"],
            result={"outcome": "inspection_only"},
            verification_passed=True,
            verification_result="independent verifier accepted evidence",
            now=COMPLETED_AT,
        )

        self.assertEqual(
            service.store.require_task(task.task_id).to_dict(),
            before_task,
        )
        self.assertEqual(
            [
                event.to_dict()
                for event in service.store.list_events(task.task_id)
            ],
            before_events,
        )
        self.assertEqual(probe.read_bytes(), before_probe)
        self.assertFalse(
            store.load(approval.approval_id).execution_authorized
        )

    def test_round_trip_preview_from_child_compatible_json(self) -> None:
        _, _, preview, _, _, _ = self._fixture()
        restored = RecoveryPreview.from_dict(
            json.loads(json.dumps(preview.to_dict()))
        )
        self.assertEqual(restored.to_dict(), preview.to_dict())
        self.assertFalse(restored.to_dict()["execution_authorized"])


if __name__ == "__main__":
    unittest.main()
