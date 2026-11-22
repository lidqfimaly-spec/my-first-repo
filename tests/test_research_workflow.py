from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.capabilities import CAPABILITY_REGISTRY  # noqa: E402
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import (  # noqa: E402
    INSPECT_WORKSPACE,
    RESEARCH_REPORT,
)
from zevorinth.service import TaskService  # noqa: E402


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ResearchWorkflowTests(unittest.TestCase):
    def make_workspace(self, root: Path) -> tuple[Path, str, str]:
        workspace = root / "workspaces" / "research"
        workspace.mkdir(parents=True)
        first = "Zevorinth verifies artifacts through independent readback.\n"
        second = (
            "Local source evidence is only internally consistent.\n"
            "Authenticity is not independently certified.\n"
        )
        (workspace / "source-a.txt").write_text(first, encoding="utf-8")
        (workspace / "source-b.txt").write_text(second, encoding="utf-8")
        return workspace, first, second

    def parameters(self, *, output_path: str = "report.md") -> dict[str, object]:
        return {
            "workspace": "research",
            "sources": ["source-a.txt", "source-b.txt"],
            "title": "Verified local research",
            "output_path": output_path,
            "findings": [
                {
                    "statement": "Independent readback is a core runtime property.",
                    "confidence": "supported",
                    "citations": [
                        {
                            "source_path": "source-a.txt",
                            "quote": "independent readback",
                        }
                    ],
                },
                {
                    "statement": "Local evidence has an explicit authenticity limit.",
                    "confidence": "uncertain",
                    "citations": [
                        {
                            "source_path": "source-b.txt",
                            "quote": "Authenticity is not independently certified.",
                        }
                    ],
                },
            ],
        }

    def test_capability_is_registered_as_trusted_offline_operation(self) -> None:
        details = CAPABILITY_REGISTRY["task_operations"][RESEARCH_REPORT]
        self.assertEqual(details["status"], "available")
        self.assertTrue(details["verified"])
        self.assertIn(
            "not model-selectable through the LLM planner",
            details["limitations"],
        )
        self.assertIn(
            "no network or browser access",
            " ".join(details["limitations"]),
        )

    def test_report_contains_sources_hashes_citations_and_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace, first, second = self.make_workspace(root)
            service = TaskService(root)

            task = service.submit(
                TaskRequest(
                    goal="create a traceable local research report",
                    operation=RESEARCH_REPORT,
                    parameters=self.parameters(),
                )
            )

            self.assertEqual(task.status, TaskStatus.SUCCEEDED, task.error)
            self.assertTrue(task.verification.passed)
            self.assertEqual(
                task.verification.reason,
                "research_report_verified",
            )
            report = (
                root / "artifacts" / task.task_id / "report.md"
            ).read_text(encoding="utf-8")
            self.assertIn("# Verified local research", report)
            self.assertIn("source-a.txt", report)
            self.assertIn(sha256(first), report)
            self.assertIn("source-b.txt", report)
            self.assertIn(sha256(second), report)
            self.assertIn("UNCERTAIN", report)
            self.assertIn("independent readback", report)
            self.assertIn("Network access: disabled", report)
            evidence = task.tool_result.research_evidence
            self.assertEqual(evidence["network_access"], False)
            self.assertEqual(len(evidence["sources"]), 2)
            self.assertEqual(
                evidence["report_sha256"],
                task.artifact.sha256,
            )
            self.assertTrue(service.audit_lifecycle(task.task_id)["ok"])

    def test_absent_quote_blocks_before_report_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.make_workspace(root)
            service = TaskService(root)
            parameters = self.parameters()
            parameters["findings"][0]["citations"][0]["quote"] = (
                "this quote is not in the source"
            )

            task = service.submit(
                TaskRequest(
                    goal="reject a fabricated citation",
                    operation=RESEARCH_REPORT,
                    parameters=parameters,
                )
            )

            self.assertEqual(task.status, TaskStatus.BLOCKED)
            self.assertEqual(
                task.error.code,
                "research_citation_not_found",
            )
            self.assertFalse(
                (
                    root / "artifacts" / task.task_id / "report.md"
                ).exists()
            )
            self.assertNotIn(
                "tool_started",
                [event.event_type for event in service.events(task.task_id)],
            )

    def test_undeclared_duplicate_and_sensitive_sources_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace, _, _ = self.make_workspace(root)
            (workspace / "secret.env").write_text(
                "TOKEN=not-a-real-secret\n",
                encoding="utf-8",
            )
            service = TaskService(root)
            cases = []
            duplicate = self.parameters()
            duplicate["sources"] = ["source-a.txt", "source-a.txt"]
            cases.append((duplicate, "duplicate_research_source"))
            sensitive = self.parameters()
            sensitive["sources"] = ["secret.env"]
            sensitive["findings"] = [
                {
                    "statement": "unsafe",
                    "confidence": "uncertain",
                    "citations": [
                        {
                            "source_path": "secret.env",
                            "quote": "TOKEN",
                        }
                    ],
                }
            ]
            cases.append((sensitive, "sensitive_workspace_path"))
            undeclared = self.parameters()
            undeclared["findings"][0]["citations"][0]["source_path"] = (
                "source-c.txt"
            )
            cases.append((undeclared, "citation_source_not_declared"))
            for parameters, code in cases:
                with self.subTest(code=code):
                    task = service.submit(
                        TaskRequest(
                            goal="reject unsafe research source",
                            operation=RESEARCH_REPORT,
                            parameters=parameters,
                        )
                    )
                    self.assertEqual(task.status, TaskStatus.BLOCKED)
                    self.assertEqual(task.error.code, code)

    def test_source_change_after_publication_fails_independent_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace, _, _ = self.make_workspace(root)
            base_time = "2026-10-19T00:00:00.000+00:00"
            future_time = "2026-10-19T00:02:01.000+00:00"
            service = TaskService(root, clock=lambda: base_time)
            task = service.enqueue(
                TaskRequest(
                    goal="detect source drift",
                    operation=RESEARCH_REPORT,
                    parameters=self.parameters(),
                )
            )

            def interrupt(stage: str, current: object) -> None:
                if stage == "after_research_report_before_verification":
                    raise RuntimeError("simulated report interruption")

            service.fault_hook = interrupt
            with self.assertRaisesRegex(RuntimeError, "report interruption"):
                service.execute_task(task.task_id)
            (workspace / "source-a.txt").write_text(
                "changed source\n",
                encoding="utf-8",
            )
            restarted = TaskService(root, clock=lambda: future_time)
            resumed = restarted.resume(task.task_id)
            self.assertEqual(resumed.status, TaskStatus.FAILED)
            self.assertFalse(resumed.verification.passed)
            self.assertIn(
                resumed.verification.reason,
                {
                    "research_sources_unavailable",
                    "research_evidence_mismatch",
                },
            )

    def test_existing_output_collision_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.make_workspace(root)
            service = TaskService(root)
            task_id = "4a991e7a-f8f4-4ffc-a69f-a691ce6fd3d1"
            service.enqueue(
                TaskRequest(
                    goal="collision",
                    operation=RESEARCH_REPORT,
                    parameters=self.parameters(),
                ),
                task_id=task_id,
            )
            task_dir = root / "artifacts" / task_id
            task_dir.mkdir(parents=True)
            (task_dir / "report.md").write_text(
                "preserve\n",
                encoding="utf-8",
            )
            task = service.execute_task(task_id)
            self.assertEqual(task.status, TaskStatus.BLOCKED)
            self.assertEqual(task.error.code, "artifact_exists")
            self.assertEqual(
                (task_dir / "report.md").read_text(encoding="utf-8"),
                "preserve\n",
            )

    def test_research_report_can_be_a_multi_step_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.make_workspace(root)
            service = TaskService(root)
            task = service.submit_plan(
                "inspect then research",
                [
                    {
                        "step_id": "inspect",
                        "tool_name": INSPECT_WORKSPACE,
                        "expected_result": "inspect source-a",
                        "parameters": {
                            "workspace": "research",
                            "path": "source-a.txt",
                        },
                    },
                    {
                        "step_id": "research",
                        "tool_name": RESEARCH_REPORT,
                        "expected_result": "create traceable report",
                        "depends_on": ["inspect"],
                        "parameters": self.parameters(),
                    },
                ],
            )
            self.assertEqual(task.status, TaskStatus.SUCCEEDED, task.error)
            self.assertEqual(
                [item.status for item in task.step_results],
                ["succeeded", "succeeded"],
            )
            report_step = task.step_results[1]
            report_path = (
                root
                / "artifacts"
                / str(report_step.child_task_id)
                / "report.md"
            )
            self.assertTrue(report_path.is_file())

    def test_recovered_published_report_is_reverified_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.make_workspace(root)
            base_time = "2026-10-19T00:00:00.000+00:00"
            future_time = "2026-10-19T00:02:01.000+00:00"
            service = TaskService(root, clock=lambda: base_time)
            task = service.enqueue(
                TaskRequest(
                    goal="recover a published report",
                    operation=RESEARCH_REPORT,
                    parameters=self.parameters(),
                )
            )
            original_record = service.store.record_event_and_update

            def record_then_fail(task_id, event_type, *args, **kwargs):
                if str(getattr(event_type, "value", event_type)) == "tool_succeeded":
                    raise OSError("simulated crash before tool result commit")
                return original_record(task_id, event_type, *args, **kwargs)

            service.store.record_event_and_update = record_then_fail
            with self.assertRaisesRegex(OSError, "tool result commit"):
                service.execute_task(task.task_id)

            restarted = TaskService(root, clock=lambda: future_time)
            resumed = restarted.resume(task.task_id)
            self.assertEqual(resumed.status, TaskStatus.SUCCEEDED, resumed.error)
            evidence = resumed.tool_result.research_evidence
            self.assertEqual(
                resumed.artifact.sha256,
                evidence["report_sha256"],
            )

    def test_generation_time_is_frozen_at_execution_not_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.make_workspace(root)
            created_at = "2026-10-19T00:00:00.000+00:00"
            executed_at = "2026-10-19T01:00:00.000+00:00"
            queued_service = TaskService(root, clock=lambda: created_at)
            task = queued_service.enqueue(
                TaskRequest(
                    goal="freeze research generation time",
                    operation=RESEARCH_REPORT,
                    parameters=self.parameters(),
                )
            )
            self.assertEqual(task.created_at, created_at)

            service = TaskService(root, clock=lambda: executed_at)
            completed = service.execute_task(task.task_id)

            self.assertEqual(completed.status, TaskStatus.SUCCEEDED)
            evidence = completed.tool_result.research_evidence
            self.assertEqual(evidence["generated_at"], executed_at)
            self.assertNotEqual(evidence["generated_at"], created_at)
            report = (
                root / "artifacts" / task.task_id / "report.md"
            ).read_text(encoding="utf-8")
            self.assertIn(f"Generated at: `{executed_at}`", report)


if __name__ == "__main__":
    unittest.main()
