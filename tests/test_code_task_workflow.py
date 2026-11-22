from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.host_grant_support import authorized_service  # noqa: E402
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.capabilities import CAPABILITY_REGISTRY  # noqa: E402
from zevorinth.planner import (  # noqa: E402
    EDIT_TEXT_FILE,
    INSPECT_WORKSPACE,
    RUN_TESTS,
)
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.workspace_edit import (  # noqa: E402
    EDIT_AUTHORIZATION_TOKEN,
    WorkspaceEditError,
    apply_text_edit,
    verify_text_edit,
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CodeTaskWorkflowTests(unittest.TestCase):
    def make_workspace(self, root: Path) -> Path:
        workspace = root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / "sample.py").write_text(
            "VALUE = 1\n\ndef get_value():\n    return VALUE\n",
            encoding="utf-8",
        )
        (workspace / "test_sample.py").write_text(
            "import unittest\n"
            "import sample\n\n"
            "class SampleTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(sample.get_value(), 2)\n",
            encoding="utf-8",
        )
        return workspace

    def test_code_operations_are_registered_as_trusted_structured_tools(self) -> None:
        for operation in (INSPECT_WORKSPACE, EDIT_TEXT_FILE):
            details = CAPABILITY_REGISTRY["task_operations"][operation]
            self.assertEqual(details["status"], "available")
            self.assertTrue(details["verified"])
            self.assertIn(
                "not model-selectable through the LLM planner",
                details["limitations"],
            )

    def test_inspect_workspace_is_bounded_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = self.make_workspace(root)
            before = {
                path.relative_to(workspace).as_posix(): path.read_bytes()
                for path in workspace.rglob("*")
                if path.is_file()
            }
            service = TaskService(root)

            task = service.submit(
                TaskRequest(
                    goal="inspect the sample module",
                    operation=INSPECT_WORKSPACE,
                    parameters={"workspace": "demo", "path": "sample.py"},
                )
            )

            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            self.assertEqual(task.execution_mode.value, "real")
            inspection = task.tool_result.workspace_inspection
            self.assertEqual(inspection["selected_path"], "sample.py")
            self.assertEqual(inspection["selected_sha256"], digest(
                (workspace / "sample.py").read_text(encoding="utf-8")
            ))
            self.assertEqual(
                before,
                {
                    path.relative_to(workspace).as_posix(): path.read_bytes()
                    for path in workspace.rglob("*")
                    if path.is_file()
                },
            )
            self.assertTrue(service.audit_lifecycle(task.task_id)["ok"])

    def test_authorized_exact_edit_is_backed_up_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = self.make_workspace(root)
            source = (workspace / "sample.py").read_text(encoding="utf-8")
            service = TaskService(root)

            task = service.submit(
                TaskRequest(
                    goal="change the constant",
                    operation=EDIT_TEXT_FILE,
                    parameters={
                        "workspace": "demo",
                        "path": "sample.py",
                        "expected_sha256": digest(source),
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                        "authorization": EDIT_AUTHORIZATION_TOKEN,
                    },
                )
            )

            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            edited = (workspace / "sample.py").read_text(encoding="utf-8")
            self.assertIn("VALUE = 2", edited)
            evidence = task.tool_result.workspace_edit
            self.assertEqual(evidence["source_sha256"], digest(source))
            self.assertEqual(evidence["result_sha256"], digest(edited))
            backup = Path(evidence["backup_path"])
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_text(encoding="utf-8"), source)
            self.assertTrue(service.audit_lifecycle(task.task_id)["ok"])

    def test_edit_requires_authorization_before_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = self.make_workspace(root)
            source = (workspace / "sample.py").read_text(encoding="utf-8")
            service = TaskService(root)

            blocked = service.submit(
                TaskRequest(
                    goal="unauthorized edit",
                    operation=EDIT_TEXT_FILE,
                    parameters={
                        "workspace": "demo",
                        "path": "sample.py",
                        "expected_sha256": digest(source),
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                        "authorization": "wrong",
                    },
                )
            )

            self.assertEqual(blocked.status, TaskStatus.BLOCKED)
            self.assertEqual(blocked.error.code, "edit_not_authorized")
            self.assertEqual(
                (workspace / "sample.py").read_text(encoding="utf-8"),
                source,
            )
            self.assertNotIn(
                "tool_started",
                [event.event_type for event in service.events(blocked.task_id)],
            )

    def test_stale_digest_and_ambiguous_match_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = self.make_workspace(root)
            (workspace / "ambiguous.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
            service = TaskService(root)
            source = (workspace / "sample.py").read_text(encoding="utf-8")
            for parameters, code in (
                (
                    {
                        "workspace": "demo",
                        "path": "sample.py",
                        "expected_sha256": "0" * 64,
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                        "authorization": EDIT_AUTHORIZATION_TOKEN,
                    },
                    "source_file_changed",
                ),
                (
                    {
                        "workspace": "demo",
                        "path": "ambiguous.py",
                        "expected_sha256": digest(
                            (workspace / "ambiguous.py").read_text(
                                encoding="utf-8"
                            )
                        ),
                        "old_text": "x = 1",
                        "new_text": "x = 2",
                        "authorization": EDIT_AUTHORIZATION_TOKEN,
                    },
                    "text_match_count",
                ),
            ):
                with self.subTest(code=code):
                    blocked = service.submit(
                        TaskRequest(
                            goal="unsafe edit",
                            operation=EDIT_TEXT_FILE,
                            parameters=parameters,
                        )
                    )
                    self.assertEqual(blocked.status, TaskStatus.BLOCKED)
                    self.assertEqual(blocked.error.code, code)
            self.assertEqual(
                (workspace / "sample.py").read_text(encoding="utf-8"),
                source,
            )
            self.assertEqual(
                (workspace / "ambiguous.py").read_text(encoding="utf-8"),
                "x = 1\nx = 1\n",
            )

    def test_concurrent_edits_serialize_and_only_one_can_win(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = self.make_workspace(root)
            source = (workspace / "sample.py").read_text(encoding="utf-8")
            source_sha = digest(source)

            def edit(replacement: str):
                service = TaskService(root)
                return service.submit(
                    TaskRequest(
                        goal=f"set the constant to {replacement}",
                        operation=EDIT_TEXT_FILE,
                        parameters={
                            "workspace": "demo",
                            "path": "sample.py",
                            "expected_sha256": source_sha,
                            "old_text": "VALUE = 1",
                            "new_text": f"VALUE = {replacement}",
                            "authorization": EDIT_AUTHORIZATION_TOKEN,
                        },
                    )
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks = list(pool.map(edit, ("2", "3")))

            self.assertEqual(
                sum(task.status is TaskStatus.SUCCEEDED for task in tasks),
                1,
            )
            loser = next(
                task for task in tasks if task.status is not TaskStatus.SUCCEEDED
            )
            self.assertIn(
                loser.status,
                {TaskStatus.BLOCKED, TaskStatus.FAILED},
            )
            self.assertEqual(loser.error.code, "source_file_changed")
            final = (workspace / "sample.py").read_text(encoding="utf-8")
            self.assertTrue(
                final == source.replace("VALUE = 1", "VALUE = 2")
                or final == source.replace("VALUE = 1", "VALUE = 3")
            )
            self.assertEqual(final.count("VALUE ="), 1)

    def test_verification_binds_to_frozen_result_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = self.make_workspace(root)
            source = (workspace / "sample.py").read_text(encoding="utf-8")
            result = source.replace("VALUE = 1", "VALUE = 2")
            (workspace / "sample.py").write_text(result, encoding="utf-8")
            evidence = {
                "workspace": "demo",
                "path": "sample.py",
                "source_sha256": digest(source),
                "result_sha256": digest(result),
                "observed_sha256": digest(result),
                "result_byte_count": len(result.encode("utf-8")),
            }
            passed, reason = verify_text_edit(
                root,
                evidence,
                expected_workspace="demo",
                expected_path="sample.py",
                expected_source_sha256=digest(source),
                expected_result_sha256="0" * 64,
            )
            self.assertFalse(passed)
            self.assertEqual(reason, "edit_evidence_mismatch")

    def test_symlink_and_sensitive_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = self.make_workspace(root)
            (workspace / "secret.env").write_text("TOKEN=secret\n", encoding="utf-8")
            (workspace / "secrets").mkdir()
            (workspace / "secrets" / "config.py").write_text(
                "TOKEN = 'secret'\n",
                encoding="utf-8",
            )
            outside = root / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            (workspace / "linked.txt").symlink_to(outside)
            service = TaskService(root)

            for path, code in (
                ("linked.txt", "unsafe_workspace_path"),
                ("secret.env", "sensitive_workspace_path"),
                ("secrets/config.py", "sensitive_workspace_path"),
            ):
                with self.subTest(path=path):
                    blocked = service.submit(
                        TaskRequest(
                            goal="inspect unsafe path",
                            operation=INSPECT_WORKSPACE,
                            parameters={"workspace": "demo", "path": path},
                        )
                    )
                    self.assertEqual(blocked.status, TaskStatus.BLOCKED)
                    self.assertEqual(blocked.error.code, code)

    def test_interrupted_edit_recovery_does_not_reapply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = self.make_workspace(root)
            source = (workspace / "sample.py").read_text(encoding="utf-8")
            base_time = "2026-10-19T00:00:00.000+00:00"
            future_time = "2026-10-19T00:02:01.000+00:00"
            service = TaskService(root, clock=lambda: base_time)
            task = service.enqueue(
                TaskRequest(
                    goal="interrupted edit",
                    operation=EDIT_TEXT_FILE,
                    parameters={
                        "workspace": "demo",
                        "path": "sample.py",
                        "expected_sha256": digest(source),
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                        "authorization": EDIT_AUTHORIZATION_TOKEN,
                    },
                )
            )

            def interrupt(stage: str, task: object) -> None:
                if stage == "after_workspace_edit_before_verification":
                    raise RuntimeError("simulated edit interruption")

            service.fault_hook = interrupt
            with self.assertRaisesRegex(RuntimeError, "edit interruption"):
                service.execute_task(task.task_id)
            self.assertIn(
                "VALUE = 2",
                (workspace / "sample.py").read_text(encoding="utf-8"),
            )
            restarted = TaskService(root, clock=lambda: future_time)
            resumed = restarted.resume(task.task_id)
            self.assertEqual(resumed.status, TaskStatus.SUCCEEDED)
            self.assertEqual(
                (workspace / "sample.py").read_text(encoding="utf-8").count(
                    "VALUE = 2"
                ),
                1,
            )

    def test_code_task_multistep_inspect_edit_test(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = self.make_workspace(root)
            source = (workspace / "sample.py").read_text(encoding="utf-8")
            service, _, plan_grant = authorized_service(
                root, workspace="demo", target="test_sample"
            )

            task = service.submit_plan(
                "inspect, edit, and test the sample",
                [
                    {
                        "step_id": "inspect",
                        "tool_name": INSPECT_WORKSPACE,
                        "expected_result": "inspect sample.py",
                        "parameters": {
                            "workspace": "demo",
                            "path": "sample.py",
                        },
                    },
                    {
                        "step_id": "edit",
                        "tool_name": EDIT_TEXT_FILE,
                        "expected_result": "change the sample constant",
                        "depends_on": ["inspect"],
                        "parameters": {
                            "workspace": "demo",
                            "path": "sample.py",
                            "expected_sha256": "${inspect.selected_sha256}",
                            "old_text": "VALUE = 1",
                            "new_text": "VALUE = 2",
                            "authorization": EDIT_AUTHORIZATION_TOKEN,
                        },
                    },
                    {
                        "step_id": "test",
                        "tool_name": RUN_TESTS,
                        "expected_result": "run the workspace test",
                        "depends_on": ["edit"],
                        "parameters": {
                            "runner": "unittest",
                            "target": "test_sample",
                            "workspace": "demo",
                            "execution_backend": "host",
                            "host_authorization": plan_grant,
                            "timeout_seconds": 10,
                        },
                    },
                ],
            )

            self.assertEqual(task.status, TaskStatus.SUCCEEDED, task.error)
            self.assertEqual(
                [item.status for item in task.step_results],
                ["succeeded", "succeeded", "succeeded"],
            )
            self.assertEqual(
                task.step_results[0].output["selected_sha256"],
                digest(source),
            )
            self.assertEqual(
                task.step_results[2].output["test_exit_code"],
                0,
            )

    def test_code_task_convenience_requires_edit_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.make_workspace(root)
            service, _, token = authorized_service(
                root, workspace="demo", target="test_sample"
            )

            waiting = service.submit_code_task(
                "inspect, approve, edit, and test",
                workspace="demo",
                path="sample.py",
                old_text="VALUE = 1",
                new_text="VALUE = 2",
                test_target="test_sample",
                test_backend="host",
                host_authorization=token,
            )

            self.assertEqual(waiting.status, TaskStatus.AWAITING_APPROVAL)
            self.assertEqual(waiting.pending_approval_step_id, "edit")
            self.assertEqual(
                [item.step_id for item in waiting.step_results],
                ["inspect"],
            )
            completed = service.approve_step(
                waiting.task_id,
                "edit",
                approved=True,
            )
            self.assertEqual(completed.status, TaskStatus.SUCCEEDED, completed.error)
            self.assertEqual(
                [item.status for item in completed.step_results],
                ["succeeded", "succeeded", "succeeded"],
            )

    def test_backup_rejects_symlinked_task_directory_without_side_effect(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            tempfile.TemporaryDirectory() as outside_dir,
        ):
            root = Path(temp_dir)
            workspace = self.make_workspace(root)
            source = (workspace / "sample.py").read_text(encoding="utf-8")
            task_id = "8f3528c1-450d-4cb0-9d50-97ec10d99ada"
            outside = Path(outside_dir)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            (artifacts / task_id).symlink_to(
                outside,
                target_is_directory=True,
            )

            with self.assertRaises(WorkspaceEditError):
                apply_text_edit(
                    root,
                    artifacts,
                    task_id,
                    {
                        "workspace": "demo",
                        "path": "sample.py",
                        "expected_sha256": digest(source),
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                        "authorization": EDIT_AUTHORIZATION_TOKEN,
                    },
                )

            self.assertFalse((outside / "workspace-backups").exists())
            self.assertEqual(
                (workspace / "sample.py").read_text(encoding="utf-8"),
                source,
            )


if __name__ == "__main__":
    unittest.main()
