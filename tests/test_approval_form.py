"""F-11: the approval form has to say what the human is saying yes to.

The gap this closes: the control centre offered ``approve_step`` as a bare
checkbox next to a step id. A person could consent to a side effect they had
never seen. Each test here pins one fact the form must carry, or one place
where it must refuse to pretend.

Honest scope note: one case below ("approved, then content rewritten under the
same id") cannot be produced through the public API today — approval executes
the step immediately and a completed step cannot be replanned. That case
builds the record state directly, for the same reason R-08's test does: an
older persisted record can hold that state, and the form must describe it
truthfully rather than assume it away.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.approval_preview import (  # noqa: E402
    APPROVAL_FORM_VERSION,
    build_approval_form,
)
from zevorinth.models import (  # noqa: E402
    ExecutionMode,
    PlannerMode,
    PlanStep,
    Task,
    TaskRequest,
    TaskStatus,
)
from zevorinth.multi_step import step_approval_digest  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.workbench_server import create_server  # noqa: E402


def write_step(
    step_id: str,
    artifact_path: str,
    content: object,
    *,
    depends_on: list[str] | None = None,
    requires_approval: bool = True,
) -> dict[str, object]:
    return {
        "step_id": step_id,
        "tool_name": "write_text_artifact",
        "expected_result": f"create {artifact_path}",
        "parameters": {"artifact_path": artifact_path, "content": content},
        "depends_on": list(depends_on or []),
        "requires_approval": requires_approval,
        "max_attempts": 1,
    }


def _waiting(root: Path, steps: list[dict[str, object]]) -> Task:
    """Drive the real engine until it stops for approval."""
    task = TaskService(root).submit_plan("approval form", steps)
    assert task.status is TaskStatus.AWAITING_APPROVAL, task.status
    return task


class ApprovalFormContentTests(unittest.TestCase):
    def test_form_names_the_tool_and_its_full_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task = _waiting(root, [write_step("write", "draft.txt", "draft\n")])
            form = build_approval_form(task, project_root=root)
            assert form is not None
            self.assertEqual(form["contract_version"], APPROVAL_FORM_VERSION)
            self.assertEqual(form["step"]["step_id"], "write")
            self.assertEqual(form["step"]["tool_name"], "write_text_artifact")
            self.assertEqual(form["step"]["tool_label"], "写一个新文件")
            self.assertEqual(form["step"]["side_effect"], "creates_artifact")
            self.assertIn("新建一个文件", form["step"]["side_effect_sentence"])
            named = {
                row["name"]: row["value"] for row in form["step"]["parameters"]
            }
            self.assertEqual(named["artifact_path"], "draft.txt")
            # Content is shown in full: approving text you cannot read is the
            # exact failure this form exists to prevent.
            self.assertEqual(named["content"], "draft\n")
            self.assertEqual(form["step"]["deferred_parameters"], [])
            self.assertEqual(form["approval"]["state"], "approval_required")

    def test_form_reports_the_target_file_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task = _waiting(root, [write_step("write", "draft.txt", "draft\n")])
            form = build_approval_form(task, project_root=root)
            assert form is not None and form["target"] is not None
            self.assertEqual(form["target"]["kind"], "artifact")
            self.assertEqual(form["target"]["path"], "draft.txt")
            self.assertEqual(form["target"]["state"], "absent")
            self.assertIn("还不存在", form["target"]["sentence"])

    def test_an_occupied_artifact_name_is_reported_before_approval(self) -> None:
        """The write tool never overwrites, so a taken name means failure."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task = _waiting(root, [write_step("write", "draft.txt", "draft\n")])
            occupied = root / "artifacts" / task.task_id / "draft.txt"
            occupied.parent.mkdir(parents=True)
            occupied.write_text("already here\n", encoding="utf-8")
            form = build_approval_form(task, project_root=root)
            assert form is not None and form["target"] is not None
            self.assertEqual(form["target"]["state"], "occupied")
            self.assertIn("会失败", form["target"]["sentence"])

    def test_form_says_when_a_parameter_resolves_later(self) -> None:
        """A deferred value is a rule, not content — and must say so."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task = _waiting(
                root,
                [
                    write_step(
                        "read", "source.txt", "source\n", requires_approval=False
                    ),
                    write_step(
                        "copy",
                        "copy.txt",
                        "${read.content}",
                        depends_on=["read"],
                    ),
                ],
            )
            form = build_approval_form(task, project_root=root)
            assert form is not None
            self.assertEqual(form["step"]["step_id"], "copy")
            self.assertEqual(form["step"]["deferred_parameters"], ["content"])
            rows = {row["name"]: row for row in form["step"]["parameters"]}
            self.assertTrue(rows["content"]["deferred"])

    def test_no_pending_step_means_no_form(self) -> None:
        task = Task(
            task_id="11111111-1111-4111-8111-111111111111",
            status=TaskStatus.QUEUED,
            request=TaskRequest(goal="x", operation="write_text_artifact"),
            execution_mode=ExecutionMode.REAL,
            attempt_id="a",
            phase="queued",
            created_at="2026-11-22T00:00:00+00:00",
            updated_at="2026-11-22T00:00:00+00:00",
            planner_mode=PlannerMode.DETERMINISTIC,
        )
        self.assertIsNone(build_approval_form(task, project_root=Path("/tmp")))

    def test_parameters_are_redacted_before_display(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
            task = _waiting(
                root,
                [write_step("write", "secret.txt", f"API_KEY={secret}\n")],
            )
            form = build_approval_form(
                task,
                project_root=root,
                redact=lambda value: value.replace(secret, "[redacted]"),
            )
            assert form is not None
            rows = {
                row["name"]: row["value"]
                for row in form["step"]["parameters"]
            }
            self.assertNotIn("sk-abcdef", rows["content"])
            self.assertIn("[redacted]", rows["content"])


class ApprovalFormApprovalStateTests(unittest.TestCase):
    def _record(
        self,
        task: Task,
        *,
        approved_digest: str,
    ) -> Task:
        data = task.to_dict()
        data["approved_step_ids"] = [task.pending_approval_step_id]
        data["approved_step_digests"] = {
            task.pending_approval_step_id: approved_digest
        }
        return Task.from_dict(data)

    def test_reports_that_content_changed_under_the_same_step_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task = _waiting(root, [write_step("write", "draft.txt", "draft\n")])
            approved = PlanStep.from_dict(
                {
                    **write_step("write", "draft.txt", "the old content\n"),
                }
            )
            changed = self._record(
                task,
                approved_digest=step_approval_digest(
                    approved, task.plan.plan_id
                ),
            )
            form = build_approval_form(changed, project_root=root)
            assert form is not None
            self.assertEqual(
                form["approval"]["state"], "approval_content_changed"
            )
            self.assertIn("内容已经变了", form["approval"]["state_label"])
            self.assertNotEqual(
                form["approval"]["digest"],
                form["approval"]["approved_digest"],
            )

    def test_reports_a_matching_approval_without_asking_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task = _waiting(root, [write_step("write", "draft.txt", "draft\n")])
            step = PlanStep.from_dict(write_step("write", "draft.txt", "draft\n"))
            same = self._record(
                task,
                approved_digest=step_approval_digest(
                    step, task.plan.plan_id
                ),
            )
            form = build_approval_form(same, project_root=root)
            assert form is not None
            self.assertEqual(form["approval"]["state"], "approved_matching")
            self.assertEqual(
                form["approval"]["digest"], form["approval"]["approved_digest"]
            )


class ApprovalFormWorkspaceTargetTests(unittest.TestCase):
    def _edit_task(self, root: Path, expected_sha256: str) -> Task:
        return _waiting(
            root,
            [
                {
                    "step_id": "edit",
                    "tool_name": "edit_text_file",
                    "expected_result": "patch the file",
                    "parameters": {
                        "workspace": "demo",
                        "path": "note.txt",
                        "expected_sha256": expected_sha256,
                        "old_text": "old",
                        "new_text": "new",
                        "authorization": "local-code-edit",
                    },
                    "requires_approval": True,
                    "max_attempts": 1,
                }
            ],
        )

    def test_matching_file_digest_is_reported_as_safe_to_approve(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "note.txt").write_text("old text\n", encoding="utf-8")
            digest = hashlib.sha256(b"old text\n").hexdigest()
            task = self._edit_task(root, digest)
            form = build_approval_form(task, project_root=root)
            assert form is not None and form["target"] is not None
            self.assertEqual(form["target"]["kind"], "workspace_file")
            self.assertEqual(form["target"]["state"], "present_matching")
            self.assertEqual(form["target"]["current_sha256"], digest)

    def test_changed_file_is_reported_as_a_broken_precondition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspaces" / "demo"
            workspace.mkdir(parents=True)
            (workspace / "note.txt").write_text(
                "someone edited this\n", encoding="utf-8"
            )
            task = self._edit_task(root, "0" * 64)
            form = build_approval_form(task, project_root=root)
            assert form is not None and form["target"] is not None
            self.assertEqual(form["target"]["state"], "present_changed")
            self.assertIn("不一致", form["target"]["sentence"])

    def test_missing_workspace_is_unknown_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task = self._edit_task(root, "0" * 64)
            form = build_approval_form(task, project_root=root)
            assert form is not None and form["target"] is not None
            self.assertEqual(form["target"]["state"], "unknown")
            self.assertIn("没法确认", form["target"]["sentence"])

    def test_reading_the_form_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task = _waiting(root, [write_step("write", "draft.txt", "draft\n")])
            before = sorted(str(path) for path in root.rglob("*"))
            build_approval_form(task, project_root=root)
            after = sorted(str(path) for path in root.rglob("*"))
            self.assertEqual(before, after)


class ApprovalFormControlSurfaceTests(unittest.TestCase):
    def test_control_preview_carries_the_form(self) -> None:
        """The button must arrive with its content, not fetch it later."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data").mkdir(parents=True)
            waiting = _waiting(
                root, [write_step("write", "draft.txt", "draft\n")]
            )
            server = create_server(root, port=0, control_enabled=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host = f"127.0.0.1:{server.server_address[1]}"
                with urlopen(
                    "http://"
                    + host
                    + f"/control/api/tasks/{waiting.task_id}/actions",
                    timeout=10,
                ) as response:
                    payload = json.loads(response.read())
                actions = {item["action"]: item for item in payload["actions"]}
                self.assertIn("approve_step", actions)
                form = actions["approve_step"]["approval_form"]
                self.assertIsNotNone(form)
                self.assertEqual(form["step"]["step_id"], "write")
                self.assertEqual(
                    form["step"]["tool_name"], "write_text_artifact"
                )
                # Rejecting is always possible, and must carry the same facts.
                self.assertEqual(
                    actions["reject_step"]["approval_form"], form
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)


if __name__ == "__main__":
    unittest.main()
