"""Path-safe artifact creation and explicit simulation support."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import os
import stat
import time
from pathlib import Path
from typing import Callable, Iterator, Protocol
from uuid import UUID

from .models import (
    ArtifactEvidence,
    ExecutionMode,
    ToolResult,
)


MAX_ARTIFACT_BYTES = 64 * 1024
_FORBIDDEN_FILENAME_CHARS = frozenset('/\\<>:"|?*')
AuthorityCheck = Callable[[str, int], None]
CheckpointHook = Callable[[str, str, str, int | None], None]


class ArtifactToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ArtifactWriter(Protocol):
    tool_name: str
    execution_mode: ExecutionMode

    def write(
        self,
        *,
        task_id: str,
        attempt_id: str,
        artifact_path: str,
        content: str,
        started_at: str,
        finished_at: str,
        lease_token: str | None = None,
        claim_generation: int | None = None,
        authorize: AuthorityCheck | None = None,
    ) -> ToolResult: ...


def validate_artifact_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ArtifactToolError("invalid_artifact_path", "artifact_path must be a non-empty string")
    if value != value.strip():
        raise ArtifactToolError("invalid_artifact_path", "artifact_path cannot start or end with whitespace")
    if value in {".", ".."} or value.startswith("."):
        raise ArtifactToolError("invalid_artifact_path", "hidden or relative artifact names are not allowed")
    if any(char in _FORBIDDEN_FILENAME_CHARS for char in value):
        raise ArtifactToolError("invalid_artifact_path", "artifact_path contains a forbidden character")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ArtifactToolError("invalid_artifact_path", "artifact_path contains a control character")
    if len(value.encode("utf-8")) > 255:
        raise ArtifactToolError("invalid_artifact_path", "artifact_path is longer than 255 UTF-8 bytes")
    if Path(value).name != value:
        raise ArtifactToolError("invalid_artifact_path", "artifact_path must be a single file name")
    return value


def _validated_task_id(task_id: str) -> str:
    try:
        parsed = UUID(task_id)
    except (ValueError, AttributeError) as exc:
        raise ArtifactToolError("invalid_task_id", "task_id is not a valid UUID") from exc
    if str(parsed) != task_id:
        raise ArtifactToolError("invalid_task_id", "task_id must use canonical UUID form")
    return task_id


def _validated_attempt_id(attempt_id: str) -> str:
    try:
        parsed = UUID(attempt_id)
    except (ValueError, AttributeError) as exc:
        raise ArtifactToolError(
            "invalid_attempt_id",
            "attempt_id is not a valid UUID",
        ) from exc
    if str(parsed) != attempt_id:
        raise ArtifactToolError(
            "invalid_attempt_id",
            "attempt_id must use canonical UUID form",
        )
    return attempt_id


def _ensure_task_directory(root: Path, task_id: str, *, create: bool) -> Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ArtifactToolError("unsafe_artifacts_root", "artifacts root must not be a symlink")
    root = root.resolve(strict=True)
    task_dir = root / _validated_task_id(task_id)
    if task_dir.exists() or task_dir.is_symlink():
        resolved = task_dir.resolve(strict=True)
        if resolved.parent != root:
            raise ArtifactToolError("path_escape", "task artifact directory escapes artifacts root")
        if not task_dir.is_dir() or task_dir.is_symlink():
            raise ArtifactToolError("unsafe_artifact_directory", "task artifact path is not a safe directory")
    elif create:
        try:
            task_dir.mkdir(mode=0o700)
        except FileExistsError:
            return _ensure_task_directory(root, task_id, create=False)
    elif not task_dir.exists():
        raise FileNotFoundError(errno.ENOENT, "task artifact directory does not exist", task_dir)
    return task_dir


def _ensure_staging_directory(task_dir: Path) -> Path:
    staging_dir = task_dir / ".staging"
    if staging_dir.exists() or staging_dir.is_symlink():
        if staging_dir.is_symlink() or not staging_dir.is_dir():
            raise ArtifactToolError(
                "unsafe_staging_directory",
                "artifact staging path is not a safe directory",
            )
        resolved = staging_dir.resolve(strict=True)
        if resolved.parent != task_dir.resolve(strict=True):
            raise ArtifactToolError(
                "path_escape",
                "artifact staging directory escapes task directory",
            )
    else:
        try:
            staging_dir.mkdir(mode=0o700)
        except FileExistsError:
            return _ensure_staging_directory(task_dir)
    return staging_dir


def _ensure_locks_directory(artifacts_root: Path) -> Path:
    artifacts_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if artifacts_root.is_symlink():
        raise ArtifactToolError(
            "unsafe_artifacts_root",
            "artifacts root must not be a symlink",
        )
    root = artifacts_root.resolve(strict=True)
    locks_dir = root / ".locks"
    if locks_dir.exists() or locks_dir.is_symlink():
        if locks_dir.is_symlink() or not locks_dir.is_dir():
            raise ArtifactToolError(
                "unsafe_lock_directory",
                "artifact lock path is not a safe directory",
            )
        if locks_dir.resolve(strict=True).parent != root:
            raise ArtifactToolError(
                "path_escape",
                "artifact lock directory escapes artifacts root",
            )
    else:
        try:
            locks_dir.mkdir(mode=0o700)
        except FileExistsError:
            return _ensure_locks_directory(root)
    return locks_dir


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES}:
            raise ArtifactToolError("unsafe_artifact_directory", "task artifact directory is not safe") from exc
        raise


def _read_artifact_from_dir(directory_fd: int, name: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(errno.ENOENT, "artifact does not exist", name) from exc
        if exc.errno in {errno.ELOOP, errno.EACCES}:
            raise ArtifactToolError("unsafe_artifact_file", "artifact is not a safe regular file") from exc
        raise
    try:
        stat_result = os.fstat(file_fd)
        if not stat.S_ISREG(stat_result.st_mode):
            raise ArtifactToolError("unsafe_artifact_file", "artifact is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 16384)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(file_fd)


def read_artifact(artifacts_root: Path, task_id: str, artifact_name: str) -> bytes:
    validate_artifact_name(artifact_name)
    task_dir = _ensure_task_directory(artifacts_root, task_id, create=False)
    directory_fd = _open_directory(task_dir)
    try:
        return _read_artifact_from_dir(directory_fd, artifact_name)
    finally:
        os.close(directory_fd)


def read_artifact_text(
    artifacts_root: Path,
    task_id: str,
    artifact_name: str,
    *,
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> tuple[str, bytes, str]:
    """Read one bounded UTF-8 artifact and return text, bytes and digest."""
    raw = read_artifact(artifacts_root, task_id, artifact_name)
    if len(raw) > max_bytes:
        raise ArtifactToolError(
            "content_too_large",
            f"artifact exceeds the {max_bytes} byte limit",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactToolError(
            "invalid_artifact_encoding",
            "artifact is not valid UTF-8 text",
        ) from exc
    return text, raw, hashlib.sha256(raw).hexdigest()


def _fsync_directory(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError:
        # Some filesystems do not support fsync on directory descriptors.
        pass


def _acquire_publish_lock(lock_fd: int, timeout_seconds: float) -> None:
    """Acquire the per-task publish lock without waiting indefinitely."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise ArtifactToolError(
                    "publish_lock_timeout",
                    "timed out waiting for the artifact publish lock",
                )
            time.sleep(0.01)


