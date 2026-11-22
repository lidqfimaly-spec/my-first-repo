"""Run fixed, isolated mutation checks without third-party dependencies."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_OS_OPEN = os.open
_OS_DUP = os.dup
_OS_CLOSE = os.close
DEFAULT_CASES = PROJECT_ROOT / "tests" / "mutation_cases.json"
PROBE_NAME = "_mutation_probe.py"
PROBE_SOURCE = '''\
"""Trace executed lines while running unittest, then report them."""
import atexit
import json
import os
import sys
import threading
import unittest

_trace_file = os.environ["MUTATION_TRACE_FILE"]
_seen = set()


def _tracer(frame, event, _arg):
    if event == "line":
        _seen.add("%s:%d" % (frame.f_code.co_filename, frame.f_lineno))
    return _tracer


def _dump():
    with open(_trace_file, "w", encoding="utf-8") as handle:
        json.dump(sorted(_seen), handle)


atexit.register(_dump)
sys.settrace(_tracer)
threading.settrace(_tracer)

_program = unittest.main(
    module=None,
    argv=["probe", *sys.argv[1:]],
    exit=False,
)
sys.exit(0 if _program.result.wasSuccessful() else 1)
'''


class UnsafePatchError(ValueError):
    pass


@dataclass(frozen=True)
class _OwnedFDRecord:
    identity: tuple[int, int]
    anchor_fd: int


class _OwnedFDCollection(list[int]):
    """List of unexposed FD handles with private ownership anchors."""

    def __init__(self, values: Iterable[int] = ()) -> None:
        super().__init__(values)
        self._records: dict[int, _OwnedFDRecord] = {}
        self._unresolved_anchor_states: dict[int, str] = {}
        self._retirement_lock = threading.RLock()

    def register(self, file_fd: int) -> None:
        with self._retirement_lock:
            with _defer_keyboard_interrupt():
                metadata = os.fstat(file_fd)
                anchor_fd = _OS_DUP(file_fd)
                try:
                    os.set_inheritable(anchor_fd, False)
                except BaseException:
                    _OS_CLOSE(anchor_fd)
                    raise
                identity = (metadata.st_dev, metadata.st_ino)
                self._records[file_fd] = _OwnedFDRecord(
                    identity=identity,
                    anchor_fd=anchor_fd,
                )
                self.append(file_fd)

    def record(self, file_fd: int) -> _OwnedFDRecord | None:
        return self._records.get(file_fd)

    def take_record(self, file_fd: int) -> _OwnedFDRecord | None:
        return self._records.pop(file_fd, None)

    def abandon_anchor(self, file_fd: int) -> None:
        with self._retirement_lock:
            self._abandon_anchor_locked(file_fd)

    def _abandon_anchor_locked(self, file_fd: int) -> None:
        record = self.record(file_fd)
        if record is None:
            raise UnsafePatchError(
                f"FD {file_fd} has no ownership anchor to abandon"
            )
        anchor_state, anchor_error = _observe_fd_identity_state(
            record.anchor_fd,
            record.identity,
        )
        if anchor_error is not None or anchor_state != "OPEN":
            error = UnsafePatchError(
                f"FD {file_fd} ownership anchor could not be verified"
            )
            error.fd_close_states = {
                file_fd: "OPEN",
                record.anchor_fd: anchor_state,
            }
            error.unresolved_fds = dict(error.fd_close_states)
            if anchor_error is not None:
                raise error from anchor_error
            raise error
        try:
            _acquire_ownership_lock(record.anchor_fd)
            fcntl.flock(
                file_fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BaseException as exc:
            error = UnsafePatchError(
                f"FD {file_fd} and ownership anchor could not be matched"
            )
            release_error = _release_ownership_lock(record.anchor_fd)
            if release_error is not None:
                _safe_add_note(
                    error,
                    "ownership lock release failed: "
                    f"{_safe_diagnostic_text(release_error)}",
                )
            error.fd_close_states = {
                file_fd: "UNKNOWN",
                record.anchor_fd: "UNKNOWN",
            }
            error.unresolved_fds = dict(error.fd_close_states)
            raise error from exc
        release_error = _release_ownership_lock(record.anchor_fd)
        if release_error is not None:
            error = UnsafePatchError(
                f"FD {file_fd} ownership lock release failed"
            )
            error.fd_close_states = {
                file_fd: "OPEN",
                record.anchor_fd: "UNKNOWN",
            }
            error.unresolved_fds = dict(error.fd_close_states)
            raise error from release_error
        try:
            _OS_CLOSE(record.anchor_fd)
        except BaseException as exc:
            state, observation_error = _observe_fd_close_state(
                record.anchor_fd,
                record.identity,
            )
            error = UnsafePatchError(
                f"FD {file_fd} ownership anchor close failed"
            )
            error.fd_close_states = {
                file_fd: "OPEN",
                record.anchor_fd: state,
            }
            error.unresolved_fds = dict(error.fd_close_states)
            if observation_error is not None:
                _safe_add_note(error, str(observation_error))
            raise error from exc
        self.take_record(file_fd)
        self.remove(file_fd)

    def adopt(
        self,
        source: "_OwnedFDCollection",
        file_fd: int,
    ) -> None:
        with source._retirement_lock, self._retirement_lock:
            record = source.take_record(file_fd)
            if record is None:
                raise UnsafePatchError(
                    f"FD {file_fd} has no transferable ownership record"
                )
            source.remove(file_fd)
            self._records[file_fd] = record
            self.append(file_fd)

    def close_all_anchors(self) -> None:
        with self._retirement_lock:
            for file_fd, record in list(self._records.items()):
                anchor_state, _ = _observe_fd_identity_state(
                    record.anchor_fd,
                    record.identity,
                )
                if anchor_state != "OPEN":
                    self._unresolved_anchor_states[
                        record.anchor_fd
                    ] = anchor_state
                    continue
                try:
                    _acquire_ownership_lock(record.anchor_fd)
                    fcntl.flock(
                        file_fd,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    fcntl.flock(
                        record.anchor_fd,
                        fcntl.LOCK_UN,
                    )
                except BaseException:
                    _release_ownership_lock(record.anchor_fd)
                    self._unresolved_anchor_states[
                        record.anchor_fd
                    ] = "UNKNOWN"
                    continue
                try:
                    _OS_CLOSE(record.anchor_fd)
                except BaseException:
                    state, _ = _observe_fd_close_state(
                        record.anchor_fd,
                        record.identity,
                    )
                    if state != "CLOSED":
                        self._unresolved_anchor_states[
                            record.anchor_fd
                        ] = state
                        continue
                self._records.pop(file_fd, None)

@dataclass
class CleanupReport:
    residual_paths: list[str]
    diagnostics: list[str]
    unknown: bool = False


def _safe_diagnostic_text(value: Any) -> str:
    try:
        text = str(value)
    except BaseException:
        return "<unavailable diagnostic value>"
    return text.encode(
        "utf-8",
        "backslashreplace",
    ).decode("utf-8")


def _safe_add_note(error: BaseException, note: str) -> None:
    try:
        error.add_note(note)
        return
    except BaseException:
        pass
    try:
        notes = list(getattr(error, "diagnostic_notes", []))
        notes.append(note)
        error.diagnostic_notes = notes
    except BaseException:
        pass


def _append_close_diagnostics(
    error: BaseException,
    diagnostics: list[str],
) -> None:
    try:
        existing = list(
            getattr(error, "close_diagnostics", []) or []
        )
    except BaseException:
        existing = []
    merged = list(dict.fromkeys(
        [_safe_diagnostic_text(item) for item in existing]
        + [_safe_diagnostic_text(item) for item in diagnostics]
    ))
    if not merged:
        return
    try:
        error.close_diagnostics = merged
    except BaseException:
        pass


def _observe_fd_close_state(
    file_fd: int,
    expected_identity: tuple[int, int] | None,
) -> tuple[str, BaseException | None]:
    try:
        metadata = os.fstat(file_fd)
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return "CLOSED", None
        return "UNKNOWN", exc
    except BaseException as exc:
        return "UNKNOWN", exc
    if expected_identity is None:
        return "UNKNOWN", None
    if (metadata.st_dev, metadata.st_ino) == expected_identity:
        # Device/inode identity describes a filesystem object, not the open
        # file description that originally occupied this numeric FD.
        return "UNKNOWN", None
    return "REUSED_OR_REPLACED", None


def _observe_fd_identity_state(
    file_fd: int,
    expected_identity: tuple[int, int],
) -> tuple[str, BaseException | None]:
    """Observe only whether the numeric FD currently names the expected object."""
    try:
        metadata = os.fstat(file_fd)
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return "CLOSED", None
        return "UNKNOWN", exc
    except BaseException as exc:
        return "UNKNOWN", exc
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        return "REUSED_OR_REPLACED", None
    return "OPEN", None


def _attach_fd_close_states(
    error: BaseException,
    close_states: dict[int, str],
) -> None:
    if not close_states:
        return
    try:
        existing = dict(getattr(error, "fd_close_states", {}))
    except BaseException:
        existing = {}
    existing.update(close_states)
    try:
        error.fd_close_states = existing
    except BaseException:
        pass
    unresolved = {
        file_fd: state
        for file_fd, state in existing.items()
        if state in {"OPEN", "UNKNOWN", "REUSED_OR_REPLACED"}
    }
    if unresolved:
        try:
            error.unresolved_fds = unresolved
        except BaseException:
            pass


def _copy_signal_metadata(
    source: BaseException,
    target: BaseException,
) -> None:
    for attribute in ("signal_mask_state", "secondary_exceptions"):
        try:
            value = getattr(source, attribute)
        except BaseException:
            continue
        try:
            setattr(target, attribute, value)
        except BaseException:
            pass


def _close_owner_anchor(
    anchor_fd: int,
    expected_identity: tuple[int, int],
    close_states: dict[int, str],
    observation_failures: list[tuple[str, BaseException]],
    label: str,
) -> bool:
    state, observation_error = _observe_fd_identity_state(
        anchor_fd,
        expected_identity,
    )
    if state != "OPEN":
        close_states[anchor_fd] = state
    if observation_error is not None:
        observation_failures.append((label, observation_error))
    if state != "OPEN":
        observation_failures.append(
            (
                label,
                UnsafePatchError(
                    "ownership anchor identity could not be verified; "
                    "numeric FD retained"
                ),
            )
        )
        return False
    release_error = _release_ownership_lock(anchor_fd)
    if release_error is not None:
        close_states[anchor_fd] = "UNKNOWN"
        error = UnsafePatchError(
            "ownership anchor lock release failed; "
            "numeric FD retained"
        )
        error.__cause__ = release_error
        observation_failures.append((label, error))
        return False
    try:
        _OS_CLOSE(anchor_fd)
    except BaseException as close_error:
        state, observation_error = _observe_fd_close_state(
            anchor_fd,
            expected_identity,
        )
        identity_state, identity_error = _observe_fd_identity_state(
            anchor_fd,
            expected_identity,
        )
        if identity_state == "OPEN":
            release_error = _release_ownership_lock(anchor_fd)
            if release_error is not None:
                observation_failures.append(
                    (label, release_error)
                )
        close_states[anchor_fd] = state
        observation_failures.append(
            (
                label,
                UnsafePatchError(
                    "ownership anchor close failed: "
                    f"{close_error}"
                ),
            )
        )
        if observation_error is not None:
            observation_failures.append(
                (label, observation_error)
            )
        if identity_error is not None:
            observation_failures.append(
                (label, identity_error)
            )
        return False
    return True


def _acquire_ownership_lock(anchor_fd: int) -> None:
    for attempt in range(20):
        try:
            fcntl.flock(
                anchor_fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            return
        except BlockingIOError:
            if attempt == 19:
                raise
            time.sleep(0.001)


def _release_ownership_lock(anchor_fd: int) -> BaseException | None:
    try:
        fcntl.flock(anchor_fd, fcntl.LOCK_UN)
    except BaseException as exc:
        return exc
    return None


def _close_fd_preserving_primary(
    file_fd: int,
    label: str,
    *,
    residual_paths: list[str] | None = None,
) -> None:
    _close_fds_preserving_primary(
        [(file_fd, label, residual_paths)]
    )


def _close_registered_fd(
    opened_fds: list[int],
    file_fd: int,
    label: str,
    *,
    residual_paths: list[str] | None = None,
) -> None:
    if isinstance(opened_fds, _OwnedFDCollection):
        with opened_fds._retirement_lock:
            return _close_registered_fd_locked(
                opened_fds,
                file_fd,
                label,
                residual_paths=residual_paths,
            )
    return _close_registered_fd_locked(
        opened_fds,
        file_fd,
        label,
        residual_paths=residual_paths,
    )


def _close_registered_fd_locked(
    opened_fds: list[int],
    file_fd: int,
    label: str,
    *,
    residual_paths: list[str] | None = None,
) -> None:
    close_states: dict[int, str] = {}
    ownership_records = {}
    if isinstance(opened_fds, _OwnedFDCollection):
        record = opened_fds.record(file_fd)
        if record is not None:
            ownership_records[file_fd] = record
    try:
        _close_fds_preserving_primary(
            [(file_fd, label, residual_paths)],
            close_states=close_states,
            ownership_records=ownership_records,
        )
    except BaseException:
        raise
    else:
        if (
            isinstance(opened_fds, _OwnedFDCollection)
            and close_states.get(file_fd) == "CLOSED"
        ):
            opened_fds.take_record(file_fd)
    finally:
        try:
            opened_fds.remove(file_fd)
        except ValueError:
            pass


def _close_owned_fds(
    opened_fds: list[int],
    label: str,
    *,
    residual_paths: list[str] | None = None,
) -> list[tuple[str, BaseException]]:
    if isinstance(opened_fds, _OwnedFDCollection):
        with opened_fds._retirement_lock:
            return _close_owned_fds_locked(
                opened_fds,
                label,
                residual_paths=residual_paths,
            )
    return _close_owned_fds_locked(
        opened_fds,
        label,
        residual_paths=residual_paths,
    )


def _close_owned_fds_locked(
    opened_fds: list[int],
    label: str,
    *,
    residual_paths: list[str] | None = None,
) -> list[tuple[str, BaseException]]:
    close_states: dict[int, str] = {}
    ownership_records = {}
    if isinstance(opened_fds, _OwnedFDCollection):
        ownership_records = {
            file_fd: record
            for file_fd in opened_fds
            if (record := opened_fds.record(file_fd)) is not None
        }
    descriptors = [
        (file_fd, label, residual_paths)
        for file_fd in reversed(opened_fds)
    ]
    try:
        result = _close_fds_preserving_primary(
            descriptors,
            close_states=close_states,
            ownership_records=ownership_records,
        )
    except BaseException:
        raise
    else:
        if isinstance(opened_fds, _OwnedFDCollection):
            for file_fd, _label, _paths in descriptors:
                if close_states.get(file_fd) == "CLOSED":
                    opened_fds.take_record(file_fd)
        return result
    finally:
        for file_fd, _label, _paths in descriptors:
            try:
                opened_fds.remove(file_fd)
            except ValueError:
                pass


def _close_fds_preserving_primary(
    descriptors: list[
        tuple[int, str, list[str] | None]
    ],
    *,
    attempted_fds: set[int] | None = None,
    close_states: dict[int, str] | None = None,
    ownership_records: Mapping[int, _OwnedFDRecord] | None = None,
) -> list[tuple[str, BaseException]]:
    """Attempt every close, preserving any active primary exception."""
    primary = sys.exc_info()[1]
    failures: list[tuple[str, BaseException]] = []
    observation_failures: list[tuple[str, BaseException]] = []
    residual_paths: list[str] = []
    close_states = {} if close_states is None else close_states
    for file_fd, label, paths in descriptors:
        ownership = (
            None
            if ownership_records is None
            else ownership_records.get(file_fd)
        )
        if ownership is None:
            state = "UNKNOWN"
            close_states[file_fd] = state
            failures.append(
                (
                    label,
                    UnsafePatchError(
                        "numeric FD has no registered ownership record"
                    ),
                )
            )
            continue
        expected_identity = ownership.identity
        anchor_state, anchor_observation_error = (
            _observe_fd_identity_state(
                ownership.anchor_fd,
                expected_identity,
            )
        )
        if anchor_state != "OPEN":
            close_states[ownership.anchor_fd] = anchor_state
        if anchor_observation_error is not None:
            observation_failures.append(
                (label, anchor_observation_error)
            )
        if anchor_state != "OPEN":
            close_states[file_fd] = "UNKNOWN"
            failures.append(
                (
                    label,
                    UnsafePatchError(
                        "ownership anchor is not current; numeric FDs "
                        "retained"
                    ),
                )
            )
            continue
        try:
            observed = os.fstat(file_fd)
        except OSError as observation_error:
            if observation_error.errno == errno.EBADF:
                close_states[file_fd] = "CLOSED"
                close_states[ownership.anchor_fd] = "UNKNOWN"
                observation_failures.append(
                    (
                        label,
                        UnsafePatchError(
                            "primary FD is closed; ownership anchor "
                            "relationship cannot be verified and was retained"
                        ),
                    )
                )
                failures.append(
                    (
                        label,
                        UnsafePatchError(
                            "primary FD was already closed before ownership "
                            "verification"
                        ),
                    )
                )
                continue
            observation_failures.append((label, observation_error))
            close_states[file_fd] = "UNKNOWN"
            close_states[ownership.anchor_fd] = "UNKNOWN"
            failures.append(
                (
                    label,
                    UnsafePatchError(
                        "numeric FD identity could not be observed; "
                        "numeric FDs retained"
                    ),
                )
            )
            continue
        except BaseException as observation_error:
            observation_failures.append((label, observation_error))
            close_states[file_fd] = "UNKNOWN"
            close_states[ownership.anchor_fd] = "UNKNOWN"
            failures.append(
                (
                    label,
                    UnsafePatchError(
                        "numeric FD identity observation failed; "
                        "numeric FDs retained"
                    ),
                )
            )
            continue
        else:
            if (
                observed.st_dev,
                observed.st_ino,
            ) != ownership.identity:
                state = "REUSED_OR_REPLACED"
                close_states[file_fd] = state
                close_states[ownership.anchor_fd] = "UNKNOWN"
                failures.append(
                    (
                        label,
                        UnsafePatchError(
                            "numeric FD identity changed before close"
                        ),
                    )
                )
                continue
            try:
                _acquire_ownership_lock(ownership.anchor_fd)
            except BlockingIOError:
                state = "UNKNOWN"
                close_states[file_fd] = state
                close_states[ownership.anchor_fd] = "UNKNOWN"
                failures.append(
                    (
                        label,
                        UnsafePatchError(
                            "ownership lock could not be acquired"
                        ),
                    )
                )
                continue
            except BaseException as observation_error:
                observation_failures.append(
                    (label, observation_error)
                )
                state = "UNKNOWN"
                close_states[file_fd] = state
                close_states[ownership.anchor_fd] = "UNKNOWN"
                failures.append(
                    (
                        label,
                        UnsafePatchError(
                            "numeric FD ownership could not be verified"
                        ),
                    )
                )
                continue
            try:
                fcntl.flock(
                    file_fd,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                state = "UNKNOWN"
                close_states[file_fd] = state
                close_states[ownership.anchor_fd] = "UNKNOWN"
                release_error = _release_ownership_lock(
                    ownership.anchor_fd
                )
                if release_error is not None:
                    observation_failures.append(
                        (label, release_error)
                    )
                failures.append(
                    (
                        label,
                        UnsafePatchError(
                            "numeric FD and ownership anchor do not share "
                            "the registered open instance; numeric FDs retained"
                        ),
                    )
                )
                continue
            except BaseException as observation_error:
                observation_failures.append(
                    (label, observation_error)
                )
                state = "UNKNOWN"
                close_states[file_fd] = state
                close_states[ownership.anchor_fd] = "UNKNOWN"
                release_error = _release_ownership_lock(
                    ownership.anchor_fd
                )
                if release_error is not None:
                    observation_failures.append(
                        (label, release_error)
                    )
                failures.append(
                    (
                        label,
                        UnsafePatchError(
                            "numeric FD ownership lock check failed"
                        ),
                    )
                )
                continue
        close_error: BaseException | None = None
        try:
            with _defer_keyboard_interrupt():
                os.close(file_fd)
        except BaseException as exc:
            close_error = exc
            state, observation_error = _observe_fd_close_state(
                file_fd,
                expected_identity,
            )
            if observation_error is not None:
                observation_failures.append(
                    (label, observation_error)
                )
        else:
            state = "CLOSED"
        close_states[file_fd] = state
        if close_error is not None:
            close_states[ownership.anchor_fd] = "UNKNOWN"
            release_error = _release_ownership_lock(
                ownership.anchor_fd
            )
            if release_error is not None:
                observation_failures.append(
                    (label, release_error)
                )
            failures.append((label, close_error))
            if paths:
                residual_paths.extend(paths)
            continue
        anchor_closed = _close_owner_anchor(
            ownership.anchor_fd,
            ownership.identity,
            close_states,
            observation_failures,
            label,
        )
        if anchor_closed and attempted_fds is not None:
            attempted_fds.add(file_fd)
    if observation_failures:
        if primary is not None:
            for label, observation_error in observation_failures:
                _safe_add_note(
                    primary,
                    f"{label} close identity observation failed: "
                    f"{observation_error}",
                )
        elif not failures:
            first_label, first_error = observation_failures[0]
            for label, observation_error in observation_failures[1:]:
                _safe_add_note(
                    first_error,
                    f"{label} close identity observation failed: "
                    f"{observation_error}",
                )
            _safe_add_note(
                first_error,
                f"{first_label} close identity observation failed",
            )
            _attach_fd_close_states(first_error, close_states)
            raise first_error
    if not failures:
        return []
    if primary is not None:
        diagnostics = [
            f"{label}: {close_error}"
            for label, close_error in failures
        ]
        _append_close_diagnostics(primary, diagnostics)
        for label, close_error in failures:
            _safe_add_note(
                primary,
                f"{label} close failed: {close_error}",
            )
        if residual_paths:
            _attach_cleanup_residual(primary, residual_paths)
        _attach_fd_close_states(primary, close_states)
        return failures
    first_label, first_error = failures[0]
    if isinstance(first_error, OSError):
        failure_to_raise: BaseException = UnsafePatchError(
            f"{first_label} close failed: {first_error}"
        )
        failure_to_raise.__cause__ = first_error
        _copy_signal_metadata(first_error, failure_to_raise)
    else:
        failure_to_raise = first_error
    diagnostics = [
        f"{label}: {close_error}"
        for label, close_error in failures
    ]
    _append_close_diagnostics(failure_to_raise, diagnostics)
    for label, observation_error in observation_failures:
        _safe_add_note(
            failure_to_raise,
            f"{label} close identity observation failed: "
            f"{observation_error}",
        )
    for label, close_error in failures[1:]:
        _safe_add_note(
            failure_to_raise,
            f"{label} close failed: {close_error}",
        )
    _attach_fd_close_states(failure_to_raise, close_states)
    if residual_paths:
        _attach_cleanup_residual(
            failure_to_raise,
            residual_paths,
        )
    raise failure_to_raise


@contextmanager
def _owned_file_descriptor(file_fd: int, label: str):
    try:
        yield file_fd
    finally:
        _close_fd_preserving_primary(file_fd, label)


@contextmanager
def _defer_keyboard_interrupt():
    """Prevent SIGINT delivery between os.open return and registration."""
    if os.name != "posix" or not hasattr(signal, "pthread_sigmask"):
        raise UnsafePatchError(
            "SIGINT shielding is required but unavailable"
        )
    previous = signal.pthread_sigmask(
        signal.SIG_BLOCK,
        {signal.SIGINT},
    )
    primary: BaseException | None = None
    try:
        yield
    except BaseException as primary_error:
        primary = primary_error
        raise
    finally:
        restore_failures: list[BaseException] = []
        for _attempt in range(2):
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            except BaseException as restore_error:
                restore_failures.append(restore_error)
            else:
                break
        try:
            current_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                set(),
            )
        except BaseException as observation_error:
            restore_failures.append(observation_error)
            mask_state = "UNKNOWN"
        else:
            mask_state = (
                "BLOCKED"
                if signal.SIGINT in current_mask
                else "RESTORED"
            )
        if primary is not None:
            for restore_error in restore_failures:
                _safe_add_note(
                    primary,
                    "SIGINT mask restore diagnostic: "
                    f"{type(restore_error).__name__}: "
                    f"{_safe_diagnostic_text(restore_error)}",
                )
            try:
                primary.signal_mask_state = mask_state
            except BaseException:
                pass
            try:
                primary.secondary_exceptions = [
                    {
                        "type": type(item).__name__,
                        "message": _safe_diagnostic_text(item),
                        "phase": "sigint_mask_restore",
                    }
                    for item in restore_failures
                ]
            except BaseException:
                pass
        elif restore_failures:
            failure = restore_failures[-1]
            try:
                failure.signal_mask_state = mask_state
            except BaseException:
                pass
            try:
                failure.secondary_exceptions = [
                    {
                        "type": type(item).__name__,
                        "message": _safe_diagnostic_text(item),
                        "phase": "sigint_mask_restore",
                    }
                    for item in restore_failures[:-1]
                ]
            except BaseException:
                pass
            raise failure


def _open_registered_fd(
    opened_fds: list[int],
    *args: Any,
    **kwargs: Any,
) -> int:
    if not isinstance(opened_fds, _OwnedFDCollection):
        raise UnsafePatchError(
            "FD operations require an ownership-tracked collection"
        )
    opened_fd: int | None = None
    try:
        with _defer_keyboard_interrupt():
            opened_fd = _OS_OPEN(*args, **kwargs)
            opened_fds.register(opened_fd)
    except BaseException:
        if (
            isinstance(opened_fd, int)
            and opened_fd not in opened_fds
        ):
            try:
                _OS_CLOSE(opened_fd)
            except BaseException:
                pass
        raise
    return opened_fd


def _dup_registered_fd(
    opened_fds: list[int],
    source_fd: int,
    label: str,
) -> int:
    del label
    if not isinstance(opened_fds, _OwnedFDCollection):
        raise UnsafePatchError(
            "FD operations require an ownership-tracked collection"
        )
    duplicated_fd: int | None = None
    try:
        with _defer_keyboard_interrupt():
            duplicated_fd = _OS_DUP(source_fd)
            opened_fds.register(duplicated_fd)
    except BaseException:
        if (
            isinstance(duplicated_fd, int)
            and duplicated_fd not in opened_fds
        ):
            try:
                _OS_CLOSE(duplicated_fd)
            except BaseException:
                pass
        raise
    return duplicated_fd


def _validate_patch_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise UnsafePatchError("patch path must be a non-empty string")
    if "\x00" in value or "\\" in value or ":" in value:
        raise UnsafePatchError(
            "patch path must not contain NUL, backslash, or drive syntax"
        )
    pure = PurePosixPath(value)
    parts = value.split("/")
    if (
        pure.is_absolute()
        or value != pure.as_posix()
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise UnsafePatchError(
            "patch path must be a normalized relative path"
        )
    return value


def _open_snapshot_parent(
    root: Path,
    relative: str,
    owned_fds: list[int] | None = None,
) -> tuple[int, str]:
    relative = _validate_patch_path(relative)
    parts = PurePosixPath(relative).parts
    opened_fds = _OwnedFDCollection() if owned_fds is None else owned_fds
    try:
        _open_registered_fd(
            opened_fds,
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        current_fd = opened_fds[-1]
        for part in parts[:-1]:
            _open_registered_fd(
                opened_fds,
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current_fd,
            )
            previous_fd = current_fd
            current_fd = opened_fds[-1]
            _close_registered_fd(
                opened_fds,
                previous_fd,
                "mutation snapshot parent",
            )
        return current_fd, parts[-1]
    except BaseException:
        _close_owned_fds(
            opened_fds,
            "mutation snapshot parent",
        )
        raise


def _classify_snapshot_target(
    directory_fd: int,
    name: str,
    *,
    new_data: bytes,
    mode: int,
    old_identity: tuple[int, int],
    old_sha256: str,
) -> str:
    opened_fds: list[int] = _OwnedFDCollection()
    try:
        file_fd = _open_registered_fd(
            opened_fds,
            name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        metadata = os.fstat(file_fd)
        chunks = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    except FileNotFoundError:
        return "ABSENT"
    except OSError:
        return "UNKNOWN"
    finally:
        _close_owned_fds(opened_fds, "snapshot target observation")
    if stat.S_IMODE(metadata.st_mode) == mode and data == new_data:
        return "COMMITTED"
    if (
        (metadata.st_dev, metadata.st_ino) == old_identity
        and hashlib.sha256(data).hexdigest() == old_sha256
    ):
        return "UNCOMMITTED"
    return "UNKNOWN"


def _read_snapshot_file(
    root: Path,
    relative: str,
) -> tuple[str, tuple[int, int], int, str]:
    parent_fds: list[int] = _OwnedFDCollection()
    directory_fd = None
    name = None
    file_fds: list[int] = _OwnedFDCollection()
    try:
        directory_fd, name = _open_snapshot_parent(
            root,
            relative,
            parent_fds,
        )
        file_fd = _open_registered_fd(
            file_fds,
            name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafePatchError(
                f"patch target is not a regular file: {relative}"
            )
        chunks = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsafePatchError(
                f"patch target is not UTF-8: {relative}"
            ) from exc
        return (
            text,
            (metadata.st_dev, metadata.st_ino),
            stat.S_IMODE(metadata.st_mode),
            hashlib.sha256(data).hexdigest(),
        )
    finally:
        _close_owned_fds(file_fds, "mutation snapshot file")
        _close_owned_fds(
            parent_fds,
            "mutation snapshot parent directory",
        )


def _write_snapshot_file(
    root: Path,
    relative: str,
    text: str,
    expected_identity: tuple[int, int],
    expected_sha256: str,
    mode: int,
) -> None:
    parent_fds: list[int] = _OwnedFDCollection()
    directory_fd = None
    name = None
    temporary_name = None
    temporary_fds: list[int] = _OwnedFDCollection()
    current_fds: list[int] = _OwnedFDCollection()
    temporary_created = False
    replace_attempted = False
    target_state = "UNCOMMITTED"
    new_data = text.encode("utf-8")
    try:
        directory_fd, name = _open_snapshot_parent(
            root,
            relative,
            parent_fds,
        )
        temporary_name = (
            f".{name}.mutation-{os.getpid()}-{os.urandom(8).hex()}"
        )
        current_input_fd = _open_registered_fd(
            current_fds,
            name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        try:
            metadata = os.fstat(current_input_fd)
            chunks = []
            while True:
                chunk = os.read(current_input_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            _close_registered_fd(
                current_fds,
                current_input_fd,
                "mutation snapshot current file",
            )
        if (
            (metadata.st_dev, metadata.st_ino) != expected_identity
            or hashlib.sha256(b"".join(chunks)).hexdigest()
            != expected_sha256
        ):
            raise UnsafePatchError(
                f"patch target changed after validation: {relative}"
            )
        temporary_fd = _open_registered_fd(
            temporary_fds,
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW,
            mode,
            dir_fd=directory_fd,
        )
        temporary_created = True
        try:
            view = memoryview(new_data)
            while view:
                written = os.write(temporary_fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fchmod(temporary_fd, mode)
            os.fsync(temporary_fd)
        finally:
            _close_registered_fd(
                temporary_fds,
                temporary_fd,
                "snapshot temporary file",
            )
        current_input_fd = _open_registered_fd(
            current_fds,
            name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        try:
            metadata = os.fstat(current_input_fd)
            chunks = []
            while True:
                chunk = os.read(current_input_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            _close_registered_fd(
                current_fds,
                current_input_fd,
                "mutation snapshot current file",
            )
        if (
            (metadata.st_dev, metadata.st_ino) != expected_identity
            or hashlib.sha256(b"".join(chunks)).hexdigest()
            != expected_sha256
        ):
            raise UnsafePatchError(
                f"patch target changed before replace: {relative}"
            )
        replace_attempted = True
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        target_state = _classify_snapshot_target(
            directory_fd,
            name,
            new_data=new_data,
            mode=mode,
            old_identity=expected_identity,
            old_sha256=expected_sha256,
        )
        if target_state == "UNKNOWN":
            raise UnsafePatchError(
                "snapshot committed state is unknown"
            )
        if target_state != "COMMITTED":
            raise UnsafePatchError(
                "snapshot replacement was not observed"
            )
        os.fsync(directory_fd)
    except BaseException as primary_error:
        if temporary_fds:
            temporary_created = True
        if current_fds:
            _close_owned_fds(
                current_fds,
                "mutation snapshot current file",
            )
        if temporary_fds:
            _close_owned_fds(
                temporary_fds,
                "snapshot temporary file",
            )
        if replace_attempted and directory_fd is not None:
            try:
                target_state = _classify_snapshot_target(
                    directory_fd,
                    name,
                    new_data=new_data,
                    mode=mode,
                    old_identity=expected_identity,
                    old_sha256=expected_sha256,
                )
            except BaseException as observation_error:
                target_state = "UNKNOWN"
                _attach_unknown_residual(
                    primary_error,
                    "snapshot target observation failed: "
                    f"{observation_error}",
                )
        if target_state == "COMMITTED":
            _attach_cleanup_residual(primary_error, [relative])
        elif target_state == "UNKNOWN":
            _attach_unknown_residual(
                primary_error,
                "snapshot target state could not be verified",
            )
        if (
            (temporary_created or temporary_fds)
            and directory_fd is not None
            and temporary_name is not None
        ):
            temporary_state = "UNKNOWN"
            try:
                os.stat(
                    temporary_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                temporary_state = "ABSENT"
            except BaseException as observation_error:
                temporary_state = "UNKNOWN"
                _safe_add_note(
                    primary_error,
                    "snapshot temporary observation failed: "
                    f"{temporary_name}: {observation_error}",
                )
            else:
                temporary_state = "PRESENT"
            if temporary_state == "PRESENT":
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                except BaseException as cleanup_error:
                    _safe_add_note(
                        primary_error,
                        "snapshot temporary cleanup failed: "
                        f"{temporary_name}: {cleanup_error}",
                    )
                    try:
                        os.stat(
                            temporary_name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        post_state = "ABSENT"
                    except BaseException:
                        post_state = "UNKNOWN"
                    else:
                        post_state = "PRESENT"
                    if post_state == "PRESENT":
                        _attach_cleanup_residual(
                            primary_error,
                            [temporary_name],
                        )
                    elif post_state == "UNKNOWN":
                        _attach_unknown_residual(
                            primary_error,
                            "snapshot temporary cleanup state is unknown: "
                            f"{temporary_name}: {cleanup_error}",
                        )
            elif temporary_state == "UNKNOWN":
                _attach_unknown_residual(
                    primary_error,
                    f"snapshot temporary state is unknown: {temporary_name}",
                )
        raise
    finally:
        try:
            _close_owned_fds(
                parent_fds,
                "snapshot parent directory",
                residual_paths=(
                    [relative]
                    if target_state == "COMMITTED"
                    else None
                ),
            )
        except BaseException as close_error:
            if target_state == "UNKNOWN":
                _attach_unknown_residual(
                    close_error,
                    f"snapshot target state is unknown: {relative}",
                )
            raise


def _copy_directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if (
        not nofollow
        or not directory
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
    ):
        raise UnsafePatchError(
            "mutation copy requires no-follow directory-relative filesystem "
            "operations"
        )
    return os.O_RDONLY | directory | nofollow | cloexec


def _assert_exclusive_private_directory(
    directory_fd: int,
    label: str,
) -> None:
    metadata = os.fstat(directory_fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafePatchError(f"{label} is not a directory")
    if (
        hasattr(os, "geteuid")
        and metadata.st_uid != os.geteuid()
    ):
        raise UnsafePatchError(
            f"{label} is not owned by the current effective user"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise UnsafePatchError(
            f"{label} must be owner-only and inaccessible to group/other"
        )
    if sys.platform == "darwin":
        _assert_macos_directory_has_no_acl(directory_fd, label)
    else:
        raise UnsafePatchError(
            f"{label} extended ACL support is unavailable on this platform"
        )


def _assert_macos_directory_has_no_acl(
    directory_fd: int,
    label: str,
) -> None:
    library = ctypes.CDLL(
        ctypes.util.find_library("c"),
        use_errno=True,
    )
    library.acl_get_fd.argtypes = [ctypes.c_int]
    library.acl_get_fd.restype = ctypes.c_void_p
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    ctypes.set_errno(0)
    acl = library.acl_get_fd(directory_fd)
    if not acl:
        error_number = ctypes.get_errno()
        if error_number == errno.ENOENT:
            return
        raise UnsafePatchError(
            f"{label} ACL could not be inspected safely"
        )
    library.acl_free(acl)
    raise UnsafePatchError(
        f"{label} has an extended ACL and is not exclusively private"
    )


def _rename_directory_no_replace(
    parent_fd: int,
    source_name: str,
    target_name: str,
) -> None:
    if sys.platform != "darwin":
        raise UnsafePatchError(
            "atomic no-replace directory rename is unavailable on this "
            "platform"
        )
    library = ctypes.CDLL(
        ctypes.util.find_library("c"),
        use_errno=True,
    )
    function = getattr(library, "renameatx_np", None)
    if function is None:
        raise UnsafePatchError(
            "renameatx_np is unavailable"
        )
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = function(
        parent_fd,
        os.fsencode(source_name),
        parent_fd,
        os.fsencode(target_name),
        0x00000004,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(
                error_number,
                os.strerror(error_number),
                target_name,
            )
        raise OSError(
            error_number,
            os.strerror(error_number),
            target_name,
        )


def _open_absolute_directory_without_links(
    path: Path,
    label: str,
) -> int:
    flags = _copy_directory_flags()
    absolute = Path(os.path.abspath(path))
    parts = list(absolute.parts)
    if (
        sys.platform == "darwin"
        and len(parts) > 1
        and parts[1] in {"etc", "tmp", "var"}
    ):
        alias = Path(os.sep) / parts[1]
        expected = Path("/private") / parts[1]
        if (
            alias.is_symlink()
            and Path(os.path.realpath(alias)) == expected
        ):
            parts[1:2] = ["private", parts[1]]
    absolute = Path(*parts)
    opened_fds: list[int] = _OwnedFDCollection()
    try:
        _open_registered_fd(opened_fds, absolute.anchor, flags)
        for part in absolute.parts[1:]:
            _open_registered_fd(
                opened_fds,
                part,
                flags,
                dir_fd=opened_fds[-1],
            )
        if not stat.S_ISDIR(os.fstat(opened_fds[-1]).st_mode):
            raise UnsafePatchError(f"{label} is not a directory: {path}")
    except BaseException:
        _close_owned_fds(opened_fds, label)
        raise
    directory_fd = opened_fds[-1]
    ancestor_fds = opened_fds[:-1]
    try:
        for ancestor_fd in ancestor_fds:
            _close_registered_fd(
                opened_fds,
                ancestor_fd,
                label,
            )
    except BaseException as close_error:
        try:
            _close_registered_fd(
                opened_fds,
                directory_fd,
                label,
            )
        except BaseException as result_close_error:
            _safe_add_note(
                close_error,
                f"{label} result close also failed: "
                f"{result_close_error}",
            )
        raise
    try:
        opened_fds.abandon_anchor(directory_fd)
    except BaseException as transfer_error:
        try:
            _close_owned_fds(
                opened_fds,
                label,
            )
        except BaseException as cleanup_error:
            _safe_add_note(
                transfer_error,
                f"directory transfer cleanup failed: {cleanup_error}",
            )
        raise
    return directory_fd


def _open_absolute_registered(
    opened_fds: list[int],
    path: Path,
    label: str,
) -> int:
    with _defer_keyboard_interrupt():
        directory_fd = _open_absolute_directory_without_links(
            path,
            label,
        )
        if not isinstance(opened_fds, _OwnedFDCollection):
            raise UnsafePatchError(
                "FD operations require an ownership-tracked collection"
            )
        opened_fds.register(directory_fd)
    return directory_fd


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _tree_identity(
    directory_fd: int,
    *,
    prefix: str = ".",
    seen: set[tuple[int, int]] | None = None,
) -> tuple[tuple[Any, ...], ...]:
    flags = _copy_directory_flags()
    visited = set() if seen is None else seen
    directory_metadata = os.fstat(directory_fd)
    directory_identity = (
        directory_metadata.st_dev,
        directory_metadata.st_ino,
    )
    if directory_identity in visited:
        raise UnsafePatchError(
            f"mutation input contains a directory cycle: {prefix}"
        )
    visited.add(directory_identity)
    entries: list[tuple[Any, ...]] = []
    try:
        for name in sorted(os.listdir(directory_fd)):
            relative = (
                name if prefix == "." else f"{prefix}/{name}"
            )
            metadata = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(metadata.st_mode):
                raise UnsafePatchError(
                    "mutation input contains a symbolic link: "
                    f"{relative}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                child_owner_fds: list[int] = _OwnedFDCollection()
                try:
                    child_fd = _open_registered_fd(
                        child_owner_fds,
                        name,
                        flags,
                        dir_fd=directory_fd,
                    )
                    child_metadata = os.fstat(child_fd)
                    if (
                        not stat.S_ISDIR(child_metadata.st_mode)
                        or (
                            child_metadata.st_dev,
                            child_metadata.st_ino,
                        )
                        != (
                            metadata.st_dev,
                            metadata.st_ino,
                        )
                    ):
                        raise UnsafePatchError(
                            "mutation input directory changed during "
                            f"inspection: {relative}"
                        )
                    entries.append(
                        (
                            relative,
                            "directory",
                            _file_identity(metadata),
                            _tree_identity(
                                child_fd,
                                prefix=relative,
                                seen=visited,
                            ),
                        )
                    )
                finally:
                    _close_owned_fds(
                        child_owner_fds,
                        f"mutation input directory {relative}",
                    )
                continue
            if stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise UnsafePatchError(
                        "mutation input contains a multiply-linked file: "
                        f"{relative}"
                    )
                entries.append(
                    (relative, "file", _file_identity(metadata))
                )
                continue
            raise UnsafePatchError(
                f"mutation input contains a special file: {relative}"
            )
    finally:
        visited.remove(directory_identity)
    return tuple(entries)


def _assert_disjoint_tree_copy_domains(
    source_fd: int,
    source_tree: tuple[tuple[Any, ...], ...],
    target_parent_fd: int,
    target_name: str,
) -> None:
    try:
        os.stat(
            target_name,
            dir_fd=target_parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        raise UnsafePatchError(
            "mutation copy target already exists and can overlap the source"
        )
    source_metadata = os.fstat(source_fd)
    source_identity = (source_metadata.st_dev, source_metadata.st_ino)
    source_directories = {source_identity}
    for entry in source_tree:
        if entry[1] == "directory":
            source_directories.add(tuple(entry[2][:2]))
    target_parent = os.fstat(target_parent_fd)
    target_parent_identity = (
        target_parent.st_dev,
        target_parent.st_ino,
    )
    if target_parent_identity in source_directories:
        raise UnsafePatchError(
            "mutation copy target is contained by the source tree"
        )


def _open_relative_directory(
    opened_fds: list[int],
    parent_fd: int,
    name: str,
    label: str,
) -> int:
    flags = _copy_directory_flags()
    try:
        _open_registered_fd(opened_fds, name, flags, dir_fd=parent_fd)
        opened = opened_fds[-1]
    except OSError as exc:
        raise UnsafePatchError(f"{label} is unavailable: {name}") from exc
    if not stat.S_ISDIR(os.fstat(opened).st_mode):
        raise UnsafePatchError(
            f"{label} is not a directory: {name}"
        )
    return opened


def _copy_open_file_to_directory(
    source_fd: int,
    source_metadata: os.stat_result,
    target_directory_fd: int,
    target_name: str,
    label: str,
    created_ledger: dict[str, tuple[str, int, int]] | None = None,
    ledger_key: str | None = None,
) -> tuple[int, int]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if created_ledger is not None and ledger_key is not None:
        created_ledger[ledger_key] = (
            "pending-file",
            -1,
            -1,
        )
    target_owner_fds: list[int] = _OwnedFDCollection()
    try:
        target_fd = _open_registered_fd(
            target_owner_fds,
            target_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | nofollow
            | cloexec,
            stat.S_IMODE(source_metadata.st_mode),
            dir_fd=target_directory_fd,
        )
    except FileExistsError:
        if created_ledger is not None and ledger_key is not None:
            created_ledger.pop(ledger_key, None)
        raise
    except BaseException:
        _close_owned_fds(
            target_owner_fds,
            f"mutation target file {label}",
        )
        raise
    try:
        created_identity = (
            os.fstat(target_fd).st_dev,
            os.fstat(target_fd).st_ino,
        )
        if created_ledger is not None and ledger_key is not None:
            created_ledger[ledger_key] = (
                "file",
                created_identity[0],
                created_identity[1],
            )
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
        os.fchmod(
            target_fd,
            stat.S_IMODE(source_metadata.st_mode),
        )
    finally:
        _close_registered_fd(
            target_owner_fds,
            target_fd,
            f"mutation target file {label}",
        )
    if _file_identity(os.fstat(source_fd)) != _file_identity(
        source_metadata
    ):
        raise UnsafePatchError(
            f"mutation input file changed during copy: {label}"
        )
    return created_identity


def _create_private_directory_entry(
    parent_fd: int,
    name: str,
    relative: str,
    ledger: dict[str, tuple[str, int, int]],
    opened_fds: list[int] | None = None,
) -> tuple[int, tuple[int, int]]:
    flags = _copy_directory_flags()
    temporary_name = f".mutation-dir-{os.urandom(8).hex()}"
    parent_relative = PurePosixPath(relative).parent
    temporary_relative = (
        temporary_name
        if parent_relative == PurePosixPath(".")
        else (parent_relative / temporary_name).as_posix()
    )
    ledger[relative] = ("pending-directory", -1, -1)
    ledger[temporary_relative] = ("pending-directory", -1, -1)
    temporary_created = False
    target_fd = None
    target_owner_fds = _OwnedFDCollection() if opened_fds is None else opened_fds
    temporary_identity = None
    transferred = False
    try:
        os.mkdir(temporary_name, 0o700, dir_fd=parent_fd)
        temporary_created = True
        target_fd = _open_registered_fd(
            target_owner_fds,
            temporary_name,
            flags,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(target_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise UnsafePatchError(
                f"mutation target directory identity is invalid: {relative}"
            )
        created_identity = (metadata.st_dev, metadata.st_ino)
        temporary_identity = created_identity
        ledger[temporary_relative] = (
            "directory",
            created_identity[0],
            created_identity[1],
        )
        _rename_directory_no_replace(
            parent_fd,
            temporary_name,
            name,
        )
        ledger.pop(temporary_relative, None)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != created_identity:
            raise UnsafePatchError(
                f"mutation target directory identity changed: {relative}"
            )
        ledger[relative] = (
            "directory",
            created_identity[0],
            created_identity[1],
        )
        transferred = True
        return target_fd, created_identity
    except FileExistsError:
        ledger.pop(relative, None)
        if temporary_identity is not None:
            ledger[temporary_relative] = (
                "directory",
                temporary_identity[0],
                temporary_identity[1],
            )
        elif temporary_created:
            ledger[temporary_relative] = (
                "pending-directory",
                -1,
                -1,
            )
        raise
    except BaseException:
        raise
    finally:
        if not transferred and target_owner_fds:
            _close_owned_fds(
                target_owner_fds,
                f"mutation target directory {relative}",
            )


def _clear_ledgered_entries(
    root_fd: int,
    ledger: Mapping[str, tuple[str, int, int]],
) -> CleanupReport:
    """Remove only objects recorded as created by this copy transaction."""
    residual: list[str] = []
    diagnostics: list[str] = []
    unknown = False
    flags = _copy_directory_flags()
    for relative in sorted(
        ledger,
        key=lambda value: len(PurePosixPath(value).parts),
        reverse=True,
    ):
        kind, expected_dev, expected_ino = ledger[relative]
        parts = PurePosixPath(relative).parts
        pending_fds: list[int] = _OwnedFDCollection()
        try:
            _open_registered_fd(
                pending_fds,
                ".",
                os.O_RDONLY | flags,
                dir_fd=root_fd,
            )
            valid = True
            for index, part in enumerate(parts[:-1], start=1):
                prefix = "/".join(parts[:index])
                parent_entry = ledger.get(prefix)
                if parent_entry is None or parent_entry[0] != "directory":
                    valid = False
                    break
                _open_registered_fd(
                    pending_fds,
                    part,
                    flags,
                    dir_fd=pending_fds[-1],
                )
                opened = os.fstat(pending_fds[-1])
                previous_fd = pending_fds[-2]
                try:
                    _close_registered_fd(
                        pending_fds,
                        previous_fd,
                        "cleanup directory traversal",
                    )
                except BaseException as close_error:
                    diagnostics.append(
                        "cleanup directory close failed: "
                        f"{previous_fd}: {close_error}"
                    )
                    unknown = True
                if (
                    opened.st_dev,
                    opened.st_ino,
                ) != (
                    parent_entry[1],
                    parent_entry[2],
                ):
                    valid = False
                    break
            if not valid:
                residual.append(relative)
                unknown = True
                continue
            try:
                current = os.stat(
                    parts[-1],
                    dir_fd=pending_fds[-1],
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except BaseException as existence_error:
                residual.append(relative)
                unknown = True
                diagnostics.append(
                    "cleanup existence or ownership is unknown for "
                    f"{relative}: {existence_error}"
                )
                continue
            if kind.startswith("pending-"):
                residual.append(relative)
                unknown = True
                continue
            if (
                current.st_dev,
                current.st_ino,
            ) != (
                expected_dev,
                expected_ino,
            ):
                residual.append(relative)
                unknown = True
                continue
            try:
                if kind == "directory":
                    os.rmdir(parts[-1], dir_fd=pending_fds[-1])
                else:
                    os.unlink(parts[-1], dir_fd=pending_fds[-1])
            except FileNotFoundError:
                continue
            except BaseException as cleanup_error:
                diagnostics.append(
                    f"cleanup failed for {relative}: {cleanup_error}"
                )
                try:
                    remaining = os.stat(
                        parts[-1],
                        dir_fd=pending_fds[-1],
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                except BaseException as existence_error:
                    residual.append(relative)
                    unknown = True
                    diagnostics.append(
                        "post-cleanup existence is unknown for "
                        f"{relative}: {existence_error}"
                    )
                else:
                    residual.append(relative)
                    if (
                        remaining.st_dev,
                        remaining.st_ino,
                    ) != (
                        expected_dev,
                        expected_ino,
                    ):
                        unknown = True
        except BaseException as traversal_error:
            residual.append(relative)
            unknown = True
            diagnostics.append(
                f"cleanup traversal failed for {relative}: "
                f"{traversal_error}"
            )
        finally:
            try:
                _close_owned_fds(
                    pending_fds,
                    "cleanup directory",
                )
            except BaseException as close_error:
                diagnostics.append(
                    f"cleanup directory close failed: {close_error}"
                )
                unknown = True
    return CleanupReport(
        residual_paths=sorted(set(residual)),
        diagnostics=diagnostics,
        unknown=unknown,
    )


def _attach_cleanup_residual(
    error: BaseException,
    residual: list[str],
) -> None:
    if residual:
        try:
            existing = list(
                getattr(error, "residual_paths", []) or []
            )
        except BaseException:
            existing = []
        paths = sorted(set(existing) | set(residual))
        try:
            error.residual_paths = paths
        except BaseException:
            pass
        _safe_add_note(
            error,
            "mutation cleanup preserved identity-mismatched, unresolved "
            f"or unreadable entries: {paths}",
        )


def _attach_unknown_residual(
    error: BaseException,
    reason: str,
) -> None:
    try:
        error.residual_unknown = True
    except BaseException:
        pass
    _safe_add_note(error, f"mutation residual state is unknown: {reason}")


def _attach_cleanup_report(
    error: BaseException,
    report: CleanupReport,
) -> None:
    if report.residual_paths:
        _attach_cleanup_residual(error, report.residual_paths)
    for diagnostic in report.diagnostics:
        _safe_add_note(error, diagnostic)
    if report.unknown:
        _attach_unknown_residual(
            error,
            "one or more cleanup descriptors or ownership checks failed",
        )


def _transfer_cleanup_diagnostics(
    source: BaseException,
    target: BaseException,
) -> None:
    for note in getattr(source, "__notes__", []):
        _safe_add_note(target, note)
    for note in getattr(source, "diagnostic_notes", []):
        _safe_add_note(target, note)
    residual = getattr(source, "residual_paths", None)
    if residual is not None:
        try:
            target.residual_paths = residual
        except BaseException:
            pass
    if getattr(source, "residual_unknown", False):
        _attach_unknown_residual(
            target,
            "source cleanup reported an unknown residual state",
        )
    for attribute in (
        "fd_close_states",
        "unresolved_fds",
        "signal_mask_state",
        "secondary_exceptions",
    ):
        try:
            value = getattr(source, attribute)
        except BaseException:
            continue
        if not value:
            continue
        if attribute in {"fd_close_states", "unresolved_fds"}:
            try:
                existing = dict(getattr(target, attribute, {}))
            except BaseException:
                existing = {}
            existing.update(dict(value))
            value = existing
        try:
            setattr(target, attribute, value)
        except BaseException:
            pass


def _unlink_relative_if_identity(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    *,
    directory: bool,
) -> CleanupReport:
    try:
        metadata = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return CleanupReport([], [])
    except BaseException as exc:
        return CleanupReport(
            [],
            [f"residual existence check failed: {exc}"],
            unknown=True,
        )
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        return CleanupReport(["."], [])
    try:
        if directory:
            os.rmdir(name, dir_fd=parent_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)
    except BaseException as exc:
        return CleanupReport(
            ["."],
            [f"target cleanup failed: {exc}"],
        )
    return CleanupReport([], [])


def _copy_directory_contents(
    source_fd: int,
    target_fd: int,
    *,
    prefix: str,
    ledger: dict[str, tuple[str, int, int]] | None = None,
) -> None:
    if ledger is None:
        ledger = {}
    flags = _copy_directory_flags()
    nofollow = getattr(os, "O_NOFOLLOW")
    cloexec = getattr(os, "O_CLOEXEC", 0)
    for name in sorted(os.listdir(source_fd)):
        relative = f"{prefix}/{name}" if prefix else name
        metadata = os.stat(
            name,
            dir_fd=source_fd,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(metadata.st_mode):
            raise UnsafePatchError(
                f"mutation input contains a symbolic link: {relative}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            source_child_fds: list[int] = _OwnedFDCollection()
            try:
                source_child = _open_registered_fd(
                    source_child_fds,
                    name,
                    flags,
                    dir_fd=source_fd,
                )
                opened = os.fstat(source_child)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (metadata.st_dev, metadata.st_ino)
                ):
                    raise UnsafePatchError(
                        "mutation input directory changed before copy: "
                        f"{relative}"
                    )
                target_child_owner: list[int] = _OwnedFDCollection()
                try:
                    target_child, _created_identity = (
                        _create_private_directory_entry(
                            target_fd,
                            name,
                            relative,
                            ledger,
                            target_child_owner,
                        )
                    )
                    _copy_directory_contents(
                        source_child,
                        target_child,
                        prefix=relative,
                        ledger=ledger,
                    )
                    os.fchmod(
                        target_child,
                        stat.S_IMODE(metadata.st_mode),
                    )
                finally:
                    _close_owned_fds(
                        target_child_owner,
                        f"mutation target directory {relative}",
                    )
            finally:
                _close_owned_fds(
                    source_child_fds,
                    f"mutation input directory {relative}",
                )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafePatchError(
                f"mutation input contains a special file: {relative}"
            )
        if metadata.st_nlink != 1:
            raise UnsafePatchError(
                f"mutation input contains a multiply-linked file: {relative}"
            )
        source_file_fds: list[int] = _OwnedFDCollection()
        try:
            source_file = _open_registered_fd(
                source_file_fds,
                name,
                os.O_RDONLY | nofollow | cloexec,
                dir_fd=source_fd,
            )
            opened = os.fstat(source_file)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _file_identity(opened) != _file_identity(metadata)
            ):
                raise UnsafePatchError(
                    f"mutation input file changed before copy: {relative}"
                )
            _copy_open_file_to_directory(
                source_file,
                metadata,
                target_fd,
                name,
                relative,
                ledger,
                relative,
            )
        finally:
            _close_owned_fds(
                source_file_fds,
                f"mutation input file {relative}",
            )


def _copy_tree_without_links(source: Path, target: Path) -> None:
    source_fd = None
    source_owner_fds: list[int] = _OwnedFDCollection()
    target_parent_fd = None
    target_parent_owner_fds: list[int] = _OwnedFDCollection()
    target_fd = None
    target_owner_fds: list[int] = _OwnedFDCollection()
    target_created = False
    target_identity = None
    root_intent = False
    target_committed = False
    ledger: dict[str, tuple[str, int, int]] = {}
    try:
        source_fd = _open_absolute_registered(
            source_owner_fds,
            source,
            "mutation input",
        )
        source_before = _tree_identity(source_fd)
        target_parent_fd = _open_absolute_registered(
            target_parent_owner_fds,
            target.parent,
            "mutation copy target parent",
        )
        _assert_exclusive_private_directory(
            target_parent_fd,
            "mutation copy target parent",
        )
        _assert_disjoint_tree_copy_domains(
            source_fd,
            source_before,
            target_parent_fd,
            target.name,
        )
        root_intent = True
        os.mkdir(target.name, 0o700, dir_fd=target_parent_fd)
        target_created = True
        target_fd = _open_relative_directory(
            target_owner_fds,
            target_parent_fd,
            target.name,
            "mutation copy target",
        )
        _assert_exclusive_private_directory(
            target_fd,
            "mutation copy target",
        )
        target_identity = (
            os.fstat(target_fd).st_dev,
            os.fstat(target_fd).st_ino,
        )
        root_intent = False
        _copy_directory_contents(
            source_fd,
            target_fd,
            prefix="",
            ledger=ledger,
        )
        if _tree_identity(source_fd) != source_before:
            raise UnsafePatchError(
                "mutation input changed during source copy"
            )
        current_source_owner: list[int] = _OwnedFDCollection()
        current_source_fd = _open_absolute_registered(
            current_source_owner,
            source,
            "mutation input",
        )
        try:
            if (
                os.fstat(current_source_fd).st_dev,
                os.fstat(current_source_fd).st_ino,
            ) != (
                os.fstat(source_fd).st_dev,
                os.fstat(source_fd).st_ino,
            ):
                raise UnsafePatchError(
                    "mutation input directory was replaced during copy"
                )
        finally:
            _close_owned_fds(
                current_source_owner,
                "mutation input recheck",
            )
        current_target_parent_owner: list[int] = _OwnedFDCollection()
        current_target_parent_fd = _open_absolute_registered(
            current_target_parent_owner,
            target.parent,
            "mutation copy target parent",
        )
        try:
            if (
                os.fstat(current_target_parent_fd).st_dev,
                os.fstat(current_target_parent_fd).st_ino,
            ) != (
                os.fstat(target_parent_fd).st_dev,
                os.fstat(target_parent_fd).st_ino,
            ):
                raise UnsafePatchError(
                    "mutation copy target parent was replaced during copy"
                )
            current_target_owner: list[int] = _OwnedFDCollection()
            current_target_fd = _open_relative_directory(
                current_target_owner,
                current_target_parent_fd,
                target.name,
                "mutation copy target",
            )
            try:
                if (
                    os.fstat(current_target_fd).st_dev,
                    os.fstat(current_target_fd).st_ino,
                ) != target_identity:
                    raise UnsafePatchError(
                        "mutation copy target was replaced during copy"
                    )
            finally:
                _close_registered_fd(
                    current_target_owner,
                    current_target_fd,
                    "mutation copy target recheck",
                )
        finally:
            _close_owned_fds(
                current_target_parent_owner,
                "mutation copy target parent recheck",
            )
        target_committed = True
        if target_identity is None:
            raise UnsafePatchError(
                "mutation copy committed without a target identity"
            )
    except OSError as exc:
        if target_committed:
            _attach_cleanup_residual(exc, ["."])
        if (
            root_intent
            and exc.errno != errno.EEXIST
            and target_parent_fd is not None
        ):
            try:
                os.stat(
                    target.name,
                    dir_fd=target_parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            except BaseException as observation_error:
                _attach_unknown_residual(
                    exc,
                    "mutation target existence is unknown after root "
                    f"creation: {observation_error}",
                )
            else:
                _attach_cleanup_residual(exc, ["."])
        if target_created and target_identity is None:
            _attach_cleanup_residual(exc, ["."])
        if (
            target_created
            and target_fd is not None
            and not target_committed
        ):
            _attach_cleanup_report(
                exc,
                _clear_ledgered_entries(target_fd, ledger),
            )
        wrapped = UnsafePatchError(
            f"mutation input could not be copied safely: {source}"
        )
        _transfer_cleanup_diagnostics(exc, wrapped)
        raise wrapped from exc
    except BaseException as exc:
        if target_committed:
            _attach_cleanup_residual(exc, ["."])
        if root_intent and target_parent_fd is not None:
            try:
                os.stat(
                    target.name,
                    dir_fd=target_parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            except BaseException as observation_error:
                _attach_unknown_residual(
                    exc,
                    "mutation target existence is unknown after root "
                    f"creation: {observation_error}",
                )
            else:
                _attach_cleanup_residual(exc, ["."])
        if target_created and target_identity is None:
            _attach_cleanup_residual(exc, ["."])
        if (
            target_created
            and target_fd is not None
            and not target_committed
        ):
            _attach_cleanup_report(
                exc,
                _clear_ledgered_entries(target_fd, ledger),
            )
        raise
    finally:
        primary = sys.exc_info()[1]
        if target_committed and primary is not None:
            _attach_cleanup_residual(primary, ["."])
        if (
            not target_committed
            and target_created
            and target_parent_fd is not None
            and target_identity is not None
        ):
            unlink_report = _unlink_relative_if_identity(
                target_parent_fd,
                target.name,
                target_identity,
                directory=True,
            )
            primary = sys.exc_info()[1]
            if primary is not None:
                _attach_cleanup_report(primary, unlink_report)
        residual_paths = ["."] if target_committed else None
        _close_owned_fds(
            target_owner_fds,
            "mutation copy target",
            residual_paths=residual_paths,
        )
        _close_owned_fds(
            target_parent_owner_fds,
            "mutation copy target parent",
            residual_paths=residual_paths,
        )
        _close_owned_fds(
            source_owner_fds,
            "mutation input",
            residual_paths=residual_paths,
        )


def _copy_file_without_links(source: Path, target: Path) -> None:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow:
        raise UnsafePatchError("O_NOFOLLOW is unavailable")
    source_parent_fd = None
    source_parent_owner_fds: list[int] = _OwnedFDCollection()
    source_fd = None
    source_owner_fds: list[int] = _OwnedFDCollection()
    target_parent_fd = None
    target_parent_owner_fds: list[int] = _OwnedFDCollection()
    target_fd = None
    target_owner_fds: list[int] = _OwnedFDCollection()
    target_created = False
    target_identity = None
    keep_target = False
    ledger: dict[str, tuple[str, int, int]] = {}
    try:
        source_parent_fd = _open_absolute_registered(
            source_parent_owner_fds,
            source.parent,
            "mutation input parent",
        )
        source_fd = _open_registered_fd(
            source_owner_fds,
            source.name,
            os.O_RDONLY | nofollow | cloexec,
            dir_fd=source_parent_fd,
        )
        metadata = os.fstat(source_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafePatchError(
                f"mutation input is not a regular file: {source}"
            )
        if metadata.st_nlink != 1:
            raise UnsafePatchError(
                f"mutation input is multiply linked: {source}"
            )
        target_parent_fd = _open_absolute_registered(
            target_parent_owner_fds,
            target.parent,
            "mutation target parent",
        )
        _assert_exclusive_private_directory(
            target_parent_fd,
            "mutation target parent",
        )
        ledger[target.name] = (
            "pending-file",
            -1,
            -1,
        )
        target_fd = _open_registered_fd(
            target_owner_fds,
            target.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | nofollow
            | cloexec,
            stat.S_IMODE(metadata.st_mode),
            dir_fd=target_parent_fd,
        )
        target_created = True
        try:
            target_identity = (
                os.fstat(target_fd).st_dev,
                os.fstat(target_fd).st_ino,
            )
            ledger[target.name] = (
                "file",
                target_identity[0],
                target_identity[1],
            )
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
            os.fchmod(
                target_fd,
                stat.S_IMODE(metadata.st_mode),
            )
            if _file_identity(
                os.fstat(source_fd)
            ) != _file_identity(metadata):
                raise UnsafePatchError(
                    f"mutation input changed during copy: {source}"
                )
        finally:
            _close_registered_fd(
                target_owner_fds,
                target_fd,
                f"mutation target file {target.name}",
            )
        current_source_parent_owner: list[int] = _OwnedFDCollection()
        current_source_parent = _open_absolute_registered(
            current_source_parent_owner,
            source.parent,
            "mutation input parent",
        )
        try:
            if (
                os.fstat(current_source_parent).st_dev,
                os.fstat(current_source_parent).st_ino,
            ) != (
                os.fstat(source_parent_fd).st_dev,
                os.fstat(source_parent_fd).st_ino,
            ):
                raise UnsafePatchError(
                    "mutation input parent was replaced during copy"
                )
        finally:
            _close_owned_fds(
                current_source_parent_owner,
                "mutation input parent recheck",
            )
        current_target_parent_owner: list[int] = _OwnedFDCollection()
        current_target_parent = _open_absolute_registered(
            current_target_parent_owner,
            target.parent,
            "mutation target parent",
        )
        try:
            if (
                os.fstat(current_target_parent).st_dev,
                os.fstat(current_target_parent).st_ino,
            ) != (
                os.fstat(target_parent_fd).st_dev,
                os.fstat(target_parent_fd).st_ino,
            ):
                raise UnsafePatchError(
                    "mutation target parent was replaced during copy"
                )
            current_target_owner: list[int] = _OwnedFDCollection()
            current_target = _open_registered_fd(
                current_target_owner,
                target.name,
                os.O_RDONLY | nofollow | cloexec,
                dir_fd=current_target_parent,
            )
            try:
                if (
                    os.fstat(current_target).st_dev,
                    os.fstat(current_target).st_ino,
                ) != target_identity:
                    raise UnsafePatchError(
                        "mutation target was replaced during copy"
                    )
            finally:
                _close_registered_fd(
                    current_target_owner,
                    current_target,
                    "mutation target recheck",
                )
        finally:
            _close_owned_fds(
                current_target_parent_owner,
                "mutation target parent recheck",
            )
        keep_target = True
    except OSError as exc:
        if (
            ledger
            and not keep_target
            and target_parent_fd is not None
        ):
            _attach_cleanup_report(
                exc,
                _clear_ledgered_entries(target_parent_fd, ledger),
            )
        wrapped = UnsafePatchError(
            f"mutation input could not be copied safely: {source}"
        )
        _transfer_cleanup_diagnostics(exc, wrapped)
        raise wrapped from exc
    except BaseException as exc:
        if (
            ledger
            and not keep_target
            and target_parent_fd is not None
        ):
            _attach_cleanup_report(
                exc,
                _clear_ledgered_entries(target_parent_fd, ledger),
            )
        raise
    finally:
        residual_paths = [target.name] if keep_target else None
        _close_owned_fds(
            target_owner_fds,
            "mutation target file",
            residual_paths=residual_paths,
        )
        _close_owned_fds(
            target_parent_owner_fds,
            "mutation target parent",
            residual_paths=residual_paths,
        )
        _close_owned_fds(
            source_owner_fds,
            "mutation input",
            residual_paths=residual_paths,
        )
        _close_owned_fds(
            source_parent_owner_fds,
            "mutation input parent",
            residual_paths=residual_paths,
        )


def _normalize_patches(case: dict[str, Any]) -> list[dict[str, str]]:
    if "patches" in case:
        patches = case["patches"]
        if not isinstance(patches, list) or not patches:
            raise ValueError(f"mutation {case.get('id')} has empty patches")
        normalized = []
        for patch in patches:
            if not isinstance(patch, dict):
                raise UnsafePatchError("mutation patch must be an object")
            if set(patch) != {"path", "old", "new"}:
                raise UnsafePatchError(
                    "mutation patch must contain only path, old, and new"
                )
            old = patch["old"]
            new = patch["new"]
            if not isinstance(old, str) or not isinstance(new, str):
                raise UnsafePatchError(
                    "mutation patch old/new values must be strings"
                )
            normalized.append(
                {
                    "path": _validate_patch_path(patch["path"]),
                    "old": old,
                    "new": new,
                }
            )
        return normalized
    if set(case) < {"path", "old", "new"}:
        raise UnsafePatchError("legacy mutation patch is incomplete")
    old = case["old"]
    new = case["new"]
    if not isinstance(old, str) or not isinstance(new, str):
        raise UnsafePatchError(
            "mutation patch old/new values must be strings"
        )
    return [
        {
            "path": _validate_patch_path(case["path"]),
            "old": old,
            "new": new,
        }
    ]


def _copy_project(target: Path) -> None:
    project_fd = None
    target_fd = None
    pyproject_fd = None
    src_fd = None
    tests_fd = None
    project_owner_fds: list[int] = _OwnedFDCollection()
    target_owner_fds: list[int] = _OwnedFDCollection()
    pyproject_owner_fds: list[int] = _OwnedFDCollection()
    src_owner_fds: list[int] = _OwnedFDCollection()
    tests_owner_fds: list[int] = _OwnedFDCollection()
    copy_started = False
    copy_completed = False
    ledger: dict[str, tuple[str, int, int]] = {}
    try:
        project_fd = _open_absolute_registered(
            project_owner_fds,
            PROJECT_ROOT,
            "mutation project root",
        )
        target_fd = _open_absolute_registered(
            target_owner_fds,
            target,
            "mutation project target",
        )
        _assert_exclusive_private_directory(
            target_fd,
            "mutation project target",
        )
        if os.listdir(target_fd):
            raise UnsafePatchError(
                "mutation project target must start empty"
            )
        copy_started = True
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        cloexec = getattr(os, "O_CLOEXEC", 0)
        pyproject_fd = _open_registered_fd(
            pyproject_owner_fds,
            "pyproject.toml",
            os.O_RDONLY | nofollow | cloexec,
            dir_fd=project_fd,
        )
        pyproject_metadata = os.fstat(pyproject_fd)
        if (
            not stat.S_ISREG(pyproject_metadata.st_mode)
            or pyproject_metadata.st_nlink != 1
        ):
            raise UnsafePatchError(
                "mutation project pyproject.toml must be a "
                "single-link regular file"
            )
        src_fd = _open_relative_directory(
            src_owner_fds,
            project_fd,
            "src",
            "mutation project src",
        )
        tests_fd = _open_relative_directory(
            tests_owner_fds,
            project_fd,
            "tests",
            "mutation project tests",
        )
        project_identity = (
            os.fstat(project_fd).st_dev,
            os.fstat(project_fd).st_ino,
        )
        src_identity = _tree_identity(src_fd)
        tests_identity = _tree_identity(tests_fd)

        _copy_open_file_to_directory(
            pyproject_fd,
            pyproject_metadata,
            target_fd,
            "pyproject.toml",
            "pyproject.toml",
            ledger,
            "pyproject.toml",
        )
        for name, source_child in (("src", src_fd), ("tests", tests_fd)):
            target_child_owner: list[int] = _OwnedFDCollection()
            try:
                target_child, _created_identity = (
                    _create_private_directory_entry(
                        target_fd,
                        name,
                        name,
                        ledger,
                        target_child_owner,
                    )
                )
                opened = os.fstat(target_child)
                if not stat.S_ISDIR(opened.st_mode):
                    raise UnsafePatchError(
                        f"mutation project target {name} is not a directory"
                    )
                _copy_directory_contents(
                    source_child,
                    target_child,
                    prefix=name,
                    ledger=ledger,
                )
                os.fchmod(
                    target_child,
                    stat.S_IMODE(os.fstat(source_child).st_mode),
                )
            finally:
                _close_owned_fds(
                    target_child_owner,
                    f"mutation project target {name}",
                )

        if (
            _tree_identity(src_fd) != src_identity
            or _tree_identity(tests_fd) != tests_identity
            or _file_identity(os.fstat(pyproject_fd))
            != _file_identity(pyproject_metadata)
        ):
            raise UnsafePatchError(
                "mutation project changed during aggregate copy"
            )
        current_project_owner: list[int] = _OwnedFDCollection()
        current_target_owner: list[int] = _OwnedFDCollection()
        current_project_fd = _open_absolute_registered(
            current_project_owner,
            PROJECT_ROOT,
            "mutation project root",
        )
        current_target_fd = _open_absolute_registered(
            current_target_owner,
            target,
            "mutation project target",
        )
        try:
            if (
                os.fstat(current_project_fd).st_dev,
                os.fstat(current_project_fd).st_ino,
            ) != project_identity:
                raise UnsafePatchError(
                    "mutation project root was replaced during copy"
                )
            if (
                os.fstat(current_target_fd).st_dev,
                os.fstat(current_target_fd).st_ino,
            ) != (
                os.fstat(target_fd).st_dev,
                os.fstat(target_fd).st_ino,
            ):
                raise UnsafePatchError(
                    "mutation project target was replaced during copy"
                )
            for name, expected_fd in (
                ("src", src_fd),
                ("tests", tests_fd),
            ):
                current_child_owner: list[int] = _OwnedFDCollection()
                current_child = _open_relative_directory(
                    current_child_owner,
                    current_project_fd,
                    name,
                    f"mutation project {name}",
                )
                try:
                    if (
                        os.fstat(current_child).st_dev,
                        os.fstat(current_child).st_ino,
                    ) != (
                        os.fstat(expected_fd).st_dev,
                        os.fstat(expected_fd).st_ino,
                    ):
                        raise UnsafePatchError(
                            f"mutation project {name} was replaced "
                            "during copy"
                        )
                finally:
                    _close_registered_fd(
                        current_child_owner,
                        current_child,
                        f"mutation project {name} recheck",
                    )
            current_pyproject_owner: list[int] = _OwnedFDCollection()
            current_pyproject = _open_registered_fd(
                current_pyproject_owner,
                "pyproject.toml",
                os.O_RDONLY | nofollow | cloexec,
                dir_fd=current_project_fd,
            )
            try:
                if (
                    os.fstat(current_pyproject).st_dev,
                    os.fstat(current_pyproject).st_ino,
                ) != (
                    pyproject_metadata.st_dev,
                    pyproject_metadata.st_ino,
                ):
                    raise UnsafePatchError(
                        "mutation project pyproject.toml was replaced "
                        "during copy"
                    )
            finally:
                _close_registered_fd(
                    current_pyproject_owner,
                    current_pyproject,
                    "mutation project pyproject recheck",
                )
        finally:
            _close_owned_fds(
                current_target_owner,
                "mutation project target recheck",
            )
            _close_owned_fds(
                current_project_owner,
                "mutation project root recheck",
            )
        if (
            _tree_identity(src_fd) != src_identity
            or _tree_identity(tests_fd) != tests_identity
            or _file_identity(os.fstat(pyproject_fd))
            != _file_identity(pyproject_metadata)
        ):
            raise UnsafePatchError(
                "mutation project changed at final source verification"
            )
        copy_completed = True
    except OSError as exc:
        if copy_started and target_fd is not None:
            _attach_cleanup_report(
                exc,
                _clear_ledgered_entries(target_fd, ledger),
            )
        wrapped = UnsafePatchError(
            f"mutation project could not be copied safely: {target}"
        )
        _transfer_cleanup_diagnostics(exc, wrapped)
        raise wrapped from exc
    except BaseException as exc:
        if copy_started and target_fd is not None:
            _attach_cleanup_report(
                exc,
                _clear_ledgered_entries(target_fd, ledger),
            )
        raise
    finally:
        residual_paths = ["."] if copy_completed else None
        _close_owned_fds(
            tests_owner_fds,
            "mutation project tests",
            residual_paths=residual_paths,
        )
        _close_owned_fds(
            src_owner_fds,
            "mutation project src",
            residual_paths=residual_paths,
        )
        _close_owned_fds(
            pyproject_owner_fds,
            "mutation project pyproject",
            residual_paths=residual_paths,
        )
        _close_owned_fds(
            target_owner_fds,
            "mutation project target",
            residual_paths=residual_paths,
        )
        _close_owned_fds(
            project_owner_fds,
            "mutation project root",
            residual_paths=residual_paths,
        )


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 60,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        environment = {
            **os.environ,
            "PYTHONPATH": "src",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if extra_env:
            environment.update(extra_env)
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
        code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        code = -999
        stdout = exc.stdout or ""
        stderr = "mutation test timed out"
    return {
        "command": command,
        "exit_code": code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": (time.perf_counter() - started) * 1000,
    }


def _executed_mutated_lines(
    trace_path: Path,
    root: Path,
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Check whether the patched lines were actually executed by the tests."""
    if not trace_path.is_file():
        return {"executed": False, "segments": [], "trace_missing": True}
    executed: set[tuple[str, int]] = set()
    for entry in json.loads(trace_path.read_text(encoding="utf-8")):
        name, _, line = entry.rpartition(":")
        executed.add((str(Path(name).resolve()), int(line)))
    resolved: list[dict[str, Any]] = []
    for segment in segments:
        target = str((root / segment["path"]).resolve())
        resolved.append(
            {
                **segment,
                "hit": any(
                    (target, line) in executed
                    for line in range(
                        segment["start_line"], segment["end_line"] + 1
                    )
                ),
            }
        )
    return {
        "executed": all(item["hit"] for item in resolved),
        "segments": resolved,
        "trace_missing": False,
    }


