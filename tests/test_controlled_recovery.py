"""Phase 3.66: one controlled, reversible, independently verified operation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.test_lifecycle_audit_coverage import _host_test_task  # noqa: E402
from zevorinth.controlled_recovery import (  # noqa: E402
    CONTROLLED_RESTORE_ACTION,
    CONTROLLED_RESTORE_CONTENT,
    CONTROLLED_RESTORE_DIRECTORY,
    CONTROLLED_RESTORE_FILENAME,
    controlled_restore_target,
    execute_controlled_restore,
    reconcile_controlled_restore,
    verify_controlled_restore,
    with_controlled_restore_action,
)
from zevorinth.recovery_preview import (  # noqa: E402
    ApprovalStoreError,
    RecoveryApprovalStore,
    approve_recovery_preview,
    build_recovery_preview,
)


NOW = "2026-10-20T00:00:00.000+00:00"
LATER = "2026-10-20T00:05:00.000+00:00"
WORKER = PROJECT_ROOT / "tests" / "controlled_recovery_worker.py"


class ControlledRecoveryTests(unittest.TestCase):
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

    def test_controlled_restore_reaches_verified_consumed_state(self) -> None:
        service, task, workspace, preview, store, approval = self._fixture()
        task_before = service.store.require_task(task.task_id).to_dict()
        events_before = [
            event.to_dict()
            for event in service.store.list_events(task.task_id)
        ]

        result = execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )

        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["approval_status"], "consumed")
        self.assertFalse(result["general_recovery_supported"])
        marker = self._marker(workspace)
        self.assertEqual(marker.read_bytes(), CONTROLLED_RESTORE_CONTENT)
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertTrue(verify_controlled_restore(workspace)["verified"])
        persisted = RecoveryApprovalStore(service.project_root).load(
            approval.approval_id
        )
        self.assertEqual(persisted.status, "consumed")
        self.assertIsNotNone(persisted.execution_intent)
        self.assertEqual(
            persisted.execution_intent["execution_attempt_id"],
            result["execution_attempt_id"],
        )
        self.assertEqual(
            service.store.require_task(task.task_id).to_dict(),
            task_before,
        )
        self.assertEqual(
            [
                event.to_dict()
                for event in service.store.list_events(task.task_id)
            ],
            events_before,
        )

    def test_missing_approval_never_executes(self) -> None:
        _, _, workspace, preview, _, approval = self._fixture()
        missing = execute_controlled_restore(
            RecoveryApprovalStore(Path(tempfile.mkdtemp())),
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        self.assertEqual(missing["status"], "denied")
        self.assertFalse(self._marker(workspace).exists())

    def test_revoked_approval_never_executes(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        revoked = store.revoke(
            approval.approval_id,
            reason="operator revoked",
            now=LATER,
        )
        denied = execute_controlled_restore(
            store,
            revoked.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        self.assertEqual(denied["status"], "denied")
        self.assertFalse(self._marker(workspace).exists())

    def test_stale_preview_never_executes(self) -> None:
        service, task, workspace, _, store, approval = self._fixture()
        service.store.record_event_and_update(
            task.task_id,
            "state_changed",
            LATER,
            updates={"phase": "changed_before_controlled_restore"},
        )
        current = service.recovery_preview(task.task_id, now=LATER)
        current_base = build_recovery_preview(
            service.store.require_task(task.task_id),
            service.store.list_events(task.task_id),
            audit=current["audit"],
            now=LATER,
        )
        current_preview = with_controlled_restore_action(
            current_base, workspace
        )
        stale = execute_controlled_restore(
            store,
            approval.approval_id,
            current_preview,
            workspace,
            now=LATER,
        )
        self.assertEqual(stale["status"], "denied")
        self.assertEqual(stale["approval_status"], "stale")
        self.assertFalse(self._marker(workspace).exists())

    def test_target_must_match_bound_workspace(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        other = workspace.parent / "other"
        other.mkdir()
        result = execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            other,
            now=LATER,
        )
        self.assertEqual(result["status"], "denied")
        self.assertEqual(
            result.get("code"), "controlled_action_binding_mismatch"
        )
        self.assertFalse(self._marker(workspace).exists())
        self.assertFalse(self._marker(other).exists())
        self.assertEqual(
            store.load(approval.approval_id).status, "approved"
        )

    def test_existing_target_is_not_overwritten(self) -> None:
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
        self.assertIn("precondition_", result["code"])
        self.assertEqual(marker.read_text(encoding="utf-8"), "operator-owned\n")
        self.assertEqual(store.load(approval.approval_id).status, "failed")

    def test_symlink_target_and_parent_are_refused(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        outside = workspace.parent / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        marker = self._marker(workspace)
        marker.parent.mkdir(mode=0o700)
        marker.symlink_to(outside)
        result = execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
        self.assertTrue(marker.is_symlink())
        self.assertEqual(
            result["observation"]["state"], "target_symlink"
        )

        service, task, events = _host_test_task()
        other_workspace = service.project_root / "workspaces" / "demo"
        base = build_recovery_preview(task, events, now=NOW)
        other_preview = with_controlled_restore_action(
            base, other_workspace
        )
        action = next(
            item
            for item in other_preview.actions
            if item.action == CONTROLLED_RESTORE_ACTION
        )
        other_approval = approve_recovery_preview(
            other_preview,
            approver="operator",
            action=action.action,
            target=action.target,
            now=NOW,
        )
        other_store = RecoveryApprovalStore(service.project_root)
        other_store.save(other_approval)
        escape = service.project_root / "escape"
        escape.mkdir()
        (other_workspace / CONTROLLED_RESTORE_DIRECTORY).symlink_to(escape)
        escaped = execute_controlled_restore(
            other_store,
            other_approval.approval_id,
            other_preview,
            other_workspace,
            now=LATER,
        )
        self.assertEqual(escaped["status"], "failed")
        self.assertEqual(
            escaped["code"], "precondition_parent_unsafe"
        )
        self.assertFalse(
            (escape / CONTROLLED_RESTORE_FILENAME).exists()
        )

    def test_target_replaced_after_intent_is_not_followed(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        outside = workspace.parent / "replacement-outside.txt"
        outside.write_text("outside\n", encoding="utf-8")

        def replace_target(stage: str) -> None:
            if stage == "after_intent":
                parent = workspace / CONTROLLED_RESTORE_DIRECTORY
                parent.mkdir(mode=0o700)
                (parent / CONTROLLED_RESTORE_FILENAME).symlink_to(outside)

        result = execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
            checkpoint=replace_target,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
        self.assertTrue(self._marker(workspace).is_symlink())
        self.assertEqual(store.load(approval.approval_id).status, "failed")

    def test_intent_persistence_failure_prevents_side_effect(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        with mock.patch.object(
            store,
            "record_intent",
            side_effect=ApprovalStoreError(
                "approval_store_write_failed", "injected"
            ),
        ):
            result = execute_controlled_restore(
                store,
                approval.approval_id,
                preview,
                workspace,
                now=LATER,
            )
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["code"], "intent_persistence_failed")
        self.assertFalse(self._marker(workspace).exists())
        persisted = store.load(approval.approval_id)
        self.assertEqual(persisted.status, "consuming")
        self.assertIsNone(persisted.execution_intent)

    def test_claim_persistence_failure_prevents_side_effect(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        with mock.patch.object(
            store,
            "claim",
            side_effect=ApprovalStoreError(
                "approval_store_write_failed", "injected"
            ),
        ):
            result = execute_controlled_restore(
                store,
                approval.approval_id,
                preview,
                workspace,
                now=LATER,
            )
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["code"], "claim_persistence_failed")
        self.assertTrue(result["manual_review"])
        self.assertFalse(result["safe_to_retry"])
        self.assertFalse(self._marker(workspace).exists())
        self.assertEqual(
            store.load(approval.approval_id).status, "approved"
        )

    def test_operation_failure_is_not_reported_success(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        failed_execution = {
            "status": "failed",
            "code": "injected_materialization_failure",
            "created": False,
        }
        with mock.patch(
            "zevorinth.controlled_recovery."
            "_materialize_controlled_restore",
            return_value=failed_execution,
        ):
            result = execute_controlled_restore(
                store,
                approval.approval_id,
                preview,
                workspace,
                now=LATER,
            )
        self.assertEqual(result["status"], "failed")
        self.assertIn("independent_verification_", result["code"])
        self.assertFalse(self._marker(workspace).exists())
        self.assertEqual(store.load(approval.approval_id).status, "failed")

    def test_independent_verifier_rejects_tampered_result(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()

        def tamper(stage: str) -> None:
            if stage == "after_side_effect":
                marker = self._marker(workspace)
                data = marker.read_bytes()
                marker.write_bytes(
                    data[:-1] + (b"X" if data[-1:] != b"X" else b"Y")
                )

        result = execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
            checkpoint=tamper,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["code"], "independent_verification_target_digest_mismatch"
        )
        self.assertEqual(store.load(approval.approval_id).status, "failed")

    def test_completion_write_failure_leaves_verified_side_effect_unverified(
        self,
    ) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        with mock.patch.object(
            store,
            "complete",
            side_effect=ApprovalStoreError(
                "approval_store_write_failed", "injected"
            ),
        ):
            result = execute_controlled_restore(
                store,
                approval.approval_id,
                preview,
                workspace,
                now=LATER,
            )
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["code"], "completion_persistence_failed")
        self.assertTrue(self._marker(workspace).exists())
        persisted = store.load(approval.approval_id)
        self.assertEqual(persisted.status, "consuming")
        reconciled = reconcile_controlled_restore(
            store, approval.approval_id, workspace
        )
        self.assertEqual(reconciled["status"], "unverified")
        self.assertTrue(reconciled["manual_review"])
        self.assertFalse(reconciled["safe_to_retry"])
        self.assertTrue(reconciled["observation"]["verified"])

    def test_created_marker_has_explicit_reversible_compensation(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        result = execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        self.assertEqual(result["status"], "succeeded")
        marker = self._marker(workspace)
        marker.unlink()
        self.assertEqual(
            verify_controlled_restore(workspace)["state"], "target_absent"
        )
        self.assertEqual(store.load(approval.approval_id).status, "consumed")
        reconciled = reconcile_controlled_restore(
            store, approval.approval_id, workspace
        )
        self.assertEqual(reconciled["status"], "unverified")
        self.assertEqual(
            reconciled["reason"],
            "terminal_state_differs_from_current_observation",
        )
        self.assertTrue(reconciled["manual_review"])
        self.assertFalse(reconciled["safe_to_retry"])

    def test_intent_identity_mismatch_is_rejected(self) -> None:
        _, _, workspace, preview, store, approval = self._fixture()
        claimed = store.claim(
            approval.approval_id,
            preview,
            action=CONTROLLED_RESTORE_ACTION,
            target=controlled_restore_target(workspace),
            now=LATER,
        )
        self.assertEqual(claimed["status"], "claimed")
        result = store.record_intent(
            approval.approval_id,
            execution_attempt_id=claimed["execution_attempt_id"],
            intent={
                "execution_attempt_id": claimed["execution_attempt_id"],
                "action": CONTROLLED_RESTORE_ACTION,
                "target": "file:outside",
                "action_digest": approval.action_digest,
            },
            now=LATER,
        )
        self.assertEqual(result["status"], "denied")
        self.assertEqual(
            result["reason"], "execution_intent_identity_mismatch"
        )
        self.assertIsNone(
            store.load(approval.approval_id).execution_intent
        )
        self.assertFalse(self._marker(workspace).exists())

    def test_two_processes_execute_side_effect_at_most_once(self) -> None:
        service, _, workspace, preview, _, approval = self._fixture()
        preview_path = service.project_root / "controlled-preview.json"
        preview_path.write_text(
            json.dumps(preview.to_dict()), encoding="utf-8"
        )
        ready = service.project_root / "unused-ready"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = self._worker_command(
            "execute",
            service.project_root,
            approval.approval_id,
            preview_path,
            workspace,
            ready,
        )
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
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout.strip()))
        self.assertEqual(
            sorted(item["status"] for item in outputs),
            ["denied", "succeeded"],
        )
        self.assertEqual(
            self._marker(workspace).read_bytes(),
            CONTROLLED_RESTORE_CONTENT,
        )
        persisted = RecoveryApprovalStore(service.project_root).load(
            approval.approval_id
        )
        self.assertEqual(persisted.status, "consumed")

    def _crash_run(self, mode: str):
        service, _, workspace, preview, store, approval = self._fixture()
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
        return service, workspace, preview, store, approval

    def test_crash_before_side_effect_is_manual_review_only(self) -> None:
        service, workspace, _, store, approval = self._crash_run("crash-before")
        self.assertFalse(self._marker(workspace).exists())
        persisted = RecoveryApprovalStore(service.project_root).load(
            approval.approval_id
        )
        self.assertIsNotNone(persisted.execution_intent)
        before = (
            service.project_root
            / "recovery_approvals"
            / f"{approval.approval_id}.json"
        ).read_bytes()
        reconciled = reconcile_controlled_restore(
            store, approval.approval_id, workspace
        )
        self.assertEqual(reconciled["status"], "unverified")
        self.assertTrue(reconciled["manual_review"])
        self.assertFalse(reconciled["safe_to_retry"])
        self.assertEqual(
            reconciled["observation"]["state"], "target_absent"
        )
        self.assertEqual(
            (
                service.project_root
                / "recovery_approvals"
                / f"{approval.approval_id}.json"
            ).read_bytes(),
            before,
        )

    def test_crash_after_side_effect_is_not_marked_completed(self) -> None:
        service, workspace, preview, store, approval = self._crash_run(
            "crash-after"
        )
        self.assertTrue(self._marker(workspace).exists())
        self.assertTrue(verify_controlled_restore(workspace)["verified"])
        persisted = RecoveryApprovalStore(service.project_root).load(
            approval.approval_id
        )
        self.assertEqual(persisted.status, "consuming")
        self.assertIsNotNone(persisted.execution_intent)
        reconciled = reconcile_controlled_restore(
            store, approval.approval_id, workspace
        )
        self.assertEqual(reconciled["status"], "unverified")
        self.assertTrue(reconciled["manual_review"])
        self.assertFalse(reconciled["safe_to_retry"])
        self.assertTrue(reconciled["observation"]["verified"])
        replay = execute_controlled_restore(
            store,
            approval.approval_id,
            preview,
            workspace,
            now=LATER,
        )
        self.assertEqual(replay["status"], "denied")
        self.assertEqual(
            self._marker(workspace).read_bytes(),
            CONTROLLED_RESTORE_CONTENT,
        )


if __name__ == "__main__":
    unittest.main()
