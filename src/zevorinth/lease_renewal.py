"""Bounded background renewal for an owned SQLite execution lease."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from .event_store import ClaimLost, EventStore, LeaseRenewalError


class LeaseRenewer:
    """Renew one generation while the caller can still prove ownership."""

    def __init__(
        self,
        store: EventStore,
        *,
        task_id: str,
        lease_token: str,
        claim_generation: int,
        clock: Callable[[], str],
        lease_seconds: float,
        interval_seconds: float,
        max_duration_seconds: float,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
        self.store = store
        self.task_id = task_id
        self.lease_token = lease_token
        self.claim_generation = claim_generation
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self.max_duration_seconds = max_duration_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._failure: LeaseRenewalError | None = None
        self._failure_lock = threading.Lock()
        self.failed_event = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("lease renewer is already started")
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run,
            name=f"zevorinth-lease-{self.task_id}",
            daemon=True,
        )
        self._thread.start()

    def _set_failure(
        self,
        code: str,
        message: str,
    ) -> LeaseRenewalError:
        failure = LeaseRenewalError(code, message)
        with self._failure_lock:
            if self._failure is None:
                self._failure = failure
        self._stop.set()
        self.failed_event.set()
        return failure

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.interval_seconds):
                if (
                    self._started_at is not None
                    and time.monotonic() - self._started_at
                    >= self.max_duration_seconds
                ):
                    self._set_failure(
                        "lease_renewal_limit_exceeded",
                        "bounded execution lease renewal window expired",
                    )
                    return
                now = self._normalize_time(self.clock())
                expires_at = (
                    datetime.fromisoformat(now)
                    + timedelta(seconds=self.lease_seconds)
                ).isoformat(timespec="milliseconds")
                try:
                    self.store.renew_lease(
                        self.task_id,
                        lease_token=self.lease_token,
                        claim_generation=self.claim_generation,
                        now=now,
                        expires_at=expires_at,
                    )
                except ClaimLost as exc:
                    self._set_failure(
                        "lease_lost",
                        "lease authority changed before renewal completed",
                    )
                    return
                except LeaseRenewalError as exc:
                    self._set_failure(exc.code, str(exc))
                    return
                except Exception as exc:
                    self._set_failure(
                        "lease_renewal_failed",
                        f"lease renewal failed with {type(exc).__name__}",
                    )
                    return
        except Exception as exc:
            self._set_failure(
                "lease_renewal_worker_failed",
                f"lease renewal worker failed with {type(exc).__name__}",
            )

    @staticmethod
    def _normalize_time(value: str) -> str:
        current = datetime.fromisoformat(value)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.isoformat(timespec="milliseconds")

    def ensure_healthy(self) -> None:
        if (
            self._started_at is not None
            and time.monotonic() - self._started_at
            >= self.max_duration_seconds
        ):
            raise self._set_failure(
                "lease_renewal_limit_exceeded",
                "bounded execution lease renewal window expired",
            )
        with self._failure_lock:
            failure = self._failure
        if failure is not None:
            raise failure
        if (
            self._thread is not None
            and not self._thread.is_alive()
            and not self._stop.is_set()
        ):
            raise self._set_failure(
                "lease_renewal_worker_failed",
                "lease renewal worker stopped unexpectedly",
            )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