def _classify_result(
    test: Mapping[str, Any],
    test_ids: list[str],
) -> tuple[str, str | None]:
    """Map a test run to one of the four mutation outcomes.

    ``unittest`` exits 1 both for a real assertion failure and for a broken
    test selection (missing module, import error).  Counting the latter as
    ``killed`` would let a runner defect masquerade as a caught mutation, so
    a kill additionally requires unittest's failure banner *and* every
    declared test id to appear in the output.
    """
    exit_code = test.get("exit_code")
    if exit_code == 0:
        return "survived", None
    output = f"{test.get('stdout', '')}\n{test.get('stderr', '')}"
    if "_FailedTest" in output or "unittest.loader" in output:
        return "error", "test selection failed to load"
    if exit_code == 1 and "FAILED (" in output and all(
        str(test_id) in output for test_id in test_ids
    ):
        return "killed", None
    return (
        "error",
        f"test command exited {exit_code} without failing the selected tests",
    )


def _mutation_invalid_result(
    case: dict[str, Any],
    reason: str,
    *,
    error: BaseException | None = None,
    **extra: Any,
) -> dict[str, Any]:
    diagnostic_errors: list[str] = []
    residual_unknown = False
    try:
        residual_paths = sorted(
            set(getattr(error, "residual_paths", []) or [])
        )
    except BaseException as getter_error:
        residual_paths = []
        residual_unknown = True
        diagnostic_errors.append(
            "residual_paths: "
            f"{type(getter_error).__name__}: {getter_error}"
        )
    try:
        residual_unknown = residual_unknown or bool(
            getattr(error, "residual_unknown", False)
        )
    except BaseException as getter_error:
        residual_unknown = True
        diagnostic_errors.append(
            "residual_unknown: "
            f"{type(getter_error).__name__}: {getter_error}"
        )
    residual_status = (
        "UNKNOWN"
        if residual_unknown
        else ("PRESERVED" if residual_paths else "NONE")
    )
    try:
        fd_close_states = dict(
            getattr(error, "fd_close_states", {}) or {}
        )
    except BaseException:
        fd_close_states = {}
    try:
        unresolved_fds = dict(
            getattr(error, "unresolved_fds", {}) or {}
        )
    except BaseException:
        unresolved_fds = {}
    try:
        signal_mask_state = str(
            getattr(error, "signal_mask_state", "NOT_APPLICABLE")
        )
    except BaseException:
        signal_mask_state = "UNKNOWN"
    try:
        secondary_exceptions = list(
            getattr(error, "secondary_exceptions", []) or []
        )
    except BaseException:
        secondary_exceptions = []
    result = {
        "id": case.get("id", "unknown"),
        "expected": case.get("expected", "killed"),
        "result": "invalid",
        "reason": reason,
        "residual": {
            "status": residual_status,
            "paths": residual_paths,
        },
        "fd_close_states": {
            str(file_fd): str(state)
            for file_fd, state in fd_close_states.items()
        },
        "unresolved_fds": {
            str(file_fd): str(state)
            for file_fd, state in unresolved_fds.items()
        },
        "signal_mask_state": signal_mask_state,
        "secondary_exceptions": secondary_exceptions,
        "diagnostic_errors": diagnostic_errors,
    }
    result.update(extra)
    return result


