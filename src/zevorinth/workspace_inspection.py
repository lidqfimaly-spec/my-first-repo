"""Bounded read-only inspection for registered workspaces."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from .local_test_runner import TestExecutionError, resolve_workspace


INSPECT_WORKSPACE_TOOL = "inspect_workspace"
MAX_INSPECTION_FILES = 200
MAX_INSPECTION_FILE_BYTES = 512 * 1024
MAX_INSPECTION_TOTAL_BYTES = 4 * 1024 * 1024
MAX_INSPECTION_QUERY_CHARS = 512

_FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".idea",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
    }
)
_SENSITIVE_NAMES = (
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "secrets",
)
_SENSITIVE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)


class WorkspaceInspectionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_workspace_relative(
    value: object,
    *,
    allow_empty: bool,
) -> str:
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str):
        raise WorkspaceInspectionError(
            "invalid_workspace_path",
            "workspace path must be a string",
        )
    if "\x00" in value or "\\" in value:
        raise WorkspaceInspectionError(
            "invalid_workspace_path",
            "workspace path contains an invalid character",
        )
    stripped = value.strip()
    if stripped != value:
        raise WorkspaceInspectionError(
            "invalid_workspace_path",
            "workspace path must not start or end with whitespace",
        )
    if not value and allow_empty:
        return ""
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkspaceInspectionError(
            "invalid_workspace_path",
            "workspace path must be a canonical relative path",
        )
    lowered = [part.lower() for part in path.parts]
    if any(part in _FORBIDDEN_PARTS for part in lowered):
        raise WorkspaceInspectionError(
            "forbidden_workspace_path",
            "workspace path contains a protected directory",
        )
    if any(
        part == "secret"
        or part == "secrets"
        or part == "credential"
        or part == "credentials"
        for part in lowered
    ):
        raise WorkspaceInspectionError(
            "sensitive_workspace_path",
            "workspace path references a sensitive location",
        )
    name = lowered[-1]
    if (
        name.startswith(".env")
        or name.endswith(".env")
        or name in _SENSITIVE_NAMES
        or "secret" in name
        or "credential" in name
    ):
        raise WorkspaceInspectionError(
            "sensitive_workspace_path",
            "workspace path references a sensitive file",
        )
    if any(name.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES):
        raise WorkspaceInspectionError(
            "sensitive_workspace_path",
            "workspace path references a sensitive file",
        )
    return path.as_posix()


def validate_inspection_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "workspace",
        "path",
        "query",
        "max_files",
        "max_file_bytes",
    }
    unknown = set(parameters) - allowed
    if unknown:
        raise WorkspaceInspectionError(
            "unsupported_parameter",
            f"unknown inspection parameters: {sorted(unknown)}",
        )
    workspace = parameters.get("workspace")
    if not isinstance(workspace, str):
        raise WorkspaceInspectionError(
            "invalid_workspace",
            "workspace must be a registered workspace name",
        )
    relative_path = normalize_workspace_relative(
        parameters.get("path", ""),
        allow_empty=True,
    )
    query = parameters.get("query")
    if query is not None:
        if (
            not isinstance(query, str)
            or not query
            or len(query) > MAX_INSPECTION_QUERY_CHARS
            or "\x00" in query
        ):
            raise WorkspaceInspectionError(
                "invalid_inspection_query",
                "query must be a non-empty bounded string",
            )
    max_files = parameters.get("max_files", MAX_INSPECTION_FILES)
    if (
        not isinstance(max_files, int)
        or isinstance(max_files, bool)
        or not 1 <= max_files <= MAX_INSPECTION_FILES
    ):
        raise WorkspaceInspectionError(
            "invalid_inspection_limit",
            f"max_files must be between 1 and {MAX_INSPECTION_FILES}",
        )
    max_file_bytes = parameters.get(
        "max_file_bytes",
        MAX_INSPECTION_FILE_BYTES,
    )
    if (
        not isinstance(max_file_bytes, int)
        or isinstance(max_file_bytes, bool)
        or not 1 <= max_file_bytes <= MAX_INSPECTION_FILE_BYTES
    ):
        raise WorkspaceInspectionError(
            "invalid_inspection_limit",
            f"max_file_bytes must be between 1 and {MAX_INSPECTION_FILE_BYTES}",
        )
    return {
        "workspace": workspace,
        "path": relative_path,
        "query": query,
        "max_files": max_files,
        "max_file_bytes": max_file_bytes,
    }


def inspect_workspace(
    project_root: Path,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    values = validate_inspection_parameters(parameters)
    try:
        workspace_root = resolve_workspace(
            project_root,
            values["workspace"],
        )
    except TestExecutionError as exc:
        raise WorkspaceInspectionError(exc.code, exc.message) from exc
    relative_path = values["path"]
    target = workspace_root if not relative_path else workspace_root / relative_path
    _reject_symlink_chain(workspace_root, relative_path)
    if not target.exists():
        raise WorkspaceInspectionError(
            "workspace_path_missing",
            "workspace path does not exist",
        )

    candidates: list[Path] = []
    if target.is_file():
        candidates = [target]
    elif target.is_dir():
        stack = [target]
        while stack and len(candidates) < values["max_files"]:
            directory = stack.pop()
            try:
                items = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as exc:
                raise WorkspaceInspectionError(
                    "workspace_unreadable",
                    f"workspace directory could not be read: {type(exc).__name__}",
                ) from exc
            for item in items:
                rel = Path(item.path).relative_to(workspace_root).as_posix()
                try:
                    normalize_workspace_relative(rel, allow_empty=False)
                except WorkspaceInspectionError:
                    continue
                if item.is_symlink():
                    continue
                if item.is_dir(follow_symlinks=False):
                    stack.append(Path(item.path))
                elif item.is_file(follow_symlinks=False):
                    candidates.append(Path(item.path))
                    if len(candidates) >= values["max_files"]:
                        break
    else:
        raise WorkspaceInspectionError(
            "unsupported_workspace_entry",
            "workspace path must be a regular file or directory",
        )

    files: list[dict[str, Any]] = []
    total_bytes = 0
    truncated = False
    selected: dict[str, Any] | None = None
    for candidate in candidates:
        relative = candidate.relative_to(workspace_root).as_posix()
        try:
            normalize_workspace_relative(relative, allow_empty=False)
        except WorkspaceInspectionError:
            continue
        try:
            stat_result = candidate.stat(follow_symlinks=False)
        except OSError:
            truncated = True
            continue
        remaining = max(
            0,
            MAX_INSPECTION_TOTAL_BYTES - total_bytes,
        )
        read_limit = min(values["max_file_bytes"], remaining)
        if read_limit <= 0:
            truncated = True
            break
        try:
            fd = os.open(
                candidate,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                chunks: list[bytes] = []
                remaining = read_limit + 1
                while remaining > 0:
                    chunk = os.read(fd, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
            finally:
                os.close(fd)
        except OSError:
            truncated = True
            continue
        limited = len(raw) > read_limit or stat_result.st_size > read_limit
        raw = raw[:read_limit]
        total_bytes += len(raw)
        text: str | None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        matches = None
        if text is not None and values["query"] is not None:
            matches = text.count(values["query"])
        item = {
            "path": relative,
            "byte_count": stat_result.st_size,
            "sha256": hashlib.sha256(raw).hexdigest() if not limited else None,
            "truncated": limited,
            "text": text is not None,
            "matches": matches,
        }
        files.append(item)
        if relative == relative_path:
            selected = {
                "path": relative,
                "byte_count": stat_result.st_size,
                "sha256": (
                    hashlib.sha256(raw).hexdigest()
                    if not limited
                    else None
                ),
                "content": text,
                "truncated": limited,
            }
        if limited:
            truncated = True
        if total_bytes >= MAX_INSPECTION_TOTAL_BYTES:
            truncated = True
            break

    selected_sha256 = selected.get("sha256") if selected else None
    selected_path = selected.get("path") if selected else None
    selected_content = selected.get("content") if selected else None
    return {
        "schema_version": "workspace-inspection-v1",
        "workspace": values["workspace"],
        "root_path": relative_path,
        "files": files,
        "file_count": len(files),
        "truncated": truncated,
        "total_bytes_read": total_bytes,
        "selected_path": selected_path,
        "selected_sha256": selected_sha256,
        "selected_content": selected_content,
        "query": values["query"],
    }


def verify_workspace_inspection(
    project_root: Path,
    parameters: dict[str, Any],
    evidence: dict[str, Any] | None,
) -> tuple[bool, str]:
    if evidence is None:
        return False, "workspace_inspection_evidence_missing"
    try:
        observed = inspect_workspace(project_root, parameters)
    except WorkspaceInspectionError:
        return False, "workspace_inspection_unavailable"
    comparable_fields = (
        "schema_version",
        "workspace",
        "root_path",
        "files",
        "file_count",
        "truncated",
        "total_bytes_read",
        "selected_path",
        "selected_sha256",
        "selected_content",
        "query",
    )
    if any(observed.get(field) != evidence.get(field) for field in comparable_fields):
        return False, "workspace_inspection_changed"
    return True, "workspace_inspection_verified"


def _reject_symlink_chain(workspace_root: Path, relative_path: str) -> None:
    if not relative_path:
        return
    current = workspace_root
    for component in PurePosixPath(relative_path).parts:
        current = current / component
        if current.is_symlink():
            raise WorkspaceInspectionError(
                "unsafe_workspace_path",
                "workspace path contains a symbolic link",
            )
