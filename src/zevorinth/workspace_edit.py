"""Authorized exact-text edits inside registered workspaces."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .artifact_tool import (
    ArtifactToolError,
    _ensure_locks_directory,
    _open_directory,
    _validated_task_id,
)
from .local_test_runner import TestExecutionError, resolve_workspace
from .workspace_inspection import (
    WorkspaceInspectionError,
    normalize_workspace_relative,
)


EDIT_TEXT_FILE_TOOL = "edit_text_file"
EDIT_AUTHORIZATION_TOKEN = "local-code-edit"
MAX_EDIT_FILE_BYTES = 1024 * 1024
MAX_EDIT_PATCH_BYTES = 256 * 1024
EDIT_LOCK_TIMEOUT_SECONDS = 10.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class WorkspaceEditError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_edit_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "workspace",
        "path",
        "expected_sha256",
        "old_text",
        "new_text",
        "authorization",
    }
    unknown = set(parameters) - allowed
    if unknown:
        raise WorkspaceEditError(
            "unsupported_parameter",
            f"unknown edit parameters: {sorted(unknown)}",
        )
    workspace = parameters.get("workspace")
    if not isinstance(workspace, str):
        raise WorkspaceEditError(
            "invalid_workspace",
            "workspace must be a registered workspace name",
        )
    try:
        path = normalize_workspace_relative(
            parameters.get("path"),
            allow_empty=False,
        )
    except WorkspaceInspectionError as exc:
        raise WorkspaceEditError(exc.code, exc.message) from exc
    expected_sha256 = parameters.get("expected_sha256")
    if (
        not isinstance(expected_sha256, str)
        or not _SHA256.fullmatch(expected_sha256)
    ):
        raise WorkspaceEditError(
            "invalid_expected_sha256",
            "expected_sha256 must be a lowercase SHA-256 digest",
        )
    old_text = parameters.get("old_text")
    new_text = parameters.get("new_text")
    if not isinstance(old_text, str) or not old_text:
        raise WorkspaceEditError(
            "invalid_old_text",
            "old_text must be a non-empty string",
        )
    if not isinstance(new_text, str):
        raise WorkspaceEditError(
            "invalid_new_text",
            "new_text must be a string",
        )
    try:
        old_bytes = old_text.encode("utf-8")
        new_bytes = new_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WorkspaceEditError(
            "invalid_text_patch",
            "text patch must be valid UTF-8",
        ) from exc
    if max(len(old_bytes), len(new_bytes)) > MAX_EDIT_PATCH_BYTES:
        raise WorkspaceEditError(
            "text_patch_too_large",
            f"text patch exceeds {MAX_EDIT_PATCH_BYTES} bytes",
        )
    authorization = parameters.get("authorization")
    if authorization != EDIT_AUTHORIZATION_TOKEN:
        raise WorkspaceEditError(
            "edit_not_authorized",
            "explicit local-code-edit authorization is required",
        )
    return {
        "workspace": workspace,
        "path": path,
        "expected_sha256": expected_sha256,
        "old_text": old_text,
        "new_text": new_text,
        "authorization": authorization,
    }


def apply_text_edit(
    project_root: Path,
    artifacts_root: Path,
    task_id: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    values = validate_edit_parameters(parameters)
    try:
        workspace = resolve_workspace(project_root, values["workspace"])
    except TestExecutionError as exc:
        raise WorkspaceEditError(exc.code, exc.message) from exc
    with workspace_edit_lock(workspace, artifacts_root):
        precondition = validate_edit_precondition(project_root, parameters)
        target = Path(precondition["target_path"])
        original = precondition["original_bytes"]
        original_sha = precondition["source_sha256"]
        result_bytes = precondition["result_bytes"]
        result_sha = precondition["result_sha256"]

        backup = _write_backup(
            artifacts_root,
            task_id,
            values["path"],
            original_sha,
            original,
        )
        _atomic_replace(target, result_bytes, original_sha)
        observed = _read_regular_file(target)
        observed_sha = hashlib.sha256(observed).hexdigest()
        if observed != result_bytes:
            raise WorkspaceEditError(
                "edit_verification_failed",
                "workspace edit did not persist the intended bytes",
            )
    return {
        "schema_version": "workspace-edit-v1",
        "workspace": values["workspace"],
        "path": values["path"],
        "source_sha256": original_sha,
        "result_sha256": result_sha,
        "observed_sha256": observed_sha,
        "source_byte_count": len(original),
        "result_byte_count": len(observed),
        "backup_path": str(backup),
        "authorization": EDIT_AUTHORIZATION_TOKEN,
    }


def validate_edit_precondition(
    project_root: Path,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    values = validate_edit_parameters(parameters)
    try:
        workspace = resolve_workspace(project_root, values["workspace"])
    except TestExecutionError as exc:
        raise WorkspaceEditError(exc.code, exc.message) from exc
    target = _safe_regular_file(workspace, values["path"])
    original = _read_regular_file(target)
    original_sha = hashlib.sha256(original).hexdigest()
    if original_sha != values["expected_sha256"]:
        raise WorkspaceEditError(
            "source_file_changed",
            "source file digest does not match expected_sha256",
        )
    try:
        source_text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceEditError(
            "invalid_utf8",
            "source file is not valid UTF-8 text",
        ) from exc
    occurrences = source_text.count(values["old_text"])
    if occurrences != 1:
        raise WorkspaceEditError(
            "text_match_count",
            "old_text must match exactly once",
        )
    result_bytes = source_text.replace(
        values["old_text"],
        values["new_text"],
        1,
    ).encode("utf-8")
    if len(result_bytes) > MAX_EDIT_FILE_BYTES:
        raise WorkspaceEditError(
            "edited_file_too_large",
            f"edited file exceeds {MAX_EDIT_FILE_BYTES} bytes",
        )
    return {
        "workspace": workspace,
        "target_path": target,
        "source_sha256": original_sha,
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "original_bytes": original,
        "result_bytes": result_bytes,
    }


def verify_text_edit(
    project_root: Path,
    evidence: dict[str, Any] | None,
    *,
    expected_workspace: str,
    expected_path: str,
    expected_source_sha256: str,
    expected_result_sha256: str,
) -> tuple[bool, str]:
    if evidence is None:
        return False, "edit_evidence_missing"
    if (
        not isinstance(expected_result_sha256, str)
        or not _SHA256.fullmatch(expected_result_sha256)
    ):
        return False, "edit_result_expectation_missing"
    try:
        workspace = resolve_workspace(project_root, expected_workspace)
        with workspace_edit_lock(workspace, project_root / "artifacts"):
            target = _safe_regular_file(workspace, expected_path)
            observed = _read_regular_file(target)
            observed_sha = hashlib.sha256(observed).hexdigest()
    except (WorkspaceEditError, TestExecutionError):
        return False, "edited_file_unavailable"
    lineage = (
        evidence.get("workspace") == expected_workspace
        and evidence.get("path") == expected_path
        and evidence.get("source_sha256") == expected_source_sha256
        and evidence.get("result_sha256") == expected_result_sha256
        and evidence.get("result_sha256") == evidence.get("observed_sha256")
        and evidence.get("observed_sha256") == observed_sha
        and evidence.get("result_byte_count") == len(observed)
    )
    if not lineage:
        return False, "edit_evidence_mismatch"
    return True, "workspace_edit_verified"


def workspace_file_observation(
    project_root: Path,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    values = validate_edit_parameters(parameters)
    try:
        workspace = resolve_workspace(project_root, values["workspace"])
    except TestExecutionError as exc:
        raise WorkspaceEditError(exc.code, exc.message) from exc
    with workspace_edit_lock(workspace, project_root / "artifacts"):
        target = _safe_regular_file(workspace, values["path"])
        raw = _read_regular_file(target)
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_count": len(raw),
    }


def workspace_file_digest(
    project_root: Path,
    parameters: dict[str, Any],
) -> str:
    return str(workspace_file_observation(project_root, parameters)["sha256"])


@contextmanager
def workspace_edit_lock(
    workspace: Path,
    artifacts_root: Path,
) -> Iterator[None]:
    """Serialize edits and verification for one canonical workspace."""
    locks_dir = _ensure_locks_directory(artifacts_root)
    locks_fd = _open_directory(locks_dir)
    lock_name = (
        hashlib.sha256(
            str(workspace.resolve(strict=True)).encode("utf-8")
        ).hexdigest()
        + ".workspace-edit.lock"
    )
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = -1
    try:
        lock_fd = os.open(lock_name, flags, 0o600, dir_fd=locks_fd)
        deadline = time.monotonic() + EDIT_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WorkspaceEditError(
                        "workspace_edit_lock_timeout",
                        "timed out waiting for the workspace edit lock",
                    )
                time.sleep(0.01)
        yield
    finally:
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(locks_fd)


def _safe_regular_file(workspace: Path, relative_path: str) -> Path:
    current = workspace
    for component in relative_path.split("/"):
        current = current / component
        if current.is_symlink():
            raise WorkspaceEditError(
                "unsafe_workspace_path",
                "workspace path contains a symbolic link",
            )
    if not current.is_file():
        raise WorkspaceEditError(
            "workspace_file_missing",
            "target workspace file does not exist",
        )
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceEditError(
            "unsafe_workspace_path",
            "workspace file cannot be resolved safely",
        ) from exc
    if workspace not in resolved.parents:
        raise WorkspaceEditError(
            "unsafe_workspace_path",
            "workspace file escapes the registered root",
        )
    return resolved


def _read_regular_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode):
            raise WorkspaceEditError(
                "unsafe_workspace_file",
                "target is not a regular file",
            )
        if stat_result.st_size > MAX_EDIT_FILE_BYTES:
            raise WorkspaceEditError(
                "source_file_too_large",
                f"source file exceeds {MAX_EDIT_FILE_BYTES} bytes",
            )
        chunks: list[bytes] = []
        remaining = MAX_EDIT_FILE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_EDIT_FILE_BYTES:
            raise WorkspaceEditError(
                "source_file_too_large",
                f"source file exceeds {MAX_EDIT_FILE_BYTES} bytes",
            )
        return raw
    finally:
        os.close(fd)


def _atomic_replace(
    target: Path,
    content: bytes,
    expected_source_sha256: str,
) -> None:
    mode = stat.S_IMODE(os.stat(target, follow_symlinks=False).st_mode)
    temporary = target.parent / f".zevorinth-edit-{uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, mode or 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise WorkspaceEditError(
                    "edit_write_failed",
                    "workspace edit write made no progress",
                )
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
    try:
        current = _read_regular_file(target)
        if hashlib.sha256(current).hexdigest() != expected_source_sha256:
            raise WorkspaceEditError(
                "source_file_changed",
                "source file changed before atomic replacement",
            )
        os.replace(temporary, target)
        directory_fd = os.open(
            target.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            try:
                os.fsync(directory_fd)
            except OSError:
                # Some filesystems do not support directory fsync.
                pass
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_backup(
    artifacts_root: Path,
    task_id: str,
    relative_path: str,
    source_sha256: str,
    content: bytes,
) -> Path:
    artifacts_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if artifacts_root.is_symlink():
        raise WorkspaceEditError(
            "unsafe_backup_path",
            "artifacts root is a symbolic link",
        )
    try:
        validated_task_id = _validated_task_id(task_id)
    except ArtifactToolError as exc:
        raise WorkspaceEditError(
            "unsafe_backup_path", "task ID is not canonical"
        ) from exc
    safe_name = relative_path.replace("/", "__")
    if len(safe_name.encode("utf-8")) > 160:
        safe_name = hashlib.sha256(
            relative_path.encode("utf-8")
        ).hexdigest()
    backup_name = f"{source_sha256[:16]}-{safe_name}.bak"
    root_fd = _open_directory(artifacts_root)
    task_fd = -1
    backup_fd = -1
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        task_fd = _ensure_child_directory(root_fd, validated_task_id)
        backup_fd = _ensure_child_directory(task_fd, "workspace-backups")
        try:
            fd = os.open(backup_name, flags, 0o600, dir_fd=backup_fd)
        except FileExistsError as exc:
            raise WorkspaceEditError(
                "backup_exists",
                "workspace backup already exists",
            ) from exc
        except OSError as exc:
            raise WorkspaceEditError(
                "unsafe_backup_path",
                "workspace backup file could not be created safely",
            ) from exc
        view = memoryview(content)
        try:
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise WorkspaceEditError(
                        "backup_write_failed",
                        "workspace backup write made no progress",
                    )
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            try:
                os.unlink(backup_name, dir_fd=backup_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
        try:
            os.fsync(backup_fd)
        except OSError:
            pass
    finally:
        if backup_fd >= 0:
            os.close(backup_fd)
        if task_fd >= 0:
            os.close(task_fd)
        os.close(root_fd)
    return (
        artifacts_root.resolve(strict=True)
        / validated_task_id
        / "workspace-backups"
        / backup_name
    )


def _ensure_child_directory(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise WorkspaceEditError(
                "unsafe_backup_path",
                "workspace backup directory could not be created safely",
            ) from exc
    except OSError as exc:
        raise WorkspaceEditError(
            "unsafe_backup_path",
            "workspace backup directory is not safe",
        ) from exc
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise WorkspaceEditError(
            "unsafe_backup_path",
            "workspace backup directory is not safe",
        ) from exc
