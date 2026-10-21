"""Phase 3.91: read-only local task workbench."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.cli import build_parser  # noqa: E402
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.recovery_preview import (  # noqa: E402
    RecoveryApprovalStore,
    approve_recovery_preview,
)
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.workbench import (  # noqa: E402
    MAX_TASK_LIMIT,
    ReadOnlyWorkbench,
    WorkbenchError,
)
from zevorinth.workbench_server import create_server  # noqa: E402


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
        before_database = hashlib.sha256(database.read_bytes()).hexdigest()
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

        after_database = hashlib.sha256(database.read_bytes()).hexdigest()
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

    def test_static_assets_have_no_external_dependencies(self) -> None:
        with urlopen(f"{self.base}/", timeout=5) as response:
            html = response.read().decode("utf-8")
            content_type = response.headers["Content-Type"]
        with urlopen(f"{self.base}/static/app.js", timeout=5) as response:
            script = response.read().decode("utf-8")
        self.assertIn("text/html", content_type)
        self.assertIn("Read-only", html)
        self.assertNotIn("https://", html)
        self.assertIn('method: "GET"', script)
        self.assertNotIn('method: "POST"', script)
        self.assertNotIn("/api/recover", script)

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
    def test_repository_rejects_invalid_page_and_status_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_tasks(root)
            workbench = ReadOnlyWorkbench(root)
            with self.assertRaisesRegex(WorkbenchError, "limit"):
                workbench.list_tasks(limit=MAX_TASK_LIMIT + 1)
            with self.assertRaisesRegex(WorkbenchError, "status"):
                workbench.list_tasks(status="invented")

    def test_repository_connection_rejects_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_tasks(root)
            workbench = ReadOnlyWorkbench(root)
            with workbench._connect() as connection:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM tasks")

    def test_cli_exposes_loopback_workbench_command(self) -> None:
        args = build_parser().parse_args(["workbench", "--port", "8877"])
        self.assertEqual(args.command, "workbench")
        self.assertEqual(args.port, 8877)


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


if __name__ == "__main__":
    unittest.main()
