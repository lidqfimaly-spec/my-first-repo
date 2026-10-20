"""Phase 3.67: post-execution state and evidence consistency audit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.test_lifecycle_audit_coverage import _host_test_task  # noqa: E402
from zevorinth.controlled_recovery import (  # noqa: E402
    CONTROLLED_RESTORE_ACTION,
    CONTROLLED_RESTORE_CONTENT,
    CONTROLLED_RESTORE_DIRECTORY,
    CONTROLLED_RESTORE_FILENAME,
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
)


NOW = "2026-10-20T00:00:00.000+00:00"
LATER = "2026-10-20T00:05:00.000+00:00"
WORKER = PROJECT_ROOT / "tests" / "controlled_recovery_worker.py"


class ControlledRecoveryPhase367Tests(unittest.TestCase):
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
            / CONTROLLED_RESTORE_DIRECTORY
            / CONTROLLED_RESTORE_FILENAME
        )

    @staticmethod
    def _approval_path(root: Path, approval_id: str) -> Path:
        return root / "recovery_approvals" / f"{approval_id}.json"

    def _worker_command(
        self,
        mode: str,
        root: Path,
        approval_id: str,
        preview_path: Path,
        workspace: Path,
        ready_path: Path,
    ) -> list[str]:
        return [
            sys.executable,
            str(WORKER),
            mode,
            str(root),
            approval_id,
            str(preview_path),
            str(workspace),
            str(ready_path),
        ]

    def _crash_at(self, mode: str):
        service, task, workspace, preview, store, approval = self._fixture()
        preview_path = service.project_root / f"{mode}-preview.json"
        preview_path.write_text(
            json.dumps(preview.to_dict()), encoding="utf-8"
        )
        ready = service.project_root / f"{mode}-ready"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.Popen(
            self._worker_command(
                mode,
                service.project_root,
                approval.approval_id,
                preview_path,
                workspace,
                ready,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.01)
        if not ready.exists():
            process.kill()
            _, stderr = process.communicate(timeout=10)
            self.fail(stderr)
        process.kill()
        process.communicate(timeout=10)
        return service, task, workspace, preview, store, approval

    def test_success_audit_is_consistent_and_read_only(self) -> None:
        service, task, workspace, preview, store, approval = self._fixture()
        result = execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        self.assertEqual(result["status"], "succeeded")
        marker = self._marker(workspace)
        approval_path = self._approval_path(
            service.project_root, approval.approval_id
        )
        before_approval = approval_path.read_bytes()
        before_task = service.store.require_task(task.task_id).to_dict()
        before_events = [
            event.to_dict()
            for event in service.store.list_events(task.task_id)
        ]
        before_marker = marker.read_bytes()
        before_stat = marker.stat()

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace, now=NOW
        )
        repeated = audit_controlled_recovery(
            store, approval.approval_id, workspace, now=NOW
        )

        self.assertEqual(audit["status"], "consistent")
        self.assertEqual(repeated, audit)
        self.assertEqual(
            audit["auditor_version"], "controlled-recovery-audit-v1"
        )
        self.assertEqual(
            audit["checked_at"], "2026-10-20T00:00:00.000+00:00"
        )
        self.assertFalse(audit["manual_review"])
        self.assertFalse(audit["safe_to_retry"])
        self.assertFalse(audit["security_certification"])
        self.assertEqual(audit["evidence_digest"], result["evidence_digest"])
        self.assertEqual(
            audit["approval_state_digest"], approval.state_digest
        )
        self.assertEqual(
            audit["approval_record_digest"],
            approval_record_digest(
                store.load(approval.approval_id)
            ),
        )
        self.assertEqual(len(audit["intent_digest"]), 64)
        self.assertEqual(len(audit["observation_digest"]), 64)
        self.assertEqual(approval_path.read_bytes(), before_approval)
        self.assertEqual(
            service.store.require_task(task.task_id).to_dict(), before_task
        )
        self.assertEqual(
            [
                event.to_dict()
                for event in service.store.list_events(task.task_id)
            ],
            before_events,
        )
        self.assertEqual(marker.read_bytes(), before_marker)
        self.assertEqual(marker.stat().st_ino, before_stat.st_ino)

    def test_audit_detects_inode_replacement_with_identical_content(
        self,
    ) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        marker = self._marker(workspace)
        marker.unlink()
        marker.write_bytes(CONTROLLED_RESTORE_CONTENT)
        marker.chmod(0o600)

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertEqual(
            audit["reason"],
            "current_file_identity_differs_from_completion_evidence",
        )
        self.assertTrue(audit["manual_review"])
        self.assertFalse(audit["safe_to_retry"])
        self.assertTrue(audit["observation"]["verified"])

    def test_audit_detects_same_inode_content_replacement(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        marker = self._marker(workspace)
        data = marker.read_bytes()
        marker.write_bytes(data[:-1] + b"X")
        marker.chmod(0o600)

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertEqual(
            audit["reason"],
            "historical_success_current_observation_diverged",
        )
        self.assertEqual(
            audit["observation"]["state"], "target_digest_mismatch"
        )
        self.assertFalse(audit["observation"]["verified"])

    def test_audit_rejects_tampered_stored_evidence_digest(self) -> None:
        service, _, workspace, preview, store, approval = self._fixture()
        execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        current = store.load(approval.approval_id)
        tampered_result = dict(current.result or {})
        tampered_result["evidence_digest"] = "0" * 64
        tampered = replace(current, result=tampered_result)
        tampered = replace(
            tampered, record_digest=approval_record_digest(tampered)
        )
        store._write_record(tampered)
        path = self._approval_path(
            service.project_root, approval.approval_id
        )
        before = path.read_bytes()

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertEqual(audit["reason"], "stored_evidence_inconsistent")
        self.assertIn("evidence_digest_mismatch", audit["evidence_issues"])
        self.assertTrue(audit["manual_review"])
        self.assertEqual(path.read_bytes(), before)

    def test_audit_rejects_missing_verification_binding(self) -> None:
        service, _, workspace, preview, store, approval = self._fixture()
        execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        current = store.load(approval.approval_id)
        tampered = replace(current, verification_result="verified")
        tampered = replace(
            tampered, record_digest=approval_record_digest(tampered)
        )
        store._write_record(tampered)

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertIn(
            "verification_result_binding_missing",
            audit["evidence_issues"],
        )
        self.assertTrue(audit["manual_review"])

    def test_audit_rejects_stored_unverified_state(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        current = store.load(approval.approval_id)
        result = json.loads(json.dumps(current.result))
        result["verification"]["state"] = "target_digest_mismatch"
        digest = controlled_recovery_evidence_digest(
            result["intent"], result["execution"], result["verification"]
        )
        result["evidence_digest"] = digest
        tampered = replace(
            current,
            result=result,
            verification_result=(
                "independent no-follow readback rejected "
                f"evidence_sha256={digest}"
            ),
        )
        tampered = replace(
            tampered, record_digest=approval_record_digest(tampered)
        )
        store._write_record(tampered)

        audit = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )

        self.assertEqual(audit["status"], "unverified")
        self.assertIn(
            "verification_not_verified", audit["evidence_issues"]
        )

    def test_target_replacement_after_first_verification_is_caught(
        self,
    ) -> None:
        _, _, workspace, preview, store, approval = self._fixture()

        def replace_target(stage: str) -> None:
            if stage == "after_verification":
                marker = self._marker(workspace)
                data = marker.read_bytes()
                marker.write_bytes(data[:-1] + b"X")
                marker.chmod(0o600)

        result = execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
            checkpoint=replace_target,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["code"],
            "independent_verification_target_digest_mismatch",
        )
        self.assertEqual(
            store.load(approval.approval_id).status, "failed"
        )

    def test_state_matrix_for_not_claimed_revoked_and_consuming(self) -> None:
        service, _, workspace, preview, store, approval = self._fixture()
        approved = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )
        self.assertEqual(approved["status"], "not_claimed")

        claimed = store.claim(
            approval.approval_id,
            preview,
            action=CONTROLLED_RESTORE_ACTION,
            target=approval.target or "",
            now=LATER,
        )
        self.assertEqual(claimed["status"], "claimed")
        no_intent = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )
        self.assertEqual(no_intent["status"], "unverified")
        self.assertEqual(
            no_intent["reason"], "claimed_without_persisted_intent"
        )
        self.assertTrue(no_intent["manual_review"])
        self.assertFalse(no_intent["safe_to_retry"])

        service2, _, workspace2, _, store2, approval2 = self._fixture()
        store2.revoke(
            approval2.approval_id, reason="operator revoked", now=LATER
        )
        revoked = audit_controlled_recovery(
            store2, approval2.approval_id, workspace2
        )
        self.assertEqual(revoked["status"], "denied")
        self.assertFalse(revoked["safe_to_retry"])

    def test_failed_and_action_mismatch_states_require_review(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        marker = self._marker(workspace)
        marker.parent.mkdir(mode=0o700)
        marker.write_text("operator-owned\n", encoding="utf-8")
        marker.chmod(0o600)
        result = execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        self.assertEqual(result["status"], "failed")
        failed = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )
        self.assertEqual(failed["status"], "verified_failure")
        self.assertTrue(failed["manual_review"])
        self.assertFalse(failed["safe_to_retry"])

        other = workspace.parent / "audit-other"
        other.mkdir()
        mismatched = audit_controlled_recovery(
            store, approval.approval_id, other
        )
        self.assertEqual(mismatched["status"], "unverified")
        self.assertEqual(
            mismatched["reason"], "controlled_action_binding_mismatch"
        )
        self.assertTrue(mismatched["manual_review"])

    def test_audit_missing_and_corrupt_records_fail_closed(self) -> None:
        service, _, workspace, _, store, approval = self._fixture()
        missing = audit_controlled_recovery(
            RecoveryApprovalStore(Path(tempfile.mkdtemp())),
            approval.approval_id,
            workspace,
        )
        self.assertEqual(missing["status"], "unverified")
        self.assertEqual(missing["reason"], "approval_store_unreadable")
        self.assertTrue(missing["manual_review"])

        path = self._approval_path(
            service.project_root, approval.approval_id
        )
        path.write_text("not-json", encoding="utf-8")
        corrupt = audit_controlled_recovery(
            store, approval.approval_id, workspace
        )
        self.assertEqual(corrupt["status"], "unverified")
        self.assertEqual(corrupt["reason"], "approval_store_unreadable")
        self.assertEqual(corrupt["code"], "approval_corrupt")

    def test_all_real_process_kill_points_preserve_safe_state(self) -> None:
        cases = (
            (
                "crash-claim",
                "consuming",
                False,
                "claimed_without_persisted_intent",
                "target_absent",
            ),
            (
                "crash-before",
                "consuming",
                True,
                "interrupted_before_or_during_side_effect",
                "target_absent",
            ),
            (
                "crash-during",
                "consuming",
                True,
                None,
                None,
            ),
            (
                "crash-after",
                "consuming",
                True,
                "side_effect_observed_without_persisted_completion",
                "verified",
            ),
            (
                "crash-after-verification",
                "consuming",
                True,
                "side_effect_observed_without_persisted_completion",
                "verified",
            ),
            (
                "crash-before-completion",
                "consuming",
                True,
                "side_effect_observed_without_persisted_completion",
                "verified",
            ),
            (
                "crash-after-completion",
                "consumed",
                True,
                None,
                "verified",
            ),
        )
        for (
            mode,
            expected_status,
            intent_recorded,
            expected_reason,
            expected_observation,
        ) in cases:
            with self.subTest(mode=mode):
                (
                    service,
                    task,
                    workspace,
                    preview,
                    store,
                    approval,
                ) = self._crash_at(mode)
                marker = self._marker(workspace)
                marker_bytes = (
                    marker.read_bytes() if marker.exists() else None
                )
                marker_inode = (
                    marker.stat().st_ino if marker.exists() else None
                )
                approval_path = self._approval_path(
                    service.project_root, approval.approval_id
                )
                before_audit = approval_path.read_bytes()
                before_task = service.store.require_task(task.task_id).to_dict()
                before_events = [
                    event.to_dict()
                    for event in service.store.list_events(task.task_id)
                ]
                audit = audit_controlled_recovery(
                    store, approval.approval_id, workspace
                )
                self.assertEqual(audit["approval_status"], expected_status)
                self.assertEqual(
                    audit["intent_recorded"], intent_recorded
                )
                self.assertFalse(audit["safe_to_retry"])
                if expected_reason is not None:
                    self.assertEqual(audit["reason"], expected_reason)
                if expected_observation is not None:
                    if expected_observation == "target_absent":
                        self.assertEqual(
                            audit["observation"]["state"], "target_absent"
                        )
                    else:
                        self.assertEqual(
                            audit["observation"]["state"], expected_observation
                        )
                if mode == "crash-after-completion":
                    self.assertEqual(audit["status"], "consistent")
                else:
                    self.assertEqual(audit["status"], "unverified")
                    self.assertTrue(audit["manual_review"])
                self.assertEqual(approval_path.read_bytes(), before_audit)
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

                replay = execute_controlled_restore(
                    store,
                    approval.approval_id,
                    preview,
                    workspace,
                    now=LATER,
                )
                self.assertEqual(replay["status"], "denied")
                self.assertFalse(replay["safe_to_retry"])
                if marker_bytes is None:
                    self.assertFalse(marker.exists())
                else:
                    self.assertEqual(marker.read_bytes(), marker_bytes)
                    self.assertEqual(marker.stat().st_ino, marker_inode)


if __name__ == "__main__":
    unittest.main()
