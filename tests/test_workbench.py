"""Phase 3.91: read-only local task workbench."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.cli import build_parser  # noqa: E402
from zevorinth.models import EventType, TaskRequest, TaskStatus  # noqa: E402
from zevorinth.recovery_preview import (  # noqa: E402
    RecoveryApprovalStore,
    approve_recovery_preview,
)
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.workbench import (  # noqa: E402
    MAX_TASK_LIMIT,
    ReadOnlyWorkbench,
    WorkbenchError,
    _payload_summary,
)
from zevorinth.workbench_server import create_server  # noqa: E402


I18N_PATH = (
    PROJECT_ROOT
    / "src"
    / "zevorinth"
    / "workbench_static"
    / "i18n.zh-CN.json"
)


def _i18n() -> dict:
    return json.loads(I18N_PATH.read_text(encoding="utf-8"))


def _logical_database_digest(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        dump = "\n".join(connection.iterdump()).encode("utf-8")
        return hashlib.sha256(dump).hexdigest()
    finally:
        connection.close()


def _create_tasks(root: Path) -> tuple[TaskService, object, object, object]:
    service = TaskService(root)
    success = service.submit(
        TaskRequest(
            goal="创建 verified.txt，内容写 verified content。",
            operation="write_text_artifact",
            parameters={
                "artifact_path": "verified.txt",
                "content": "verified content\n",
            },
        )
    )
    clarification = service.submit(
        TaskRequest(
            goal="创建一个需要补充字段的文件",
            operation="write_text_artifact",
            parameters={},
        )
    )
    blocked = service.submit(
        TaskRequest(
            goal="写入不安全路径",
            operation="write_text_artifact",
            parameters={
                "artifact_path": "../escape.txt",
                "content": "blocked",
            },
        )
    )
    return service, success, clarification, blocked


class WorkbenchHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (
            self.service,
            self.success,
            self.clarification,
            self.blocked,
        ) = _create_tasks(self.root)
        self._save_stale_approval(self.success.task_id)
        self.server = create_server(self.root, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]
        self.base = f"http://{self.host}:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def _save_stale_approval(self, task_id: str) -> None:
        preview = self.service.recovery_preview(task_id)
        action = preview["actions"][0]
        approval = approve_recovery_preview(
            self.service.recovery_preview_for_test(task_id)
            if hasattr(self.service, "recovery_preview_for_test")
            else _to_preview_object(preview),
            approver="workbench-test",
            action=action["action"],
            target=action["target"],
            now="2026-10-21T00:00:00.000+00:00",
        )
        RecoveryApprovalStore(self.root).save(approval)
        self.service.store.record_event_and_update(
            task_id,
            "plan_revised",
            "2026-10-21T00:00:01.000+00:00",
            payload={"source": "workbench-stale-test"},
        )

    def _json(self, path: str) -> tuple[int, dict, dict]:
        with urlopen(f"{self.base}{path}", timeout=5) as response:
            return (
                response.status,
                dict(response.headers),
                json.loads(response.read().decode("utf-8")),
            )

    def test_task_discovery_detail_and_attention_are_read_only(self) -> None:
        database = self.root / "data" / "zevorinth.sqlite3"
        artifact = (
            self.root
            / "artifacts"
            / self.success.task_id
            / "verified.txt"
        )
        before_database = _logical_database_digest(database)
        before_artifact = hashlib.sha256(artifact.read_bytes()).hexdigest()
        before_events = self.service.events(self.success.task_id)

        status, headers, listing = self._json("/api/tasks?limit=40")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(len(listing["tasks"]), 3)
        self.assertTrue(listing["read_only"])
        self.assertEqual(listing["page"]["limit"], 40)

        detail_status, _, detail = self._json(
            f"/api/tasks/{self.success.task_id}"
        )
        self.assertEqual(detail_status, 200)
        self.assertEqual(detail["task"]["status"], "succeeded")
        self.assertNotIn("lease_token", detail["task"])
        self.assertEqual(detail["task"]["lease_state"], "none")
        self.assertFalse(detail["task"]["claim_held"])
        self.assertIn("request_digest", detail["task"])
        self.assertIn("plan_revisions", detail["task"])
        self.assertEqual(detail["task"]["plan_revision_count"], 0)
        self.assertTrue(detail["lifecycle_audit"]["ok"])
        self.assertEqual(
            [event["sequence"] for event in detail["events"]],
            sorted(event["sequence"] for event in detail["events"]),
        )
        self.assertIn("recovery_preview", detail)
        self.service.store.record_event_and_update(
            self.clarification.task_id,
            "plan_revised",
            "2026-10-21T00:00:02.000+00:00",
            payload={
                "api_key": "must-not-leak",
                "note": "Authorization: Bearer abc.def",
            },
        )
        before_database = _logical_database_digest(database)
        _, _, clarification_detail = self._json(
            f"/api/tasks/{self.clarification.task_id}"
        )
        encoded = json.dumps(clarification_detail)
        self.assertNotIn("must-not-leak", encoded)
        self.assertNotIn("abc.def", encoded)
        sensitive_event = clarification_detail["events"][-1]
        self.assertNotIn("api_key", sensitive_event["payload_summary"])
        self.assertIn(
            "[redacted]",
            sensitive_event["payload_summary"]["note"],
        )

        attention_status, _, attention = self._json("/api/attention")
        self.assertEqual(attention_status, 200)
        kinds = {item["attention"]["kind"] for item in attention["items"]}
        self.assertIn("clarification", kinds)
        self.assertIn("blocked", kinds)
        self.assertTrue(
            any(
                item.get("kind") == "approval"
                and item.get("approval_status") == "stale"
                for item in attention["items"]
            )
        )

        after_database = _logical_database_digest(database)
        after_artifact = hashlib.sha256(artifact.read_bytes()).hexdigest()
        after_events = self.service.events(self.success.task_id)
        self.assertEqual(before_database, after_database)
        self.assertEqual(before_artifact, after_artifact)
        self.assertEqual(
            [event.to_dict() for event in before_events],
            [event.to_dict() for event in after_events],
        )

    def test_status_filter_and_bounded_pagination(self) -> None:
        _, _, blocked = self._json("/api/tasks?status=blocked")
        self.assertEqual(len(blocked["tasks"]), 1)
        self.assertEqual(blocked["tasks"][0]["task_id"], self.blocked.task_id)
        _, _, first = self._json("/api/tasks?limit=2&offset=0")
        self.assertEqual(len(first["tasks"]), 2)
        self.assertEqual(first["page"]["next_offset"], 2)
        _, _, second = self._json("/api/tasks?limit=2&offset=2")
        self.assertEqual(len(second["tasks"]), 1)
        self.assertIsNone(second["page"]["next_offset"])

    def test_registered_artifact_preview_is_bounded_to_task_evidence(self) -> None:
        status, _, preview = self._json(
            f"/api/tasks/{self.success.task_id}/artifact"
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["artifact_path"], "verified.txt")
        self.assertEqual(preview["content"], "verified content\n")
        self.assertEqual(preview["state"], "verified")
        self.assertEqual(preview["observed"], preview["observed"])
        self.assertEqual(
            preview["observed"]["sha256"],
            preview["registered"]["sha256"],
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(
                f"{self.base}/api/tasks/{self.success.task_id}/artifact"
                "?path=/etc/passwd",
                timeout=5,
            )
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        with self.assertRaises(HTTPError) as caught:
            urlopen(
                f"{self.base}/api/tasks/{self.success.task_id}/artifact/../../etc/passwd",
                timeout=5,
            )
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_multi_step_parent_artifact_is_available_through_http(
        self,
    ) -> None:
        task = self.service.submit_plan(
            "两步骤交付并验证",
            [
                {
                    "step_id": "source",
                    "tool_name": "write_text_artifact",
                    "expected_result": "创建源文件",
                    "parameters": {
                        "artifact_path": "source.txt",
                        "content": "source value",
                    },
                },
                {
                    "step_id": "copy",
                    "tool_name": "write_text_artifact",
                    "expected_result": "创建最终副本",
                    "depends_on": ["source"],
                    "parameters": {
                        "artifact_path": "final.txt",
                        "content": "${source.content}",
                    },
                },
            ],
        )
        self.assertEqual(task.status, TaskStatus.SUCCEEDED, task.error)
        self.assertIsNotNone(task.artifact)
        self.assertEqual(task.artifact.relative_path, "final.txt")

        detail_status, _, detail = self._json(
            f"/api/tasks/{task.task_id}"
        )
        self.assertEqual(detail_status, 200)
        self.assertEqual(
            detail["task"]["artifact_observation"]["state"],
            "current",
        )

        preview_status, _, preview = self._json(
            f"/api/tasks/{task.task_id}/artifact"
        )
        self.assertEqual(preview_status, 200)
        self.assertEqual(preview["content"], "source value")
        self.assertEqual(preview["state"], "verified")

    def test_artifact_change_is_reported_without_restoring_it(self) -> None:
        artifact = (
            self.root
            / "artifacts"
            / self.success.task_id
            / "verified.txt"
        )
        artifact.write_text("changed after verification\n", encoding="utf-8")
        _, _, preview = self._json(
            f"/api/tasks/{self.success.task_id}/artifact"
        )
        self.assertEqual(preview["state"], "modified_since_registration")
        self.assertTrue(preview["verification"]["passed"])
        _, _, detail = self._json(f"/api/tasks/{self.success.task_id}")
        self.assertEqual(
            detail["task"]["artifact_observation"]["state"],
            "modified_since_registration",
        )
        self.assertEqual(
            detail["task"]["integrity_attention"]["kind"],
            "artifact_integrity",
        )
        self.assertEqual(
            artifact.read_text(encoding="utf-8"),
            "changed after verification\n",
        )

    def test_mutation_methods_and_unknown_routes_are_rejected(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            request = Request(
                f"{self.base}/api/tasks",
                method=method,
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with self.subTest(method=method), self.assertRaises(
                HTTPError
            ) as caught:
                urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 405)
            self.assertEqual(
                caught.exception.headers["Allow"],
                "GET, HEAD",
            )
            caught.exception.close()
        with self.assertRaises(HTTPError) as caught:
            urlopen(f"{self.base}/api/tasks/not-a-task", timeout=5)
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_events_stream_head_returns_sse_content_type(self) -> None:
        request = Request(
            f"{self.base}/api/tasks/{self.success.task_id}/events/stream",
            method="HEAD",
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(
                "text/event-stream",
                response.headers["Content-Type"],
            )

    def test_events_stream_is_read_only(self) -> None:
        digest_before = _logical_database_digest(
            self.root / "data" / "zevorinth.sqlite3"
        )
        request = Request(
            f"{self.base}/api/tasks/{self.success.task_id}/events/stream",
            method="HEAD",
        )
        with urlopen(request, timeout=5):
            pass
        digest_after = _logical_database_digest(
            self.root / "data" / "zevorinth.sqlite3"
        )
        self.assertEqual(digest_before, digest_after)

    def test_events_stream_rejects_unknown_task(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self._json(
                f"/api/tasks/{uuid.uuid4()}/events/stream"
            )
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_events_stream_rejects_mutations(self) -> None:
        request = Request(
            f"{self.base}/api/tasks/{self.success.task_id}/events/stream",
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 405)
        caught.exception.close()

    def test_task_events_since_returns_incremental_projections(self) -> None:
        workbench = ReadOnlyWorkbench(self.root)
        all_events = workbench.task_events_since(
            self.success.task_id, 0
        )
        self.assertTrue(all_events)
        max_seq = max(item["sequence"] for item in all_events)
        self.assertEqual(
            workbench.task_events_since(self.success.task_id, max_seq),
            [],
        )
        self.assertEqual(
            set(all_events[0]),
            {
                "sequence",
                "event_id",
                "task_id",
                "event_type",
                "occurred_at",
                "payload_summary",
            },
        )

    def test_model_usage_projection_has_required_shape(self) -> None:
        _, _, detail = self._json(f"/api/tasks/{self.success.task_id}")
        usage = detail["model_usage"]
        self.assertTrue(usage["read_only"])
        self.assertIn(
            usage["measurement_note"],
            {"no_model_call", "missing", "provider_reported"},
        )
        self.assertIsInstance(usage["calls"], list)
        self.assertEqual(usage["call_count"], len(usage["calls"]))
        self.assertIn("total_input_tokens", usage)
        self.assertIn("total_output_tokens", usage)
        self.assertIn("total_tokens", usage)
        self.assertIn("total_latency_ms", usage)
        self.assertIsNone(usage["cost_estimate"])
        self.assertEqual(usage["cost_note"], "no_price_table")

    def test_model_usage_aggregates_injected_planning_tokens(self) -> None:
        from zevorinth.models import Event

        event = Event(
            sequence=0,
            event_id=str(uuid.uuid4()),
            task_id=self.success.task_id,
            event_type="task_planned",
            occurred_at="2026-11-21T00:00:00.000+00:00",
            payload={
                "planning_metadata": {
                    "classification": "planned",
                    "latency_ms": 523.1,
                    "input_tokens": 120,
                    "output_tokens": 45,
                    "total_tokens": 165,
                    "response_id": "resp_test_aggregate",
                    "model_call_attempted": True,
                }
            },
        )
        self.service.store.append_event(self.success.task_id, event)
        _, _, detail = self._json(f"/api/tasks/{self.success.task_id}")
        usage = detail["model_usage"]
        injected = [
            call
            for call in usage["calls"]
            if call.get("response_id") == "resp_test_aggregate"
        ]
        self.assertEqual(len(injected), 1)
        self.assertEqual(injected[0]["input_tokens"], 120)
        self.assertEqual(injected[0]["output_tokens"], 45)
        self.assertEqual(injected[0]["total_tokens"], 165)
        self.assertEqual(injected[0]["latency_ms"], 523.1)
        self.assertEqual(usage["measurement_note"], "provider_reported")
        self.assertGreaterEqual(usage["total_input_tokens"], 120)
        self.assertGreaterEqual(usage["total_output_tokens"], 45)
        self.assertGreaterEqual(usage["total_tokens"], 165)

    def test_model_usage_reports_missing_not_zero(self) -> None:
        from zevorinth.models import Event

        event = Event(
            sequence=0,
            event_id=str(uuid.uuid4()),
            task_id=self.success.task_id,
            event_type="task_planned",
            occurred_at="2026-11-21T00:00:01.000+00:00",
            payload={
                "planning_metadata": {
                    "classification": "failed",
                    "latency_ms": 100.0,
                    "model_call_attempted": True,
                    "error_type": "TimeoutError",
                }
            },
        )
        self.service.store.append_event(self.success.task_id, event)
        _, _, detail = self._json(f"/api/tasks/{self.success.task_id}")
        usage = detail["model_usage"]
        self.assertGreaterEqual(usage["missing_usage_count"], 1)
        # Missing usage must never be reported as zero.
        if not usage["has_complete_usage"]:
            self.assertIsNone(usage["total_input_tokens"])
            self.assertIsNone(usage["total_output_tokens"])

    def test_static_assets_have_no_external_dependencies(self) -> None:
        with urlopen(f"{self.base}/", timeout=5) as response:
            html = response.read().decode("utf-8")
            content_type = response.headers["Content-Type"]
        with urlopen(f"{self.base}/static/app.js", timeout=5) as response:
            script = response.read().decode("utf-8")
        with urlopen(
            f"{self.base}/static/i18n.zh-CN.json",
            timeout=5,
        ) as response:
            translations = json.loads(response.read().decode("utf-8"))
        self.assertIn("text/html", content_type)
        self.assertIn('lang="zh-CN"', html)
        self.assertIn("只读", html)
        self.assertIn("任务列表", html)
        self.assertIn('id="create-task-form"', html)
        self.assertIn('id="create-preview-goal"', html)
        self.assertIn('id="create-structured-mode"', html)
        self.assertIn('id="create-structured-parameters"', html)
        self.assertIn('id="create-json-mode"', html)
        self.assertIn('id="create-and-submit"', html)
        self.assertNotIn("Skip to task detail", html)
        self.assertNotIn("Needs attention", html)
        self.assertNotIn("https://", html)
        self.assertIn("默认仅提供", html)
        self.assertNotIn("此处仅开放澄清回答", html)
        self.assertIn('method: "GET"', script)
        self.assertNotIn("/api/recover", script)
        self.assertIn("X-Zevorinth-Control-Token", script)
        self.assertNotIn("innerHTML", script)
        self.assertIn("textContent", script)
        self.assertIn('"/control/api/tasks"', script)
        self.assertIn('"/control/api/preview"', script)
        self.assertIn("createGoalPreview", script)
        self.assertIn("createGuidedRoute", script)
        self.assertIn("preview.guided_flow", script)
        self.assertIn("selected_task_id: state.selectedTaskId", script)
        self.assertIn("beginArtifactContinuation", script)
        self.assertIn("continue_artifact", script)
        self.assertIn("continuation_source_bound", script)
        self.assertIn("createSelectField", script)
        self.assertIn("modify_mode_append", script)
        self.assertIn('operation.value === "run_tests"', script)
        self.assertIn("create_missing_fields", script)
        self.assertIn("guidedParametersComplete", script)
        self.assertIn('operation === "edit_text_file"', script)
        self.assertNotIn('compact.authorization = "local-code-edit"', script)
        self.assertIn('operation === "research_report"', script)
        self.assertIn("research_findings_rows", script)
        self.assertIn("stateActionForm", script)
        self.assertIn("approvalStepActionForm", script)
        self.assertIn("replanActionForm", script)
        self.assertIn("taskNoteForm(", script)
        self.assertIn("conversationPanel(", script)
        self.assertEqual(translations["language"], "zh-CN")
        self.assertIn("continue_artifact", translations["ui"])
        self.assertIn("continuation_source_bound", translations["ui"])
        self.assertIn("modify_mode_append", translations["ui"])
        self.assertIn("modify_mode_replace", translations["ui"])

    def test_head_supported_and_non_loopback_rejected(self) -> None:
        request = Request(f"{self.base}/api/tasks", method="HEAD")
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
        with self.assertRaisesRegex(ValueError, "loopback"):
            create_server(self.root, host="0.0.0.0", port=0)
        request = Request(
            f"{self.base}/api/tasks",
            headers={"Host": "attacker.example"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 421)
        caught.exception.close()


class WorkbenchMissingStoreTests(unittest.TestCase):
    def test_missing_database_returns_safe_service_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = create_server(Path(temp_dir), port=0)
            thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            thread.start()
            host, port = server.server_address[:2]
            try:
                with self.assertRaises(HTTPError) as caught:
                    urlopen(f"http://{host}:{port}/api/tasks", timeout=5)
                self.assertEqual(caught.exception.code, 503)
                payload = json.loads(
                    caught.exception.read().decode("utf-8")
                )
                self.assertEqual(
                    payload["error"]["code"],
                    "task_store_unavailable",
                )
                caught.exception.close()
                self.assertFalse(
                    (Path(temp_dir) / "data" / "zevorinth.sqlite3").exists()
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class WorkbenchProjectionTests(unittest.TestCase):
    def test_nested_clarification_parameters_still_redact_sensitive_keys(self) -> None:
        summary = _payload_summary(
            {
                "provided_parameters": {
                    "api_key": "secret-value",
                    "content": "safe",
                }
            }
        )
        self.assertEqual(summary["provided_parameters"], {"content": "safe"})

    def test_repository_rejects_invalid_page_and_status_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_tasks(root)
            workbench = ReadOnlyWorkbench(root)
            with self.assertRaisesRegex(WorkbenchError, "limit"):
                workbench.list_tasks(limit=MAX_TASK_LIMIT + 1)
            with self.assertRaisesRegex(WorkbenchError, "status"):
                workbench.list_tasks(status="invented")

    def test_repository_searches_by_id_goal_and_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, success, _, _ = _create_tasks(root)
            workbench = ReadOnlyWorkbench(root)
            self.assertEqual(
                workbench.list_tasks(query=success.task_id)["tasks"][0][
                    "task_id"
                ],
                success.task_id,
            )
            self.assertEqual(
                len(workbench.list_tasks(query="需要补充字段")["tasks"]),
                1,
            )
            self.assertEqual(
                len(workbench.list_tasks(query="write_text_artifact")["tasks"]),
                3,
            )
            self.assertEqual(workbench.list_tasks(query="%")["tasks"], [])

    def test_repository_connection_rejects_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_tasks(root)
            workbench = ReadOnlyWorkbench(root)
            with workbench._connect() as connection:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM tasks")

    def test_control_request_evidence_is_targeted_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            task = service.enqueue(
                TaskRequest(
                    goal="限定控制请求证据范围",
                    operation="write_text_artifact",
                    parameters={
                        "artifact_path": "bounded.txt",
                        "content": "bounded\n",
                    },
                )
            )
            request_id = str(uuid.uuid4())
            for index in range(70):
                service.store.record_event_and_update(
                    task.task_id,
                    EventType.CONTROL_ACTION_REQUESTED,
                    f"2026-10-22T00:00:{index % 60:02d}.000+00:00",
                    payload={
                        "request_id": str(uuid.uuid4()),
                        "action": "revise_unstarted_task",
                        "request_digest": "a" * 64,
                    },
                )
            service.store.record_event_and_update(
                task.task_id,
                EventType.CONTROL_ACTION_REQUESTED,
                "2026-10-22T00:01:00.000+00:00",
                payload={
                    "request_id": request_id,
                    "action": "revise_unstarted_task",
                    "request_digest": "a" * 64,
                },
            )
            workbench = ReadOnlyWorkbench(root)
            evidence = workbench.control_request_evidence(
                task.task_id,
                request_id,
            )
            self.assertEqual(len(evidence["control_events"]), 1)
            for index in range(64):
                service.store.record_event_and_update(
                    task.task_id,
                    EventType.CONTROL_ACTION_REQUESTED,
                    f"2026-10-22T00:02:{index % 60:02d}.000+00:00",
                    payload={
                        "request_id": request_id,
                        "action": "revise_unstarted_task",
                        "request_digest": "a" * 64,
                    },
                )
            with self.assertRaises(WorkbenchError) as caught:
                workbench.control_request_evidence(
                    task.task_id,
                    request_id,
                )
            self.assertEqual(
                caught.exception.code,
                "control_request_evidence_too_large",
            )

    def test_task_detail_reports_authoritative_revision_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            task = service.enqueue(
                TaskRequest(
                    goal="创建 bounded.txt",
                    operation="write_text_artifact",
                    parameters={
                        "artifact_path": "bounded.txt",
                        "content": "bounded\n",
                    },
                )
            )
            for index in range(21):
                service.revise_plan(
                    task.task_id,
                    {"goal": f"历史修订 {index}"},
                    source_id=f"revision-count-{index}",
                    expected_plan_version=index,
                    confirmed=True,
                )
            detail = ReadOnlyWorkbench(root).task_detail(task.task_id)
        self.assertEqual(detail["task"]["plan_revision_count"], 21)
        self.assertEqual(len(detail["task"]["plan_revisions"]), 20)
        self.assertEqual(detail["task"]["plan_version"], 21)

    def test_clarification_detail_uses_stage_applicable_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = TaskService(root)
            task = service.submit(
                TaskRequest(
                    goal="run tests before workspace is known",
                    operation="run_tests",
                    parameters={
                        "runner": "unittest",
                        "target": "tests.test_demo",
                    },
                )
            )
            detail = ReadOnlyWorkbench(root).task_detail(task.task_id)
        self.assertEqual(detail["task"]["status"], "awaiting_clarification")
        self.assertTrue(detail["lifecycle_audit"]["ok"])
        self.assertNotIn(
            "missing_final_execution_plan",
            [item["code"] for item in detail["lifecycle_audit"]["issues"]],
        )
        action = detail["recovery_preview"]["actions"][0]
        self.assertEqual(action["action"], "none")
        self.assertEqual(action["risk"], "low")

    def test_chinese_mapping_covers_statuses_events_and_semantics(self) -> None:
        translations = _i18n()
        self.assertEqual(
            set(translations["statuses"]),
            {status.value for status in TaskStatus},
        )
        self.assertEqual(
            set(translations["events"]),
            {event.value for event in EventType},
        )
        self.assertEqual(
            translations["statuses"]["awaiting_clarification"]["label"],
            "等待补充信息",
        )
        self.assertEqual(
            translations["statuses"]["failed"]["label"],
            "执行失败",
        )
        self.assertEqual(
            translations["attention_kinds"]["clarification"],
            "需要你补充信息",
        )
        self.assertEqual(
            translations["artifact_states"]["modified_since_registration"],
            "验证后已发生变化",
        )
        self.assertEqual(
            translations["artifact_states"]["verified"],
            "当前内容与历史验证结论一致",
        )
        self.assertEqual(
            translations["artifact_states"]["unverified"],
            "当前没有可确认的通过验证记录",
        )
        self.assertEqual(
            translations["risk_levels"]["low"],
            "低风险",
        )
        self.assertEqual(
            translations["completeness_states"]["open"],
            "当前阶段尚未闭合",
        )
        self.assertIn("unknown_status", translations["ui"])
        self.assertIn("raw_identifier", translations["ui"])
        self.assertEqual(
            translations["ui"]["manual_intervention"],
            "人工介入",
        )
        self.assertIn("不代表任务执行成功", translations["ui"]["audit_boundary"])
        self.assertEqual(
            translations["ui"]["not_currently_verified"],
            "当前内容未通过验证",
        )
        self.assertIn(
            "不得视为当前仍通过验证",
            translations["ui"]["preview_drift_warning"],
        )
        for key in (
            "control_request_completed",
            "control_request_failed",
            "control_request_in_progress",
            "control_request_unknown",
            "control_request_inconsistent",
        ):
            with self.subTest(key=key):
                self.assertIn(key, translations["ui"])
        self.assertIn("create_task_copy", translations["ui"])
        self.assertIn("create_interpret_goal", translations["ui"])
        self.assertIn("create_preview_requires_interpretation", translations["ui"])
        self.assertIn("create_assumptions", translations["ui"])
        self.assertIn("create_structured_mode", translations["ui"])
        self.assertIn("control_pause_title", translations["ui"])
        self.assertIn("approve_step", translations["control_actions"])
        self.assertIn("task_conversation", translations["ui"])
        self.assertIn("add_task_note", translations["control_actions"])
        self.assertIn("task_note_added", translations["events"])
        self.assertNotIn(
            "人工",
            translations["ui"]["control_request_in_progress"],
        )
        self.assertIn(
            "人工",
            translations["ui"]["control_request_unknown"],
        )
        self.assertIn(
            "人工",
            translations["ui"]["control_request_inconsistent"],
        )
        self.assertEqual(
            translations["errors"]["task_claim_not_acquired"],
            "任务已被其他执行者处理或已经结束，本次操作未执行。",
        )
        for code in (
            "task_interrupted_revision_not_supported",
            "action_in_progress",
            "action_already_failed",
            "action_receipt_inconsistent",
            "control_action_outcome_unknown",
            "task_claim_active",
            "task_claim_recovery_required",
            "invalid_json_number",
        ):
            with self.subTest(code=code):
                self.assertIn(code, translations["errors"])
                self.assertNotEqual(
                    translations["errors"][code],
                    translations["ui"]["unknown_error"],
                )

    def test_localization_scripts_use_safe_text_rendering(self) -> None:
        translations = _i18n()
        script = (
            PROJECT_ROOT
            / "src"
            / "zevorinth"
            / "workbench_static"
            / "app.js"
        ).read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", script)
        self.assertIn("node.textContent = value", script)
        self.assertIn("statusBadge(task.status)", script)
        self.assertIn("labelEvent(event.event_type)", script)
        self.assertIn("labelVerification(task.verification.reason)", script)
        self.assertIn("labelArtifactState(preview.state)", script)
        self.assertIn("/requests/", script)
        self.assertIn('ui("control_query_status")', script)
        self.assertIn('receipt.state === "in_progress"', script)
        self.assertIn('ui("control_request_in_progress"', script)
        self.assertIn('error.code === "action_in_progress"', script)
        self.assertNotIn('|| error.code === "action_already_failed"', script)
        self.assertIn("labelError(code)", script)
        self.assertIn("recoverStaleControlPreview(", script)
        self.assertIn("elements[\"global-message\"].textContent = message", script)
        self.assertIn('"task_claim_not_acquired"', script)
        self.assertIn('"task_claim_recovery_required"', script)
        self.assertIn('"task_cancel_not_allowed"', script)
        self.assertIn('"task_revision_not_allowed"', script)
        self.assertIn("detailRequestGeneration", script)
        self.assertIn("control_available_actions_next_step", script)
        self.assertIn("statusGuidance(task, controlPreview)", script)
        self.assertIn("renderControlActions(task.task_id, controlHost, controlPreview)", script)
        self.assertIn("create_and_submit_query_hint", script)
        self.assertIn("submitTracker.pending()", script)
        self.assertIn("() => createSubmitTaskId", script)
        self.assertIn('operation.value === "run_tests"', script)
        self.assertIn(
            'elements["global-message"].textContent =\n'
            '          ui("control_request_completed")',
            script,
        )
        self.assertIn("controlSessionError", script)
        self.assertIn('session.contract_version === "task-control-v1"', script)
        self.assertIn('error.code = "control_session_invalid"', script)
        self.assertIn("revisionActionForm(", script)
        self.assertIn("cancelActionForm(", script)
        self.assertIn("startActionForm(", script)
        self.assertIn(
            'if (key === "code") return `${labelError(value)}（${value}）`;',
            script,
        )
        self.assertIn(
            'return `${ui("unknown_status")}（${raw || ui("unknown")}）`;',
            script,
        )
        self.assertIn(
            "页面不会自动轮询",
            translations["ui"]["control_request_in_progress"],
        )
        self.assertIn(
            "不支持中断任务修订",
            translations["ui"]["control_interrupted_next_step"],
        )
        self.assertIn('task.status === "interrupted"', script)
        self.assertNotIn(
            '["queued", "blocked", "interrupted"]',
            script,
        )

    def test_cli_exposes_loopback_workbench_command(self) -> None:
        args = build_parser().parse_args(["workbench", "--port", "8877"])
        self.assertEqual(args.command, "workbench")
        self.assertEqual(args.port, 8877)
        control = build_parser().parse_args(
            ["control-center", "--port", "8878"]
        )
        self.assertEqual(control.command, "control-center")
        self.assertEqual(control.port, 8878)


class ConversationEndpointTests(unittest.TestCase):
    """P1-1: Conversational intake endpoints on the control surface."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = create_server(
            self.root, port=0, control_enabled=True
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]
        self.base = f"http://{self.host}:{self.port}"
        status, _, session = self._get("/control/api/session")
        self.assertEqual(status, 200)
        self.token = session["token"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def _get(self, path: str) -> tuple[int, dict, dict]:
        with urlopen(f"{self.base}{path}", timeout=5) as response:
            return (
                response.status,
                dict(response.headers),
                json.loads(response.read().decode("utf-8")),
            )

    def _post(
        self,
        path: str,
        body: dict,
        *,
        token: str | None = None,
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Zevorinth-Control-Token"] = token
            headers["Origin"] = self.base
        request = Request(
            f"{self.base}{path}",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                return (
                    response.status,
                    json.loads(response.read().decode("utf-8")),
                )
        except HTTPError as exc:
            payload = None
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = {}
            return exc.code, payload

    def test_session_advertises_conversation_intake(self) -> None:
        _, _, session = self._get("/control/api/session")
        self.assertTrue(session["conversation_intake"])

    def test_create_conversation_returns_id_and_active_task_none(self) -> None:
        status, body = self._post(
            "/control/api/conversations",
            {},
            token=self.token,
        )
        self.assertEqual(status, 201)
        self.assertIn("conversation_id", body)
        self.assertEqual(body["status"], "active")
        self.assertIsNone(body["active_task_id"])
        self.assertIsNone(body["active_task_status"])

    def test_create_conversation_requires_control_token(self) -> None:
        status, body = self._post(
            "/control/api/conversations",
            {},
            token=None,
        )
        self.assertEqual(status, 403)

    def test_submit_message_creates_task_for_new_task_intent(self) -> None:
        _, conv = self._post(
            "/control/api/conversations",
            {},
            token=self.token,
        )
        conversation_id = conv["conversation_id"]
        status, result = self._post(
            f"/control/api/conversations/{conversation_id}/messages",
            {
                "content": "创建一个 hello.txt，内容写 hello world。",
                "client_message_id": str(uuid.uuid4()),
            },
            token=self.token,
        )
        self.assertEqual(status, 200)
        # A fully stated goal now runs to completion from one message: the
        # action and both parameters are understood, so nothing is asked
        # (previously this returned three internal field names — R-01/R-18).
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["intent"]["kind"], "new_task")
        self.assertIsNotNone(result["task"])
        self.assertEqual(result["task"]["status"], "succeeded")
        self.assertEqual(result["clarifications"], [])

    def test_submit_message_requires_control_token(self) -> None:
        _, conv = self._post(
            "/control/api/conversations",
            {},
            token=self.token,
        )
        conversation_id = conv["conversation_id"]
        status, body = self._post(
            f"/control/api/conversations/{conversation_id}/messages",
            {
                "content": "test message",
                "client_message_id": str(uuid.uuid4()),
            },
            token=None,
        )
        self.assertEqual(status, 403)

    def test_submit_message_rejects_empty_content(self) -> None:
        _, conv = self._post(
            "/control/api/conversations",
            {},
            token=self.token,
        )
        conversation_id = conv["conversation_id"]
        status, body = self._post(
            f"/control/api/conversations/{conversation_id}/messages",
            {
                "content": "   ",
                "client_message_id": str(uuid.uuid4()),
            },
            token=self.token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_message_content")

    def test_submit_message_unknown_conversation_returns_404(self) -> None:
        fake_id = str(uuid.uuid4())
        status, body = self._post(
            f"/control/api/conversations/{fake_id}/messages",
            {
                "content": "test",
                "client_message_id": str(uuid.uuid4()),
            },
            token=self.token,
        )
        self.assertEqual(status, 500)

    def test_conversation_detail_returns_active_task_after_message(self) -> None:
        _, conv = self._post(
            "/control/api/conversations",
            {},
            token=self.token,
        )
        conversation_id = conv["conversation_id"]
        self._post(
            f"/control/api/conversations/{conversation_id}/messages",
            {
                # Needs one missing detail, so the task stays active — which is
                # what this test is about. (A fully specified goal now runs to
                # completion immediately, leaving no active task.)
                "content": "读取一下",
                "client_message_id": str(uuid.uuid4()),
            },
            token=self.token,
        )
        status, _, detail = self._get(
            f"/control/api/conversations/{conversation_id}"
        )
        self.assertEqual(status, 200)
        self.assertIsNotNone(detail["active_task_id"])
        self.assertEqual(detail["active_task_status"], "awaiting_clarification")

    def test_conversation_messages_history_returns_user_messages(self) -> None:
        _, conv = self._post(
            "/control/api/conversations",
            {},
            token=self.token,
        )
        conversation_id = conv["conversation_id"]
        self._post(
            f"/control/api/conversations/{conversation_id}/messages",
            {
                "content": "创建 a.txt，内容写 aaa。",
                "client_message_id": str(uuid.uuid4()),
            },
            token=self.token,
        )
        status, _, messages = self._get(
            f"/control/api/conversations/{conversation_id}/messages?limit=50"
        )
        self.assertEqual(status, 200)
        # The conversation now records what the system said as well: one user
        # message and one assistant reply (F-03 / R-05).
        self.assertEqual(messages["count"], 2)
        self.assertEqual(messages["messages"][0]["role"], "user")
        self.assertIn("a.txt", messages["messages"][0]["content"])
        self.assertEqual(messages["messages"][1]["role"], "assistant")
        self.assertTrue(messages["messages"][1]["content"].strip())

    def test_conversation_messages_unknown_conversation_returns_404(self) -> None:
        fake_id = str(uuid.uuid4())
        with self.assertRaises(HTTPError) as caught:
            urlopen(
                f"{self.base}/control/api/conversations/{fake_id}/messages",
                timeout=5,
            )
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_conversation_detail_unknown_conversation_returns_404(self) -> None:
        fake_id = str(uuid.uuid4())
        with self.assertRaises(HTTPError) as caught:
            urlopen(
                f"{self.base}/control/api/conversations/{fake_id}",
                timeout=5,
            )
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_submit_message_rejects_unknown_fields(self) -> None:
        _, conv = self._post(
            "/control/api/conversations",
            {},
            token=self.token,
        )
        conversation_id = conv["conversation_id"]
        status, body = self._post(
            f"/control/api/conversations/{conversation_id}/messages",
            {
                "content": "test",
                "client_message_id": str(uuid.uuid4()),
                "unknown_field": "bad",
            },
            token=self.token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "unknown_message_fields")

    def test_status_query_returns_status_outcome(self) -> None:
        _, conv = self._post(
            "/control/api/conversations",
            {},
            token=self.token,
        )
        conversation_id = conv["conversation_id"]
        status, result = self._post(
            f"/control/api/conversations/{conversation_id}/messages",
            {
                "content": "状态",
                "client_message_id": str(uuid.uuid4()),
            },
            token=self.token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["outcome"], "status")
        self.assertEqual(result["intent"]["kind"], "status_query")

    def test_list_conversations_returns_created_conversations(self) -> None:
        self._post("/control/api/conversations", {}, token=self.token)
        self._post("/control/api/conversations", {}, token=self.token)
        status, _, body = self._get("/control/api/conversations?limit=10")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["count"], 2)
        for conv in body["conversations"]:
            self.assertIn("conversation_id", conv)
            self.assertIn("status", conv)
            self.assertIn("active_task_id", conv)

    def test_list_conversations_shows_active_task_status(self) -> None:
        _, conv = self._post(
            "/control/api/conversations", {}, token=self.token,
        )
        self._post(
            f"/control/api/conversations/{conv['conversation_id']}/messages",
            {
                # Leaves the task awaiting one missing detail, so the list view
                # has an active task to show.
                "content": "读取一下",
                "client_message_id": str(uuid.uuid4()),
            },
            token=self.token,
        )
        status, _, body = self._get("/control/api/conversations?limit=10")
        self.assertEqual(status, 200)
        found = [
            c for c in body["conversations"]
            if c["conversation_id"] == conv["conversation_id"]
        ]
        self.assertEqual(len(found), 1)
        self.assertIsNotNone(found[0]["active_task_id"])
        self.assertEqual(
            found[0]["active_task_status"], "awaiting_clarification"
        )


def _to_preview_object(report: dict):
    from zevorinth.recovery_preview import (
        RecoveryAction,
        RecoveryPreview,
    )

    return RecoveryPreview(
        preview_id=str(report["preview_id"]),
        preview_version=str(report["preview_version"]),
        created_at=str(report["created_at"]),
        task_id=str(report["task_id"]),
        state_digest=str(report["state_digest"]),
        confirmed=tuple(report["confirmed"]),
        unknown=tuple(report["unknown"]),
        actions=tuple(
            RecoveryAction.from_dict(item) for item in report["actions"]
        ),
        audit=dict(report["audit"]),
    )


class TaskRetryEndpointTests(unittest.TestCase):
    """R3/R6: POST /control/api/tasks/{id}/retry is idempotent and real."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = create_server(
            self.root, port=0, control_enabled=True
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]
        self.base = f"http://{self.host}:{self.port}"
        with urlopen(f"{self.base}/control/api/session", timeout=5) as response:
            self.token = json.loads(response.read().decode("utf-8"))["token"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def _post(
        self,
        path: str,
        body: dict,
        *,
        token: str | None = None,
    ) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Zevorinth-Control-Token"] = token
            headers["Origin"] = self.base
        request = Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                return (
                    response.status,
                    json.loads(response.read().decode("utf-8")),
                )
        except HTTPError as exc:
            return (exc.code, json.loads(exc.read().decode("utf-8")))

    def _failed_shell_task(self, *, code: str = "timeout"):
        from zevorinth.models import ExecutionMode, ToolResult

        real = __import__(
            "zevorinth.service", fromlist=["execute_controlled_shell"]
        ).execute_controlled_shell
        calls = {"n": 0}

        def fake(**kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                return real(**kwargs)
            return ToolResult(
                attempt_id=kwargs["attempt_id"],
                tool_name="controlled_shell",
                execution_mode=ExecutionMode.REAL,
                success=False,
                started_at=kwargs["started_at"],
                finished_at=kwargs["finished_at"],
                error_code=code,
                error_message=f"simulated {code}",
            )

        from unittest import mock

        with mock.patch("zevorinth.service.execute_controlled_shell",
                        side_effect=fake):
            service = TaskService(self.root, recover_leases=False)
            return service.submit(
                TaskRequest(
                    goal="列出受控目录内容",
                    operation="controlled_shell",
                    parameters={"command": "echo", "arguments": ["ok"]},
                )
            )

    def _retry(self, task_id: str, body: dict, *, token: str | None = ""):
        return self._post(
            f"/control/api/tasks/{task_id}/retry",
            body,
            token=self.token if token == "" else token,
        )

    def test_retry_requires_control_token(self) -> None:
        task = self._failed_shell_task()
        status, payload = self._retry(
            task.task_id, {"request_id": str(uuid.uuid4())}, token=None,
        )
        self.assertIn(status, (401, 403))
        self.assertIn("error", payload)

    def test_retry_requires_canonical_request_id(self) -> None:
        task = self._failed_shell_task()
        status, payload = self._retry(task.task_id, {})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_action_request_id")

        status, payload = self._retry(
            task.task_id, {"request_id": "not-a-uuid"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_action_request_id")

    def test_retry_rejects_unknown_fields(self) -> None:
        task = self._failed_shell_task()
        status, payload = self._retry(
            task.task_id,
            {"request_id": str(uuid.uuid4()), "unexpected": 1},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "unknown_request_fields")

    def test_retry_creates_task_and_repeats_are_idempotent(self) -> None:
        task = self._failed_shell_task()
        request_id = str(uuid.uuid4())

        status, body = self._retry(
            task.task_id, {"request_id": request_id},
        )
        self.assertEqual(status, 201)
        self.assertTrue(body["retry"])
        self.assertFalse(body["already_exists"])
        self.assertEqual(body["task_id"], request_id)
        self.assertEqual(body["retry_attempt"], 1)
        self.assertEqual(body["original_task_id"], task.task_id)
        self.assertEqual(body["status"], "succeeded")

        status, repeat = self._retry(
            task.task_id, {"request_id": request_id},
        )
        self.assertEqual(status, 200)
        self.assertFalse(repeat["retry"])
        self.assertTrue(repeat["already_exists"])
        self.assertEqual(repeat["task_id"], request_id)

        service = TaskService(self.root, recover_leases=False)
        self.assertEqual(service._count_retry_attempts(task.task_id), 1)
        original = service.store.require_task(task.task_id)
        self.assertEqual(original.status, TaskStatus.FAILED)

    def test_retry_reports_non_retryable_tasks(self) -> None:
        service = TaskService(self.root, recover_leases=False)
        task = service.submit(
            TaskRequest(
                goal="写入被拒绝的命令",
                operation="controlled_shell",
                parameters={"command": "rm", "arguments": ["x"]},
            )
        )
        status, body = self._retry(
            task.task_id, {"request_id": str(uuid.uuid4())},
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["retry"])
        self.assertFalse(body["already_exists"])
        self.assertIn("reason", body)


if __name__ == "__main__":
    unittest.main()
