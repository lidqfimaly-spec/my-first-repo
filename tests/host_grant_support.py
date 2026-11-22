"""Shared test support: real single-use host execution grants.

Host test execution is no longer authorized by a standing token, so every
test that actually runs workspace code on the host must issue a grant, hand
the store to the service, and pass the token with the request.  These helpers
keep that boilerplate out of the individual assertions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zevorinth.host_authorization import HostGrantStore
from zevorinth.service import TaskService


def grant_for(
    root: Path,
    *,
    workspace: str,
    target: str,
    issuer: str = "pytest",
    ttl_minutes: int = 30,
) -> tuple[HostGrantStore, str]:
    """Issue one grant beside ``root`` and return its store and token."""
    store = HostGrantStore(root / "host-grants")
    token = store.issue(
        workspace=workspace,
        target=target,
        issuer=issuer,
        ttl_minutes=ttl_minutes,
    )["token"]
    return store, token


def authorized_service(
    root: Path,
    *,
    workspace: str,
    target: str,
    issuer: str = "pytest",
    **service_kwargs: Any,
) -> tuple[TaskService, HostGrantStore, str]:
    """Return ``(service, store, token)`` wired to a fresh grant."""
    store, token = grant_for(
        root, workspace=workspace, target=target, issuer=issuer
    )
    service = TaskService(root, host_grant_store=store, **service_kwargs)
    return service, store, token
