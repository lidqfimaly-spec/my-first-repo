"""Loopback-only HTTP server for the read-only Zevorinth workbench."""

from __future__ import annotations

import json
import hmac
import logging
import re
import secrets
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .conversation_service import ConversationService
from .models import TaskStatus
from .task_executor import TaskExecutor
from .memory_service import (
    DEFAULT_RECALL_LIMIT,
    MAX_RECALL_LIMIT,
    MAX_QUERY_LENGTH,
    MemoryError,
    MemoryService,
)
from .workbench import (
    DEFAULT_TASK_LIMIT,
    MAX_ATTENTION_ITEMS,
    MAX_TASK_LIMIT,
    ReadOnlyWorkbench,
    WorkbenchError,
)
from .workbench_control import (
    CONTROL_CONTRACT_VERSION,
    NATURAL_GOAL_PREVIEW_VERSION,
    ControlActionError,
    SUPPORTED_ACTIONS,
    TaskControlService,
)
from .workspaces import (
    WorkspaceError,
    create_workspace,
    list_workspaces,
)


STATIC_ROOT = Path(__file__).with_name("workbench_static")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
DEFAULT_PORT = 8765
MAX_REQUEST_TARGET = 2048
MAX_CONTROL_BODY_BYTES = 16 * 1024
SSE_POLL_INTERVAL = 1.5
SSE_MAX_SECONDS = 300
CONVERSATION_ID_PATTERN = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
MAX_CONVERSATION_MESSAGES = 200
DEFAULT_CONVERSATION_MESSAGE_LIMIT = 50
MAX_CONVERSATION_CONTENT_BYTES = 16 * 1024
_REQUEST_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TASK_PATH = re.compile(
    r"^/api/tasks/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})(/artifact)?$"
)
_EVENTS_STREAM_PATH = re.compile(
    r"^/api/tasks/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})/events/stream$"
)
_CONVERSATION_MESSAGES_PATH = re.compile(
    rf"^/control/api/conversations/({CONVERSATION_ID_PATTERN})/messages$"
)
_CONVERSATION_DETAIL_PATH = re.compile(
    rf"^/control/api/conversations/({CONVERSATION_ID_PATTERN})$"
)
_CONTROL_ACTIONS_PATH = re.compile(
    r"^/control/api/tasks/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})/actions$"
)
_CONTROL_TASKS_PATH = re.compile(r"^/control/api/tasks$")
_CONTROL_WORKSPACES_PATH = re.compile(r"^/control/api/workspaces$")
_CONTROL_GOAL_PREVIEW_PATH = re.compile(r"^/control/api/preview$")
_CONTROL_CONVERSATIONS_PATH = re.compile(
    r"^/control/api/conversations$"
)
_CONTROL_MEMORIES_PATH = re.compile(
    r"^/control/api/memories$"
)
_CONTROL_MEMORY_DETAIL_PATH = re.compile(
    rf"^/control/api/memories/({CONVERSATION_ID_PATTERN})$"
)
_CONTROL_MEMORY_CORRECT_PATH = re.compile(
    rf"^/control/api/memories/({CONVERSATION_ID_PATTERN})/correct$"
)
_CONTROL_TASK_RETRY_PATH = re.compile(
    r"^/control/api/tasks/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})/retry$"
)
_CONTROL_TASK_DISPATCH_PATH = re.compile(
    r"^/control/api/tasks/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})/dispatch$"
)
_CONTROL_TASK_CANCEL_REQUEST_PATH = re.compile(
    r"^/control/api/tasks/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})/cancel-request$"
)
_TASK_PROGRESS_PATH = re.compile(
    r"^/api/tasks/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})/progress$"
)
_CONTROL_REQUEST_PATH = re.compile(
    r"^/control/api/tasks/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})/requests/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/i18n.zh-CN.json": (
        "i18n.zh-CN.json",
        "application/json; charset=utf-8",
    ),
}
LOGGER = logging.getLogger(__name__)


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        workbench: ReadOnlyWorkbench,
        *,
        control_enabled: bool = False,
    ) -> None:
        super().__init__(server_address, WorkbenchRequestHandler)
        self.workbench = workbench
        self.control_enabled = control_enabled
        self.control_token = (
            secrets.token_urlsafe(32) if control_enabled else None
        )
        self.control_service = (
            TaskControlService(workbench.project_root)
            if control_enabled
            else None
        )
        # Background driver for queued tasks. Only the *driver* moves off the
        # request thread; the engine keeps every transition and lease.
        self.task_executor = (
            TaskExecutor(workbench.project_root) if control_enabled else None
        )
        if self.task_executor is not None:
            self.task_executor.start()
        self.conversation_service = (
            ConversationService(workbench.project_root)
            if control_enabled
            else None
        )
        self.memory_service = (
            MemoryService(workbench.project_root)
            if control_enabled
            else None
        )
        host, port = self.server_address[:2]
        self.control_origins = {
            f"http://{host}:{port}",
            f"http://localhost:{port}",
        }

    def server_close(self) -> None:
        executor = getattr(self, "task_executor", None)
        if executor is not None:
            executor.shutdown()
        super().server_close()


