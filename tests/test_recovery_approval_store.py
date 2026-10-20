"""Phase 3.64: durable approval records and authorization boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.test_lifecycle_audit_coverage import _host_test_task  # noqa: E402
from zevorinth.recovery_preview import (  # noqa: E402
    ApprovalStoreError,
    RecoveryApprovalStore,
    approval_record_digest,
    approve_recovery_preview,
    authorize_recovery_action,
    build_recovery_preview,
    reject_recovery_preview,
    validate_recovery_approval,
)


NOW = "2026-10-20T00:00:00.000+00:00"
LATER = "2026-10-20T00:05:00.000+00:00"
WORKER = PROJECT_ROOT / "tests" / "approval_revalidation_worker.py"


def _approve(preview, *, approver: str = "operator", now: str = NOW):
    action = preview.actions[0]
    return approve_recovery_preview(
        preview,
        approver=approver,
        action=action.action,
        target=action.target,
        now=now,
    )


class RecoveryApprovalStoreTests(unittest.TestCase):
    def _preview(self):
        service, task, events = _host_test_task()
        preview = build_recovery_preview(task, events, now=NOW)
        return service, task, preview

    def test_approval_survives_restart(self) -> None:
        _, task, preview = self._preview()
        root = Path(tempfile.mkdtemp(prefix="zv-approval-store-"))
        store = RecoveryApprovalStore(root)
        approval = _approve(preview)
        self.assertEqual(store.save(approval)["status"], "saved")
        reloaded = RecoveryApprovalStore(root).load(approval.approval_id)
        self.assertEqual(reloaded.to_dict(), approval.to_dict())
        self.assertTrue(
            validate_recovery_approval(reloaded, preview)["valid"]
        )
        self.assertFalse(reloaded.execution_authorized)

    def test_duplicate_submission_is_idempotent(self) -> None:
        _, _, preview = self._preview()
        root = Path(tempfile.mkdtemp(prefix="zv-approval-duplicate-"))
        store = RecoveryApprovalStore(root)
        approval = _approve(preview)
        self.assertEqual(store.save(approval)["status"], "saved")
        self.assertEqual(store.save(approval)["status"], "unchanged")
        other = replace(approval, approver="other")
        other = replace(
            other, record_digest=approval_record_digest(other)
        )
        with self.assertRaises(ApprovalStoreError) as raised:
            store.save(other)
        self.assertEqual(raised.exception.code, "approval_conflict")

    def test_tampered_record_is_rejected(self) -> None:
        _, _, preview = self._preview()
        root = Path(tempfile.mkdtemp(prefix="zv-approval-tamper-"))
        store = RecoveryApprovalStore(root)
        approval = _approve(preview)
        store.save(approval)
        path = root / "recovery_approvals" / f"{approval.approval_id}.json"
        path.write_text(json.dumps({"tampered": True}), encoding="utf-8")
        with self.assertRaises(ApprovalStoreError) as raised:
            store.load(approval.approval_id)
        self.assertEqual(raised.exception.code, "approval_corrupt")

    def test_approval_cannot_authorize_execution(self) -> None:
        _, _, preview = self._preview()
        root = Path(tempfile.mkdtemp(prefix="zv-approval-exec-"))
        store = RecoveryApprovalStore(root)
        approval = _approve(preview)
        forbidden = replace(approval, execution_authorized=True)
        forbidden = replace(
            forbidden, record_digest=approval_record_digest(forbidden)
        )
        with self.assertRaises(ApprovalStoreError) as raised:
            store.save(forbidden)
        self.assertEqual(
            raised.exception.code, "execution_authorization_forbidden"
        )

    def test_revoke_persists_and_denies_authorization(self) -> None:
        _, task, preview = self._preview()
        root = Path(tempfile.mkdtemp(prefix="zv-approval-revoke-"))
        store = RecoveryApprovalStore(root)
        approval = _approve(preview)
        store.save(approval)
        revoked = store.revoke(
            approval.approval_id, reason="operator revoked", now=LATER
        )
        reloaded = RecoveryApprovalStore(root).load(approval.approval_id)
        self.assertEqual(reloaded.status, "revoked")
        verdict = validate_recovery_approval(reloaded, preview)
        self.assertFalse(verdict["valid"])
        self.assertEqual(verdict["state"], "revoked")
        denied = authorize_recovery_action(
            reloaded,
            preview,
            action="none",
            target=f"task:{task.task_id}",
        )
        self.assertFalse(denied["authorized"])
        self.assertFalse(denied["execution_supported"])

    def test_rejected_approval_cannot_authorize(self) -> None:
        _, task, preview = self._preview()
        root = Path(tempfile.mkdtemp(prefix="zv-approval-reject-"))
        store = RecoveryApprovalStore(root)
        rejected = reject_recovery_preview(
            preview,
            approver="operator",
            reason="not safe",
            now=NOW,
        )
        store.save(rejected)
        verdict = validate_recovery_approval(rejected, preview)
        self.assertFalse(verdict["valid"])
        self.assertEqual(verdict["state"], "rejected")
        denied = authorize_recovery_action(
            rejected,
            preview,
            action="none",
            target=f"task:{task.task_id}",
        )
        self.assertFalse(denied["authorized"])

    def test_stale_approval_and_action_scope(self) -> None:
        service, task, preview = self._preview()
        approval = _approve(preview)
        service.store.record_event_and_update(
            task.task_id,
            "state_changed",
            LATER,
            updates={"phase": "changed"},
        )
        current = service.recovery_preview(task.task_id, now=LATER)
        current_preview = build_recovery_preview(
            service.store.require_task(task.task_id),
            service.store.list_events(task.task_id),
            audit=current["audit"],
            now=LATER,
        )
        verdict = validate_recovery_approval(approval, current_preview)
        self.assertFalse(verdict["valid"])
        self.assertEqual(verdict["state"], "stale")
        allowed = authorize_recovery_action(
            approval,
            preview,
            action="none",
            target=f"task:{task.task_id}",
        )
        self.assertTrue(allowed["authorized"])
        self.assertFalse(allowed["execution_supported"])
        denied = authorize_recovery_action(
            approval,
            preview,
            action="none",
            target="task:other",
        )
        self.assertFalse(denied["authorized"])

    def test_two_processes_reject_stale_approval(self) -> None:
        service, task, preview = self._preview()
        root = Path(tempfile.mkdtemp(prefix="zv-approval-process-"))
        store = RecoveryApprovalStore(root)
        approval = _approve(preview)
        store.save(approval)
        service.store.record_event_and_update(
            task.task_id,
            "state_changed",
            LATER,
            updates={"phase": "changed_for_process"},
        )
        current = service.recovery_preview(task.task_id, now=LATER)
        current_preview = build_recovery_preview(
            service.store.require_task(task.task_id),
            service.store.list_events(task.task_id),
            audit=current["audit"],
            now=LATER,
        )
        preview_path = root / "current-preview.json"
        preview_path.write_text(
            json.dumps(current_preview.to_dict()), encoding="utf-8"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(WORKER),
                    str(root),
                    approval.approval_id,
                    str(preview_path),
                ],
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
        self.assertEqual({item["state"] for item in outputs}, {"stale"})
        self.assertTrue(
            all(item["execution_authorized"] is False for item in outputs)
        )


if __name__ == "__main__":
    unittest.main()