def _evaluate_mutation(
    case: dict[str, Any],
    *,
    command_override: list[str] | None = None,
) -> dict[str, Any]:
    try:
        patches = _normalize_patches(case)
    except UnsafePatchError as exc:
        return _mutation_invalid_result(
            case,
            f"unsafe mutation patch: {exc}",
            error=exc,
        )
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        try:
            _copy_project(root)
        except (OSError, UnsafePatchError) as exc:
            return _mutation_invalid_result(
                case,
                f"unsafe mutation input: {exc}",
                error=exc,
            )
        mutated_lines: list[dict[str, Any]] = []
        contents: dict[str, dict[str, Any]] = {}
        try:
            for patch in patches:
                relative = patch["path"]
                state = contents.get(relative)
                if state is None:
                    (
                        original,
                        identity,
                        mode,
                        digest,
                    ) = _read_snapshot_file(root, relative)
                    state = {
                        "mutated": original,
                        "identity": identity,
                        "mode": mode,
                        "sha256": digest,
                    }
                    contents[relative] = state
                original = state["mutated"]
                occurrences = original.count(patch["old"])
                if occurrences != 1:
                    return _mutation_invalid_result(
                        case,
                        f"patch target count is {occurrences} "
                        f"for {relative}",
                    )
                index = original.index(patch["old"])
                start_line = original[:index].count("\n") + 1
                state["mutated"] = original.replace(
                    patch["old"],
                    patch["new"],
                    1,
                )
                mutated_lines.append(
                    {
                        "path": relative,
                        "start_line": start_line,
                        "end_line": start_line + patch["new"].count("\n"),
                    }
                )
            identities = [
                state["identity"] for state in contents.values()
            ]
            if len(identities) != len(set(identities)):
                raise UnsafePatchError(
                    "multiple mutation paths alias the same file identity"
                )
            for relative, state in contents.items():
                _write_snapshot_file(
                    root,
                    relative,
                    state["mutated"],
                    state["identity"],
                    state["sha256"],
                    state["mode"],
                )
        except (OSError, UnsafePatchError) as exc:
            return _mutation_invalid_result(
                case,
                f"unsafe mutation patch: {exc}",
                error=exc,
            )
        python_paths = [
            root / relative
            for relative in contents
            if PurePosixPath(relative).suffix == ".py"
        ]
        javascript_paths = [
            root / relative
            for relative in contents
            if PurePosixPath(relative).suffix == ".js"
        ]
        compile_results: list[dict[str, Any]] = []
        if python_paths:
            compile_results.append(
                _run(
                    [
                        sys.executable,
                        "-m",
                        "py_compile",
                        *[str(path) for path in python_paths],
                    ],
                    cwd=root,
                )
            )
        if javascript_paths:
            compile_results.append(
                _run(
                    ["node", "--check", *[str(path) for path in javascript_paths]],
                    cwd=root,
                )
            )
        invalid_compile = next(
            (
                result
                for result in compile_results
                if result["exit_code"] != 0
            ),
            None,
        )
        if invalid_compile is not None:
            return _mutation_invalid_result(
                case,
                "mutated file does not compile",
                compile=invalid_compile,
            )
        trace_path = root / ".mutation-trace.json"
        if command_override:
            command = command_override
        else:
            (root / PROBE_NAME).write_text(PROBE_SOURCE, encoding="utf-8")
            command = [
                sys.executable,
                PROBE_NAME,
                "-q",
                *[str(test) for test in case["tests"]],
            ]
        test_result = _run(
            command,
            cwd=root,
            extra_env={"MUTATION_TRACE_FILE": str(trace_path)},
        )
        executed = _executed_mutated_lines(
            trace_path, root, mutated_lines
        )
        result, reason = _classify_result(test_result, list(case["tests"]))
        static_source_check = all(
            PurePosixPath(path).suffix == ".js" for path in contents
        )
        scope = (
            "static_source_check"
            if static_source_check
            else "in_process"
            if executed["executed"]
            else "unverified"
        )
        return {
            "id": case["id"],
            "property": case.get("property"),
            "path": patches[0]["path"],
            "patches": mutated_lines,
            "patch_count": len(patches),
            "tests": list(case["tests"]),
            "expected": case.get("expected", "killed"),
            "result": result,
            "reason": reason,
            "mutated_lines_executed": executed,
            "execution_evidence": {
                "scope": scope,
                "executed_in_process": bool(executed["executed"]),
                "static_source_check": static_source_check,
                # Child processes are not traced, so this is never claimed.
                "executed_in_child_process": False,
                "child_trace_supported": False,
                "trace_missing": bool(executed.get("trace_missing")),
            },
            "test": {
                "command": test_result["command"],
                "exit_code": test_result["exit_code"],
            },
        }