class WorkbenchRequestHandler(BaseHTTPRequestHandler):
    server: WorkbenchServer

    def do_GET(self) -> None:
        self._handle(send_body=True)

    def do_HEAD(self) -> None:
        self._handle(send_body=False)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if (
            self.server.control_enabled
            and _CONTROL_GOAL_PREVIEW_PATH.fullmatch(path)
        ):
            self._handle_control_goal_preview()
            return
        if self.server.control_enabled and _CONTROL_TASKS_PATH.fullmatch(path):
            self._handle_control_task_create()
            return
        if (
            self.server.control_enabled
            and _CONTROL_WORKSPACES_PATH.fullmatch(path)
        ):
            self._handle_workspace_create()
            return
        if (
            self.server.control_enabled
            and _CONTROL_ACTIONS_PATH.fullmatch(path)
        ):
            self._handle_control_post()
            return
        if (
            self.server.control_enabled
            and _CONTROL_CONVERSATIONS_PATH.fullmatch(path)
        ):
            self._handle_conversation_create()
            return
        if (
            self.server.control_enabled
            and _CONVERSATION_MESSAGES_PATH.fullmatch(path)
        ):
            self._handle_conversation_message()
            return
        if (
            self.server.control_enabled
            and _CONTROL_MEMORIES_PATH.fullmatch(path)
        ):
            self._handle_memory_create()
            return
        if (
            self.server.control_enabled
            and _CONTROL_MEMORY_CORRECT_PATH.fullmatch(path)
        ):
            self._handle_memory_correct()
            return
        if (
            self.server.control_enabled
            and _CONTROL_TASK_RETRY_PATH.fullmatch(path)
        ):
            self._handle_task_retry()
            return
        if (
            self.server.control_enabled
            and _CONTROL_TASK_DISPATCH_PATH.fullmatch(path)
        ):
            self._handle_task_dispatch()
            return
        if (
            self.server.control_enabled
            and _CONTROL_TASK_CANCEL_REQUEST_PATH.fullmatch(path)
        ):
            self._handle_task_cancel_request()
            return
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        if (
            self.server.control_enabled
            and _CONTROL_MEMORY_DETAIL_PATH.fullmatch(path)
        ):
            self._handle_memory_forget()
            return
        self._method_not_allowed()

    def _handle_control_goal_preview(self) -> None:
        try:
            self._validate_control_request()
            _reject_unknown_query(
                parse_qs(urlsplit(self.path).query, strict_parsing=False),
                set(),
            )
            if self.server.control_service is None:
                raise ControlActionError(
                    "control_disabled",
                    "control actions are not enabled",
                    status=404,
                )
            result = self.server.control_service.preview_goal(
                self._read_json_body()
            )
            self._send_json(HTTPStatus.OK, result, send_body=True)
        except ControlActionError as exc:
            self._send_json(
                exc.status,
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                    }
                },
                send_body=True,
            )
        except Exception as exc:
            LOGGER.error(
                "unexpected goal preview failure exception_type=%s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": {
                        "code": "goal_preview_internal_error",
                        "message": (
                            "the control center could not interpret the "
                            f"goal: {type(exc).__name__}"
                        ),
                    }
                },
                send_body=True,
            )

    def _handle_control_task_create(self) -> None:
        try:
            self._validate_control_request()
            _reject_unknown_query(
                parse_qs(urlsplit(self.path).query, strict_parsing=False),
                set(),
            )
            if self.server.control_service is None:
                raise ControlActionError(
                    "control_disabled",
                    "control actions are not enabled",
                    status=404,
                )
            result = self.server.control_service.create_task(
                self._read_json_body()
            )
            self._send_json(HTTPStatus.CREATED, result, send_body=True)
        except (ControlActionError, WorkbenchError) as exc:
            self._send_json(
                exc.status,
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                    }
                },
                send_body=True,
            )
        except Exception as exc:
            LOGGER.error(
                "unexpected control request failure exception_type=%s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": {
                        "code": "task_creation_internal_error",
                        "message": (
                            "the control center could not create the task: "
                            f"{type(exc).__name__}"
                        ),
                    }
                },
                send_body=True,
            )

    def _handle_conversation_create(self) -> None:
        try:
            self._validate_control_request()
            _reject_unknown_query(
                parse_qs(urlsplit(self.path).query, strict_parsing=False),
                set(),
            )
            if self.server.conversation_service is None:
                raise ControlActionError(
                    "control_disabled",
                    "control actions are not enabled",
                    status=404,
                )
            payload = self._read_json_body()
            unknown = set(payload) - {"client_message_id", "metadata"}
            if unknown:
                raise ControlActionError(
                    "unknown_conversation_fields",
                    "conversation creation payload contains unknown fields",
                    status=400,
                )
            client_message_id = payload.get("client_message_id")
            if client_message_id is not None:
                if (
                    not isinstance(client_message_id, str)
                    or len(client_message_id) > 128
                ):
                    raise ControlActionError(
                        "invalid_client_message_id",
                        "client_message_id must be a bounded string",
                        status=400,
                    )
            metadata = payload.get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                raise ControlActionError(
                    "invalid_conversation_metadata",
                    "metadata must be an object",
                    status=400,
                )
            conversation = (
                self.server.conversation_service.create_conversation(
                    metadata=metadata or {},
                )
            )
            self._send_json(
                HTTPStatus.CREATED,
                self._conversation_projection(conversation),
                send_body=True,
            )
        except (ControlActionError, WorkbenchError) as exc:
            self._send_json(
                exc.status,
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                    }
                },
                send_body=True,
            )
        except Exception as exc:
            LOGGER.error(
                "unexpected conversation creation failure "
                "exception_type=%s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": {
                        "code": "conversation_creation_internal_error",
                        "message": (
                            "the control center could not create the "
                            f"conversation: {type(exc).__name__}"
                        ),
                    }
                },
                send_body=True,
            )

    def _handle_conversation_message(self) -> None:
        try:
            self._validate_control_request()
            path = urlsplit(self.path).path
            match = _CONVERSATION_MESSAGES_PATH.fullmatch(path)
            assert match is not None
            conversation_id = match.group(1)
            _reject_unknown_query(
                parse_qs(urlsplit(self.path).query, strict_parsing=False),
                set(),
            )
            if self.server.conversation_service is None:
                raise ControlActionError(
                    "control_disabled",
                    "control actions are not enabled",
                    status=404,
                )
            payload = self._read_json_body()
            unknown = set(payload) - {
                "content",
                "client_message_id",
                "answer_to",
            }
            if unknown:
                raise ControlActionError(
                    "unknown_message_fields",
                    "message payload contains unknown fields",
                    status=400,
                )
            content = payload.get("content")
            if (
                not isinstance(content, str)
                or not content.strip()
                or "\x00" in content
                or len(content.encode("utf-8"))
                > MAX_CONVERSATION_CONTENT_BYTES
            ):
                raise ControlActionError(
                    "invalid_message_content",
                    "content must be a non-empty bounded string",
                    status=400,
                )
            client_message_id = payload.get("client_message_id")
            if (
                not isinstance(client_message_id, str)
                or len(client_message_id) > 128
            ):
                raise ControlActionError(
                    "invalid_client_message_id",
                    "client_message_id must be a bounded string",
                    status=400,
                )
            answer_to = payload.get("answer_to")
            if answer_to is not None:
                if not isinstance(answer_to, str):
                    raise ControlActionError(
                        "invalid_answer_to",
                        "answer_to must be a string question id",
                        status=400,
                    )
            result = (
                self.server.conversation_service.process_message(
                    conversation_id,
                    content.strip(),
                    client_message_id=client_message_id,
                    answer_to=answer_to,
                )
            )
            self._send_json(
                HTTPStatus.OK,
                self._conversation_result_projection(result),
                send_body=True,
            )
        except (ControlActionError, WorkbenchError) as exc:
            self._send_json(
                exc.status,
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                    }
                },
                send_body=True,
            )
        except Exception as exc:
            LOGGER.error(
                "unexpected conversation message failure "
                "exception_type=%s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": {
                        "code": "conversation_message_internal_error",
                        "message": (
                            "the control center could not process the "
                            f"message: {type(exc).__name__}"
                        ),
                    }
                },
                send_body=True,
            )

    def _conversation_list(
        self, limit: int, offset: int,
    ) -> dict[str, Any]:
        if self.server.conversation_service is None:
            raise WorkbenchError(
                "not_found",
                "the requested workbench route does not exist",
                status=404,
            )
        conversations = (
            self.server.conversation_service.list_conversations(
                limit=limit, offset=offset,
            )
        )
        return {
            "conversations": conversations,
            "count": len(conversations),
            "limit": limit,
            "offset": offset,
            "read_only": False,
        }

    def _conversation_detail(self, conversation_id: str) -> dict[str, Any]:
        if self.server.conversation_service is None:
            raise WorkbenchError(
                "not_found",
                "the requested workbench route does not exist",
                status=404,
            )
        conversation = (
            self.server.conversation_service.get_conversation(conversation_id)
        )
        if conversation is None:
            raise WorkbenchError(
                "conversation_not_found",
                "the conversation does not exist",
                status=404,
            )
        active_task = (
            self.server.conversation_service.store
            .find_active_task_for_conversation(conversation_id)
        )
        return self._conversation_projection(
            conversation,
            active_task_id=(
                active_task.task_id if active_task is not None else None
            ),
            active_task_status=(
                active_task.status.value
                if active_task is not None
                else None
            ),
        )

    def _conversation_messages(
        self,
        conversation_id: str,
        limit: int,
    ) -> dict[str, Any]:
        if self.server.conversation_service is None:
            raise WorkbenchError(
                "not_found",
                "the requested workbench route does not exist",
                status=404,
            )
        conversation = (
            self.server.conversation_service.get_conversation(conversation_id)
        )
        if conversation is None:
            raise WorkbenchError(
                "conversation_not_found",
                "the conversation does not exist",
                status=404,
            )
        messages = (
            self.server.conversation_service.history(
                conversation_id,
                limit=limit,
            )
        )
        return {
            "conversation_id": conversation_id,
            "messages": [msg.to_dict() for msg in messages],
            "count": len(messages),
            "limit": limit,
        }

    @staticmethod
    def _conversation_projection(
        conversation: Any,
        *,
        active_task_id: str | None = None,
        active_task_status: str | None = None,
    ) -> dict[str, Any]:
        base = conversation.to_dict()
        base["active_task_id"] = active_task_id
        base["active_task_status"] = active_task_status
        base["read_only"] = False
        return base

    @staticmethod
    def _conversation_result_projection(result: Any) -> dict[str, Any]:
        base = result.to_dict()
        base["read_only"] = False
        return base

    def _memory_list(self, query: dict[str, list[str]]) -> dict[str, Any]:
        if self.server.memory_service is None:
            raise WorkbenchError(
                "not_found",
                "the requested workbench route does not exist",
                status=404,
            )
        _reject_unknown_query(query, {"type", "scope", "status", "limit", "q"})
        mem_type = _query_single(query, "type")
        scope = _query_single(query, "scope")
        status = _query_single(query, "status") or "active"
        limit = _query_int(
            query, "limit",
            default=50, minimum=1, maximum=MAX_RECALL_LIMIT,
        )
        search = _query_single(query, "q")
        if search is not None and len(search) > MAX_QUERY_LENGTH:
            raise WorkbenchError(
                "invalid_query_parameter",
                "q must be at most %d characters" % MAX_QUERY_LENGTH,
            )
        if search:
            memories = self.server.memory_service.recall(
                search, scope=scope, type=mem_type, limit=limit,
            )
        else:
            memories = self.server.memory_service.list_memories(
                scope=scope, type=mem_type, status=status, limit=limit,
            )
        return {
            "memories": [m.to_dict() for m in memories],
            "count": len(memories),
            "limit": limit,
            "read_only": False,
        }

    def _memory_detail(self, memory_id: str) -> dict[str, Any]:
        if self.server.memory_service is None:
            raise WorkbenchError(
                "not_found",
                "the requested workbench route does not exist",
                status=404,
            )
        try:
            memory = self.server.memory_service.get(memory_id)
        except MemoryError as exc:
            raise WorkbenchError(exc.code, exc.message, status=404)
        return memory.to_dict()

    def _handle_workspace_create(self) -> None:
        """Create an empty workspace.

        This endpoint is deliberately *not* an authorization for anything:
        the response says ``execution_authorized: false`` and no grant is
        issued, consumed or inspected here. Creating a directory you can see
        and running untrusted code in it stay two separate decisions.
        """
        try:
            self._validate_control_request()
            _reject_unknown_query(
                parse_qs(urlsplit(self.path).query, strict_parsing=False),
                set(),
            )
            payload = self._read_json_body()
            created = create_workspace(
                self.server.workbench.project_root, payload.get("name")
            )
            self._send_json(HTTPStatus.CREATED, created, send_body=True)
        except (WorkspaceError, ControlActionError, WorkbenchError) as exc:
            status = getattr(exc, "status", 400)
            self._send_json(
                status,
                {"error": {"code": exc.code, "message": exc.message}},
                send_body=True,
            )
        except Exception as exc:
            LOGGER.error(
                "unexpected workspace creation failure exception_type=%s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {
                    "code": "workspace_creation_internal_error",
                    "message": (
                        f"could not create workspace: {type(exc).__name__}"
                    ),
                }},
                send_body=True,
            )

    def _handle_memory_create(self) -> None:
        try:
            self._validate_control_request()
            _reject_unknown_query(
                parse_qs(urlsplit(self.path).query, strict_parsing=False),
                set(),
            )
            if self.server.memory_service is None:
                raise ControlActionError(
                    "control_disabled",
                    "control actions are not enabled",
                    status=404,
                )
            payload = self._read_json_body()
            required = {"type", "scope", "key", "content"}
            missing = required - set(payload)
            if missing:
                raise ControlActionError(
                    "missing_memory_fields",
                    f"missing required fields: {sorted(missing)}",
                    status=400,
                )
            memory = self.server.memory_service.record(
                type=payload["type"],
                scope=payload["scope"],
                key=payload["key"],
                content=payload["content"],
                source_task_id=payload.get("source_task_id"),
                source_event_id=payload.get("source_event_id"),
                confidence=payload.get("confidence", "medium"),
                metadata=payload.get("metadata"),
            )
            self._send_json(
                HTTPStatus.CREATED,
                memory.to_dict(),
                send_body=True,
            )
        except (ControlActionError, WorkbenchError, MemoryError) as exc:
            status = getattr(exc, "status", 400)
            self._send_json(
                status,
                {"error": {"code": exc.code, "message": exc.message}},
                send_body=True,
            )
        except Exception as exc:
            LOGGER.error(
                "unexpected memory creation failure exception_type=%s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {
                    "code": "memory_creation_internal_error",
                    "message": f"could not create memory: {type(exc).__name__}",
                }},
                send_body=True,
            )

    def _handle_memory_correct(self) -> None:
        try:
            self._validate_control_request()
            path = urlsplit(self.path).path
            match = _CONTROL_MEMORY_CORRECT_PATH.fullmatch(path)
            assert match is not None
            memory_id = match.group(1)
            _reject_unknown_query(
                parse_qs(urlsplit(self.path).query, strict_parsing=False),
                set(),
            )
            if self.server.memory_service is None:
                raise ControlActionError(
                    "control_disabled",
                    "control actions are not enabled",
                    status=404,
                )
            payload = self._read_json_body()
            if not isinstance(payload.get("content"), str):
                raise ControlActionError(
                    "invalid_content",
                    "content must be a string",
                    status=400,
                )
            memory = self.server.memory_service.correct(
                memory_id, payload["content"],
            )
            self._send_json(HTTPStatus.OK, memory.to_dict(), send_body=True)
        except (ControlActionError, WorkbenchError, MemoryError) as exc:
            status = getattr(exc, "status", 400)
            self._send_json(
                status,
                {"error": {"code": exc.code, "message": exc.message}},
                send_body=True,
            )
        except Exception as exc:
            LOGGER.error(
                "unexpected memory correction failure exception_type=%s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {
                    "code": "memory_correction_internal_error",
                    "message": f"could not correct memory: {type(exc).__name__}",
                }},
                send_body=True,
            )

    def _handle_memory_forget(self) -> None:
        try:
            self._validate_control_request()
            path = urlsplit(self.path).path
            match = _CONTROL_MEMORY_DETAIL_PATH.fullmatch(path)
            assert match is not None
            memory_id = match.group(1)
            _reject_unknown_query(
                parse_qs(urlsplit(self.path).query, strict_parsing=False),
                set(),
            )
            if self.server.memory_service is None:
                raise ControlActionError(
                    "control_disabled",
                    "control actions are not enabled",
                    status=404,
                )
            memory = self.server.memory_service.forget(memory_id)
            self._send_json(HTTPStatus.OK, memory.to_dict(), send_body=True)
        except (ControlActionError, WorkbenchError, MemoryError) as exc:
            status = getattr(exc, "status", 400)
            self._send_json(
                status,
                {"error": {"code": exc.code, "message": exc.message}},
                send_body=True,
            )
        except Exception as exc:
            LOGGER.error(
                "unexpected memory forget failure exception_type=%s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {
                    "code": "memory_forget_internal_error",
                    "message": f"could not forget memory: {type(exc).__name__}",
                }},
                send_body=True,
            )

    def _handle_task_retry(self) -> None:
        try:
            self._validate_control_request()
            path = urlsplit(self.path).path
            match = _CONTROL_TASK_RETRY_PATH.fullmatch(path)
            assert match is not None
            task_id = match.group(1)
            _reject_unknown_query(
                parse_qs(urlsplit(self.path).query, strict_parsing=False),
                set(),
            )
            if self.server.control_service is None:
                raise ControlActionError(
                    "control_disabled",
                    "control actions are not enabled",
                    status=404,
                )
            body = self._read_json_body()
            unknown = set(body) - {"request_id"}
            if unknown:
                raise ControlActionError(
                    "unknown_request_fields",
                    "retry payload contains unknown fields",
                    status=400,
                )
            request_id = body.get("request_id")
            if (
                not isinstance(request_id, str)
                or not _REQUEST_ID_PATTERN.fullmatch(request_id)
            ):
                raise ControlActionError(
                    "invalid_action_request_id",
                    "request_id must be a canonical UUIDv4",
                    status=400,
                )
            from .service import TaskService
            from .retry_policy import TaskRetryError

            service = TaskService(
                self.server.workbench.project_root,
                recover_leases=False,
            )
            try:
                outcome = service.retry_task_detailed(
                    task_id,
                    request_id=request_id,
                )
            except TaskRetryError as exc:
                status = (
                    HTTPStatus.CONFLICT
                    if exc.code == "retry_request_conflict"
                    else HTTPStatus.BAD_REQUEST
                )
                raise ControlActionError(
                    exc.code, exc.message, status=status
                ) from exc
            if outcome["retried"]:
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "retry": True,
                        "already_exists": False,
                        "original_task_id": task_id,
                        "request_id": request_id,
                        "task_id": outcome["task_id"],
                        "retry_attempt": outcome["retry_attempt"],
                        "status": outcome["task"].status.value,
                        "reason": outcome["reason"],
                    },
                    send_body=True,
                )
            elif outcome["already_exists"]:
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "retry": False,
                        "already_exists": True,
                        "original_task_id": task_id,
                        "request_id": request_id,
                        "task_id": outcome["task_id"],
                        "retry_attempt": outcome["retry_attempt"],
                        "status": outcome["task"].status.value,
                        "reason": outcome["reason"],
                    },
                    send_body=True,
                )
            else:
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "retry": False,
                        "already_exists": False,
                        "original_task_id": task_id,
                        "request_id": request_id,
                        "reason": outcome["reason"],
                        "decision": outcome["decision"],
                    },
                    send_body=True,
                )
        except (ControlActionError, WorkbenchError) as exc:
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message}},
                send_body=True,
            )
        except Exception as exc:
            LOGGER.error(
                "unexpected task retry failure exception_type=%s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {
                    "code": "task_retry_internal_error",
                    "message": f"could not retry task: {type(exc).__name__}",
                }},
                send_body=True,
            )

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def _handle(self, *, send_body: bool) -> None:
        try:
            host_header = self.headers.get("Host", "")
            host_name = host_header.rsplit(":", 1)[0].lower()
            if host_name not in LOOPBACK_HOSTS:
                raise WorkbenchError(
                    "invalid_host",
                    "workbench requests must use a loopback Host header",
                    status=421,
                )
            if len(self.path) > MAX_REQUEST_TARGET:
                raise WorkbenchError(
                    "request_too_large",
                    "request path exceeds the workbench limit",
                    status=414,
                )
            parsed = urlsplit(self.path)
            path = parsed.path
            if path in _STATIC_ASSETS:
                filename, content_type = _STATIC_ASSETS[path]
                self._send_bytes(
                    HTTPStatus.OK,
                    content_type,
                    (STATIC_ROOT / filename).read_bytes(),
                    send_body=send_body,
                )
                return
            if path == "/api/tasks":
                payload = self._tasks(parse_qs(parsed.query, strict_parsing=False))
            elif path == "/api/attention":
                payload = self._attention(
                    parse_qs(parsed.query, strict_parsing=False)
                )
            elif path == "/control/api/session":
                payload = self._control_session()
            elif _CONTROL_REQUEST_PATH.fullmatch(path):
                self._validate_control_request(require_origin=False)
                _reject_unknown_query(
                    parse_qs(parsed.query, strict_parsing=False),
                    set(),
                )
                match = _CONTROL_REQUEST_PATH.fullmatch(path)
                assert match is not None
                payload = self._control_request_status(
                    match.group(1),
                    match.group(2),
                )
            elif _CONTROL_ACTIONS_PATH.fullmatch(path):
                _reject_unknown_query(
                    parse_qs(parsed.query, strict_parsing=False),
                    set(),
                )
                match = _CONTROL_ACTIONS_PATH.fullmatch(path)
                assert match is not None
                payload = self._control_action_preview(match.group(1))
            elif _EVENTS_STREAM_PATH.fullmatch(path):
                _reject_unknown_query(
                    parse_qs(parsed.query, strict_parsing=False),
                    {"since"},
                )
                match = _EVENTS_STREAM_PATH.fullmatch(path)
                assert match is not None
                since = _query_int(
                    parse_qs(parsed.query, strict_parsing=False),
                    "since",
                    default=0,
                    minimum=0,
                    maximum=10_000_000,
                )
                self._handle_sse_stream(
                    match.group(1), since, send_body=send_body
                )
                return
            elif (
                self.server.control_enabled
                and _CONTROL_CONVERSATIONS_PATH.fullmatch(path)
            ):
                query_params = parse_qs(
                    parsed.query, strict_parsing=False
                )
                _reject_unknown_query(query_params, {"limit", "offset"})
                limit = _query_int(
                    query_params, "limit",
                    default=20, minimum=1, maximum=100,
                )
                offset = _query_int(
                    query_params, "offset",
                    default=0, minimum=0, maximum=10000,
                )
                payload = self._conversation_list(limit, offset)
            elif (
                self.server.control_enabled
                and _CONVERSATION_DETAIL_PATH.fullmatch(path)
            ):
                _reject_unknown_query(
                    parse_qs(parsed.query, strict_parsing=False),
                    set(),
                )
                match = _CONVERSATION_DETAIL_PATH.fullmatch(path)
                assert match is not None
                payload = self._conversation_detail(match.group(1))
            elif (
                self.server.control_enabled
                and _CONVERSATION_MESSAGES_PATH.fullmatch(path)
            ):
                _reject_unknown_query(
                    parse_qs(parsed.query, strict_parsing=False),
                    {"limit"},
                )
                match = _CONVERSATION_MESSAGES_PATH.fullmatch(path)
                assert match is not None
                limit = _query_int(
                    parse_qs(parsed.query, strict_parsing=False),
                    "limit",
                    default=DEFAULT_CONVERSATION_MESSAGE_LIMIT,
                    minimum=1,
                    maximum=MAX_CONVERSATION_MESSAGES,
                )
                payload = self._conversation_messages(match.group(1), limit)
            elif (
                self.server.control_enabled
                and _CONTROL_MEMORIES_PATH.fullmatch(path)
            ):
                query_params = parse_qs(
                    parsed.query, strict_parsing=False
                )
                payload = self._memory_list(query_params)
            elif (
                self.server.control_enabled
                and _CONTROL_WORKSPACES_PATH.fullmatch(path)
            ):
                _reject_unknown_query(
                    parse_qs(parsed.query, strict_parsing=False),
                    set(),
                )
                # A GET carries no Origin header in a normal same-origin
                # navigation or fetch, so requiring one would make the list
                # unreachable from the very page it exists for. The control
                # token still gates it; Origin stays mandatory on writes.
                self._validate_control_request(require_origin=False)
                payload = list_workspaces(self.server.workbench.project_root)
            elif (
                self.server.control_enabled
                and _CONTROL_MEMORY_DETAIL_PATH.fullmatch(path)
            ):
                _reject_unknown_query(
                    parse_qs(parsed.query, strict_parsing=False),
                    set(),
                )
                match = _CONTROL_MEMORY_DETAIL_PATH.fullmatch(path)
                assert match is not None
                payload = self._memory_detail(match.group(1))
            elif _TASK_PROGRESS_PATH.fullmatch(path):
                _reject_unknown_query(
                    parse_qs(parsed.query, strict_parsing=False),
                    set(),
                )
                match = _TASK_PROGRESS_PATH.fullmatch(path)
                assert match is not None
                payload = self.server.workbench.task_progress(match.group(1))
            else:
                match = _TASK_PATH.fullmatch(path)
                if match is None:
                    raise WorkbenchError(
                        "not_found",
                        "the requested workbench route does not exist",
                        status=404,
                    )
                _reject_unknown_query(
                    parse_qs(parsed.query, strict_parsing=False),
                    {"download"},
                )
                task_id, artifact_suffix = match.groups()
                wants_download = (
                    parse_qs(parsed.query, strict_parsing=False)
                    .get("download", ["0"])[0]
                    .lower()
                    in {"1", "true", "yes"}
                )
                if artifact_suffix and wants_download:
                    body, filename, content_type = (
                        self.server.workbench.artifact_download(task_id)
                    )
                    self._send_bytes(
                        HTTPStatus.OK,
                        content_type,
                        body,
                        send_body=send_body,
                        extra_headers={
                            "Content-Disposition": (
                                "attachment; filename=\""
                                + filename.replace('"', "")
                                + "\""
                            ),
                        },
                    )
                    return
                payload = (
                    self.server.workbench.artifact_preview(task_id)
                    if artifact_suffix
                    else self.server.workbench.task_detail(task_id)
                )
            self._send_json(HTTPStatus.OK, payload, send_body=send_body)
        except (WorkbenchError, ControlActionError, WorkspaceError) as exc:
            self._send_json(
                exc.status,
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                    }
                },
                send_body=send_body,
            )
        except Exception as exc:
            LOGGER.error(
                "unexpected workbench request failure exception_type=%s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": {
                        "code": "workbench_internal_error",
                        "message": (
                            "the read-only workbench could not complete "
                            f"the request: {type(exc).__name__}"
                        ),
                    }
                },
                send_body=send_body,
            )

    def _handle_task_dispatch(self) -> None:
        """Start a queued task in the background and return immediately.

        This is deliberately *not* the ``start_queued_task`` control action:
        that action runs the whole task inline while holding the control lock,
        which is exactly the blocking behaviour this endpoint removes. The
        engine still claims the task, so a dispatch racing with any other
        driver loses the claim instead of running twice.
        """
        try:
            self._validate_control_request()
            match = _CONTROL_TASK_DISPATCH_PATH.fullmatch(
                urlsplit(self.path).path
            )
            assert match is not None
            task_id = match.group(1)
            _reject_unknown_query(
                parse_qs(urlsplit(self.path).query, strict_parsing=False),
                set(),
            )
            executor = self.server.task_executor
            if executor is None:
                raise ControlActionError(
                    "control_disabled",
                    "background execution is not enabled",
                    status=404,
                )
            body = self._read_json_body()
            unknown = set(body) - {"request_id"}
            if unknown:
                raise ControlActionError(
                    "unknown_request_fields",
                    "dispatch payload contains unknown fields",
                    status=400,
                )
            request_id = body.get("request_id")
            if (
                not isinstance(request_id, str)
                or not _REQUEST_ID_PATTERN.fullmatch(request_id)
            ):
                raise ControlActionError(
                    "invalid_action_request_id",
                    "request_id must be a canonical UUIDv4",
                    status=400,
                )

            from .service import TaskService

            service = TaskService(
                self.server.workbench.project_root,
                recover_leases=False,
            )
            task = service.require_task(task_id)
            if task.status is not TaskStatus.QUEUED:
                raise ControlActionError(
                    "task_not_startable",
                    "only a queued task can be dispatched"
                    f" (current status: {task.status.value})",
                    status=409,
                )
            outcome = executor.submit(
                task_id,
                source=f"control_center:{request_id}",
            )
            if outcome == "queued":
                status = HTTPStatus.ACCEPTED
            elif outcome == "duplicate":
                status = HTTPStatus.OK
            elif outcome == "full":
                raise ControlActionError(
                    "executor_queue_full",
                    "the background executor is saturated; retry shortly",
                    status=503,
                )
            else:
                raise ControlActionError(
                    "executor_unavailable",
                    "the background executor is shutting down",
                    status=503,
                )
            self._send_json(
                status,
                {
                    "status": "dispatched" if outcome == "queued" else "already_dispatched",
                    "task_id": task_id,
                    "request_id": request_id,
                    "task_status": task.status.value,
                    "queue": {
                        "queued": executor.stats().queued,
                        "in_flight": executor.stats().in_flight,
                    },
                },
                send_body=True,
            )
        except ControlActionError as exc:
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message}},
                send_body=True,
            )
        except Exception as exc:
            LOGGER.error(
                "unexpected dispatch failure exception_type=%s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {
                    "code": "dispatch_internal_error",
                    "message": f"could not dispatch the task: {type(exc).__name__}",
                }},
                send_body=True,
            )

    def _handle_task_cancel_request(self) -> None:
        """Ask a running task to stop at its next cooperative checkpoint.

        Cooperative by design: the engine checks the flag at six checkpoints
        and stops *there*. This endpoint therefore reports ``cancel_requested``,
        never ``cancelled`` — claiming otherwise would be a lie until the
        engine actually reaches a checkpoint.
        """
        try:
            self._validate_control_request()
            match = _CONTROL_TASK_CANCEL_REQUEST_PATH.fullmatch(
                urlsplit(self.path).path
            )
            assert match is not None
            task_id = match.group(1)
            _reject_unknown_query(
                parse_qs(urlsplit(self.path).query, strict_parsing=False),
                set(),
            )
            body = self._read_json_body()
            unknown = set(body) - {"request_id", "reason"}
            if unknown:
                raise ControlActionError(
                    "unknown_request_fields",
                    "cancel payload contains unknown fields",
                    status=400,
                )
            request_id = body.get("request_id")
            if (
                not isinstance(request_id, str)
                or not _REQUEST_ID_PATTERN.fullmatch(request_id)
            ):
                raise ControlActionError(
                    "invalid_action_request_id",
                    "request_id must be a canonical UUIDv4",
                    status=400,
                )
            reason = body.get("reason")
            if reason is not None and (
                not isinstance(reason, str) or len(reason) > 400
            ):
                raise ControlActionError(
                    "invalid_cancel_reason",
                    "reason must be a string of at most 400 characters",
                    status=400,
                )

            from .service import TaskService
            from .state_machine import is_terminal

            service = TaskService(
                self.server.workbench.project_root,
                recover_leases=False,
            )
            task = service.require_task(task_id)
            if is_terminal(task.status):
                raise ControlActionError(
                    "task_already_finished",
                    "the task already finished and cannot be cancelled"
                    f" (status: {task.status.value})",
                    status=409,
                )
            updated = service.request_cancel(
                task_id,
                requested_by=f"control-center:{request_id}",
                reason=reason,
            )
            self._send_json(
                HTTPStatus.ACCEPTED,
                {
                    "status": "cancel_requested",
                    "task_id": task_id,
                    "request_id": request_id,
                    "task_status": updated.status.value,
                    "note": "the task stops at its next cooperative checkpoint",
                },
                send_body=True,
            )
        except ControlActionError as exc:
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message}},
                send_body=True,
            )
        except Exception as exc:
            LOGGER.error(
                "unexpected cancel-request failure exception_type=%s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {
                    "code": "cancel_request_internal_error",
                    "message": (
                        "could not request cancellation: "
                        f"{type(exc).__name__}"
                    ),
                }},
                send_body=True,
            )

    def _handle_control_post(self) -> None:
        failure_task_id: str | None = None
        failure_request_id: str | None = None
        try:
            self._validate_control_request()
            path = urlsplit(self.path).path
            match = _CONTROL_ACTIONS_PATH.fullmatch(path)
            assert match is not None
            failure_task_id = match.group(1)
            payload = self._read_json_body()
            if isinstance(payload, dict) and isinstance(
                payload.get("request_id"), str
            ):
                failure_request_id = payload["request_id"]
            if self.server.control_service is None:
                raise ControlActionError(
                    "control_disabled",
                    "control actions are not enabled",
                    status=404,
                )
            result = self.server.control_service.execute(
                match.group(1),
                payload,
            )
            self._send_json(
                HTTPStatus.OK,
                result,
                send_body=True,
            )
        except ControlActionError as exc:
            self._send_json(
                exc.status,
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                    }
                },
                send_body=True,
            )
        except WorkbenchError as exc:
            self._send_json(
                exc.status,
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                    }
                },
                send_body=True,
            )
        except Exception as exc:
            LOGGER.error(
                "unexpected control action request failure task_id=%s "
                "request_id=%s exception_type=%s",
                failure_task_id,
                failure_request_id,
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": {
                        "code": "control_internal_error",
                        "message": (
                            "the control center could not complete the "
                            f"request: {type(exc).__name__}"
                        ),
                    }
                },
                send_body=True,
            )

    def _validate_control_request(
        self,
        *,
        require_origin: bool = True,
    ) -> None:
        if not self.server.control_enabled:
            raise ControlActionError(
                "control_disabled",
                "control actions are not enabled",
                status=404,
            )
        host_header = self.headers.get("Host", "")
        host_name = host_header.rsplit(":", 1)[0].lower()
        if host_name not in LOOPBACK_HOSTS:
            raise ControlActionError(
                "invalid_host",
                "control requests must use a loopback Host header",
                status=421,
            )
        if require_origin:
            origin = self.headers.get("Origin")
            if origin not in self.server.control_origins:
                raise ControlActionError(
                    "invalid_origin",
                    "control requests require the matching local origin",
                    status=403,
                )
        token = self.headers.get("X-Zevorinth-Control-Token")
        expected = self.server.control_token or ""
        if not token or not hmac.compare_digest(token, expected):
            raise ControlActionError(
                "invalid_control_token",
                "control request token is missing or invalid",
                status=403,
            )

    def _read_json_body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ControlActionError(
                "invalid_content_type",
                "control requests require application/json",
                status=415,
            )
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError as exc:
            raise ControlActionError(
                "invalid_content_length",
                "Content-Length is required",
                status=411,
            ) from exc
        if length < 1 or length > MAX_CONTROL_BODY_BYTES:
            raise ControlActionError(
                "invalid_content_length",
                "control request body size is invalid",
                status=413,
            )
        raw = self.rfile.read(length)
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant: {value}")
                ),
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise ControlActionError(
                "invalid_json",
                "control request body is not valid JSON",
                status=400,
            ) from exc
        if not isinstance(payload, dict):
            raise ControlActionError(
                "invalid_json",
                "control request body must be an object",
                status=400,
            )
        return payload

    def _control_session(self) -> dict[str, Any]:
        if not self.server.control_enabled:
            raise WorkbenchError(
                "not_found",
                "the requested workbench route does not exist",
                status=404,
            )
        return {
            "enabled": True,
            "contract_version": CONTROL_CONTRACT_VERSION,
            "token": self.server.control_token,
            "actions": sorted(SUPPORTED_ACTIONS),
            "authentication": "none",
            "csrf_protection": "loopback_origin_and_process_token",
            "read_only_api_preserved": True,
            "goal_preview": NATURAL_GOAL_PREVIEW_VERSION,
            "conversation_intake": self.server.conversation_service is not None,
            "cross_task_memory": self.server.memory_service is not None,
            "manual_retry": self.server.control_enabled,
            "automatic_recovery": False,
        }

    def _control_action_preview(self, task_id: str) -> dict[str, Any]:
        if (
            not self.server.control_enabled
            or self.server.control_service is None
        ):
            raise WorkbenchError(
                "not_found",
                "the requested workbench route does not exist",
                status=404,
            )
        return self.server.control_service.action_preview(task_id)

    def _control_request_status(
        self,
        task_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        if (
            not self.server.control_enabled
            or self.server.control_service is None
        ):
            raise WorkbenchError(
                "not_found",
                "the requested workbench route does not exist",
                status=404,
            )
        return self.server.control_service.request_status(
            task_id,
            request_id,
        )

    def _tasks(self, query: dict[str, list[str]]) -> dict[str, Any]:
        _reject_unknown_query(
            query,
            {"limit", "offset", "status", "mode", "q"},
        )
        limit = _query_int(
            query,
            "limit",
            default=DEFAULT_TASK_LIMIT,
            minimum=1,
            maximum=MAX_TASK_LIMIT,
        )
        offset = _query_int(
            query,
            "offset",
            default=0,
            minimum=0,
            maximum=1_000_000,
        )
        status = _query_single(query, "status")
        mode = _query_single(query, "mode") or "all"
        search = _query_single(query, "q")
        if search is not None and len(search) > 200:
            raise WorkbenchError(
                "invalid_query_parameter",
                "q must be at most 200 characters",
            )
        return self.server.workbench.list_tasks(
            limit=limit,
            offset=offset,
            status=status,
            mode=mode,
            query=search,
        )

    def _attention(self, query: dict[str, list[str]]) -> dict[str, Any]:
        _reject_unknown_query(query, {"limit"})
        limit = _query_int(
            query,
            "limit",
            default=MAX_ATTENTION_ITEMS,
            minimum=1,
            maximum=MAX_ATTENTION_ITEMS,
        )
        return self.server.workbench.attention(limit=limit)

    def _method_not_allowed(self) -> None:
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {
                "error": {
                    "code": "read_only_workbench",
                    "message": "only GET and HEAD requests are supported",
                }
            },
            send_body=True,
            extra_headers={"Allow": "GET, HEAD"},
        )

    def _handle_sse_stream(
        self,
        task_id: str,
        since: int,
        *,
        send_body: bool,
    ) -> None:
        try:
            initial = self.server.workbench.task_events_since(
                task_id, since
            )
        except WorkbenchError as exc:
            self._send_json(
                exc.status,
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                    }
                },
                send_body=send_body,
            )
            return
        self.send_response(int(HTTPStatus.OK))
        self.send_header(
            "Content-Type", "text/event-stream; charset=utf-8"
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Cross-Origin-Resource-Policy", "same-origin"
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'none'; connect-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        if not send_body:
            return
        last_seq = since
        try:
            self._sse_write_field("retry", "5000")
            for projection in initial:
                self._sse_write_data(projection)
                last_seq = projection["sequence"]
            deadline = time.monotonic() + SSE_MAX_SECONDS
            while time.monotonic() < deadline:
                time.sleep(SSE_POLL_INTERVAL)
                try:
                    batch = self.server.workbench.task_events_since(
                        task_id, last_seq
                    )
                except WorkbenchError:
                    break
                if not batch:
                    continue
                for projection in batch:
                    self._sse_write_data(projection)
                    last_seq = projection["sequence"]
            self._sse_write_comment("stream-end")
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
        ):
            return

    def _sse_write_data(self, payload: Any) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _sse_write_field(self, field: str, value: str) -> None:
        self.wfile.write(f"{field}: {value}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _sse_write_comment(self, text: str) -> None:
        self.wfile.write(f": {text}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _send_json(
        self,
        status: int,
        payload: Any,
        *,
        send_body: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        self._send_bytes(
            status,
            "application/json; charset=utf-8",
            body,
            send_body=send_body,
            extra_headers=extra_headers,
        )

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        body: bytes,
        *,
        send_body: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'none'; connect-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'none'",
        )
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


def create_server(
    project_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    control_enabled: bool = False,
) -> WorkbenchServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("workbench may bind only to a loopback host")
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if control_enabled:
        # The control center owns a writable runtime. Initialize an empty store
        # before serving so a first visit is a normal empty-state experience,
        # while the read-only workbench keeps its non-initializing contract.
        from .service import TaskService

        TaskService(project_root, recover_leases=False)
    workbench = ReadOnlyWorkbench(project_root)
    return WorkbenchServer(
        (host, port),
        workbench,
        control_enabled=control_enabled,
    )


def serve_workbench(
    project_root: Path,
    *,
    port: int = DEFAULT_PORT,
    control_enabled: bool = False,
) -> None:
    server = create_server(
        project_root,
        port=port,
        control_enabled=control_enabled,
    )
    host, bound_port = server.server_address[:2]
    label = "control center" if control_enabled else "workbench"
    print(f"Zevorinth {label}: http://{host}:{bound_port}/", flush=True)
    if control_enabled:
        print(
            "Read-only views plus explicitly confirmed lightweight actions.",
            flush=True,
        )
    else:
        print("Read-only. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _reject_unknown_query(
    query: dict[str, list[str]],
    allowed: set[str],
) -> None:
    unknown = set(query) - allowed
    if unknown:
        raise WorkbenchError(
            "unknown_query_parameter",
            f"unknown query parameter: {sorted(unknown)[0]}",
        )


def _query_single(
    query: dict[str, list[str]],
    name: str,
) -> str | None:
    values = query.get(name)
    if values is None:
        return None
    if len(values) != 1:
        raise WorkbenchError(
            "invalid_query_parameter",
            f"{name} must be provided at most once",
        )
    value = values[0]
    if value == "":
        raise WorkbenchError(
            "invalid_query_parameter",
            f"{name} must not be empty",
        )
    return value


def _query_int(
    query: dict[str, list[str]],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = _query_single(query, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise WorkbenchError(
            "invalid_query_parameter",
            f"{name} must be an integer",
        ) from exc
    if not minimum <= value <= maximum:
        raise WorkbenchError(
            "invalid_query_parameter",
            f"{name} must be between {minimum} and {maximum}",
        )
    return value
