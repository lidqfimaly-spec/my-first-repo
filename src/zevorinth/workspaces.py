"""Registered workspaces: list and create them without weakening the fence.

A workspace is the only place a task may read or write. Until now the fence
existed in ``local_test_runner.resolve_workspace`` but there was no way to
enumerate or create a workspace: the user had to ``mkdir`` and know the name.

Two rules this module keeps:

1. **One parser, not two.** Creation validates the name with the same
   ``WORKSPACE_PATTERN`` and finishes by calling the same ``resolve_workspace``
   the runner uses. If creation succeeds, the runner will resolve it; there is
   no second path that could disagree.
2. **Creating a workspace is not permission to execute.** This module never
   touches ``HostGrantStore``. A workspace you created still needs a one-time
   host execution grant before any test runs in it. Being able to make a
   directory and being allowed to run untrusted code in it are different
   decisions, made by different code paths and different actors.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any

from .local_test_runner import WORKSPACE_PATTERN, TestExecutionError, resolve_workspace

#: How many entries a listing will stat before giving up on an exact count.
MAX_SCANNED_ENTRIES = 5000
MAX_LISTED_WORKSPACES = 200
MAX_NAME_LENGTH = 64


class WorkspaceError(RuntimeError):
    """A workspace request that cannot be honoured, with a stable code."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def workspace_root(project_root: Path) -> Path:
    return Path(project_root) / "workspaces"


def _relative(name: str) -> str:
    return f"workspaces/{name}"


def _validate_name(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise WorkspaceError(
            "invalid_workspace_name",
            "workspace name is required",
        )
    if len(name) > MAX_NAME_LENGTH or not WORKSPACE_PATTERN.fullmatch(name):
        raise WorkspaceError(
            "invalid_workspace_name",
            "workspace name must match ^[A-Za-z0-9_-]{1,64}$",
        )
    return name


def _summarize(root: Path, name: str) -> dict[str, Any]:
    """Describe one workspace without following links out of the root."""
    path = root / name
    entries = 0
    files = 0
    total_bytes = 0
    newest = 0.0
    test_files = 0
    truncated = False
    try:
        for current, dirnames, filenames in os.walk(path, followlinks=False):
            # A symlinked directory is reported as an entry, never descended.
            dirnames[:] = [
                item
                for item in dirnames
                if not (Path(current) / item).is_symlink()
            ]
            for filename in filenames:
                entries += 1
                if filename.startswith("test_") and filename.endswith(".py"):
                    test_files += 1
                try:
                    stat = (Path(current) / filename).lstat()
                except OSError:
                    continue
                files += 1
                total_bytes += stat.st_size
                newest = max(newest, stat.st_mtime)
            if entries >= MAX_SCANNED_ENTRIES:
                truncated = True
                break
    except OSError:
        pass
    return {
        "name": name,
        "relative_path": _relative(name),
        "file_count": files,
        "entry_count": entries,
        "size_bytes": total_bytes,
        "last_modified": (
            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(newest))
            if newest
            else None
        ),
        "has_test_files": test_files > 0,
        "scan_truncated": truncated,
    }


def list_workspaces(project_root: Path) -> dict[str, Any]:
    """List registered workspaces. Read-only: never creates anything."""
    root = workspace_root(project_root)
    workspaces: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    if not root.is_dir():
        return {
            "root_relative_path": "workspaces",
            "root_exists": False,
            "workspaces": [],
            "skipped": [],
        }
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        raise WorkspaceError(
            "workspace_root_unreadable",
            f"workspace root could not be read: {type(exc).__name__}",
            status=500,
        ) from exc
    for name in names:
        if len(workspaces) >= MAX_LISTED_WORKSPACES:
            break
        path = root / name
        if path.is_symlink():
            # Present it, but as something the runner will refuse.
            skipped.append({"name": name, "reason": "symlink"})
            continue
        if not path.is_dir():
            skipped.append({"name": name, "reason": "not_a_directory"})
            continue
        if not WORKSPACE_PATTERN.fullmatch(name):
            skipped.append({"name": name, "reason": "invalid_name"})
            continue
        workspaces.append(_summarize(root, name))
    return {
        "root_relative_path": "workspaces",
        "root_exists": True,
        "workspaces": workspaces,
        "skipped": skipped,
    }