def run_mutations(cases_path: Path | None = None) -> dict[str, Any]:
    payload = json.loads(
        (cases_path or DEFAULT_CASES).read_text(encoding="utf-8")
    )
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("mutation cases are missing")
    results = [_evaluate_mutation(case) for case in cases]
    counts = {
        result: sum(1 for item in results if item["result"] == result)
        for result in ("killed", "survived", "invalid", "error")
    }
    evidence_scope_counts = {
        scope: sum(
            1
            for item in results
            if item.get("execution_evidence", {}).get("scope") == scope
        )
        for scope in ("in_process", "static_source_check", "unverified")
    }
    unexpected = [
        item
        for item in results
        if item.get("expected") == "killed"
        and item["result"] != "killed"
    ]
    unverified = [
        item["id"]
        for item in results
        if item.get("execution_evidence", {}).get("scope")
        == "unverified"
    ]
    # A survived result is only meaningful when the mutated line ran; an
    # untraced survival could simply mean the test never reached the edit.
    unverified_survivals = [
        item["id"]
        for item in results
        if item["result"] == "survived" and item["id"] in unverified
    ]
    return {
        "version": payload.get("version", "unknown"),
        "case_count": len(results),
        "counts": counts,
        "evidence_scope_counts": evidence_scope_counts,
        "execution_evidence_counts": {
            "in_process": evidence_scope_counts["in_process"],
            "static_source_check":
                evidence_scope_counts["static_source_check"],
            "unverified": evidence_scope_counts["unverified"],
            "child_process": 0,
        },
        "execution_unverified": unverified,
        "unverified_survivals": unverified_survivals,
        "results": results,
        "status": (
            "PASS"
            if not unexpected and not unverified_survivals
            else "FAIL"
        ),
        "unexpected_results": [item["id"] for item in unexpected],
    }


