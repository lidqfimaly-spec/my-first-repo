"""Phase 3.71: assurance contract consistency across product boundaries."""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.test_lifecycle_audit_coverage import _host_test_task  # noqa: E402
from zevorinth.assurance import with_status_assurance  # noqa: E402
from zevorinth.cli import doctor_report, main  # noqa: E402
from zevorinth.controlled_recovery import (  # noqa: E402
    CONTROLLED_RESTORE_ACTION,
    with_controlled_restore_action,
)
from zevorinth.recovery_preview import (  # noqa: E402
    RecoveryApprovalStore,
    approve_recovery_preview,
    build_recovery_preview,
)
from zevorinth.task_lifecycle import (  # noqa: E402
    audit_storage_error,
    audit_task_lifecycle,
)


NOW = "2026-10-20T00:00:00.000+00:00"


class AssuranceContractEndToEndTests(unittest.TestCase):
    def _approval_fixture(self):
        service, task, events = _host_test_task()
        workspace = service.project_root / "workspaces" / "demo"
        preview = with_controlled_restore_action(
            build_recovery_preview(task, events, now=NOW),
            workspace,
        )
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
    def _assert_contract(test: unittest.TestCase, report: dict) -> None:
        test.assertIn(report["assurance_level"], {"L0", "L1"})
        test.assertNotEqual(report.get("authenticity"), "authenticated")
        test.assertEqual(report.get("trust_root"), "none")
        test.assertFalse(report.get("same_privilege_rewrite_protected"))
        test.assertFalse(report.get("security_certification"))
        test.assertFalse(report["assurance"]["security_certification"])
        test.assertFalse(report["assurance"]["evidence_authenticated"])

    def test_lifecycle_audit_and_preview_use_shared_contract(self) -> None:
        service, task, events = _host_test_task()
        lifecycle = audit_task_lifecycle(task, events, now=NOW)
        self._assert_contract(self, lifecycle)
        self.assertEqual(lifecycle["assurance_level"], "L1")
        self.assertTrue(lifecycle["ok"])

        unavailable = audit_storage_error(task.task_id, RuntimeError("x"), now=NOW)
        self._assert_contract(self, unavailable)
        self.assertEqual(unavailable["assurance_level"], "L0")
        self.assertTrue(unavailable["task_recovery"]["manual_intervention"])

        preview = build_recovery_preview(task, events, now=NOW).to_dict()
        self._assert_contract(self, preview)
        self.assertFalse(preview["execution_authorized"])
        self.assertEqual(preview["assurance_level"], "L1")

    def test_status_mapping_is_closed_and_conservative(self) -> None:
        l1_statuses = {
            "completed",
            "consistent",
            "denied",
            "failed",
            "not_claimed",
            "succeeded",
            "verified_failure",
        }
        for status in l1_statuses:
            with self.subTest(status=status):
                report = with_status_assurance({"status": status})
                self._assert_contract(self, report)
                self.assertEqual(report["assurance_level"], "L1")
        for status in ("claimed", "consuming", "unverified", "unknown"):
            with self.subTest(status=status):
                report = with_status_assurance({"status": status})
                self._assert_contract(self, report)
                self.assertEqual(report["assurance_level"], "L0")

    def test_cli_recovery_audit_is_read_only_and_truthful(self) -> None:
        service, _, workspace, _, _, approval = self._approval_fixture()
        approval_path = (
            service.project_root
            / "recovery_approvals"
            / f"{approval.approval_id}.json"
        )
        before_approval = approval_path.read_bytes()
        before_workspace = {
            path.relative_to(workspace): path.read_bytes()
            for path in workspace.rglob("*")
            if path.is_file()
        }
        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "--home",
                    str(service.project_root),
                    "recovery-audit",
                    approval.approval_id,
                    "--workspace",
                    str(workspace),
                    "--checked-at",
                    NOW,
                    "--json",
                ]
            )
        self.assertEqual(code, 3)
        report = json.loads(output.getvalue())
        self._assert_contract(self, report)
        self.assertEqual(report["status"], "not_claimed")
        self.assertEqual(report["assurance_level"], "L1")
        self.assertEqual(approval_path.read_bytes(), before_approval)
        self.assertEqual(
            {
                path.relative_to(workspace): path.read_bytes()
                for path in workspace.rglob("*")
                if path.is_file()
            },
            before_workspace,
        )

        human = StringIO()
        with redirect_stdout(human):
            human_code = main(
                [
                    "--home",
                    str(service.project_root),
                    "recovery-audit",
                    approval.approval_id,
                    "--workspace",
                    str(workspace),
                    "--checked-at",
                    NOW,
                ]
            )
        self.assertEqual(human_code, 3)
        rendered = human.getvalue()
        self.assertIn("Assurance: L1", rendered)
        self.assertIn("Authenticity: not_authenticated", rendered)
        self.assertIn("Trust root: none", rendered)
        self.assertIn("Security certification: False", rendered)

    def test_cli_recovery_audit_reports_l0_for_corrupt_record(self) -> None:
        service, _, workspace, _, store, approval = self._approval_fixture()
        path = (
            store.root
            / "recovery_approvals"
            / f"{approval.approval_id}.json"
        )
        path.write_text("not-json", encoding="utf-8")
        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "--home",
                    str(service.project_root),
                    "recovery-audit",
                    approval.approval_id,
                    "--workspace",
                    str(workspace),
                    "--checked-at",
                    NOW,
                    "--json",
                ]
            )
        self.assertEqual(code, 5)
        report = json.loads(output.getvalue())
        self._assert_contract(self, report)
        self.assertEqual(report["status"], "unverified")
        self.assertEqual(report["assurance_level"], "L0")
        self.assertEqual(report["code"], "approval_corrupt")
        self.assertTrue(report["manual_review"])

    def test_doctor_reports_only_implemented_assurance_capabilities(self) -> None:
        service, _, _, _, _, _ = self._approval_fixture()
        report = doctor_report(service.project_root)
        capabilities = report["capabilities"]
        self.assertTrue(capabilities["controlled_recovery_audit"])
        self.assertTrue(capabilities["task_lifecycle_audit"])
        self.assertEqual(
            capabilities["recovery_assurance_levels"], ["L0", "L1"]
        )
        self.assertEqual(
            capabilities["recovery_authentication"], "not_authenticated"
        )


if __name__ == "__main__":
    unittest.main()
