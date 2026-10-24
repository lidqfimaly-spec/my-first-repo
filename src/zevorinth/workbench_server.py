"""Loopback-only HTTP server for the read-only Zevorinth workbench."""

from __future__ import annotations

import json
import hmac
import logging
import re
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .workbench import (
    DEFAULT_TASK_LIMIT,
    MAX_ATTENTION_ITEMS,
    MAX_TASK_LIMIT,
    ReadOnlyWorkbench,
    WorkbenchError,
)
from .workbench_control import (
    CONTROL_CONTRACT_VERSION,
    ControlActionError,
    SUPPORTED_ACTIONS,
    TaskControlService,
)


STATIC_ROOT = Path(__file__).with_name("workbench_static")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
DEFAULT_PORT = 8765
MAX_REQUEST_TARGET = 2048
MAX_CONTROL_BODY_BYTES = 16 * 1024
_TASK_PATH = re.compile(
    r"^/api/tasks/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})(/artifact)?$"
)
_CONTROL_ACTIONS_PATH = re.compile(
    r"^/control/api/tasks/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})/actions$"
)
_CONTROL_TASKS_PATH = re.compile(r"^/control/api/tasks$")
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
        host, port = self.server_address[:2]
        self.control_origins = {
            f"http://{host}:{port}",
            f"http://localhost:{port}",
        }


class WorkbenchRequestHandler(BaseHTTPRequestHandler):
    server: WorkbenchServer

    def do_GET(self) -> None:
        self._handle(send_body=True)

    def do_HEAD(self) -> None:
        self._handle(send_body=False)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if self.server.control_enabled and _CONTROL_TASKS_PATH.fullmatch(path):
            self._handle_control_task_create()
            return
        if (
            self.server.control_enabled
            and _CONTROL_ACTIONS_PATH.fullmatch(path)
        ):
            self._handle_control_post()
            return
        self._method_not_allowed()

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

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
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
                    set(),
                )
                task_id, artifact_suffix = match.groups()
                payload = (
                    self.server.workbench.artifact_preview(task_id)
                    if artifact_suffix
                    else self.server.workbench.task_detail(task_id)
                )
            self._send_json(HTTPStatus.OK, payload, send_body=send_body)
        except (WorkbenchError, ControlActionError) as exc:
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