def run_self_test() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "src" / "mini.py").write_text("VALUE = 1\n")
        (root / "tests" / "test_mini.py").write_text(
            "import unittest\nfrom mini import VALUE\n"
            "class T(unittest.TestCase):\n"
            "    def test_value(self): self.assertEqual(VALUE, 1)\n"
        )
        cases = [
            {
                "id": "self_killed",
                "property": "self test",
                "path": "src/mini.py",
                "old": "VALUE = 1",
                "new": "VALUE = 2",
                "tests": ["tests.test_mini"],
                "expected": "killed",
            },
            {
                "id": "self_survived",
                "property": "self test",
                "path": "src/mini.py",
                "old": "# none",
                "new": "# changed",
                "tests": ["tests.test_mini"],
                "expected": "killed",
            },
            {
                "id": "self_invalid",
                "property": "self test",
                "path": "src/mini.py",
                "old": "NOT_PRESENT",
                "new": "VALUE = 9",
                "tests": ["tests.test_mini"],
                "expected": "killed",
            },
            {
                "id": "self_error",
                "property": "self test",
                "path": "src/mini.py",
                "old": "VALUE = 1",
                "new": "VALUE = 1 # changed",
                "tests": ["tests.test_mini"],
                "expected": "killed",
            },
        ]
        # The survived case needs a present comment target.
        (root / "src" / "mini.py").write_text("# none\nVALUE = 1\n")
        results = []
        for index, case in enumerate(cases):
            override = (
                [sys.executable, "-c", "raise SystemExit(5)"]
                if case["id"] == "self_error"
                else None
            )
            with tempfile.TemporaryDirectory() as case_dir:
                # Reuse the runner by materializing a temporary project.
                project = Path(case_dir)
                shutil.copytree(root / "src", project / "src")
                shutil.copytree(root / "tests", project / "tests")
                shutil.copy2(
                    PROJECT_ROOT / "pyproject.toml",
                    project / "pyproject.toml",
                )
                path = project / case["path"]
                original = path.read_text()
                count = original.count(case["old"])
                if count != 1:
                    results.append({"id": case["id"], "result": "invalid"})
                    continue
                path.write_text(
                    original.replace(case["old"], case["new"], 1)
                )
                compile_result = _run(
                    [sys.executable, "-m", "py_compile", str(path)],
                    cwd=project,
                )
                if compile_result["exit_code"] != 0:
                    results.append({"id": case["id"], "result": "invalid"})
                    continue
                command = override or [
                    sys.executable,
                    "-m",
                    "unittest",
                    "-q",
                    "tests.test_mini",
                ]
                test = _run(command, cwd=project)
                result, _ = _classify_result(test, ["tests.test_mini"])
                results.append({"id": case["id"], "result": result})
        counts = {
            name: sum(1 for item in results if item["result"] == name)
            for name in ("killed", "survived", "invalid", "error")
        }
        expected = {
            "killed": 1,
            "survived": 1,
            "invalid": 1,
            "error": 1,
        }
        return {
            "counts": counts,
            "results": results,
            "status": "PASS" if counts == expected else "FAIL",
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    report = run_self_test() if args.self_test else run_mutations(args.cases)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