def _identity(path: Path) -> os.stat_result:
    return os.lstat(path)


def _same_object(path: Path, identity: os.stat_result) -> bool:
    """True when ``path`` still names the very inode we created.

    Name equality is not enough: between the ``mkdir`` and the check, another
    process can remove our directory and put a different one at the same path.
    """
    try:
        current = os.stat(path)
    except OSError:
        return False
    return (current.st_dev, current.st_ino) == (
        identity.st_dev,
        identity.st_ino,
    )


def _rollback_created(candidate: Path, identity: os.stat_result) -> str:
    """Remove only the directory this request created, and only if it is ours.

    Deliberately ``os.rmdir``, never ``shutil.rmtree``: a recursive delete on a
    path is a delete on whatever occupies that path *now*. ``rmdir`` refuses a
    non-empty directory, and the inode check refuses anything we did not create.
    """
    try:
        current = os.lstat(candidate)
    except OSError:
        return "already_gone"
    if stat.S_ISLNK(current.st_mode):
        return "left_in_place_replaced_by_symlink"
    if (current.st_dev, current.st_ino) != (
        identity.st_dev,
        identity.st_ino,
    ):
        return "left_in_place_replaced_by_another_object"
    try:
        os.rmdir(candidate)
    except OSError as exc:
        return f"left_in_place_rmdir_failed:{type(exc).__name__}"
    return "removed"


def create_workspace(project_root: Path, name: Any) -> dict[str, Any]:
    """Create one empty workspace inside the root the runner resolves against."""
    name = _validate_name(name)
    root = workspace_root(project_root)
    if root.is_symlink():
        raise WorkspaceError(
            "unsafe_workspace_root",
            "workspace root must not be a symlink",
            status=409,
        )
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError:
        if not root.is_dir():
            raise WorkspaceError(
                "workspace_root_unavailable",
                "workspace root could not be created",
                status=500,
            )
    candidate = root / name
    if candidate.is_symlink():
        raise WorkspaceError(
            "unsafe_workspace",
            "workspace must not be a symlink",
            status=409,
        )
    try:
        # ``os.mkdir`` without exist_ok is the atomic "claim the name" step:
        # two concurrent creations cannot both believe they won.
        os.mkdir(candidate, 0o700)
    except FileExistsError as exc:
        if not candidate.is_dir():
            raise WorkspaceError(
                "workspace_already_exists",
                "a file with that name already exists",
                status=409,
            ) from exc
        raise WorkspaceError(
            "workspace_already_exists",
            f"workspace already exists: {name}",
            status=409,
        ) from exc
    except OSError as exc:
        raise WorkspaceError(
            "workspace_creation_failed",
            f"workspace could not be created: {type(exc).__name__}",
            status=500,
        ) from exc
    created = _identity(candidate)
    # Verify with the parser the runner will use. If these two ever disagree,
    # the creation is rolled back instead of leaving a half-registered folder.
    try:
        resolved = resolve_workspace(Path(project_root), name)
    except TestExecutionError as exc:
        outcome = _rollback_created(candidate, created)
        raise WorkspaceError(
            "unsafe_workspace",
            f"created workspace would not resolve: {exc.code}"
            f" (rollback: {outcome})",
            status=409,
        ) from exc
    if not _same_object(resolved, created):
        outcome = _rollback_created(candidate, created)
        raise WorkspaceError(
            "unsafe_workspace",
            "workspace was replaced by a different object while being created"
            f" (rollback: {outcome})",
            status=409,
        )
    return {
        "name": name,
        "relative_path": _relative(name),
        "absolute_path": str(resolved),
        "exists": resolved.is_dir(),
        "is_symlink": candidate.is_symlink(),
        "resolves_inside_workspace_root": (
            resolved == workspace_root(project_root).resolve() / name
        ),
        "created": True,
        "execution_authorized": False,
    }