def _open_task_publish_lock(
    artifacts_root: Path,
    task_id: str,
) -> tuple[int, int]:
    locks_dir = _ensure_locks_directory(artifacts_root)
    directory_fd = _open_directory(locks_dir)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(
            f"{_validated_task_id(task_id)}.lock",
            flags,
            0o600,
            dir_fd=directory_fd,
        )
    except Exception:
        os.close(directory_fd)
        raise
    return directory_fd, lock_fd


@contextmanager
def task_publication_lock(
    artifacts_root: Path,
    task_id: str,
    *,
    timeout_seconds: float = 10.0,
) -> Iterator[None]:
    """Coordinate one task's lease takeover and final artifact publication."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    directory_fd, lock_fd = _open_task_publish_lock(
        artifacts_root,
        task_id,
    )
    try:
        _acquire_publish_lock(lock_fd, timeout_seconds)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
            os.close(directory_fd)


class RealArtifactWriter:
    """Publish immutable staged content with single-host authority checks.

    The writer can only reduce stale-writer risk; it cannot make SQLite and
    the filesystem atomic. The final hard link is never overwritten and is
    created only after the caller's authority callback succeeds under the
    same per-task lock used by all publishers.
    """

    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.REAL

    def __init__(
        self,
        artifacts_root: Path,
        *,
        publish_lock_timeout_seconds: float = 10.0,
        checkpoint_hook: CheckpointHook | None = None,
    ) -> None:
        if publish_lock_timeout_seconds <= 0:
            raise ValueError("publish_lock_timeout_seconds must be positive")
        self.artifacts_root = artifacts_root.absolute()
        self.publish_lock_timeout_seconds = publish_lock_timeout_seconds
        self.checkpoint_hook = checkpoint_hook

    def _checkpoint(
        self,
        stage: str,
        task_id: str,
        attempt_id: str,
        claim_generation: int | None,
    ) -> None:
        if self.checkpoint_hook is not None:
            self.checkpoint_hook(
                stage,
                task_id,
                attempt_id,
                claim_generation,
            )

    def write(
        self,
        *,
        task_id: str,
        attempt_id: str,
        artifact_path: str,
        content: str,
        started_at: str,
        finished_at: str,
        lease_token: str | None = None,
        claim_generation: int | None = None,
        authorize: AuthorityCheck | None = None,
    ) -> ToolResult:
        """Stage, authorize, and atomically publish one artifact."""
        name = validate_artifact_name(artifact_path)
        attempt_id = _validated_attempt_id(attempt_id)
        if not isinstance(content, str):
            raise ArtifactToolError("invalid_content", "content must be text")
        if not content:
            raise ArtifactToolError("empty_content", "content must not be empty")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise ArtifactToolError("content_too_large", "content exceeds the 64 KiB limit")
        if authorize is not None and (
            lease_token is None or claim_generation is None
        ):
            raise ArtifactToolError(
                "authority_context_missing",
                "authorized publication requires lease token and generation",
            )

        task_dir = _ensure_task_directory(self.artifacts_root, task_id, create=True)
        staging_dir = _ensure_staging_directory(task_dir)
        directory_fd = _open_directory(task_dir)
        staging_fd: int | None = None
        staging_name = f"{attempt_id}.part"
        file_fd: int | None = None
        lock_dir_fd: int | None = None
        lock_fd: int | None = None
        published = False
        try:
            staging_fd = _open_directory(staging_dir)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(staging_name, flags, 0o600, dir_fd=staging_fd)
            view = memoryview(encoded)
            while view:
                written = os.write(file_fd, view)
                view = view[written:]
            self._checkpoint(
                "after_staging_write",
                task_id,
                attempt_id,
                claim_generation,
            )
            os.fsync(file_fd)
            os.close(file_fd)
            file_fd = None
            _fsync_directory(staging_fd)

            lock_dir_fd, lock_fd = _open_task_publish_lock(
                self.artifacts_root,
                task_id,
            )
            _acquire_publish_lock(
                lock_fd,
                self.publish_lock_timeout_seconds,
            )
            if authorize is not None:
                authorize(lease_token, claim_generation)
            self._checkpoint(
                "after_publish_authority",
                task_id,
                attempt_id,
                claim_generation,
            )
            try:
                os.link(
                    staging_name,
                    name,
                    src_dir_fd=staging_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise ArtifactToolError(
                    "artifact_exists",
                    "refusing to overwrite an existing artifact",
                ) from exc
            published = True
            _fsync_directory(directory_fd)
            try:
                os.unlink(staging_name, dir_fd=staging_fd)
            except FileNotFoundError:
                pass
            self._checkpoint(
                "after_publish",
                task_id,
                attempt_id,
                claim_generation,
            )
            actual = _read_artifact_from_dir(directory_fd, name)
            actual_sha = hashlib.sha256(actual).hexdigest()
            evidence = ArtifactEvidence(
                relative_path=name,
                byte_count=len(actual),
                sha256=actual_sha,
                attempt_id=attempt_id,
                claim_generation=claim_generation,
            )
            return ToolResult(
                attempt_id=attempt_id,
                tool_name=self.tool_name,
                execution_mode=ExecutionMode.REAL,
                success=True,
                started_at=started_at,
                finished_at=finished_at,
                artifact=evidence,
            )
        except ArtifactToolError:
            raise
        except (OSError, UnicodeError) as exc:
            raise ArtifactToolError("filesystem_error", f"artifact write failed: {exc}") from exc
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if not published and staging_fd is not None:
                try:
                    os.unlink(staging_name, dir_fd=staging_fd)
                except FileNotFoundError:
                    pass
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            if lock_dir_fd is not None:
                os.close(lock_dir_fd)
            if staging_fd is not None:
                os.close(staging_fd)
            os.close(directory_fd)


class SimulatedArtifactWriter:
    """Test/demo double that never creates a real artifact."""

    tool_name = "write_text_artifact"
    execution_mode = ExecutionMode.SIMULATED

    def write(
        self,
        *,
        task_id: str,
        attempt_id: str,
        artifact_path: str,
        content: str,
        started_at: str,
        finished_at: str,
        lease_token: str | None = None,
        claim_generation: int | None = None,
        authorize: AuthorityCheck | None = None,
    ) -> ToolResult:
        return ToolResult(
            attempt_id=attempt_id,
            tool_name=self.tool_name,
            execution_mode=ExecutionMode.SIMULATED,
            success=True,
            started_at=started_at,
            finished_at=finished_at,
            artifact=None,
        )
