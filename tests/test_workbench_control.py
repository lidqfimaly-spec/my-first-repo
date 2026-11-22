"""Task control center v1: confirmed clarification actions."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.control_receipt import (  # noqa: E402
    classify_control_receipt,
    resolve_control_receipt,
)
from zevorinth.models import (  # noqa: E402
    Event,
    EventType,
    PlannerMode,
    TaskError,
    TaskRequest,
    TaskStatus,
)
from zevorinth.event_store import EventStore, PlanRevisionConflict  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.workbench_control import (  # noqa: E402
    ControlActionError,
    TaskControlService,
    _request_digest,
    _versioned_request_digest,
)
from zevorinth.workbench_server import create_server  # noqa: E402


class ControlReceiptClassificationTests(unittest.TestCase):
    @staticmethod
    def _event(
        event_type: EventType,
        digest: str = "a" * 64,
        request_id: str | None = None,
    ) -> tuple[str, dict, str]:
        return (
            event_type.value,
            {
                "request_id": request_id or str(uuid.uuid4()),
                "action": "answer_clarification",
                "request_digest": digest,
                "task_status": "awaiting_clarification",
            },
            "2026-10-22T00:00:00.000+00:00",
        )

    def test_receipt_states_are_fail_closed_and_shared(self) -> None:
        request_id = str(uuid.uuid4())
        request = self._event(
            EventType.CONTROL_ACTION_REQUESTED,
            request_id=request_id,
        )
        completed = self._event(
            EventType.CONTROL_ACTION_COMPLETED,
            request_id=request_id,
        )
        failed = self._event(
            EventType.CONTROL_ACTION_FAILED,
            request_id=request_id,
        )
        cases = (
            ([request], "unknown"),
            ([request, completed], "completed"),
            ([request, failed], "failed"),
            ([request, completed, failed], "inconsistent"),
            ([completed], "inconsistent"),
            (
                [
                    request,
                    self._event(
                        EventType.CONTROL_ACTION_COMPLETED,
                        "b" * 64,
                        request_id=request_id,
                    ),
                ],
                "inconsistent",
            ),
        )
        for events, expected in cases:
            with self.subTest(expected=expected, events=events):
                receipt = classify_control_receipt(events)
                self.assertIsNotNone(receipt)
                self.assertEqual(receipt["state"], expected)

    def test_failure_plus_side_effect_is_inconsistent(self) -> None:
        request_id = str(uuid.uuid4())
        for action, side_evidence in (
            (
                "revise_unstarted_task",
                {"revision": {"source_id": request_id}},
            ),
            (
                "cancel_queued_task",
                {"cancelled_event": {"payload": {}, "occurred_at": "t"}},
            ),
            (
                "start_queued_task",
                {
                    "claimed_event": {
                        "payload": {
                            "attempt_id": "attempt-1",
                            "control_action": "start_queued_task",
                        },
                        "occurred_at": "t",
                    }
                },
            ),
        ):
            request = (
                EventType.CONTROL_ACTION_REQUESTED.value,
                {
                    "request_id": request_id,
                    "action": action,
                    "request_digest": "a" * 64,
                },
                "t",
            )
            failed = (
                EventType.CONTROL_ACTION_FAILED.value,
                {
                    "request_id": request_id,
                    "action": action,
                    "request_digest": "a" * 64,
                    "error_code": "simulated_failure",
                },
                "t",
            )
            with self.subTest(action=action):
                receipt, reconciled_from = resolve_control_receipt(
                    [request, failed],
                    task_status="running",
                    task_attempt_id="attempt-1",
                    **side_evidence,
                )
                self.assertIsNotNone(receipt)
                self.assertEqual(receipt["state"], "inconsistent")
                self.assertIsNone(reconciled_from)

    def test_request_linked_clarification_claim_is_in_progress(self) -> None:
        request_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        digest = "a" * 64
        receipt, source = resolve_control_receipt(
            [
                (
                    EventType.CONTROL_ACTION_REQUESTED.value,
                    {
                        "request_id": request_id,
                        "action": "answer_clarification",
                        "request_digest": digest,
                        "task_status": "awaiting_clarification",
                    },
                    "2026-10-23T00:00:00Z",
                )
            ],
            task_status="awaiting_clarification",
            task_attempt_id=attempt_id,
            lease_active=True,
            claimed_event={
                "payload": {
                    "attempt_id": attempt_id,
                    "control_request_id": request_id,
                    "control_action": "answer_clarification",
                },
                "occurred_at": "2026-10-23T00:00:01Z",
            },
        )
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["state"], "in_progress")
        self.assertEqual(receipt["action"], "answer_clarification")
        self.assertEqual(source, "task_claimed_event")

    def test_completed_receipt_rejects_mismatched_side_evidence(self) -> None:
        request_id = str(uuid.uuid4())
        receipt, source = resolve_control_receipt(
            [
                (
                    EventType.CONTROL_ACTION_REQUESTED.value,
                    {
                        "request_id": request_id,
                        "action": "cancel_queued_task",
                        "request_digest": "a" * 64,
                    },
                    "t",
                ),
                (
                    EventType.CONTROL_ACTION_COMPLETED.value,
                    {
                        "request_id": request_id,
                        "action": "cancel_queued_task",
                        "request_digest": "a" * 64,
                    },
                    "t",
                ),
            ],
            task_status="cancelled",
            task_attempt_id=None,
            revision={"source_id": request_id},
        )
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["state"], "inconsistent")
        self.assertIsNone(source)

    def test_completed_receipt_rejects_mismatched_claim_attempt(self) -> None:
        request_id = str(uuid.uuid4())
        digest = "a" * 64
        receipt, source = resolve_control_receipt(
            [
                (
                    EventType.CONTROL_ACTION_REQUESTED.value,
                    {
                        "request_id": request_id,
                        "action": "start_queued_task",
                        "request_digest": digest,
                    },
                    "t",
                ),
                (
                    EventType.CONTROL_ACTION_COMPLETED.value,
                    {
                        "request_id": request_id,
                        "action": "start_queued_task",
                        "request_digest": digest,
                    },
                    "t",
                ),
            ],
            task_status="succeeded",
            task_attempt_id="current-attempt",
            claimed_event={
                "payload": {
                    "attempt_id": "different-attempt",
                    "control_request_id": request_id,
                    "control_action": "start_queued_task",
                },
                "occurred_at": "t",
            },
        )
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["state"], "inconsistent")
        self.assertIsNone(source)

    def test_conflicting_completed_receipt_semantics_are_inconsistent(self) -> None:
        request_id = str(uuid.uuid4())
        digest = "a" * 64
        receipt = classify_control_receipt(
            [
                (
                    EventType.CONTROL_ACTION_REQUESTED.value,
                    {
                        "request_id": request_id,
                        "action": "start_queued_task",
                        "request_digest": digest,
                    },
                    "t",
                ),
                (
                    EventType.CONTROL_ACTION_COMPLETED.value,
                    {
                        "request_id": request_id,
                        "action": "start_queued_task",
                        "request_digest": digest,
                        "task_status": "succeeded",
                        "plan_version": 1,
                    },
                    "t",
                ),
                (
                    EventType.CONTROL_ACTION_COMPLETED.value,
                    {
                        "request_id": request_id,
                        "action": "start_queued_task",
                        "request_digest": digest,
                        "task_status": "failed",
                        "plan_version": 2,
                    },
                    "t",
                ),
            ]
        )
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["state"], "inconsistent")


class WorkbenchControlHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = TaskService(self.root)
        self.task = self.service.submit(
            TaskRequest(
                goal="创建一个等待补充的文件任务",
                operation="write_text_artifact",
                parameters={},
            )
        )
        self.queued_task = self.service.enqueue(
            TaskRequest(
                goal="创建一个尚未执行的排队任务",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "queued.txt",
                    "content": "original\n",
                },
            )
        )
        self.server = create_server(
            self.root,
            port=0,
            control_enabled=True,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"
        self.origin = self.base

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def _get(self, path: str) -> tuple[int, dict]:
        return self._get_from(self.base, path)

    def _get_from(
        self,
        base: str,
        path: str,
        *,
        token: str | None = None,
        origin: str | None = None,
    ) -> tuple[int, dict]:
        headers = {}
        if token is not None:
            headers["X-Zevorinth-Control-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        request = Request(f"{base}{path}", headers=headers, method="GET")
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _session(self) -> dict:
        _, payload = self._get("/control/api/session")
        return payload

    def _post(
        self,
        task_id: str,
        payload: dict,
        *,
        base: str | None = None,
        token: str | None = None,
        origin: str | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict]:
        headers = {"Content-Type": content_type}
        if token is not None:
            headers["X-Zevorinth-Control-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        request = Request(
            (
                f"{base or self.base}/control/api/tasks/{task_id}/actions"
            ),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _post_create(
        self,
        payload: dict,
        *,
        token: str,
        origin: str,
    ) -> tuple[int, dict]:
        request = Request(
            f"{self.base}/control/api/tasks",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Zevorinth-Control-Token": token,
                "Origin": origin,
            },
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _post_goal_preview(
        self,
        payload: dict,
        *,
        token: str,
        origin: str,
    ) -> tuple[int, dict]:
        request = Request(
            f"{self.base}/control/api/preview",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Zevorinth-Control-Token": token,
                "Origin": origin,
            },
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _action_payload(self, preview: dict) -> dict:
        answer = next(
            action
            for action in preview["actions"]
            if action["action"] == "answer_clarification"
        )
        return {
            "action": "answer_clarification",
            "request_id": str(uuid.uuid4()),
            "confirmed": True,
            "expected_updated_at": preview["task_updated_at"],
            "expected_plan_version": preview["plan_version"],
            "expected_missing_fields": answer["fields"],
            "parameters": {
                "artifact_path": "clarified.txt",
                "content": "补充后的内容\n",
            },
        }

    def _revision_payload(
        self,
        preview: dict,
        *,
        goal: str = "修改后的尚未执行任务",
        parameters: dict | None = None,
    ) -> dict:
        revise = next(
            action
            for action in preview["actions"]
            if action["action"] == "revise_unstarted_task"
        )
        return {
            "action": "revise_unstarted_task",
            "request_id": str(uuid.uuid4()),
            "confirmed": True,
            "expected_updated_at": preview["task_updated_at"],
            "expected_plan_version": preview["plan_version"],
            "expected_request_digest": revise["request_digest"],
            "changes": {
                "goal": goal,
                "parameters": parameters or {"content": "revised\n"},
            },
        }

    def _cancel_payload(self, preview: dict) -> dict:
        return {
            "action": "cancel_queued_task",
            "request_id": str(uuid.uuid4()),
            "confirmed": True,
            "expected_updated_at": preview["task_updated_at"],
            "expected_plan_version": preview["plan_version"],
        }

    def _start_payload(self, preview: dict) -> dict:
        return {
            "action": "start_queued_task",
            "request_id": str(uuid.uuid4()),
            "confirmed": True,
            "expected_updated_at": preview["task_updated_at"],
            "expected_plan_version": preview["plan_version"],
        }

    def _set_expired_claim(self, task_id: str) -> str:
        return self._set_claim_lease(
            task_id,
            expires_at="2000-01-01T00:00:00.000+00:00",
        )

    def _set_claim_lease(
        self,
        task_id: str,
        *,
        expires_at: str | None,
    ) -> str:
        task = self.service.require_task(task_id)
        lease_token = str(uuid.uuid4())
        self.service.store.record_event_and_update(
            task_id,
            EventType.TASK_CLAIMED,
            "2026-10-22T00:00:00.000+00:00",
            payload={
                "attempt_id": task.attempt_id,
                "generation": 1,
                "source": "test",
            },
            updates={
                "execution_owner": "crashed-worker",
                "lease_token": lease_token,
                "lease_expires_at": expires_at,
                "claim_generation": 1,
                "phase": "claimed",
            },
        )
        return lease_token

    def test_confirmed_clarification_action_executes_and_rereads_state(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.task.task_id}/actions"
        )
        self.assertEqual(preview["contract_version"], "task-control-v1")
        self.assertEqual(
            preview["actions"][0]["action"],
            "answer_clarification",
        )
        session = self._session()
        self.assertEqual(session["contract_version"], "task-control-v1")
        self.assertEqual(
            session["actions"],
            [
                "add_task_note",
                "answer_clarification",
                "approve_step",
                "cancel_queued_task",
                "pause_task",
                "reject_step",
                "replan_pending_steps",
                "resume_task",
                "revise_unstarted_task",
                "start_queued_task",
            ],
        )
        self.assertEqual(session["authentication"], "none")
        self.assertEqual(
            session["csrf_protection"],
            "loopback_origin_and_process_token",
        )
        self.assertTrue(session["read_only_api_preserved"])
        payload = self._action_payload(preview)

        status, result = self._post(
            self.task.task_id,
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "completed")
        updated = self.service.require_task(self.task.task_id)
        self.assertEqual(updated.status, TaskStatus.SUCCEEDED)
        self.assertTrue(updated.verification.passed)
        artifact = (
            self.root
            / "artifacts"
            / self.task.task_id
            / "clarified.txt"
        )
        self.assertEqual(artifact.read_text(encoding="utf-8"), "补充后的内容\n")
        database = self.root / "data" / "zevorinth.sqlite3"
        before_query_database = database.read_bytes()
        before_query_artifact = artifact.read_bytes()
        request_status_code, request_status = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(request_status_code, 200)
        self.assertEqual(request_status["state"], "completed")
        self.assertFalse(request_status["manual_review_required"])
        self.assertEqual(request_status["task_status"], "succeeded")
        self.assertTrue(request_status["read_only"])
        self.assertEqual(database.read_bytes(), before_query_database)
        self.assertEqual(artifact.read_bytes(), before_query_artifact)
        event_types = [
            event.event_type for event in self.service.events(self.task.task_id)
        ]
        self.assertIn(EventType.CONTROL_ACTION_REQUESTED.value, event_types)
        self.assertIn(EventType.CONTROL_ACTION_COMPLETED.value, event_types)
        clarification = next(
            event
            for event in self.service.events(self.task.task_id)
            if event.event_type == EventType.CLARIFICATION_UPDATED.value
        )
        self.assertEqual(
            clarification.payload["provided_parameters"],
            payload["parameters"],
        )

        replay_status, replay = self._post(
            self.task.task_id,
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay["status"], "already_processed")
        conflicting = dict(payload)
        conflicting["parameters"] = {
            **payload["parameters"],
            "content": "不同的内容\n",
        }
        with self.assertRaises(HTTPError) as caught:
            self._post(
                self.task.task_id,
                conflicting,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(error["error"]["code"], "action_request_conflict")
        caught.exception.close()

    def test_task_creation_is_real_idempotent_and_reloadable(self) -> None:
        session = self._session()
        request_id = str(uuid.uuid4())
        payload = {
            "request_id": request_id,
            "confirmed": True,
            "goal": "通过控制台创建任务",
            "operation": "write_text_artifact",
            "parameters": {
                "artifact_path": "created-from-ui.txt",
                "content": "created through the real control API\n",
                "supplement": "创建补充说明",
                "constraints": "不得离开任务产物目录",
                "expected_artifact": "created-from-ui.txt",
                "acceptance": "独立读回验证通过",
            },
        }
        for token, origin in (
            ("", self.origin),
            (session["token"], "http://attacker.example"),
        ):
            with self.subTest(token=token, origin=origin), self.assertRaises(
                HTTPError
            ) as caught:
                self._post_create(
                    payload,
                    token=token,
                    origin=origin,
                )
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()
        status, created = self._post_create(
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["status"], "created")
        self.assertEqual(created["task_id"], request_id)
        self.assertEqual(created["task_status"], "queued")
        detail_status, detail = self._get(
            f"/api/tasks/{created['task_id']}"
        )
        self.assertEqual(detail_status, 200)
        self.assertEqual(detail["task"]["goal"], payload["goal"])
        self.assertEqual(
            {
                item["field"]
                for item in detail["task"]["definition_metadata"]
            },
            {
                "supplement",
                "constraints",
                "expected_artifact",
                "acceptance",
            },
        )
        self.assertEqual(
            detail["task"]["creation_preview"]["classification"],
            "planned",
        )
        persisted = self.service.require_task(created["task_id"])
        self.assertEqual(persisted.status, TaskStatus.QUEUED)
        self.assertFalse(
            (
                self.root
                / "artifacts"
                / created["task_id"]
                / "created-from-ui.txt"
            ).exists()
        )
        self.assertEqual(
            [
                event.event_type
                for event in self.service.events(created["task_id"])
            ].count(EventType.TASK_CREATED.value),
            1,
        )

        replay_status, replay = self._post_create(
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(replay_status, 201)
        self.assertEqual(replay["status"], "already_exists")
        self.assertEqual(
            [
                event.event_type
                for event in self.service.events(created["task_id"])
            ].count(EventType.TASK_CREATED.value),
            1,
        )
        conflict = {
            **payload,
            "goal": "相同请求 ID 的不同任务",
        }
        with self.assertRaises(HTTPError) as caught:
            self._post_create(
                conflict,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(error["error"]["code"], "task_creation_conflict")
        caught.exception.close()

    def test_natural_goal_preview_is_bounded_and_never_creates_a_task(
        self,
    ) -> None:
        session = self._session()
        self.assertEqual(
            session["goal_preview"],
            "natural-goal-preview-v1",
        )
        before_status, before = self._get("/api/tasks")
        self.assertEqual(before_status, 200)
        before_task_ids = {
            item["task_id"] for item in before["tasks"]
        }
        for token, origin in (
            ("", self.origin),
            (session["token"], "http://attacker.example"),
        ):
            with self.subTest(token=token, origin=origin), self.assertRaises(
                HTTPError
            ) as caught:
                self._post_goal_preview(
                    {"goal": "创建 hello.txt，内容写 Hello"},
                    token=token,
                    origin=origin,
                )
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()

        status, preview = self._post_goal_preview(
            {
                "goal": (
                    "创建 `hello.txt`，内容写："
                    "Hello from the natural-language control center"
                )
            },
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["state"], "ready")
        self.assertEqual(preview["operation"], "write_text_artifact")
        self.assertEqual(
            preview["operation_label"],
            "创建并验证文本文件",
        )
        self.assertEqual(
            preview["parameters"],
            {
                "artifact_path": "hello.txt",
                "content": "Hello from the natural-language control center",
            },
        )
        self.assertEqual(preview["missing_fields"], [])
        self.assertEqual(preview["plan_classification"], "planned")
        self.assertTrue(preview["confirmation_required"])
        self.assertFalse(preview["execution_started"])
        self.assertFalse(preview["mutation_started"])
        self.assertTrue(preview["limits"])
        after_status, after = self._get("/api/tasks")
        self.assertEqual(after_status, 200)
        self.assertEqual(
            {item["task_id"] for item in after["tasks"]},
            before_task_ids,
        )

    def test_natural_goal_preview_requests_clarification_or_fails_closed(
        self,
    ) -> None:
        session = self._session()
        _, incomplete = self._post_goal_preview(
            {"goal": "创建一份简短报告"},
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(incomplete["state"], "needs_clarification")
        self.assertEqual(
            incomplete["parameters"],
            {"artifact_path": "report.md"},
        )
        self.assertEqual(incomplete["missing_fields"], ["content"])
        self.assertIn("未指定文件名", incomplete["assumptions"][0])

        _, unsupported = self._post_goal_preview(
            {"goal": "运行测试 tests.test_demo"},
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(unsupported["state"], "needs_clarification")
        self.assertEqual(unsupported["guided_flow"], "code")
        self.assertEqual(unsupported["operation"], "run_tests")
        self.assertEqual(
            unsupported["reason_code"],
            "guided_code_fields_required",
        )
        self.assertFalse(unsupported["confirmation_required"])

        with self.assertRaises(HTTPError) as caught:
            self._post_goal_preview(
                {"goal": "创建 `../escape.txt`，内容写 no"},
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 400)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(
            error["error"]["code"],
            "invalid_artifact_path",
        )
        caught.exception.close()

    def test_natural_goal_routes_task_classes_without_mutation(self) -> None:
        session = self._session()
        before_status, before = self._get("/api/tasks")
        self.assertEqual(before_status, 200)
        before_ids = {item["task_id"] for item in before["tasks"]}
        cases = (
            (
                "检查并修改工作区 demo 的 sample.py，然后运行测试",
                "code",
                {"workspace", "path", "expected_sha256", "old_text", "new_text"},
            ),
            (
                "根据本地资料研究这个主题，并生成带来源引用的报告",
                "research",
                {"workspace", "sources", "title", "output_path", "findings"},
            ),
            (
                "先创建来源文件，然后创建依赖它的副本",
                "complex",
                {"steps"},
            ),
        )
        for goal, flow, required in cases:
            with self.subTest(flow=flow):
                status, preview = self._post_goal_preview(
                    {"goal": goal},
                    token=session["token"],
                    origin=self.origin,
                )
                self.assertEqual(status, 200)
                self.assertEqual(preview["guided_flow"], flow)
                self.assertTrue(preview["supported"])
                self.assertEqual(preview["state"], "needs_clarification")
                self.assertEqual(set(preview["missing_fields"]), required)
                self.assertFalse(preview["confirmation_required"])
                self.assertFalse(preview["execution_started"])
                self.assertFalse(preview["mutation_started"])
        after_status, after = self._get("/api/tasks")
        self.assertEqual(after_status, 200)
        self.assertEqual(
            {item["task_id"] for item in after["tasks"]},
            before_ids,
        )

    def test_natural_goal_preview_to_confirmed_task_delivery(self) -> None:
        session = self._session()
        status, preview = self._post_goal_preview(
            {
                "goal": (
                    "创建 `guided.txt`，内容写："
                    "guided through the natural-language first-use flow"
                )
            },
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["state"], "ready")

        request_id = str(uuid.uuid4())
        created_status, created = self._post_create(
            {
                "request_id": request_id,
                "confirmed": True,
                "goal": preview["goal"],
                "operation": preview["operation"],
                "parameters": preview["parameters"],
            },
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(created_status, 201)
        self.assertEqual(created["task_status"], TaskStatus.QUEUED.value)
        _, action_preview = self._get(
            f"/control/api/tasks/{request_id}/actions"
        )
        action_status, action = self._post(
            request_id,
            self._start_payload(action_preview),
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(action_status, 200)
        self.assertEqual(action["status"], "completed")
        task = self.service.require_task(request_id)
        self.assertEqual(task.status, TaskStatus.SUCCEEDED)
        artifact = (
            self.root
            / "artifacts"
            / request_id
            / "guided.txt"
        )
        self.assertEqual(
            artifact.read_text(encoding="utf-8"),
            "guided through the natural-language first-use flow",
        )

    def test_natural_goal_continuation_binds_verified_source_context(self) -> None:
        source = self.service.submit(
            TaskRequest(
                goal="创建可继续修改的来源文件",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "source.txt",
                    "content": "source\n",
                },
            )
        )
        session = self._session()
        status, preview = self._post_goal_preview(
            {
                "goal": "在刚才那个文件末尾再加一段说明，内容写：结论已验证。",
                "selected_task_id": source.task_id,
            },
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["state"], "ready")
        self.assertTrue(preview["continuation_context"])
        self.assertEqual(preview["operation"], "modify_text_artifact")
        self.assertEqual(
            preview["parameters"],
            {
                "source_task_id": source.task_id,
                "artifact_path": "source.txt",
                "expected_sha256": source.artifact.sha256,
                "output_path": "source-v2.txt",
                "mode": "append",
                "content": "结论已验证。",
            },
        )
        self.assertTrue(preview["confirmation_required"])

        request_id = str(uuid.uuid4())
        created_status, _ = self._post_create(
            {
                "request_id": request_id,
                "confirmed": True,
                "goal": preview["goal"],
                "operation": preview["operation"],
                "parameters": preview["parameters"],
            },
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(created_status, 201)
        _, action_preview = self._get(
            f"/control/api/tasks/{request_id}/actions"
        )
        action_status, _ = self._post(
            request_id,
            self._start_payload(action_preview),
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(action_status, 200)
        revised = self.service.require_task(request_id)
        self.assertEqual(revised.status, TaskStatus.SUCCEEDED)
        revised_path = self.root / "artifacts" / request_id / "source-v2.txt"
        self.assertEqual(
            revised_path.read_text(encoding="utf-8"),
            "source\n结论已验证。",
        )
        original_path = (
            self.root / "artifacts" / source.task_id / "source.txt"
        )
        self.assertEqual(original_path.read_text(encoding="utf-8"), "source\n")

    def test_natural_continuation_requires_current_verified_source(self) -> None:
        session = self._session()
        status, preview = self._post_goal_preview(
            {"goal": "把这份报告更新一下，保留原文件"},
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["state"], "needs_clarification")
        self.assertIn("selected_task_id", preview["missing_fields"])
        self.assertFalse(preview["confirmation_required"])

        source = self.service.submit(
            TaskRequest(
                goal="创建可检查来源",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "checked.txt",
                    "content": "checked\n",
                },
            )
        )
        with self.assertRaises(HTTPError) as caught:
            self._post_goal_preview(
                {
                    "goal": "继续修改 checked.txt，替换内容",
                    "selected_task_id": self.queued_task.task_id,
                },
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(
            error["error"]["code"],
            "continuation_source_not_verified",
        )
        caught.exception.close()

        source_path = (
            self.root / "artifacts" / source.task_id / "checked.txt"
        )
        source_path.write_text("changed outside\n", encoding="utf-8")
        with self.assertRaises(HTTPError) as changed:
            self._post_goal_preview(
                {
                    "goal": "继续修改 checked.txt，替换内容",
                    "selected_task_id": source.task_id,
                },
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(changed.exception.code, 409)
        changed_error = json.loads(changed.exception.read().decode("utf-8"))
        self.assertEqual(
            changed_error["error"]["code"],
            "continuation_source_not_verified",
        )
        changed.exception.close()

    def test_natural_continuation_supports_replace_and_explicit_output(self) -> None:
        source = self.service.submit(
            TaskRequest(
                goal="创建可替换来源",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "draft.md",
                    "content": "old\n",
                },
            )
        )
        session = self._session()
        status, preview = self._post_goal_preview(
            {
                "goal": (
                    "继续修改 draft.md，把内容替换成“new content”，"
                    "另存为 final.md"
                ),
                "selected_task_id": source.task_id,
            },
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["state"], "ready")
        self.assertEqual(preview["parameters"]["mode"], "replace")
        self.assertEqual(preview["parameters"]["content"], "new content")
        self.assertEqual(preview["parameters"]["output_path"], "final.md")
        self.assertEqual(
            preview["parameters"]["expected_sha256"],
            source.artifact.sha256,
        )

    def test_natural_continuation_phrases_bind_context_or_clarify(self) -> None:
        source = self.service.submit(
            TaskRequest(
                goal="创建自然语言续版本来源",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "report.md",
                    "content": "source\n",
                },
            )
        )
        session = self._session()
        cases = (
            (
                "在刚才那个文件末尾再加一段说明。",
                "append",
                {"content"},
            ),
            (
                "把这份报告更新一下，保留原文件。",
                None,
                {"content", "mode"},
            ),
            (
                "继续完善 report.md，另存为新版本。",
                None,
                {"content", "mode"},
            ),
        )
        for goal, expected_mode, expected_missing in cases:
            with self.subTest(goal=goal):
                status, preview = self._post_goal_preview(
                    {
                        "goal": goal,
                        "selected_task_id": source.task_id,
                    },
                    token=session["token"],
                    origin=self.origin,
                )
                self.assertEqual(status, 200)
                self.assertTrue(preview["continuation_context"])
                self.assertEqual(preview["operation"], "modify_text_artifact")
                self.assertEqual(preview["state"], "needs_clarification")
                self.assertEqual(
                    set(preview["missing_fields"]),
                    expected_missing,
                )
                self.assertEqual(
                    preview["parameters"].get("mode"),
                    expected_mode,
                )
                self.assertEqual(
                    preview["parameters"]["expected_sha256"],
                    source.artifact.sha256,
                )

    def test_multi_step_actions_pause_approve_resume_and_replan(self) -> None:
        waiting = self.service.submit_plan(
            "approval through control center",
            [
                {
                    "step_id": "edit",
                    "tool_name": "write_text_artifact",
                    "expected_result": "create approved output",
                    "parameters": {
                        "artifact_path": "approved.txt",
                        "content": "approved through control center\n",
                    },
                    "requires_approval": True,
                }
            ],
        )
        self.assertEqual(waiting.status, TaskStatus.AWAITING_APPROVAL)
        status, preview = self._get(
            f"/control/api/tasks/{waiting.task_id}/actions"
        )
        self.assertEqual(status, 200)
        actions = {item["action"]: item for item in preview["actions"]}
        self.assertEqual(actions["approve_step"]["step_id"], "edit")
        self.assertEqual(actions["reject_step"]["step_id"], "edit")
        self.assertIn("pause_task", actions)
        self.assertIn("replan_pending_steps", actions)

        session = self._session()
        approve_request = str(uuid.uuid4())
        approved_status, approved = self._post(
            waiting.task_id,
            {
                "action": "approve_step",
                "request_id": approve_request,
                "confirmed": True,
                "expected_updated_at": preview["task_updated_at"],
                "expected_plan_version": preview["plan_version"],
                "step_id": "edit",
            },
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(approved_status, 200)
        self.assertEqual(approved["status"], "completed")
        completed = self.service.require_task(waiting.task_id)
        self.assertEqual(completed.status, TaskStatus.SUCCEEDED)
        self.assertEqual(
            (
                self.root
                / "artifacts"
                / waiting.task_id
                / "approved.txt"
            ).read_text(encoding="utf-8"),
            "approved through control center\n",
        )

        queued = self.service.enqueue(
            TaskRequest(
                goal="pause and resume",
                operation="execute_plan",
                parameters={
                    "steps": [
                        {
                            "step_id": "one",
                            "tool_name": "write_text_artifact",
                            "expected_result": "write one",
                            "parameters": {
                                "artifact_path": "one.txt",
                                "content": "one\n",
                            },
                        }
                    ],
                    "failure_policy": "fail_fast",
                },
            )
        )
        _, queued_preview = self._get(
            f"/control/api/tasks/{queued.task_id}/actions"
        )
        pause_id = str(uuid.uuid4())
        pause_status, _ = self._post(
            queued.task_id,
            {
                "action": "pause_task",
                "request_id": pause_id,
                "confirmed": True,
                "expected_updated_at": queued_preview["task_updated_at"],
                "expected_plan_version": queued_preview["plan_version"],
            },
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(pause_status, 200)
        paused = self.service.require_task(queued.task_id)
        self.assertEqual(paused.status, TaskStatus.PAUSED)
        _, paused_preview = self._get(
            f"/control/api/tasks/{queued.task_id}/actions"
        )
        resume_id = str(uuid.uuid4())
        resume_status, _ = self._post(
            queued.task_id,
            {
                "action": "resume_task",
                "request_id": resume_id,
                "confirmed": True,
                "expected_updated_at": paused_preview["task_updated_at"],
                "expected_plan_version": paused_preview["plan_version"],
            },
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(resume_status, 200)
        self.assertEqual(
            self.service.require_task(queued.task_id).status,
            TaskStatus.SUCCEEDED,
        )

    def test_pause_action_is_not_advertised_while_claim_blocks_it(self) -> None:
        queued = self.service.enqueue(
            TaskRequest(
                goal="pause only without a claim",
                operation="execute_plan",
                parameters={
                    "steps": [
                        {
                            "step_id": "one",
                            "tool_name": "write_text_artifact",
                            "expected_result": "write one",
                            "parameters": {
                                "artifact_path": "one.txt",
                                "content": "one\n",
                            },
                        }
                    ],
                    "failure_policy": "fail_fast",
                },
            )
        )
        self._set_claim_lease(
            queued.task_id,
            expires_at="2999-01-01T00:00:00.000+00:00",
        )
        _, active_preview = self._get(
            f"/control/api/tasks/{queued.task_id}/actions"
        )
        self.assertEqual(active_preview["lease_state"], "active")
        self.assertNotIn(
            "pause_task",
            {item["action"] for item in active_preview["actions"]},
        )

        task = self.service.require_task(queued.task_id)
        self.service.store.record_event_and_update(
            queued.task_id,
            "claim_expiry_fixture",
            "2026-11-10T00:00:00.000+00:00",
            updates={"lease_expires_at": "2000-01-01T00:00:00.000+00:00"},
            lease_token=task.lease_token,
        )
        _, expired_preview = self._get(
            f"/control/api/tasks/{queued.task_id}/actions"
        )
        self.assertEqual(expired_preview["lease_state"], "expired")
        self.assertNotIn(
            "pause_task",
            {item["action"] for item in expired_preview["actions"]},
        )

    def test_advanced_structured_operation_can_be_created_and_started(self) -> None:
        workspace = self.root / "workspaces" / "inspect-ui"
        workspace.mkdir(parents=True)
        (workspace / "sample.txt").write_text("inspect me\n", encoding="utf-8")
        session = self._session()
        task_id = str(uuid.uuid4())
        status, created = self._post_create(
            {
                "request_id": task_id,
                "confirmed": True,
                "goal": "inspect the registered workspace",
                "operation": "inspect_workspace",
                "parameters": {
                    "workspace": "inspect-ui",
                    "path": "sample.txt",
                },
            },
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["task_status"], TaskStatus.QUEUED.value)
        _, preview = self._get(f"/control/api/tasks/{task_id}/actions")
        self.assertIn(
            "start_queued_task",
            {item["action"] for item in preview["actions"]},
        )
        action_status, _ = self._post(
            task_id,
            self._start_payload(preview),
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(action_status, 200)
        task = self.service.require_task(task_id)
        self.assertEqual(task.status, TaskStatus.SUCCEEDED)
        self.assertEqual(
            task.tool_result.workspace_inspection["selected_path"],
            "sample.txt",
        )

    def test_control_center_injects_code_edit_authorization_server_side(
        self,
    ) -> None:
        workspace = self.root / "workspaces" / "edit-auth"
        workspace.mkdir(parents=True)
        source = "VALUE = 1\n"
        (workspace / "sample.py").write_text(source, encoding="utf-8")
        session = self._session()
        task_id = str(uuid.uuid4())
        status, created = self._post_create(
            {
                "request_id": task_id,
                "confirmed": True,
                "goal": "edit the sample source",
                "operation": "edit_text_file",
                "parameters": {
                    "workspace": "edit-auth",
                    "path": "sample.py",
                    "expected_sha256": hashlib.sha256(
                        source.encode("utf-8")
                    ).hexdigest(),
                    "old_text": "VALUE = 1",
                    "new_text": "VALUE = 2",
                    "authorization": "caller-supplied",
                },
            },
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["task_status"], TaskStatus.QUEUED.value)
        task = self.service.require_task(task_id)
        self.assertEqual(
            task.request.parameters["authorization"],
            "local-code-edit",
        )

    def test_task_creation_with_omitted_parameters_requires_clarification(self) -> None:
        session = self._session()
        request_id = str(uuid.uuid4())
        payload = {
            "request_id": request_id,
            "confirmed": True,
            "goal": "创建后等待补充内容",
            "operation": "write_text_artifact",
            "parameters": {"artifact_path": "awaiting-content.txt"},
        }
        status, created = self._post_create(
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["task_status"], TaskStatus.QUEUED.value)
        self.assertEqual(created["preview"]["classification"], "clarification")
        self.assertIn("content", created["preview"]["missing_fields"])
        _, detail_before_start = self._get(f"/api/tasks/{request_id}")
        self.assertEqual(
            detail_before_start["task"]["creation_preview"]["missing_fields"],
            ["content"],
        )
        preview_status, preview = self._get(
            f"/control/api/tasks/{request_id}/actions"
        )
        self.assertEqual(preview_status, 200)
        action_status, action = self._post(
            request_id,
            self._start_payload(preview),
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(action_status, 200)
        self.assertEqual(action["status"], "completed")
        persisted = self.service.require_task(request_id)
        self.assertEqual(persisted.status, TaskStatus.AWAITING_CLARIFICATION)
        self.assertFalse(
            (
                self.root
                / "artifacts"
                / request_id
                / "awaiting-content.txt"
            ).exists()
        )

    def test_concurrent_identical_task_creation_has_one_created_response(self) -> None:
        request_id = str(uuid.uuid4())
        payload = {
            "request_id": request_id,
            "confirmed": True,
            "goal": "并发创建同一任务",
            "operation": "write_text_artifact",
            "parameters": {
                "artifact_path": "concurrent-create.txt",
                "content": "created once\n",
            },
        }
        services = (
            TaskControlService(self.root),
            TaskControlService(self.root),
        )
        barrier = threading.Barrier(2)
        original_enqueue = TaskService.enqueue

        def synchronized_enqueue(service, request, **kwargs):
            barrier.wait(timeout=5)
            return original_enqueue(service, request, **kwargs)

        with patch.object(
            TaskService,
            "enqueue",
            new=synchronized_enqueue,
        ), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda service: service.create_task(payload),
                    services,
                )
            )
        self.assertEqual(
            sorted(item["status"] for item in results),
            ["already_exists", "created"],
        )
        self.assertEqual(
            sum(
                event.event_type == EventType.TASK_CREATED.value
                for event in self.service.events(request_id)
            ),
            1,
        )
        self.assertFalse(
            (
                self.root
                / "artifacts"
                / request_id
                / "concurrent-create.txt"
            ).exists()
        )

    def test_task_creation_does_not_recover_unrelated_expired_claim(self) -> None:
        unrelated = self.service.enqueue(
            TaskRequest(
                goal="无关任务",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "unrelated.txt",
                    "content": "not executed\n",
                },
            )
        )
        self.service.store.record_event_and_update(
            unrelated.task_id,
            EventType.TASK_CLAIMED,
            "2026-10-22T00:00:00.000+00:00",
            payload={"attempt_id": unrelated.attempt_id, "generation": 1},
            updates={
                "status": TaskStatus.RUNNING,
                "execution_owner": "crashed-worker",
                "lease_token": str(uuid.uuid4()),
                "lease_expires_at": "2000-01-01T00:00:00.000+00:00",
                "claim_generation": 1,
                "phase": "claimed",
            },
        )
        service = TaskControlService(self.root)
        service.create_task(
            {
                "request_id": str(uuid.uuid4()),
                "confirmed": True,
                "goal": "新任务",
                "operation": "write_text_artifact",
                "parameters": {
                    "artifact_path": "new.txt",
                    "content": "new\n",
                },
            }
        )
        unchanged = self.service.require_task(unrelated.task_id)
        self.assertEqual(unchanged.status, TaskStatus.RUNNING)
        self.assertIsNotNone(unchanged.lease_token)
        self.assertEqual(unchanged.phase, "claimed")

    def test_task_note_is_auditable_and_does_not_change_status(self) -> None:
        preview_status, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        self.assertEqual(preview_status, 200)
        self.assertIn(
            "add_task_note",
            {action["action"] for action in preview["actions"]},
        )
        session = self._session()
        request_id = str(uuid.uuid4())
        payload = {
            "action": "add_task_note",
            "request_id": request_id,
            "expected_updated_at": preview["task_updated_at"],
            "expected_plan_version": preview["plan_version"],
            "message": "仅供人工交流的说明 password=secret-value",
        }
        status, result = self._post(
            self.queued_task.task_id,
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "completed")
        task = self.service.require_task(self.queued_task.task_id)
        self.assertEqual(task.status, TaskStatus.QUEUED)
        self.assertEqual(task.plan_version, 0)
        notes = [
            event
            for event in self.service.events(self.queued_task.task_id)
            if event.event_type == EventType.TASK_NOTE_ADDED.value
        ]
        self.assertEqual(len(notes), 1)
        self.assertIn("[redacted]", notes[0].payload["message"])
        self.assertNotIn("secret-value", notes[0].payload["message"])
        _, receipt = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.queued_task.task_id}/requests/"
                f"{request_id}"
            ),
            token=session["token"],
        )
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(receipt["action"], "add_task_note")

    def test_clarification_claim_rejects_racing_plan_revision(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.task.task_id}/actions"
        )
        session = self._session()
        payload = self._action_payload(preview)
        original_reserve = TaskService.reserve_control_action

        def revise_before_reservation(
            service: TaskService,
            task_id: str,
            **kwargs,
        ):
            if kwargs.get("action") == "answer_clarification":
                service.revise_plan(
                    task_id,
                    {"goal": "并发修订后的任务目标"},
                    source_id="racing-revision",
                    expected_plan_version=0,
                    confirmed=True,
                )
            return original_reserve(service, task_id, **kwargs)

        with patch.object(
            TaskService,
            "reserve_control_action",
            new=revise_before_reservation,
        ), self.assertRaises(HTTPError) as caught:
            self._post(
                self.task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(error["error"]["code"], "task_version_changed")
        caught.exception.close()

        current = self.service.require_task(self.task.task_id)
        self.assertEqual(current.status, TaskStatus.AWAITING_CLARIFICATION)
        self.assertEqual(current.plan_version, 1)
        self.assertFalse(
            (self.root / "artifacts" / self.task.task_id).exists()
        )
        event_types = [
            event.event_type
            for event in self.service.events(self.task.task_id)
        ]
        self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 0)
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
            0,
        )
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_FAILED.value),
            1,
        )
        _, request_status = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(request_status["state"], "failed")
        self.assertEqual(
            request_status["control_error_code"],
            "task_version_changed",
        )
        self.assertEqual(
            request_status["task_status"],
            TaskStatus.AWAITING_CLARIFICATION.value,
        )

    def test_control_claim_atomically_rejects_stale_plan_version(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.task.task_id}/actions"
        )
        self.service.revise_plan(
            self.task.task_id,
            {"goal": "先完成并发修订"},
            source_id="stale-claim-revision",
            expected_plan_version=0,
            confirmed=True,
        )
        with self.assertRaises(PlanRevisionConflict):
            self.service.reserve_control_action(
                self.task.task_id,
                request_id=str(uuid.uuid4()),
                action="answer_clarification",
                request_digest="a" * 64,
                expected_updated_at=preview["task_updated_at"],
                expected_plan_version=preview["plan_version"],
                payload={
                    "request_id": str(uuid.uuid4()),
                    "action": "answer_clarification",
                    "request_digest": "a" * 64,
                },
                require_claim=True,
                allowed_statuses=frozenset(
                    {TaskStatus.AWAITING_CLARIFICATION}
                ),
            )

    def test_preclaimed_validation_failure_releases_lease(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.task.task_id}/actions"
        )
        session = self._session()
        payload = self._action_payload(preview)
        before = self.service.require_task(self.task.task_id)
        original = TaskControlService._validate_action_eligibility
        calls = 0

        def fail_after_claim(
            control: TaskControlService,
            task,
            **kwargs,
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ControlActionError(
                    "task_clarification_changed",
                    "the missing fields changed after reservation",
                )
            return original(control, task, **kwargs)

        with patch.object(
            TaskControlService,
            "_validate_action_eligibility",
            new=fail_after_claim,
        ), self.assertRaises(HTTPError) as caught:
            self._post(
                self.task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        caught.exception.close()
        task = self.service.require_task(self.task.task_id)
        self.assertIsNone(task.lease_token)
        self.assertIsNone(task.lease_expires_at)
        self.assertEqual(task.attempt_id, before.attempt_id)
        self.assertEqual(task.claim_generation, before.claim_generation)
        self.assertEqual(task.phase, before.phase)
        self.assertEqual(
            task.execution_started_at,
            before.execution_started_at,
        )
        self.assertEqual(
            task.execution_deadline_at,
            before.execution_deadline_at,
        )
        _, receipt = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(
            receipt["control_error_code"],
            "task_clarification_changed",
        )

    def test_revision_cannot_overwrite_active_clarification_claim(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.task.task_id}/actions"
        )
        session = self._session()
        payload = self._action_payload(preview)
        claimed = threading.Event()
        release = threading.Event()
        original_reserve = EventStore.reserve_control_action_with_claim

        def pause_after_claim(
            store: EventStore,
            task_id: str,
            **kwargs,
        ):
            result = original_reserve(store, task_id, **kwargs)
            if result.get("reserved"):
                claimed.set()
                if not release.wait(timeout=5):
                    raise AssertionError("test did not release claim")
            return result

        def invoke() -> tuple[int, dict]:
            try:
                return self._post(
                    self.task.task_id,
                    payload,
                    token=session["token"],
                    origin=self.origin,
                )
            except HTTPError as exc:
                try:
                    return (
                        exc.code,
                        json.loads(exc.read().decode("utf-8")),
                    )
                finally:
                    exc.close()

        with patch.object(
            EventStore,
            "reserve_control_action_with_claim",
            new=pause_after_claim,
        ), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(invoke)
            try:
                self.assertTrue(claimed.wait(timeout=5))
                with self.assertRaises(PlanRevisionConflict) as caught:
                    self.service.revise_plan(
                        self.task.task_id,
                        {"goal": "不得覆盖活动 claim"},
                        source_id="active-claim-revision",
                        expected_plan_version=0,
                        confirmed=True,
                    )
                self.assertIn("lease", str(caught.exception))
            finally:
                release.set()
            status, result = future.result(timeout=10)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "completed")
        current = self.service.require_task(self.task.task_id)
        self.assertEqual(current.status, TaskStatus.SUCCEEDED)
        self.assertEqual(current.plan_version, 0)
        event_types = [
            event.event_type
            for event in self.service.events(self.task.task_id)
        ]
        self.assertEqual(event_types.count(EventType.PLAN_REVISED.value), 0)
        self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 1)

    def test_control_reservation_and_claim_roll_back_together(self) -> None:
        service = TaskService(self.root)
        task = service.require_task(self.queued_task.task_id)
        request_id = str(uuid.uuid4())
        digest = _versioned_request_digest(
            action="start_queued_task",
            expected_updated_at=task.updated_at,
            expected_plan_version=task.plan_version,
        )
        original_insert_events = EventStore._insert_events

        def fail_atomic_insert(store, connection, events):
            if any(
                event.event_type
                in {
                    EventType.CONTROL_ACTION_REQUESTED.value,
                    EventType.TASK_CLAIMED.value,
                }
                for event in events
            ):
                raise OSError("simulated atomic insert failure")
            return original_insert_events(store, connection, events)

        with patch.object(
            EventStore,
            "_insert_events",
            new=fail_atomic_insert,
        ), self.assertRaises(OSError):
            service.reserve_control_action(
                task.task_id,
                request_id=request_id,
                action="start_queued_task",
                request_digest=digest,
                expected_updated_at=task.updated_at,
                expected_plan_version=task.plan_version,
                payload={
                    "request_id": request_id,
                    "action": "start_queued_task",
                    "request_digest": digest,
                    "expected_updated_at": task.updated_at,
                    "expected_plan_version": task.plan_version,
                },
                require_claim=True,
                allowed_statuses=frozenset({TaskStatus.QUEUED}),
            )
        unchanged = service.require_task(task.task_id)
        self.assertEqual(unchanged.claim_generation, 0)
        self.assertIsNone(unchanged.lease_token)
        event_types = [
            event.event_type for event in service.events(task.task_id)
        ]
        self.assertNotIn(
            EventType.CONTROL_ACTION_REQUESTED.value,
            event_types,
        )
        self.assertNotIn(EventType.TASK_CLAIMED.value, event_types)

    def test_preclaim_failure_is_idempotent_across_instances(self) -> None:
        services = (TaskService(self.root), TaskService(self.root))
        task = services[0].require_task(self.queued_task.task_id)
        request_id = str(uuid.uuid4())
        digest = _versioned_request_digest(
            action="start_queued_task",
            expected_updated_at=task.updated_at,
            expected_plan_version=task.plan_version,
        )
        requested_payload = {
            "request_id": request_id,
            "action": "start_queued_task",
            "request_digest": digest,
            "expected_updated_at": task.updated_at,
            "expected_plan_version": task.plan_version,
        }
        original = EventStore.record_control_action_preclaim_failure
        barrier = threading.Barrier(2)

        def synchronized_record(store, *args, **kwargs):
            barrier.wait(timeout=5)
            return original(store, *args, **kwargs)

        def invoke(service: TaskService) -> dict:
            return service.store.record_control_action_preclaim_failure(
                task.task_id,
                request_id=request_id,
                action="start_queued_task",
                request_digest=digest,
                occurred_at="2026-10-22T00:00:00.000+00:00",
                requested_payload=requested_payload,
                error_code="task_version_changed",
                error_type="PlanRevisionConflict",
                task_status=task.status.value,
            )

        with patch.object(
            EventStore,
            "record_control_action_preclaim_failure",
            new=synchronized_record,
        ), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(invoke, services))
        self.assertEqual(
            sum(bool(result.get("recorded")) for result in results),
            1,
        )
        events = self.service.events(task.task_id)
        event_types = [event.event_type for event in events]
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_REQUESTED.value),
            1,
        )
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_FAILED.value),
            1,
        )

    def test_unexpected_postclaim_error_requires_manual_reconciliation(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.task.task_id}/actions"
        )
        session = self._session()
        payload = self._action_payload(preview)

        def explode(*args, **kwargs):
            raise RuntimeError("simulated post-claim failure")

        with patch.object(
            TaskService,
            "resume",
            new=explode,
        ), self.assertRaises(HTTPError) as caught:
            self._post(
                self.task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 500)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(
            error["error"]["code"],
            "control_action_outcome_unknown",
        )
        caught.exception.close()
        _, status = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(status["state"], "inconsistent")
        self.assertTrue(status["manual_review_required"])
        event_types = [
            event.event_type
            for event in self.service.events(self.task.task_id)
        ]
        self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 0)

    def test_unstarted_revision_is_versioned_idempotent_and_non_executing(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        actions = {item["action"]: item for item in preview["actions"]}
        self.assertIn("revise_unstarted_task", actions)
        self.assertEqual(
            actions["revise_unstarted_task"]["current_goal"],
            "创建一个尚未执行的排队任务",
        )
        session = self._session()
        payload = self._revision_payload(
            preview,
            goal="修改后的排队任务",
            parameters={
                "artifact_path": "revised.txt",
                "content": "revised content\n",
            },
        )

        status, result = self._post(
            self.queued_task.task_id,
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["plan_version"], 1)
        revised = self.service.require_task(self.queued_task.task_id)
        self.assertEqual(revised.status, TaskStatus.QUEUED)
        self.assertEqual(revised.execution_mode.value, "not_executed")
        self.assertEqual(revised.request.goal, "修改后的排队任务")
        self.assertEqual(
            revised.request.parameters,
            {
                "artifact_path": "revised.txt",
                "content": "revised content\n",
            },
        )
        self.assertEqual(revised.plan_version, 1)
        self.assertEqual(
            revised.plan_revisions[0].source_id,
            payload["request_id"],
        )
        self.assertFalse(
            (self.root / "artifacts" / self.queued_task.task_id).exists()
        )
        event_types = [
            event.event_type
            for event in self.service.events(self.queued_task.task_id)
        ]
        self.assertEqual(event_types.count(EventType.PLAN_REVISED.value), 1)
        self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 0)
        self.assertIn(
            EventType.CONTROL_ACTION_COMPLETED.value,
            event_types,
        )

        replay_status, replay = self._post(
            self.queued_task.task_id,
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay["status"], "already_processed")
        self.assertEqual(
            self.service.require_task(self.queued_task.task_id).plan_version,
            1,
        )

    def test_revision_rejects_noop_stale_and_execution_evidence(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        session = self._session()
        noop = self._revision_payload(preview)
        noop["changes"] = {"goal": preview["actions"][0]["current_goal"]}
        with self.assertRaises(HTTPError) as caught:
            self._post(
                self.queued_task.task_id,
                noop,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 400)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(error["error"]["code"], "no_task_changes")
        caught.exception.close()

        stale = self._revision_payload(preview)
        self.service.revise_plan(
            self.queued_task.task_id,
            {"parameters": {"content": "external\n"}},
            source_id="external-revision",
            expected_plan_version=0,
            confirmed=True,
        )
        with self.assertRaises(HTTPError) as caught:
            self._post(
                self.queued_task.task_id,
                stale,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(error["error"]["code"], "task_version_changed")
        caught.exception.close()

        completed = self.service.submit(
            TaskRequest(
                goal="已完成任务不可修改",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "completed.txt",
                    "content": "complete\n",
                },
            )
        )
        _, completed_preview = self._get(
            f"/control/api/tasks/{completed.task_id}/actions"
        )
        self.assertEqual(
            completed_preview["actions"],
            [{"action": "add_task_note", "requires_confirmation": False}],
        )
        request = self._revision_payload(
            preview,
            parameters={"content": "must-not-apply\n"},
        )
        request["expected_updated_at"] = completed.updated_at
        request["expected_plan_version"] = completed.plan_version
        revise_action = next(
            action
            for action in preview["actions"]
            if action["action"] == "revise_unstarted_task"
        )
        request["expected_request_digest"] = revise_action["request_digest"]
        with self.assertRaises(HTTPError) as caught:
            self._post(
                completed.task_id,
                request,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn(
            error["error"]["code"],
            {"task_status_changed", "task_revision_not_allowed"},
        )
        caught.exception.close()

    def test_revision_rejects_invalid_changes_and_reused_request_content(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        session = self._session()
        payload = self._revision_payload(preview)
        invalid_operation = {
            **payload,
            "request_id": str(uuid.uuid4()),
            "changes": {"operation": "run_tests"},
        }
        invalid_parameters = {
            **payload,
            "request_id": str(uuid.uuid4()),
            "changes": {"parameters": []},
        }
        for request, expected_code in (
            (invalid_operation, "unknown_revision_fields"),
            (invalid_parameters, "invalid_revision_parameters"),
        ):
            with self.subTest(expected_code=expected_code), self.assertRaises(
                HTTPError
            ) as caught:
                self._post(
                    self.queued_task.task_id,
                    request,
                    token=session["token"],
                    origin=self.origin,
                )
            self.assertEqual(caught.exception.code, 400)
            error = json.loads(caught.exception.read().decode("utf-8"))
            self.assertEqual(error["error"]["code"], expected_code)
            caught.exception.close()
        nonfinite = {
            **payload,
            "request_id": str(uuid.uuid4()),
            "changes": {
                "parameters": {
                    "artifact_path": "finite.txt",
                    "injected": float("inf"),
                }
            },
        }
        with self.assertRaises(HTTPError) as caught:
            self._post(
                self.queued_task.task_id,
                nonfinite,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 400)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(error["error"]["code"], "invalid_json")
        caught.exception.close()
        self.assertEqual(
            self.service.require_task(self.queued_task.task_id).plan_version,
            0,
        )

    def test_competing_revision_instances_have_one_winner(self) -> None:
        competing = create_server(
            self.root,
            port=0,
            control_enabled=True,
        )
        thread = threading.Thread(
            target=competing.serve_forever,
            daemon=True,
        )
        thread.start()
        host, port = competing.server_address[:2]
        competing_base = f"http://{host}:{port}"
        try:
            bases = (self.base, competing_base)
            previews = []
            sessions = []
            for base in bases:
                _, preview = self._get_from(
                    base,
                    f"/control/api/tasks/{self.queued_task.task_id}/actions",
                )
                _, session = self._get_from(
                    base,
                    "/control/api/session",
                )
                previews.append(preview)
                sessions.append(session)
            payloads = [
                self._revision_payload(
                    previews[index],
                    goal=f"并发修订 {index}",
                    parameters={"content": f"winner-{index}\n"},
                )
                for index in range(2)
            ]
            original_require_task = TaskService.require_task
            barrier = threading.Barrier(2)
            synchronized = threading.local()

            def synchronized_require_task(
                service: TaskService,
                task_id: str,
            ):
                task = original_require_task(service, task_id)
                if not getattr(synchronized, "entered", False):
                    synchronized.entered = True
                    barrier.wait(timeout=5)
                return task

            def invoke(index: int) -> tuple[int, dict]:
                base = bases[index]
                try:
                    return self._post(
                        self.queued_task.task_id,
                        payloads[index],
                        base=base,
                        token=sessions[index]["token"],
                        origin=base,
                    )
                except HTTPError as exc:
                    try:
                        return (
                            exc.code,
                            json.loads(exc.read().decode("utf-8")),
                        )
                    finally:
                        exc.close()

            with patch.object(
                TaskService,
                "require_task",
                new=synchronized_require_task,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(invoke, range(2)))

            self.assertEqual(
                [status for status, _ in results].count(200),
                1,
            )
            self.assertEqual(
                [
                    body["error"]["code"]
                    for status, body in results
                    if status == 409
                ],
                ["task_version_changed"],
            )
            final = self.service.require_task(self.queued_task.task_id)
            self.assertEqual(final.plan_version, 1)
            event_types = [
                event.event_type
                for event in self.service.events(self.queued_task.task_id)
            ]
            self.assertEqual(event_types.count(EventType.PLAN_REVISED.value), 1)
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_REQUESTED.value),
                2,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
                1,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_FAILED.value),
                1,
            )
            self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 0)
        finally:
            competing.shutdown()
            competing.server_close()
            thread.join(timeout=5)

    def test_unstarted_queued_task_can_be_cancelled_idempotently(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        self.assertIn(
            "revise_unstarted_task",
            {action["action"] for action in preview["actions"]},
        )
        session = self._session()
        payload = self._cancel_payload(preview)
        status, result = self._post(
            self.queued_task.task_id,
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "completed")
        cancelled = self.service.require_task(self.queued_task.task_id)
        self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
        self.assertIsNone(cancelled.artifact)
        events = self.service.events(self.queued_task.task_id)
        cancelled_events = [
            event
            for event in events
            if event.event_type == EventType.TASK_CANCELLED.value
        ]
        self.assertEqual(len(cancelled_events), 1)
        self.assertEqual(
            cancelled_events[0].payload.get("request_id"),
            payload["request_id"],
        )
        self.assertEqual(
            sum(
                event.event_type == EventType.TOOL_STARTED.value
                for event in events
            ),
            0,
        )
        replay_status, replay = self._post(
            self.queued_task.task_id,
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay["status"], "already_processed")
        self.assertEqual(
            len(
                [
                    event
                    for event in self.service.events(self.queued_task.task_id)
                    if event.event_type == EventType.TASK_CANCELLED.value
                ]
            ),
            1,
        )

    def test_cancel_rechecks_claim_generation_atomically(self) -> None:
        service = TaskService(self.root)
        claimed = service._claim(
            self.queued_task.task_id,
            allowed=TaskService.RUNNABLE_STATUSES,
        )
        self.assertTrue(claimed.acquired)
        lease_token = claimed.task.lease_token
        self.assertIsNotNone(lease_token)
        service.store.release_claim(
            self.queued_task.task_id,
            lease_token=lease_token,
            now=service.clock(),
        )
        with self.assertRaises(PlanRevisionConflict):
            service.cancel(
                self.queued_task.task_id,
                expected_plan_version=0,
                expected_claim_generation=0,
            )
        unchanged = service.require_task(self.queued_task.task_id)
        self.assertEqual(unchanged.status, TaskStatus.QUEUED)
        self.assertEqual(unchanged.claim_generation, 1)

    def test_llm_planner_queued_task_can_still_be_cancelled(self) -> None:
        task = self.service.enqueue(
            TaskRequest(
                goal="LLM 规划但尚未执行的任务",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "llm-queued.txt",
                    "content": "not executed\n",
                },
            )
        )
        self.service.store.record_event_and_update(
            task.task_id,
            "planner_mode_fixture",
            "2026-10-22T00:00:00.000+00:00",
            updates={"planner_mode": PlannerMode.LLM},
        )
        _, preview = self._get(
            f"/control/api/tasks/{task.task_id}/actions"
        )
        self.assertIn(
            "cancel_queued_task",
            {action["action"] for action in preview["actions"]},
        )
        session = self._session()
        status, result = self._post(
            task.task_id,
            self._cancel_payload(preview),
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            self.service.require_task(task.task_id).status,
            TaskStatus.CANCELLED,
        )

    def test_cancel_completion_loss_reconciles_from_task_event(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        session = self._session()
        payload = self._cancel_payload(preview)
        original = EventStore.record_event_and_update
        failed_once = {"value": False}

        def fail_completion(
            store,
            task_id,
            event_type,
            occurred_at,
            payload=None,
            updates=None,
            lease_token=None,
        ):
            if (
                event_type == EventType.CONTROL_ACTION_COMPLETED
                and not failed_once["value"]
            ):
                failed_once["value"] = True
                raise OSError("simulated completion persistence failure")
            return original(
                store,
                task_id,
                event_type,
                occurred_at,
                payload=payload,
                updates=updates,
                lease_token=lease_token,
            )

        with patch.object(
            EventStore,
            "record_event_and_update",
            new=fail_completion,
        ), self.assertRaises(HTTPError) as caught:
            self._post(
                self.queued_task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 500)
        caught.exception.close()

        _, status = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.queued_task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["reconciled_from"], "task_cancelled_event")
        self.assertEqual(status["task_status"], "cancelled")
        self.assertFalse(status["manual_review_required"])
        event_types = [
            event.event_type
            for event in self.service.events(self.queued_task.task_id)
        ]
        self.assertEqual(event_types.count(EventType.TASK_CANCELLED.value), 1)
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
            0,
        )

    def test_cancel_and_revision_race_has_one_side_effect(self) -> None:
        competing = create_server(
            self.root,
            port=0,
            control_enabled=True,
        )
        thread = threading.Thread(
            target=competing.serve_forever,
            daemon=True,
        )
        thread.start()
        host, port = competing.server_address[:2]
        competing_base = f"http://{host}:{port}"
        try:
            bases = (self.base, competing_base)
            previews = []
            sessions = []
            for base in bases:
                _, preview = self._get_from(
                    base,
                    f"/control/api/tasks/{self.queued_task.task_id}/actions",
                )
                _, session = self._get_from(
                    base,
                    "/control/api/session",
                )
                previews.append(preview)
                sessions.append(session)
            requests = (
                self._cancel_payload(previews[0]),
                self._revision_payload(
                    previews[1],
                    parameters={"content": "revision-wins\n"},
                ),
            )
            original_reserve = EventStore.reserve_control_action
            revision_reserved = threading.Event()

            def coordinated_reserve(store, *args, **kwargs):
                if kwargs.get("action") == "cancel_queued_task":
                    revision_reserved.wait(timeout=5)
                result = original_reserve(store, *args, **kwargs)
                if (
                    kwargs.get("action") == "revise_unstarted_task"
                    and result.get("reserved")
                ):
                    revision_reserved.set()
                return result

            def invoke(index: int) -> tuple[int, dict]:
                base = bases[index]
                try:
                    return self._post(
                        self.queued_task.task_id,
                        requests[index],
                        base=base,
                        token=sessions[index]["token"],
                        origin=base,
                    )
                except HTTPError as exc:
                    try:
                        return (
                            exc.code,
                            json.loads(exc.read().decode("utf-8")),
                        )
                    finally:
                        exc.close()

            with patch.object(
                EventStore,
                "reserve_control_action",
                new=coordinated_reserve,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(invoke, range(2)))
            self.assertEqual([status for status, _ in results].count(200), 1)
            self.assertEqual([status for status, _ in results].count(409), 1)
            final = self.service.require_task(self.queued_task.task_id)
            self.assertEqual(final.status, TaskStatus.QUEUED)
            self.assertEqual(final.plan_version, 1)
            event_types = [
                event.event_type
                for event in self.service.events(self.queued_task.task_id)
            ]
            self.assertEqual(
                event_types.count(EventType.PLAN_REVISED.value),
                1,
            )
            self.assertEqual(
                event_types.count(EventType.TASK_CANCELLED.value),
                0,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
                1,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_FAILED.value),
                1,
            )
            self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 0)
        finally:
            competing.shutdown()
            competing.server_close()
            thread.join(timeout=5)

    def test_start_queued_task_executes_once_and_links_claim_receipt(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        self.assertIn(
            "start_queued_task",
            {action["action"] for action in preview["actions"]},
        )
        session = self._session()
        payload = self._start_payload(preview)
        status, result = self._post(
            self.queued_task.task_id,
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "completed")
        started = self.service.require_task(self.queued_task.task_id)
        self.assertEqual(started.status, TaskStatus.SUCCEEDED)
        self.assertTrue(started.verification.passed)
        artifact = (
            self.root
            / "artifacts"
            / self.queued_task.task_id
            / "queued.txt"
        )
        self.assertEqual(artifact.read_text(encoding="utf-8"), "original\n")
        events = self.service.events(self.queued_task.task_id)
        claims = [
            event
            for event in events
            if event.event_type == EventType.TASK_CLAIMED.value
            and event.payload.get("control_request_id")
            == payload["request_id"]
        ]
        self.assertEqual(len(claims), 1)
        self.assertEqual(
            claims[0].payload.get("attempt_id"),
            started.attempt_id,
        )
        replay_status, replay = self._post(
            self.queued_task.task_id,
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay["status"], "already_processed")
        self.assertEqual(
            sum(
                event.event_type == EventType.TOOL_STARTED.value
                for event in self.service.events(self.queued_task.task_id)
            ),
            1,
        )

    def test_in_progress_receipt_is_not_treated_as_unknown_or_retried(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        session = self._session()
        payload = self._start_payload(preview)
        digest = _versioned_request_digest(
            action=payload["action"],
            expected_updated_at=payload["expected_updated_at"],
            expected_plan_version=payload["expected_plan_version"],
        )
        task = self.service.require_task(self.queued_task.task_id)
        self.service.store.record_event_and_update(
            task.task_id,
            EventType.CONTROL_ACTION_REQUESTED,
            "2026-10-22T00:00:00.000+00:00",
            payload={
                "request_id": payload["request_id"],
                "action": payload["action"],
                "request_digest": digest,
            },
        )
        self.service.store.record_event_and_update(
            task.task_id,
            EventType.TASK_CLAIMED,
            "2026-10-22T00:00:01.000+00:00",
            payload={
                "attempt_id": task.attempt_id,
                "control_request_id": payload["request_id"],
                "control_action": "start_queued_task",
            },
            updates={
                "lease_token": str(uuid.uuid4()),
                "lease_expires_at": "2099-01-01T00:00:00.000+00:00",
            },
        )
        _, receipt = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(receipt["state"], "in_progress")
        self.assertFalse(receipt["manual_review_required"])
        with self.assertRaises(HTTPError) as caught:
            self._post(
                task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(error["error"]["code"], "action_in_progress")
        caught.exception.close()
        event_types = [
            event.event_type
            for event in self.service.events(task.task_id)
        ]
        self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 0)

    def test_released_or_expired_claim_is_not_reported_in_progress(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        session = self._session()
        payload = self._start_payload(preview)
        digest = _versioned_request_digest(
            action=payload["action"],
            expected_updated_at=payload["expected_updated_at"],
            expected_plan_version=payload["expected_plan_version"],
        )
        task = self.service.require_task(self.queued_task.task_id)
        self.service.store.record_event_and_update(
            task.task_id,
            EventType.CONTROL_ACTION_REQUESTED,
            "2026-10-22T00:00:00.000+00:00",
            payload={
                "request_id": payload["request_id"],
                "action": payload["action"],
                "request_digest": digest,
            },
        )
        self.service.store.record_event_and_update(
            task.task_id,
            EventType.TASK_CLAIMED,
            "2026-10-22T00:00:01.000+00:00",
            payload={
                "attempt_id": task.attempt_id,
                "control_request_id": payload["request_id"],
                "control_action": "start_queued_task",
            },
        )
        _, released = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(released["state"], "unknown")
        self.assertTrue(released["manual_review_required"])

        self.service.store.record_event_and_update(
            task.task_id,
            EventType.LEASE_EXPIRED,
            "2026-10-22T00:00:02.000+00:00",
            updates={
                "lease_token": str(uuid.uuid4()),
                "lease_expires_at": "2000-01-01T00:00:00.000+00:00",
            },
        )
        _, expired = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(expired["state"], "unknown")
        self.assertTrue(expired["manual_review_required"])

        self.service.store.record_event_and_update(
            task.task_id,
            EventType.LEASE_EXPIRED,
            "2026-10-22T00:00:03.000+00:00",
            updates={
                "lease_token": str(uuid.uuid4()),
                "lease_expires_at": None,
            },
        )
        _, missing_expiry = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(missing_expiry["state"], "in_progress")
        self.assertFalse(missing_expiry["manual_review_required"])
        with self.assertRaises(HTTPError) as replay:
            self._post(
                task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(replay.exception.code, 409)
        replay_error = json.loads(
            replay.exception.read().decode("utf-8")
        )
        self.assertEqual(
            replay_error["error"]["code"],
            "action_in_progress",
        )
        replay.exception.close()

    def test_test_task_start_is_not_enabled(self) -> None:
        test_task = self.service.enqueue(
            TaskRequest(
                goal="不应从控制台启动的测试任务",
                operation="run_tests",
                parameters={
                    "workspace": "workspace",
                    "test_target": "tests.test_example",
                },
            )
        )
        _, preview = self._get(
            f"/control/api/tasks/{test_task.task_id}/actions"
        )
        actions = {item["action"] for item in preview["actions"]}
        self.assertNotIn("start_queued_task", actions)
        self.assertIn("revise_unstarted_task", actions)
        self.assertIn("cancel_queued_task", actions)
        request = {
            "action": "start_queued_task",
            "request_id": str(uuid.uuid4()),
            "confirmed": True,
            "expected_updated_at": preview["task_updated_at"],
            "expected_plan_version": preview["plan_version"],
        }
        session = self._session()
        with self.assertRaises(HTTPError) as caught:
            self._post(
                test_task.task_id,
                request,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(error["error"]["code"], "task_start_not_allowed")
        caught.exception.close()
        unchanged = self.service.require_task(test_task.task_id)
        self.assertEqual(unchanged.status, TaskStatus.QUEUED)
        self.assertEqual(unchanged.claim_generation, 0)

    def test_test_task_clarification_cannot_bypass_start_boundary(self) -> None:
        test_task = self.service.enqueue(
            TaskRequest(
                goal="不得通过澄清启动测试任务",
                operation="run_tests",
                parameters={"workspace": "workspace"},
            )
        )
        self.service.execute_task(test_task.task_id)
        clarified = self.service.require_task(test_task.task_id)
        self.assertEqual(
            clarified.status,
            TaskStatus.AWAITING_CLARIFICATION,
        )
        _, preview = self._get(
            f"/control/api/tasks/{test_task.task_id}/actions"
        )
        actions = {item["action"] for item in preview["actions"]}
        self.assertNotIn("answer_clarification", actions)
        before = [
            event.event_type
            for event in self.service.events(test_task.task_id)
        ]
        session = self._session()
        request = {
            "action": "answer_clarification",
            "request_id": str(uuid.uuid4()),
            "confirmed": True,
            "expected_updated_at": preview["task_updated_at"],
            "expected_plan_version": preview["plan_version"],
            "expected_missing_fields": ["runner", "target"],
            "parameters": {
                "runner": "unittest",
                "target": "tests.test_example",
            },
        }
        with self.assertRaises(HTTPError) as caught:
            self._post(
                test_task.task_id,
                request,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(
            error["error"]["code"],
            "task_clarification_start_not_supported",
        )
        caught.exception.close()
        unchanged = self.service.require_task(test_task.task_id)
        self.assertEqual(
            unchanged.status,
            TaskStatus.AWAITING_CLARIFICATION,
        )
        after = [
            event.event_type
            for event in self.service.events(test_task.task_id)
        ]
        self.assertEqual(
            after.count(EventType.TOOL_STARTED.value),
            before.count(EventType.TOOL_STARTED.value),
        )
        self.assertEqual(
            after.count(EventType.TASK_CLAIMED.value),
            before.count(EventType.TASK_CLAIMED.value),
        )

    def test_service_start_and_cancel_guard_stale_plan_versions(self) -> None:
        revised = self.service.revise_plan(
            self.queued_task.task_id,
            {"parameters": {"content": "new-version\n"}},
            source_id="direct-revision",
            expected_plan_version=0,
            confirmed=True,
        )
        self.assertEqual(revised.plan_version, 1)
        with self.assertRaises(PlanRevisionConflict):
            self.service.execute_task(
                self.queued_task.task_id,
                require_claim=True,
                expected_plan_version=0,
            )
        with self.assertRaises(PlanRevisionConflict):
            self.service.cancel(
                self.queued_task.task_id,
                expected_plan_version=0,
            )
        unchanged = self.service.require_task(self.queued_task.task_id)
        self.assertEqual(unchanged.status, TaskStatus.QUEUED)
        self.assertEqual(unchanged.plan_version, 1)
        self.assertEqual(unchanged.claim_generation, 0)
        event_types = [
            event.event_type
            for event in self.service.events(self.queued_task.task_id)
        ]
        self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 0)
        self.assertEqual(event_types.count(EventType.TASK_CANCELLED.value), 0)

    def test_start_completion_loss_reconciles_from_claim_terminal(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        session = self._session()
        payload = self._start_payload(preview)
        original = EventStore.record_event_and_update
        failed_once = {"value": False}

        def fail_completion(
            store,
            task_id,
            event_type,
            occurred_at,
            payload=None,
            updates=None,
            lease_token=None,
        ):
            if (
                event_type == EventType.CONTROL_ACTION_COMPLETED
                and not failed_once["value"]
            ):
                failed_once["value"] = True
                raise OSError("simulated completion persistence failure")
            return original(
                store,
                task_id,
                event_type,
                occurred_at,
                payload=payload,
                updates=updates,
                lease_token=lease_token,
            )

        with patch.object(
            EventStore,
            "record_event_and_update",
            new=fail_completion,
        ), self.assertRaises(HTTPError) as caught:
            self._post(
                self.queued_task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 500)
        caught.exception.close()

        _, status = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.queued_task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["reconciled_from"], "task_claimed_terminal")
        self.assertEqual(status["task_status"], "succeeded")
        self.assertFalse(status["manual_review_required"])
        event_types = [
            event.event_type
            for event in self.service.events(self.queued_task.task_id)
        ]
        self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 1)
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
            0,
        )

    def test_start_reconciliation_requires_same_attempt_id(self) -> None:
        completed = self.service.submit(
            TaskRequest(
                goal="后续尝试不能被旧请求冒认",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "attempt-mismatch.txt",
                    "content": "latest\n",
                },
            )
        )
        self.assertEqual(completed.status, TaskStatus.SUCCEEDED)
        request_id = str(uuid.uuid4())
        digest = _versioned_request_digest(
            action="start_queued_task",
            expected_updated_at=completed.updated_at,
            expected_plan_version=completed.plan_version,
        )
        self.service.store.record_event_and_update(
            completed.task_id,
            EventType.CONTROL_ACTION_REQUESTED,
            "2026-10-22T00:00:00.000+00:00",
            payload={
                "request_id": request_id,
                "action": "start_queued_task",
                "request_digest": digest,
            },
        )
        self.service.store.record_event_and_update(
            completed.task_id,
            EventType.TASK_CLAIMED,
            "2026-10-22T00:00:01.000+00:00",
            payload={
                "control_request_id": request_id,
                "attempt_id": str(uuid.uuid4()),
                "generation": 1,
                "control_action": "start_queued_task",
            },
        )
        session = self._session()
        _, status = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{completed.task_id}/requests/"
                f"{request_id}"
            ),
            token=session["token"],
        )
        self.assertEqual(status["state"], "unknown")
        self.assertTrue(status["manual_review_required"])
        self.assertIsNone(status["reconciled_from"])
        self.assertEqual(status["task_status"], "succeeded")

    def test_start_and_cancel_race_has_one_terminal_effect(self) -> None:
        competing = create_server(
            self.root,
            port=0,
            control_enabled=True,
        )
        thread = threading.Thread(
            target=competing.serve_forever,
            daemon=True,
        )
        thread.start()
        host, port = competing.server_address[:2]
        competing_base = f"http://{host}:{port}"
        try:
            bases = (self.base, competing_base)
            previews = []
            sessions = []
            for base in bases:
                _, preview = self._get_from(
                    base,
                    f"/control/api/tasks/{self.queued_task.task_id}/actions",
                )
                _, session = self._get_from(
                    base,
                    "/control/api/session",
                )
                previews.append(preview)
                sessions.append(session)
            requests = (
                self._start_payload(previews[0]),
                self._cancel_payload(previews[1]),
            )
            original_require_task = TaskService.require_task
            barrier = threading.Barrier(2)
            synchronized = threading.local()

            def synchronized_require_task(
                service: TaskService,
                task_id: str,
            ):
                task = original_require_task(service, task_id)
                if not getattr(synchronized, "entered", False):
                    synchronized.entered = True
                    barrier.wait(timeout=5)
                return task

            def invoke(index: int) -> tuple[int, dict]:
                base = bases[index]
                try:
                    return self._post(
                        self.queued_task.task_id,
                        requests[index],
                        base=base,
                        token=sessions[index]["token"],
                        origin=base,
                    )
                except HTTPError as exc:
                    try:
                        return (
                            exc.code,
                            json.loads(exc.read().decode("utf-8")),
                        )
                    finally:
                        exc.close()

            with patch.object(
                TaskService,
                "require_task",
                new=synchronized_require_task,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(invoke, range(2)))
            self.assertEqual([status for status, _ in results].count(200), 1)
            self.assertEqual([status for status, _ in results].count(409), 1)
            final = self.service.require_task(self.queued_task.task_id)
            self.assertIn(
                final.status,
                {TaskStatus.SUCCEEDED, TaskStatus.CANCELLED},
            )
            events = self.service.events(self.queued_task.task_id)
            event_types = [event.event_type for event in events]
            self.assertEqual(
                event_types.count(EventType.TASK_SUCCEEDED.value)
                + event_types.count(EventType.TASK_CANCELLED.value),
                1,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
                1,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_FAILED.value),
                1,
            )
        finally:
            competing.shutdown()
            competing.server_close()
            thread.join(timeout=5)

    def test_start_and_revision_race_respects_expected_plan_version(
        self,
    ) -> None:
        competing = create_server(
            self.root,
            port=0,
            control_enabled=True,
        )
        thread = threading.Thread(
            target=competing.serve_forever,
            daemon=True,
        )
        thread.start()
        host, port = competing.server_address[:2]
        competing_base = f"http://{host}:{port}"
        try:
            bases = (self.base, competing_base)
            previews = []
            sessions = []
            for base in bases:
                _, preview = self._get_from(
                    base,
                    f"/control/api/tasks/{self.queued_task.task_id}/actions",
                )
                _, session = self._get_from(
                    base,
                    "/control/api/session",
                )
                previews.append(preview)
                sessions.append(session)
            requests = (
                self._start_payload(previews[0]),
                self._revision_payload(
                    previews[1],
                    parameters={"content": "revision-race\n"},
                ),
            )
            original_reserve = EventStore.reserve_control_action
            original_claiming_reserve = (
                EventStore.reserve_control_action_with_claim
            )
            revision_reserved = threading.Event()

            def coordinated_reserve(store, *args, **kwargs):
                result = original_reserve(store, *args, **kwargs)
                if (
                    kwargs.get("action") == "revise_unstarted_task"
                    and result.get("reserved")
                ):
                    revision_reserved.set()
                return result

            def coordinated_claiming_reserve(store, *args, **kwargs):
                revision_reserved.wait(timeout=5)
                return original_claiming_reserve(store, *args, **kwargs)

            def invoke(index: int) -> tuple[int, dict]:
                base = bases[index]
                try:
                    return self._post(
                        self.queued_task.task_id,
                        requests[index],
                        base=base,
                        token=sessions[index]["token"],
                        origin=base,
                    )
                except HTTPError as exc:
                    try:
                        return (
                            exc.code,
                            json.loads(exc.read().decode("utf-8")),
                        )
                    finally:
                        exc.close()

            with patch.object(
                EventStore,
                "reserve_control_action",
                new=coordinated_reserve,
            ), patch.object(
                EventStore,
                "reserve_control_action_with_claim",
                new=coordinated_claiming_reserve,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(invoke, range(2)))
            self.assertEqual([status for status, _ in results].count(200), 1)
            self.assertEqual([status for status, _ in results].count(409), 1)
            final = self.service.require_task(self.queued_task.task_id)
            self.assertEqual(final.status, TaskStatus.QUEUED)
            self.assertEqual(final.plan_version, 1)
            events = self.service.events(self.queued_task.task_id)
            event_types = [event.event_type for event in events]
            self.assertEqual(
                event_types.count(EventType.PLAN_REVISED.value),
                1,
            )
            self.assertEqual(
                event_types.count(EventType.TASK_SUCCEEDED.value),
                0,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
                1,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_FAILED.value),
                1,
            )
        finally:
            competing.shutdown()
            competing.server_close()
            thread.join(timeout=5)

    def test_revision_completion_loss_reconciles_from_plan_revision(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        session = self._session()
        payload = self._revision_payload(preview)
        original = EventStore.record_event_and_update
        failed_once = {"value": False}

        def fail_completion(
            store,
            task_id,
            event_type,
            occurred_at,
            payload=None,
            updates=None,
            lease_token=None,
        ):
            if (
                event_type == EventType.CONTROL_ACTION_COMPLETED
                and not failed_once["value"]
            ):
                failed_once["value"] = True
                raise OSError("simulated completion persistence failure")
            return original(
                store,
                task_id,
                event_type,
                occurred_at,
                payload=payload,
                updates=updates,
                lease_token=lease_token,
            )

        with patch.object(
            EventStore,
            "record_event_and_update",
            new=fail_completion,
        ), self.assertRaises(HTTPError) as caught:
            self._post(
                self.queued_task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 500)
        caught.exception.close()

        _, status = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.queued_task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["reconciled_from"], "plan_revision")
        self.assertFalse(status["manual_review_required"])
        self.assertEqual(status["plan_version"], 1)
        replay_status, replay = self._post(
            self.queued_task.task_id,
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay["status"], "already_processed")
        event_types = [
            event.event_type
            for event in self.service.events(self.queued_task.task_id)
        ]
        self.assertEqual(event_types.count(EventType.PLAN_REVISED.value), 1)
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
            0,
        )

    def test_revision_reconciliation_finds_evidence_after_many_revisions(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        session = self._session()
        payload = self._revision_payload(preview)
        original = EventStore.record_event_and_update
        failed_once = {"value": False}

        def fail_completion(
            store,
            task_id,
            event_type,
            occurred_at,
            payload=None,
            updates=None,
            lease_token=None,
        ):
            if (
                event_type == EventType.CONTROL_ACTION_COMPLETED
                and not failed_once["value"]
            ):
                failed_once["value"] = True
                raise OSError("simulated completion persistence failure")
            return original(
                store,
                task_id,
                event_type,
                occurred_at,
                payload=payload,
                updates=updates,
                lease_token=lease_token,
            )

        with patch.object(
            EventStore,
            "record_event_and_update",
            new=fail_completion,
        ), self.assertRaises(HTTPError):
            self._post(
                self.queued_task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        for index in range(20):
            self.service.revise_plan(
                self.queued_task.task_id,
                {"goal": f"历史修订 {index}"},
                source_id=f"history-revision-{index}",
                expected_plan_version=index + 1,
                confirmed=True,
            )
        current = self.service.require_task(self.queued_task.task_id)
        self.assertEqual(current.plan_version, 21)
        self.assertEqual(len(current.plan_revisions), 21)
        _, status = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.queued_task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["reconciled_from"], "plan_revision")
        self.assertEqual(status["plan_version"], 1)

    def test_completed_receipt_keeps_historical_version_and_error(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        session = self._session()
        payload = self._revision_payload(preview)
        status, result = self._post(
            self.queued_task.task_id,
            payload,
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["plan_version"], 1)
        self.service.revise_plan(
            self.queued_task.task_id,
            {"goal": "后续修订"},
            source_id="later-revision",
            expected_plan_version=1,
            confirmed=True,
        )
        self.service.store.record_event_and_update(
            self.queued_task.task_id,
            "later_failure_fixture",
            "2026-10-22T00:00:02.000+00:00",
            updates={
                "error": TaskError("later_failure", "later failure"),
            },
        )
        _, receipt = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.queued_task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(receipt["plan_version"], 1)
        self.assertIsNone(receipt["task_error_code"])

    def test_interrupted_task_is_not_advertised_as_revisable(self) -> None:
        self.service.store.record_event_and_update(
            self.queued_task.task_id,
            EventType.TASK_INTERRUPTED,
            "2026-10-22T00:00:00.000+00:00",
            updates={
                "status": TaskStatus.INTERRUPTED,
                "phase": "test_interrupted",
            },
        )
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        self.assertEqual(
            preview["actions"],
            [{"action": "add_task_note", "requires_confirmation": False}],
        )
        self.assertIsNone(preview["unavailable_reason"])
        with self.assertRaises(PlanRevisionConflict):
            self.service.revise_plan(
                self.queued_task.task_id,
                {"goal": "控制中心专属状态限制"},
                source_id="control-interrupted-revision",
                expected_plan_version=0,
                confirmed=True,
                allowed_statuses=frozenset(
                    {
                        TaskStatus.QUEUED,
                        TaskStatus.AWAITING_CLARIFICATION,
                        TaskStatus.BLOCKED,
                    }
                ),
            )
        event_types = [
            event.event_type
            for event in self.service.events(self.queued_task.task_id)
        ]
        self.assertEqual(event_types.count(EventType.PLAN_REVISED.value), 0)

    def test_shared_service_keeps_cli_cancel_and_revision_semantics(self) -> None:
        completed = self.service.submit(
            TaskRequest(
                goal="可取消的中断任务",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "interrupted.txt",
                    "content": "evidence\n",
                },
            )
        )
        self.service.store.record_event_and_update(
            completed.task_id,
            EventType.TASK_INTERRUPTED,
            "2026-10-22T00:00:00.000+00:00",
            updates={"status": TaskStatus.INTERRUPTED},
        )
        cancelled = self.service.cancel(completed.task_id)
        self.assertEqual(cancelled.status, TaskStatus.CANCELLED)

        interrupted = self.service.enqueue(
            TaskRequest(
                goal="修订后仍需补充信息",
                operation="write_text_artifact",
                parameters={},
            )
        )
        self.service.store.record_event_and_update(
            interrupted.task_id,
            EventType.TASK_INTERRUPTED,
            "2026-10-22T00:00:01.000+00:00",
            updates={"status": TaskStatus.INTERRUPTED},
        )
        revised = self.service.revise_plan(
            interrupted.task_id,
            {"goal": "修订后仍未提供必填参数"},
            source_id="cli-interrupted-revision",
            expected_plan_version=0,
            confirmed=True,
        )
        self.assertEqual(revised.status, TaskStatus.AWAITING_CLARIFICATION)

        expired = self.service.enqueue(
            TaskRequest(
                goal="过期 lease 仍可取消",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "expired.txt",
                    "content": "not executed\n",
                },
            )
        )
        self.service.store.record_event_and_update(
            expired.task_id,
            EventType.LEASE_EXPIRED,
            "2026-10-22T00:00:02.000+00:00",
            updates={
                "lease_token": str(uuid.uuid4()),
                "lease_expires_at": "2026-10-21T00:00:00.000+00:00",
            },
        )
        cancelled_expired = self.service.cancel(expired.task_id)
        self.assertEqual(cancelled_expired.status, TaskStatus.CANCELLED)

    def test_control_action_requires_token_origin_confirmation_and_version(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.task.task_id}/actions"
        )
        session = self._session()
        payload = self._action_payload(preview)
        attempts = [
            {
                "token": None,
                "origin": self.origin,
                "body": payload,
            },
            {
                "token": session["token"],
                "origin": "http://attacker.example",
                "body": payload,
            },
            {
                "token": session["token"],
                "origin": self.origin,
                "body": {**payload, "confirmed": False},
            },
            {
                "token": session["token"],
                "origin": self.origin,
                "body": {**payload, "expected_updated_at": "stale"},
            },
        ]
        for attempt in attempts:
            with self.subTest(attempt=attempt), self.assertRaises(
                HTTPError
            ) as caught:
                self._post(
                    self.task.task_id,
                    attempt["body"],
                    token=attempt["token"],
                    origin=attempt["origin"],
                )
            self.assertIn(caught.exception.code, {403, 409})
            caught.exception.close()
        with self.assertRaises(HTTPError) as caught:
            self._post(
                self.task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
                content_type="text/plain",
            )
        self.assertEqual(caught.exception.code, 415)
        caught.exception.close()
        unchanged = self.service.require_task(self.task.task_id)
        self.assertEqual(unchanged.status, TaskStatus.AWAITING_CLARIFICATION)

    def test_requested_without_completion_fails_closed(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.task.task_id}/actions"
        )
        payload = self._action_payload(preview)
        request_digest = _request_digest(
            action=payload["action"],
            expected_updated_at=payload["expected_updated_at"],
            expected_plan_version=payload["expected_plan_version"],
            expected_fields=payload["expected_missing_fields"],
            values=payload["parameters"],
        )
        self.service.store.record_event_and_update(
            self.task.task_id,
            EventType.CONTROL_ACTION_REQUESTED,
            "2026-10-21T00:00:00.000+00:00",
            payload={
                "request_id": payload["request_id"],
                "action": "answer_clarification",
                "request_digest": request_digest,
            },
        )
        session = self._session()
        with self.assertRaises(HTTPError) as caught:
            self._post(
                self.task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(error["error"]["code"], "action_state_unknown")
        caught.exception.close()
        _, status = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(status["state"], "unknown")
        self.assertTrue(status["manual_review_required"])
        self.assertEqual(status["task_status"], "awaiting_clarification")

    def test_request_status_requires_control_session(self) -> None:
        request_id = str(uuid.uuid4())
        path = (
            f"/control/api/tasks/{self.task.task_id}/requests/{request_id}"
        )
        for token, origin in (
            (None, self.origin),
            ("invalid-token", None),
        ):
            with self.subTest(token=token, origin=origin), self.assertRaises(
                HTTPError
            ) as caught:
                self._get_from(
                    self.base,
                    path,
                    token=token,
                    origin=origin,
                )
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()
        with self.assertRaises(HTTPError) as caught:
            self._get_from(
                self.base,
                path,
                token=self._session()["token"],
            )
        self.assertEqual(caught.exception.code, 404)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(error["error"]["code"], "control_request_not_found")
        caught.exception.close()

    def test_request_status_flags_conflicting_receipt_evidence(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.task.task_id}/actions"
        )
        session = self._session()
        payload = self._action_payload(preview)
        request_id = payload["request_id"]
        digest = _request_digest(
            action=payload["action"],
            expected_updated_at=payload["expected_updated_at"],
            expected_plan_version=payload["expected_plan_version"],
            expected_fields=payload["expected_missing_fields"],
            values=payload["parameters"],
        )
        self.service.store.record_event_and_update(
            self.task.task_id,
            EventType.CONTROL_ACTION_REQUESTED,
            "2026-10-22T00:00:00.000+00:00",
            payload={
                "request_id": request_id,
                "action": "answer_clarification",
                "request_digest": digest,
            },
        )
        self.service.store.record_event_and_update(
            self.task.task_id,
            EventType.CONTROL_ACTION_FAILED,
            "2026-10-22T00:00:01.000+00:00",
            payload={
                "request_id": request_id,
                "action": "answer_clarification",
                "request_digest": "b" * 64,
                "error_code": "simulated_conflict",
            },
        )
        self.service.store.record_event_and_update(
            self.task.task_id,
            EventType.CONTROL_ACTION_COMPLETED,
            "2026-10-22T00:00:02.000+00:00",
            payload={
                "request_id": request_id,
                "action": "answer_clarification",
                "request_digest": digest,
                "task_status": "succeeded",
            },
        )
        _, status = self._get_from(
            self.base,
            f"/control/api/tasks/{self.task.task_id}/requests/{request_id}",
            token=session["token"],
        )
        self.assertEqual(status["state"], "inconsistent")
        self.assertTrue(status["manual_review_required"])
        self.assertEqual(
            status["control_error_code"],
            "action_receipt_inconsistent",
        )
        with self.assertRaises(HTTPError) as caught:
            self._post(
                self.task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(
            error["error"]["code"],
            "action_receipt_inconsistent",
        )
        caught.exception.close()

    def test_replay_and_status_share_conflicting_receipt_state(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.task.task_id}/actions"
        )
        session = self._session()
        payload = self._action_payload(preview)
        digest = _request_digest(
            action=payload["action"],
            expected_updated_at=payload["expected_updated_at"],
            expected_plan_version=payload["expected_plan_version"],
            expected_fields=payload["expected_missing_fields"],
            values=payload["parameters"],
        )
        for offset, event_type in enumerate(
            (
                EventType.CONTROL_ACTION_REQUESTED,
                EventType.CONTROL_ACTION_FAILED,
                EventType.CONTROL_ACTION_COMPLETED,
            )
        ):
            self.service.store.record_event_and_update(
                self.task.task_id,
                event_type,
                f"2026-10-22T00:00:0{offset}.000+00:00",
                payload={
                    "request_id": payload["request_id"],
                    "action": payload["action"],
                    "request_digest": digest,
                    "error_code": "simulated_failure",
                    "task_status": "awaiting_clarification",
                },
            )
        with self.assertRaises(HTTPError) as caught:
            self._post(
                self.task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(
            error["error"]["code"],
            "action_receipt_inconsistent",
        )
        caught.exception.close()
        _, status = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(status["state"], "inconsistent")
        self.assertTrue(status["manual_review_required"])
        self.assertFalse(
            (self.root / "artifacts" / self.task.task_id).exists()
        )

    def test_lost_completion_is_reported_for_manual_review(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.task.task_id}/actions"
        )
        session = self._session()
        payload = self._action_payload(preview)
        original = EventStore.record_event_and_update
        failed_once = {"value": False}

        def fail_completion(
            store,
            task_id,
            event_type,
            occurred_at,
            payload=None,
            updates=None,
            lease_token=None,
        ):
            if (
                event_type == EventType.CONTROL_ACTION_COMPLETED
                and not failed_once["value"]
            ):
                failed_once["value"] = True
                raise OSError("simulated completion persistence failure")
            return original(
                store,
                task_id,
                event_type,
                occurred_at,
                payload=payload,
                updates=updates,
                lease_token=lease_token,
            )

        with patch.object(
            EventStore,
            "record_event_and_update",
            new=fail_completion,
        ), self.assertRaises(HTTPError) as caught:
            self._post(
                self.task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 500)
        caught.exception.close()

        _, status = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.task.task_id}/requests/"
                f"{payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(status["state"], "unknown")
        self.assertTrue(status["manual_review_required"])
        self.assertEqual(status["task_status"], "succeeded")
        events = self.service.events(self.task.task_id)
        event_types = [event.event_type for event in events]
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
            0,
        )
        self.assertEqual(
            event_types.count(EventType.TOOL_STARTED.value),
            1,
        )

    def test_competing_control_instances_only_one_records_completion(
        self,
    ) -> None:
        competing = create_server(
            self.root,
            port=0,
            control_enabled=True,
        )
        thread = threading.Thread(
            target=competing.serve_forever,
            daemon=True,
        )
        thread.start()
        host, port = competing.server_address[:2]
        competing_base = f"http://{host}:{port}"
        try:
            previews = []
            sessions = []
            for base in (self.base, competing_base):
                _, preview = self._get_from(
                    base,
                    f"/control/api/tasks/{self.task.task_id}/actions",
                )
                _, session = self._get_from(
                    base,
                    "/control/api/session",
                )
                previews.append(preview)
                sessions.append(session)

            payloads = [
                {
                    **self._action_payload(previews[index]),
                    "parameters": {
                        "artifact_path": "competing.txt",
                        "content": f"competing-{index}\n",
                    },
                }
                for index in range(2)
            ]
            original_require_task = TaskService.require_task
            barrier = threading.Barrier(2)
            original_record_event = EventStore.record_event_and_update
            request_barrier = threading.Barrier(2)
            record_state = threading.local()

            def synchronized_require_task(
                service: TaskService,
                task_id: str,
            ):
                task = original_require_task(service, task_id)
                barrier.wait(timeout=5)
                return task

            def synchronized_record_event(
                store,
                task_id,
                event_type,
                occurred_at,
                payload=None,
                updates=None,
                lease_token=None,
            ):
                stored = original_record_event(
                    store,
                    task_id,
                    event_type,
                    occurred_at,
                    payload=payload,
                    updates=updates,
                    lease_token=lease_token,
                )
                if (
                    getattr(event_type, "value", event_type)
                    == EventType.CONTROL_ACTION_REQUESTED.value
                    and not getattr(record_state, "entered", False)
                ):
                    record_state.entered = True
                    request_barrier.wait(timeout=5)
                return stored

            def invoke(index: int) -> tuple[int, dict]:
                base = (self.base, competing_base)[index]
                try:
                    return self._post(
                        self.task.task_id,
                        payloads[index],
                        base=base,
                        token=sessions[index]["token"],
                        origin=base,
                    )
                except HTTPError as exc:
                    try:
                        return (
                            exc.code,
                            json.loads(exc.read().decode("utf-8")),
                        )
                    finally:
                        exc.close()

            with patch.object(
                TaskService,
                "require_task",
                new=synchronized_require_task,
            ), patch.object(
                EventStore,
                "record_event_and_update",
                new=synchronized_record_event,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(invoke, range(2)))

            self.assertEqual(
                sorted(result[1].get("status") for result in results if result[0] == 200),
                ["completed"],
            )
            conflicts = [
                result[1]["error"]["code"]
                for result in results
                if result[0] == 409
            ]
            self.assertEqual(conflicts, ["task_claim_not_acquired"])
            conflict_index = next(
                index
                for index, result in enumerate(results)
                if result[0] == 409
            )
            conflict_base = (self.base, competing_base)[conflict_index]
            conflict_code, conflict_status = self._get_from(
                conflict_base,
                (
                    f"/control/api/tasks/{self.task.task_id}/requests/"
                    f"{payloads[conflict_index]['request_id']}"
                ),
                token=sessions[conflict_index]["token"],
                origin=conflict_base,
            )
            self.assertEqual(conflict_code, 200)
            self.assertEqual(conflict_status["state"], "failed")
            self.assertEqual(
                conflict_status["control_error_code"],
                "task_claim_not_acquired",
            )

            events = self.service.events(self.task.task_id)
            event_types = [event.event_type for event in events]
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_REQUESTED.value),
                2,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
                1,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_FAILED.value),
                1,
            )
            self.assertEqual(
                event_types.count(EventType.TOOL_STARTED.value),
                1,
            )
            updated = self.service.require_task(self.task.task_id)
            self.assertEqual(updated.status, TaskStatus.SUCCEEDED)
        finally:
            competing.shutdown()
            competing.server_close()
            thread.join(timeout=5)

    def test_same_request_id_concurrent_duplicate_has_one_effect(self) -> None:
        competing = create_server(
            self.root,
            port=0,
            control_enabled=True,
        )
        thread = threading.Thread(
            target=competing.serve_forever,
            daemon=True,
        )
        thread.start()
        host, port = competing.server_address[:2]
        competing_base = f"http://{host}:{port}"
        try:
            bases = (self.base, competing_base)
            previews = []
            sessions = []
            for base in bases:
                _, preview = self._get_from(
                    base,
                    f"/control/api/tasks/{self.task.task_id}/actions",
                )
                _, session = self._get_from(
                    base,
                    "/control/api/session",
                )
                previews.append(preview)
                sessions.append(session)
            payload = self._action_payload(previews[0])
            requests = (payload, json.loads(json.dumps(payload)))
            original_reserve = EventStore.reserve_control_action
            barrier = threading.Barrier(2)

            def synchronized_reserve(store, *args, **kwargs):
                barrier.wait(timeout=5)
                return original_reserve(store, *args, **kwargs)

            def invoke(index: int) -> tuple[int, dict]:
                base = bases[index]
                try:
                    return self._post(
                        self.task.task_id,
                        requests[index],
                        base=base,
                        token=sessions[index]["token"],
                        origin=base,
                    )
                except HTTPError as exc:
                    try:
                        return (
                            exc.code,
                            json.loads(exc.read().decode("utf-8")),
                        )
                    finally:
                        exc.close()

            with patch.object(
                EventStore,
                "reserve_control_action",
                new=synchronized_reserve,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(invoke, range(2)))
            outcomes = [
                body.get("status") or body.get("error", {}).get("code")
                for _, body in results
            ]
            self.assertEqual(outcomes.count("completed"), 1)
            self.assertEqual(len(outcomes), 2)
            self.assertTrue(
                set(outcomes) - {"completed"}
                <= {
                    "already_processed",
                    "action_state_unknown",
                    "action_in_progress",
                }
            )
            events = self.service.events(self.task.task_id)
            event_types = [event.event_type for event in events]
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_REQUESTED.value),
                1,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
                1,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_FAILED.value),
                0,
            )
            self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 1)
        finally:
            competing.shutdown()
            competing.server_close()
            thread.join(timeout=5)

    def test_same_request_id_concurrent_conflict_preserves_exact_content(
        self,
    ) -> None:
        competing = create_server(
            self.root,
            port=0,
            control_enabled=True,
        )
        thread = threading.Thread(
            target=competing.serve_forever,
            daemon=True,
        )
        thread.start()
        host, port = competing.server_address[:2]
        competing_base = f"http://{host}:{port}"
        try:
            bases = (self.base, competing_base)
            previews = []
            sessions = []
            for base in bases:
                _, preview = self._get_from(
                    base,
                    f"/control/api/tasks/{self.task.task_id}/actions",
                )
                _, session = self._get_from(
                    base,
                    "/control/api/session",
                )
                previews.append(preview)
                sessions.append(session)
            request_id = str(uuid.uuid4())
            requests = [
                {
                    **self._action_payload(previews[index]),
                    "request_id": request_id,
                    "parameters": {
                        "artifact_path": "same-id-conflict.txt",
                        "content": f"variant-{index}\n",
                    },
                }
                for index in range(2)
            ]
            original_reserve = EventStore.reserve_control_action
            barrier = threading.Barrier(2)

            def synchronized_reserve(store, *args, **kwargs):
                barrier.wait(timeout=5)
                return original_reserve(store, *args, **kwargs)

            def invoke(index: int) -> tuple[int, dict]:
                base = bases[index]
                try:
                    return self._post(
                        self.task.task_id,
                        requests[index],
                        base=base,
                        token=sessions[index]["token"],
                        origin=base,
                    )
                except HTTPError as exc:
                    try:
                        return (
                            exc.code,
                            json.loads(exc.read().decode("utf-8")),
                        )
                    finally:
                        exc.close()

            with patch.object(
                EventStore,
                "reserve_control_action",
                new=synchronized_reserve,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(invoke, range(2)))
            winner = next(
                index for index, (status, _) in enumerate(results)
                if status == 200
            )
            loser = 1 - winner
            self.assertEqual(results[winner][1]["status"], "completed")
            self.assertEqual(results[loser][0], 409)
            self.assertEqual(
                results[loser][1]["error"]["code"],
                "action_request_conflict",
            )
            with self.assertRaises(HTTPError) as winner_payload:
                self._post(
                    self.task.task_id,
                    requests[loser],
                    base=bases[loser],
                    token=sessions[loser]["token"],
                    origin=bases[loser],
                )
            self.assertEqual(winner_payload.exception.code, 409)
            winner_payload.exception.close()
            replay_status, replay = self._post(
                self.task.task_id,
                requests[winner],
                base=bases[winner],
                token=sessions[winner]["token"],
                origin=bases[winner],
            )
            self.assertEqual(replay_status, 200)
            self.assertEqual(replay["status"], "already_processed")
            events = self.service.events(self.task.task_id)
            event_types = [event.event_type for event in events]
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_REQUESTED.value),
                1,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
                1,
            )
            self.assertEqual(
                event_types.count(EventType.CONTROL_ACTION_FAILED.value),
                0,
            )
            self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 1)
        finally:
            competing.shutdown()
            competing.server_close()
            thread.join(timeout=5)

    def test_active_external_claim_fails_clarification_closed(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.task.task_id}/actions"
        )
        _, queued_preview_before_claim = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        revision_payload = self._revision_payload(queued_preview_before_claim)
        note_payload = {
            "action": "add_task_note",
            "request_id": str(uuid.uuid4()),
            "confirmed": True,
            "expected_updated_at":
                queued_preview_before_claim["task_updated_at"],
            "expected_plan_version":
                queued_preview_before_claim["plan_version"],
            "message": "must not be written while claimed",
        }
        session = self._session()
        payload = self._action_payload(preview)
        holding = TaskService(self.root)
        claimed = holding._claim(
            self.task.task_id,
            allowed=TaskService.RESUMABLE_STATUSES,
        )
        self.assertTrue(claimed.acquired)
        _, claimed_preview = self._get(
            f"/control/api/tasks/{self.task.task_id}/actions"
        )
        self.assertEqual(claimed_preview["actions"], [])
        self.assertEqual(
            claimed_preview["unavailable_reason"],
            "task_claim_active",
        )
        queued_claimed = holding._claim(
            self.queued_task.task_id,
            allowed=TaskService.RUNNABLE_STATUSES,
        )
        self.assertTrue(queued_claimed.acquired)
        _, queued_preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        self.assertEqual(queued_preview["actions"], [])
        self.assertEqual(queued_preview["lease_state"], "active")
        self.assertEqual(
            queued_preview["unavailable_reason"],
            "task_claim_active",
        )
        with self.assertRaises(HTTPError) as caught:
            self._post(
                self.task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(error["error"]["code"], "task_claim_not_acquired")
        caught.exception.close()
        for blocked_payload in (revision_payload, note_payload):
            with self.assertRaises(HTTPError) as blocked:
                self._post(
                    self.queued_task.task_id,
                    blocked_payload,
                    token=session["token"],
                    origin=self.origin,
                )
            self.assertEqual(blocked.exception.code, 409)
            blocked_error = json.loads(
                blocked.exception.read().decode("utf-8")
            )
            self.assertEqual(
                blocked_error["error"]["code"],
                "task_claim_not_acquired",
            )
            blocked.exception.close()
        event_types = [
            event.event_type
            for event in self.service.events(self.task.task_id)
        ]
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_REQUESTED.value),
            1,
        )
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
            0,
        )
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_FAILED.value),
            1,
        )
        self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 0)
        queued_events = self.service.events(self.queued_task.task_id)
        self.assertEqual(
            sum(
                event.event_type == EventType.CONTROL_ACTION_COMPLETED.value
                for event in queued_events
            ),
            0,
        )
        self.assertEqual(
            sum(
                event.event_type == EventType.TOOL_STARTED.value
                for event in queued_events
            ),
            0,
        )

    def test_expired_claim_preview_and_submit_require_recovery(self) -> None:
        lease_token = self._set_expired_claim(self.queued_task.task_id)
        before = self.service.require_task(self.queued_task.task_id)
        before_task = before.to_dict()
        before_events = [
            (event.event_type, event.payload, event.occurred_at)
            for event in self.service.events(self.queued_task.task_id)
        ]
        task_artifacts = (
            self.root / "artifacts" / self.queued_task.task_id
        )
        before_artifacts = (
            sorted(path.name for path in task_artifacts.iterdir())
            if task_artifacts.is_dir()
            else []
        )
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        self.assertEqual(preview["actions"], [])
        self.assertEqual(preview["lease_state"], "expired")
        self.assertEqual(
            preview["unavailable_reason"],
            "task_claim_recovery_required",
        )
        recovered = self.service.require_task(self.queued_task.task_id)
        self.assertEqual(recovered.to_dict(), before_task)
        self.assertEqual(recovered.lease_token, before.lease_token)
        self.assertEqual(recovered.phase, before.phase)
        self.assertEqual(recovered.status, TaskStatus.QUEUED)
        self.assertEqual(
            [
                (event.event_type, event.payload, event.occurred_at)
                for event in self.service.events(self.queued_task.task_id)
            ],
            before_events,
        )
        after_artifacts = (
            sorted(path.name for path in task_artifacts.iterdir())
            if task_artifacts.is_dir()
            else []
        )
        self.assertEqual(after_artifacts, before_artifacts)

        _, detail = self._get(
            f"/api/tasks/{self.queued_task.task_id}"
        )
        request_payload = {
            "action": "revise_unstarted_task",
            "request_id": str(uuid.uuid4()),
            "confirmed": True,
            "expected_updated_at": preview["task_updated_at"],
            "expected_plan_version": preview["plan_version"],
            "expected_request_digest": detail["task"]["request_digest"],
            "changes": {
                "goal": "不得绕过过期 lease",
                "parameters": {"content": "rejected\n"},
            },
        }
        session = self._session()
        with self.assertRaises(HTTPError) as caught:
            self._post(
                self.queued_task.task_id,
                request_payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(
            error["error"]["code"],
            "task_claim_recovery_required",
        )
        caught.exception.close()

        unchanged = self.service.require_task(self.queued_task.task_id)
        self.assertEqual(unchanged.lease_token, lease_token)
        self.assertEqual(unchanged.status, TaskStatus.QUEUED)
        self.assertEqual(unchanged.claim_generation, before.claim_generation)
        events = self.service.events(self.queued_task.task_id)
        self.assertEqual(
            sum(
                event.event_type == EventType.CONTROL_ACTION_COMPLETED.value
                for event in events
            ),
            0,
        )
        self.assertEqual(
            sum(
                event.event_type == EventType.TOOL_STARTED.value
                for event in events
            ),
            0,
        )
        after_artifacts = (
            sorted(path.name for path in task_artifacts.iterdir())
            if task_artifacts.is_dir()
            else []
        )
        self.assertEqual(after_artifacts, before_artifacts)
        _, receipt = self._get_from(
            self.base,
            (
                f"/control/api/tasks/{self.queued_task.task_id}/requests/"
                f"{request_payload['request_id']}"
            ),
            token=session["token"],
        )
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(
            receipt["control_error_code"],
            "task_claim_recovery_required",
        )

    def test_released_claim_restores_only_eligible_operations(self) -> None:
        service = TaskService(self.root)
        claimed = service._claim(
            self.queued_task.task_id,
            allowed=TaskService.RUNNABLE_STATUSES,
        )
        self.assertTrue(claimed.acquired)
        lease_token = claimed.task.lease_token
        self.assertIsNotNone(lease_token)
        service.store.release_claim(
            self.queued_task.task_id,
            lease_token=lease_token,
            now=service.clock(),
        )

        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        actions = {action["action"] for action in preview["actions"]}
        self.assertEqual(preview["lease_state"], "none")
        self.assertFalse(preview["claim_held"])
        self.assertIn("revise_unstarted_task", actions)
        self.assertIn("add_task_note", actions)
        self.assertNotIn("start_queued_task", actions)
        self.assertNotIn("cancel_queued_task", actions)

        session = self._session()
        status, result = self._post(
            self.queued_task.task_id,
            self._revision_payload(preview),
            token=session["token"],
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "completed")
        updated = self.service.require_task(self.queued_task.task_id)
        self.assertEqual(updated.status, TaskStatus.QUEUED)
        self.assertEqual(updated.plan_version, 1)
        self.assertEqual(
            sum(
                event.event_type == EventType.TOOL_STARTED.value
                for event in self.service.events(self.queued_task.task_id)
            ),
            0,
        )

    def test_claim_race_atomic_reservation_rejects_expired_lease(self) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        payload = self._start_payload(preview)
        session = self._session()
        original = TaskControlService._validate_action_eligibility
        injected = False

        def validate_then_expire(
            control: TaskControlService,
            task,
            **kwargs,
        ) -> None:
            nonlocal injected
            result = original(control, task, **kwargs)
            if not injected:
                self._set_expired_claim(task.task_id)
                injected = True
            return result

        with patch.object(
            TaskControlService,
            "_validate_action_eligibility",
            new=validate_then_expire,
        ), self.assertRaises(HTTPError) as caught:
            self._post(
                self.queued_task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertTrue(injected)
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(
            error["error"]["code"],
            "task_claim_recovery_required",
        )
        caught.exception.close()
        task = self.service.require_task(self.queued_task.task_id)
        self.assertEqual(task.status, TaskStatus.QUEUED)
        self.assertIsNotNone(task.lease_token)
        event_types = [
            event.event_type
            for event in self.service.events(self.queued_task.task_id)
        ]
        self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 0)
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
            0,
        )

    def test_note_race_after_reservation_cannot_mutate_claimed_task(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        payload = {
            "action": "add_task_note",
            "request_id": str(uuid.uuid4()),
            "confirmed": True,
            "expected_updated_at": preview["task_updated_at"],
            "expected_plan_version": preview["plan_version"],
            "message": "must not race with an execution claim",
        }
        session = self._session()
        original = EventStore.record_event_and_update
        injected = False

        def claim_before_note(store, task_id, event_type, *args, **kwargs):
            nonlocal injected
            if event_type == EventType.TASK_NOTE_ADDED and not injected:
                holding = TaskService(self.root, recover_leases=False)
                claimed = holding._claim(
                    task_id,
                    allowed=TaskService.RESUMABLE_STATUSES,
                )
                self.assertTrue(claimed.acquired)
                injected = True
            return original(store, task_id, event_type, *args, **kwargs)

        with patch.object(
            EventStore,
            "record_event_and_update",
            new=claim_before_note,
        ), self.assertRaises(HTTPError) as caught:
            self._post(
                self.queued_task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertTrue(injected)
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(
            error["error"]["code"],
            "task_claim_not_acquired",
        )
        caught.exception.close()
        task = self.service.require_task(self.queued_task.task_id)
        self.assertIsNotNone(task.lease_token)
        event_types = [
            event.event_type
            for event in self.service.events(self.queued_task.task_id)
        ]
        self.assertEqual(event_types.count(EventType.TASK_NOTE_ADDED.value), 0)
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
            0,
        )
        self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 0)

    def test_revision_race_after_reservation_cannot_clear_expired_lease(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        payload = self._revision_payload(preview)
        session = self._session()
        original = EventStore.apply_plan_revision
        injected = False

        def expire_before_revision(store, task_id, **kwargs):
            nonlocal injected
            if not injected:
                self._set_expired_claim(task_id)
                injected = True
            return original(store, task_id, **kwargs)

        with patch.object(
            EventStore,
            "apply_plan_revision",
            new=expire_before_revision,
        ), self.assertRaises(HTTPError) as caught:
            self._post(
                self.queued_task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertTrue(injected)
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(
            error["error"]["code"],
            "task_claim_recovery_required",
        )
        caught.exception.close()
        task = self.service.require_task(self.queued_task.task_id)
        self.assertEqual(task.status, TaskStatus.QUEUED)
        self.assertEqual(task.plan_version, 0)
        self.assertIsNotNone(task.lease_token)
        event_types = [
            event.event_type
            for event in self.service.events(self.queued_task.task_id)
        ]
        self.assertEqual(event_types.count(EventType.PLAN_REVISED.value), 0)
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
            0,
        )
        self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 0)

    def test_cancel_race_after_reservation_reports_lease_recovery(
        self,
    ) -> None:
        _, preview = self._get(
            f"/control/api/tasks/{self.queued_task.task_id}/actions"
        )
        payload = self._cancel_payload(preview)
        session = self._session()
        original = EventStore.cancel_task
        injected = False

        def expire_before_cancel(store, task_id, **kwargs):
            nonlocal injected
            if not injected:
                self._set_expired_claim(task_id)
                injected = True
            return original(store, task_id, **kwargs)

        with patch.object(
            EventStore,
            "cancel_task",
            new=expire_before_cancel,
        ), self.assertRaises(HTTPError) as caught:
            self._post(
                self.queued_task.task_id,
                payload,
                token=session["token"],
                origin=self.origin,
            )
        self.assertTrue(injected)
        self.assertEqual(caught.exception.code, 409)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(
            error["error"]["code"],
            "task_claim_recovery_required",
        )
        caught.exception.close()
        task = self.service.require_task(self.queued_task.task_id)
        self.assertEqual(task.status, TaskStatus.QUEUED)
        self.assertIsNotNone(task.lease_token)
        event_types = [
            event.event_type
            for event in self.service.events(self.queued_task.task_id)
        ]
        self.assertEqual(
            event_types.count(EventType.TASK_CANCELLED.value),
            0,
        )
        self.assertEqual(
            event_types.count(EventType.CONTROL_ACTION_COMPLETED.value),
            0,
        )
        self.assertEqual(event_types.count(EventType.TOOL_STARTED.value), 0)

    def test_naive_lease_timestamp_has_consistent_preview_and_submit(
        self,
    ) -> None:
        for expiry, lease_state, reason, error_code in (
            (
                "2099-01-01T00:00:00",
                "active",
                "task_claim_active",
                "task_claim_not_acquired",
            ),
            (
                "2000-01-01T00:00:00",
                "expired",
                "task_claim_recovery_required",
                "task_claim_recovery_required",
            ),
            (
                None,
                "active",
                "task_claim_active",
                "task_claim_not_acquired",
            ),
        ):
            with self.subTest(expiry=expiry):
                task = self.service.enqueue(
                    TaskRequest(
                        goal="naive lease timestamp",
                        operation="write_text_artifact",
                        parameters={
                            "artifact_path": "naive.txt",
                            "content": "not executed\n",
                        },
                    )
                )
                self._set_claim_lease(task.task_id, expires_at=expiry)
                _, preview = self._get(
                    f"/control/api/tasks/{task.task_id}/actions"
                )
                self.assertEqual(preview["lease_state"], lease_state)
                self.assertEqual(preview["actions"], [])
                self.assertEqual(preview["unavailable_reason"], reason)
                payload = {
                    "action": "add_task_note",
                    "request_id": str(uuid.uuid4()),
                    "confirmed": True,
                    "expected_updated_at": preview["task_updated_at"],
                    "expected_plan_version": preview["plan_version"],
                    "message": "must not be written",
                }
                session = self._session()
                with self.assertRaises(HTTPError) as caught:
                    self._post(
                        task.task_id,
                        payload,
                        token=session["token"],
                        origin=self.origin,
                    )
                self.assertEqual(caught.exception.code, 409)
                error = json.loads(
                    caught.exception.read().decode("utf-8")
                )
                self.assertEqual(
                    error["error"]["code"],
                    error_code,
                )
                caught.exception.close()

    def test_missing_expiry_lease_atomic_paths_use_active_conflict(
        self,
    ) -> None:
        task = self.service.enqueue(
            TaskRequest(
                goal="missing lease expiry",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "missing-expiry.txt",
                    "content": "not executed\n",
                },
            )
        )
        self._set_claim_lease(task.task_id, expires_at=None)
        current = self.service.require_task(task.task_id)
        preview = TaskControlService(self.root).action_preview(task.task_id)
        self.assertEqual(preview["lease_state"], "active")
        self.assertTrue(preview["claim_held"])
        self.assertEqual(preview["actions"], [])
        self.assertEqual(
            preview["unavailable_reason"],
            "task_claim_active",
        )
        with self.assertRaises(PlanRevisionConflict) as note_error:
            self.service.store.record_event_and_update(
                task.task_id,
                EventType.TASK_NOTE_ADDED,
                "2026-10-22T00:00:03.000+00:00",
                payload={"message": "must not be written"},
                require_no_lease=True,
                expected_updated_at=current.updated_at,
                expected_plan_version=0,
            )
        self.assertIn(
            "active execution lease",
            str(note_error.exception),
        )

        with self.assertRaises(PlanRevisionConflict) as revision_error:
            self.service.store.apply_plan_revision(
                task.task_id,
                expected_plan_version=0,
                updated_task=current,
                event=Event.new(
                    task.task_id,
                    EventType.PLAN_REVISED,
                    "2026-10-22T00:00:03.000+00:00",
                    {},
                ),
            )
        self.assertIn(
            "active execution lease",
            str(revision_error.exception),
        )

    def test_execution_claim_rejects_missing_expiry_lease(self) -> None:
        task = self.service.enqueue(
            TaskRequest(
                goal="execution claim missing expiry",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "claim-missing-expiry.txt",
                    "content": "not executed\n",
                },
            )
        )
        self._set_claim_lease(task.task_id, expires_at=None)
        before = self.service.require_task(task.task_id)
        result = self.service._claim(
            task.task_id,
            allowed=TaskService.RUNNABLE_STATUSES,
        )
        self.assertFalse(result.acquired)
        self.assertEqual(result.reason, "active_lease")
        after = self.service.require_task(task.task_id)
        self.assertEqual(after.lease_token, before.lease_token)
        self.assertEqual(
            after.claim_generation,
            before.claim_generation,
        )
        self.assertEqual(after.status, TaskStatus.QUEUED)
        self.assertEqual(
            sum(
                event.event_type == EventType.TASK_CLAIMED.value
                for event in self.service.events(task.task_id)
            ),
            1,
        )
        self.assertEqual(
            sum(
                event.event_type == EventType.TOOL_STARTED.value
                for event in self.service.events(task.task_id)
            ),
            0,
        )

    def test_read_only_api_remains_405_and_workbench_has_no_control_session(
        self,
    ) -> None:
        request = Request(
            f"{self.base}/api/tasks",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 405)
        caught.exception.close()

        read_only = create_server(self.root, port=0)
        thread = threading.Thread(target=read_only.serve_forever, daemon=True)
        thread.start()
        host, port = read_only.server_address[:2]
        try:
            with self.assertRaises(HTTPError) as read_only_error:
                urlopen(
                    f"http://{host}:{port}/control/api/session",
                    timeout=5,
                )
            self.assertEqual(read_only_error.exception.code, 404)
            read_only_error.exception.close()
            with self.assertRaises(HTTPError) as request_status_error:
                urlopen(
                    (
                        f"http://{host}:{port}/control/api/tasks/"
                        f"{self.task.task_id}/requests/{uuid.uuid4()}"
                    ),
                    timeout=5,
                )
            self.assertEqual(request_status_error.exception.code, 404)
            request_status_error.exception.close()
        finally:
            read_only.shutdown()
            read_only.server_close()
            thread.join(timeout=5)


class EmptyControlRuntimeTests(unittest.TestCase):
    def test_control_server_initializes_empty_store_before_first_visit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = create_server(root, port=0, control_enabled=True)
            thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            thread.start()
            host, port = server.server_address[:2]
            try:
                self.assertTrue(
                    (root / "data" / "zevorinth.sqlite3").is_file()
                )
                with urlopen(
                    f"http://{host}:{port}/api/tasks",
                    timeout=5,
                ) as response:
                    self.assertEqual(response.status, 200)
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["tasks"], [])
                self.assertEqual(sum(payload["counts"].values()), 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
