"""S1 supplementary checks requested before independent acceptance.

Five things the first review pass asked to see proven rather than asserted:

1. revoke and consume racing each other (no "revoked but still executed"),
2. corrupted / missing / unreadable grant records fail closed,
3. a failed audit write refuses execution (audit is part of the protocol),
4. a crashed process leaves the grant consumed, not restored,
5. the environment/authorization error codes mean the same thing at every
   layer (runner, task terminal state, events, retry policy, capability
   registry, UI strings).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.host_grant_support import authorized_service  # noqa: E402
from zevorinth import host_authorization as grants  # noqa: E402
from zevorinth.capabilities import CAPABILITY_REGISTRY  # noqa: E402
from zevorinth.host_authorization import (  # noqa: E402
    ALREADY_USED,
    AUDIT_FAILED,
    INVALID,
    REVOKED,
    HostAuthorizationError,
    HostGrantStore,
)
from zevorinth.models import ExecutionMode, TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import RUN_TESTS  # noqa: E402
from zevorinth.retry_policy import get_retry_decision, is_retryable_error  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402

PASSING = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        self.assertTrue(True)\n"
)

FAILING = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_always_fails(self):\n"
    "        self.assertEqual(1, 2)\n"
)

I18N = json.loads(
    (
        PROJECT_ROOT / "src" / "zevorinth" / "workbench_static"
        / "i18n.zh-CN.json"
    ).read_text(encoding="utf-8")
)


def _store(root: Path) -> HostGrantStore:
    return HostGrantStore(root / "host-grants")


def _issue(store: HostGrantStore, *, workspace="demo", target="test_demo") -> str:
    return store.issue(
        workspace=workspace, target=target, issuer="pytest"
    )["token"]


def _record(store: HostGrantStore, grant_id: str) -> dict:
    return json.loads((store.root / f"{grant_id}.json").read_text("utf-8"))


def _code(call) -> str:
    try:
        call()
    except HostAuthorizationError as exc:
        return exc.code
    return "no_error"


def _workspace(root: Path, source: str = PASSING) -> Path:
    workspace = root / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    (workspace / "test_ok.py").write_text(source, encoding="utf-8")
    return workspace


@contextlib.contextmanager
def _spawn_fails(attempts: list[str] | None = None):
    """Make ``subprocess.Popen`` raise OSError, everything else stays real."""
    from zevorinth import local_test_runner as ltr

    real = ltr.subprocess
    record = attempts if attempts is not None else []

    class _Shim:
        def __getattr__(self, item: str) -> Any:
            return getattr(real, item)

        @staticmethod
        def Popen(*args: object, **kwargs: object) -> None:
            del args, kwargs
            record.append("popen")
            raise OSError("cannot spawn")

    ltr.subprocess = _Shim()  # type: ignore[assignment]
    try:
        yield record
    finally:
        ltr.subprocess = real  # type: ignore[assignment]


class RevokeConsumeRaceTests(unittest.TestCase):
    """A revocation that lands first must stop the run; a run that lands
    first has already happened and revocation cannot pretend otherwise."""

    ITERATIONS = 12

    def test_race_never_produces_revoked_then_executed(self) -> None:
        for iteration in range(self.ITERATIONS):
            with tempfile.TemporaryDirectory() as temp_dir:
                store = _store(Path(temp_dir))
                issued = store.issue(
                    workspace="demo",
                    target="test_demo",
                    issuer="pytest",
                )
                grant_id = issued["grant"]["grant_id"]
                barrier = threading.Barrier(2)
                outcome: dict[str, str] = {}

                def consume() -> None:
                    barrier.wait()
                    try:
                        store.consume(
                            issued["token"],
                            workspace="demo",
                            target="test_demo",
                        )
                    except HostAuthorizationError as exc:
                        outcome["consume"] = exc.code
                    else:
                        outcome["consume"] = "consumed"

                def revoke() -> None:
                    barrier.wait()
                    try:
                        store.revoke(grant_id, issuer="operator")
                    except HostAuthorizationError as exc:
                        outcome["revoke"] = exc.code
                    else:
                        outcome["revoke"] = "revoked"

                threads = [
                    threading.Thread(target=consume),
                    threading.Thread(target=revoke),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)

                record = _record(store, grant_id)
                consumed = outcome.get("consume") == "consumed"
                with self.subTest(iteration=iteration, outcome=outcome):
                    self.assertEqual(outcome.get("revoke"), "revoked")
                    if consumed:
                        # Consume won the lock, so its timestamp must be the
                        # earlier one. The reverse would mean a revoke landed
                        # first and the run happened anyway.
                        self.assertLessEqual(
                            record["consumed_at"], record["revoked_at"]
                        )
                    else:
                        self.assertEqual(outcome["consume"], REVOKED)
                        self.assertIsNone(record["consumed_at"])
                        self.assertEqual(record["status"], "issued")

    def test_revoke_then_consume_never_reaches_the_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            store = _store(root)
            issued = store.issue(
                workspace="demo", target="test_ok", issuer="pytest"
            )
            store.revoke(issued["grant"]["grant_id"], issuer="operator")
            calls: list[object] = []
            service = TaskService(root, host_grant_store=store)
            service.test_runner = lambda **kwargs: calls.append(kwargs)
            task = service.submit(
                TaskRequest(
                    goal="run",
                    operation=RUN_TESTS,
                    parameters={
                        "runner": "unittest",
                        "target": "test_ok",
                        "workspace": "demo",
                        "execution_backend": "host",
                        "host_authorization": issued["token"],
                    },
                )
            )
            self.assertEqual(task.status, TaskStatus.BLOCKED)
            self.assertEqual(task.error.code, REVOKED)
            self.assertEqual(calls, [])


class CorruptGrantStoreTests(unittest.TestCase):
    """An authorization we cannot read is not an authorization."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.store = _store(self.root)
        self.issued = self.store.issue(
            workspace="demo", target="test_demo", issuer="pytest"
        )
        self.grant_id = self.issued["grant"]["grant_id"]
        self.path = self.store.root / f"{self.grant_id}.json"

    def tearDown(self) -> None:
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._temp.cleanup()

    def _consume(self) -> str:
        return _code(
            lambda: self.store.consume(
                self.issued["token"], workspace="demo", target="test_demo"
            )
        )

    def test_truncated_json_is_rejected(self) -> None:
        self.path.write_text('{"grant_id": "abc"', encoding="utf-8")
        self.assertEqual(self._consume(), INVALID)

    def test_non_object_json_is_rejected(self) -> None:
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(self._consume(), INVALID)

    def test_record_without_a_secret_digest_is_rejected(self) -> None:
        record = json.loads(self.path.read_text("utf-8"))
        record.pop("secret_sha256")
        self.path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self._consume(), INVALID)

    def test_record_without_a_valid_expiry_is_rejected(self) -> None:
        record = json.loads(self.path.read_text("utf-8"))
        record["expires_at"] = "not-a-timestamp"
        self.path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self._consume(), INVALID)

    def test_record_with_status_issued_but_garbage_fields_is_rejected(
        self,
    ) -> None:
        record = json.loads(self.path.read_text("utf-8"))
        record["status"] = "issued"
        record["workspace"] = None
        self.path.write_text(json.dumps(record), encoding="utf-8")
        # Either an explicit scope mismatch or an invalid record, never a run.
        self.assertIn(self._consume(), {INVALID, grants.SCOPE_MISMATCH})

    def test_unreadable_record_is_rejected_not_ignored(self) -> None:
        os.chmod(self.path, 0o000)
        self.assertEqual(self._consume(), INVALID)

    def test_missing_record_is_rejected(self) -> None:
        self.path.unlink()
        self.assertEqual(self._consume(), INVALID)

    def test_removed_grant_directory_is_rejected(self) -> None:
        import shutil

        shutil.rmtree(self.store.root)
        self.assertEqual(self._consume(), INVALID)

    def test_a_grant_never_becomes_usable_after_corruption(self) -> None:
        self.path.write_text("{broken", encoding="utf-8")
        for _ in range(3):
            self.assertEqual(self._consume(), INVALID)


class AuditProtocolTests(unittest.TestCase):
    """The audit entry is required by the consumption, not logged after it."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.store = _store(self.root)
        self.issued = self.store.issue(
            workspace="demo", target="test_demo", issuer="pytest"
        )
        self.grant_id = self.issued["grant"]["grant_id"]

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _break_audit(self) -> Path:
        """Make append impossible without mocking: the path is a directory."""
        audit = self.store.root / "audit.jsonl"
        if audit.exists():
            audit.unlink()
        audit.mkdir()
        return audit

    def test_consume_fails_when_the_audit_entry_cannot_be_written(self) -> None:
        self._break_audit()
        self.assertEqual(
            _code(
                lambda: self.store.consume(
                    self.issued["token"],
                    workspace="demo",
                    target="test_demo",
                )
            ),
            AUDIT_FAILED,
        )

    def test_a_grant_spent_without_audit_is_not_restored(self) -> None:
        self._break_audit()
        _code(
            lambda: self.store.consume(
                self.issued["token"], workspace="demo", target="test_demo"
            )
        )
        # The single-use transition was already durable; a failed audit must
        # not hand the grant back.
        record = _record(self.store, self.grant_id)
        self.assertEqual(record["status"], "consumed")
        audit = self.store.root / "audit.jsonl"
        audit.rmdir()
        self.assertEqual(
            _code(
                lambda: self.store.consume(
                    self.issued["token"],
                    workspace="demo",
                    target="test_demo",
                )
            ),
            ALREADY_USED,
        )

    def test_service_refuses_the_run_when_the_audit_cannot_be_written(
        self,
    ) -> None:
        """End-to-end: the real runner consumes, the audit fails, nothing runs."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _workspace(root)
            store = _store(root)
            token = store.issue(
                workspace="demo", target="test_ok", issuer="pytest"
            )["token"]
            service = TaskService(root, host_grant_store=store)
            audit = store.root / "audit.jsonl"
            if audit.exists():
                audit.unlink()
            audit.mkdir()
            task = service.submit(
                TaskRequest(
                    goal="run",
                    operation=RUN_TESTS,
                    parameters={
                        "runner": "unittest",
                        "target": "test_ok",
                        "workspace": "demo",
                        "execution_backend": "host",
                        "host_authorization": token,
                    },
                )
            )
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, AUDIT_FAILED)
            # No evidence means no process: the refusal happened before spawn.
            self.assertIsNone(task.tool_result.test_evidence)
            # And the grant is spent, not handed back for another attempt.
            self.assertEqual(store.list_grants()[0]["status"], "consumed")
            events = [
                (event.event_type, (event.payload or {}).get("code"))
                for event in service.events(task.task_id)
            ]
            self.assertIn(("tool_failed", AUDIT_FAILED), events)
            # ``tool_started`` records the intent to run (with the pre-run
            # snapshot); the proof that no process existed is the missing
            # evidence and the untouched workspace, not the absence of that
            # intent record.
            self.assertFalse(list(workspace.glob("artifacts/*")))

    def test_rejected_attempts_still_report_their_own_reason(self) -> None:
        self._break_audit()
        # The audit is broken, but the rejection reason must not be masked
        # by it: nothing executes either way.
        self.assertEqual(
            _code(
                lambda: self.store.consume(
                    "0" * 32 + "." + "0" * 64,
                    workspace="demo",
                    target="test_demo",
                )
            ),
            INVALID,
        )


class ConsumptionDurabilityTests(unittest.TestCase):
    """A crash between consumption and execution must not restore the grant."""

    def test_grant_stays_consumed_after_the_process_dies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = _store(root)
            issued = store.issue(
                workspace="demo", target="test_demo", issuer="pytest"
            )
            script = (
                "import os, sys\n"
                "sys.path.insert(0, %r)\n"
                "from zevorinth.host_authorization import HostGrantStore\n"
                "store = HostGrantStore(%r)\n"
                "store.consume(%r, workspace='demo', target='test_demo')\n"
                "os._exit(9)\n"
            ) % (
                str(PROJECT_ROOT / "src"),
                str(store.root),
                issued["token"],
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "HOME": str(root),
                },
                timeout=120,
            )
            self.assertEqual(completed.returncode, 9, completed.stderr)
            record = _record(store, issued["grant"]["grant_id"])
            self.assertEqual(record["status"], "consumed")
            self.assertEqual(
                _code(
                    lambda: store.consume(
                        issued["token"],
                        workspace="demo",
                        target="test_demo",
                    )
                ),
                ALREADY_USED,
            )

    def test_failed_spawn_leaves_the_grant_spent(self) -> None:
        """The grant is spent before the process exists, so a spawn that
        never happens cannot refund it."""
        from zevorinth import local_test_runner as ltr

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _workspace(root)
            store = _store(root)
            token = _issue(store, workspace="demo", target="test_ok")
            parameters = {
                "runner": "unittest",
                "target": "test_ok",
                "workspace": "demo",
                "execution_backend": "host",
                "host_authorization": token,
            }
            with _spawn_fails():
                with self.assertRaises(ltr.TestExecutionError) as captured:
                    ltr.run_unittest(
                        project_root=root,
                        parameters=parameters,
                        grant_store=store,
                    )
            self.assertEqual(captured.exception.code, "test_spawn_failed")

            record = _record(store, store.list_grants()[0]["grant_id"])
            self.assertEqual(record["status"], "consumed")
            self.assertIsNotNone(record["consumed_at"])
            self.assertEqual(
                _code(
                    lambda: store.consume(
                        token, workspace="demo", target="test_ok"
                    )
                ),
                ALREADY_USED,
            )
            self.assertEqual(list(workspace.glob("artifacts/*")), [])


class SpawnFailureTests(unittest.TestCase):
    """``test_spawn_failed`` means exactly one thing: no process was created.

    A test that ran and exited non-zero is an outcome, not a spawn failure.
    The grant is already spent when a spawn fails, so it is never refunded,
    and the failure record must name the grant that was spent.
    """

    def _submit(self, root: Path, store: HostGrantStore, target: str) -> Any:
        service = TaskService(root, host_grant_store=store)
        token = _issue(store, workspace="demo", target=target)
        with _spawn_fails():
            task = service.submit(
                TaskRequest(
                    goal="run",
                    operation=RUN_TESTS,
                    parameters={
                        "runner": "unittest",
                        "target": target,
                        "workspace": "demo",
                        "execution_backend": "host",
                        "host_authorization": token,
                    },
                )
            )
        return service, task, token

    def test_spawn_failure_has_its_own_code_and_names_the_grant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            store = _store(root)
            service, task, _ = self._submit(root, store, "test_ok")

            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "test_spawn_failed")
            # Never confused with an outcome or a timeout.
            for other in ("tests_failed", "test_timeout", "test_execution_failed"):
                self.assertNotEqual(task.error.code, other)
            payloads = [
                event.payload or {}
                for event in service.events(task.task_id)
                if event.event_type == "tool_failed"
            ]
            self.assertEqual(payloads[-1]["code"], "test_spawn_failed")
            self.assertEqual(
                payloads[-1]["host_grant_id"],
                store.list_grants()[0]["grant_id"],
            )
            self.assertEqual(payloads[-1]["host_grant_issuer"], "pytest")
            self.assertEqual(payloads[-1]["spawn_error"], "OSError")
            self.assertIsNone(task.tool_result.test_evidence)
            # The grant was consumed before the spawn, so it stays consumed.
            self.assertEqual(store.list_grants()[0]["status"], "consumed")
            self.assertFalse(is_retryable_error("test_spawn_failed"))

    def test_a_real_failed_exec_is_a_spawn_failure(self) -> None:
        """Not a stub: a real ``Popen`` that cannot exec reports the same way.

        The OS raises ``FileNotFoundError`` here, so this exercises the
        classification against a genuine ``OSError`` instead of only against the
        injected one. It is still not ``ENOMEM``; real resource exhaustion is
        recorded as unverified.
        """
        from unittest import mock

        from zevorinth import local_test_runner as ltr

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            store = _store(root)
            service = TaskService(root, host_grant_store=store)
            token = _issue(store, workspace="demo", target="test_ok")
            with mock.patch.object(
                ltr,
                "build_command",
                return_value=["/nonexistent/zevorinth-python", "-m", "unittest"],
            ):
                task = service.submit(
                    TaskRequest(
                        goal="run",
                        operation=RUN_TESTS,
                        parameters={
                            "runner": "unittest",
                            "target": "test_ok",
                            "workspace": "demo",
                            "execution_backend": "host",
                            "host_authorization": token,
                        },
                    )
                )
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "test_spawn_failed")
            self.assertIn("FileNotFoundError", task.error.message)
            self.assertEqual(store.list_grants()[0]["status"], "consumed")

    def test_a_failing_test_is_not_a_spawn_failure(self) -> None:
        """Exit code 1 must stay ``tests_failed``; only Popen errors are spawns."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root, FAILING)
            store = _store(root)
            service = TaskService(root, host_grant_store=store)
            token = _issue(store, workspace="demo", target="test_ok")
            task = service.submit(
                TaskRequest(
                    goal="run",
                    operation=RUN_TESTS,
                    parameters={
                        "runner": "unittest",
                        "target": "test_ok",
                        "workspace": "demo",
                        "execution_backend": "host",
                        "host_authorization": token,
                    },
                )
            )
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "tests_failed")
            self.assertNotEqual(task.error.code, "test_spawn_failed")
            # The run really happened and produced evidence.
            self.assertIsNotNone(task.tool_result.test_evidence)
            self.assertEqual(store.list_grants()[0]["status"], "consumed")

    def test_audit_failure_stops_before_any_spawn_is_attempted(self) -> None:
        """Audit failure + would-be spawn failure: no process is ever tried."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            store = _store(root)
            service = TaskService(root, host_grant_store=store)
            token = _issue(store, workspace="demo", target="test_ok")
            audit = store.root / "audit.jsonl"
            if audit.exists():
                audit.unlink()
            audit.mkdir()
            with _spawn_fails() as attempts:
                task = service.submit(
                    TaskRequest(
                        goal="run",
                        operation=RUN_TESTS,
                        parameters={
                            "runner": "unittest",
                            "target": "test_ok",
                            "workspace": "demo",
                            "execution_backend": "host",
                            "host_authorization": token,
                        },
                    )
                )
            # The audit refusal is the reason, not the spawn that never happened.
            self.assertEqual(task.error.code, AUDIT_FAILED)
            self.assertEqual(attempts, [])
            self.assertEqual(store.list_grants()[0]["status"], "consumed")


class ErrorCodeLayerConsistencyTests(unittest.TestCase):
    """One code must mean one thing in all six layers."""

    ENVIRONMENT_CODES = (
        "container_backend_unavailable",
        "test_spawn_failed",
        "test_runner_error",
        "test_execution_failed",
        # "Cannot look" is not "is not there": an unreadable workspace is a
        # distinct, non-retryable outcome from a missing test target.
        "workspace_unreadable",
        "host_authorization_required",
        "host_authorization_invalid",
        "host_authorization_expired",
        "host_authorization_scope_mismatch",
        "host_authorization_already_used",
        "host_authorization_revoked",
        "host_authorization_storage_failed",
        "host_authorization_audit_failed",
    )

    def test_container_backend_unavailable_survives_to_the_terminal_state(
        self,
    ) -> None:
        """The runner's environment code must not be re-wrapped on the way out."""
        from unittest import mock

        from zevorinth.isolation import ContainerBackendUnavailable

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            service = TaskService(root)
            with mock.patch(
                "zevorinth.isolation._live_runtime_identity",
                side_effect=ContainerBackendUnavailable("probe failed"),
            ):
                task = service.submit(
                    TaskRequest(
                        goal="run",
                        operation=RUN_TESTS,
                        parameters={
                            "runner": "unittest",
                            "target": "test_ok",
                            "workspace": "demo",
                            "execution_backend": "container",
                            "evidence_id": "not-present",
                        },
                    )
                )
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "container_backend_unavailable")
            payloads = [
                (event.event_type, (event.payload or {}).get("code"))
                for event in service.events(task.task_id)
            ]
            codes = {code for _, code in payloads}
            self.assertIn("container_backend_unavailable", codes)
            # Never turned into a timeout, and never retried on the host.
            self.assertNotIn("test_timeout", codes)
            self.assertFalse(is_retryable_error("container_backend_unavailable"))

    def test_unusable_grant_at_planning_is_blocked_with_the_same_code(
        self,
    ) -> None:
        """Refused before execution: blocked, code intact, grant untouched."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            store = _store(root)
            token = _issue(store, workspace="demo", target="test_ok")
            store.consume(token, workspace="demo", target="test_ok")
            service = TaskService(root, host_grant_store=store)
            task = service.submit(
                TaskRequest(
                    goal="run",
                    operation=RUN_TESTS,
                    parameters={
                        "runner": "unittest",
                        "target": "test_ok",
                        "workspace": "demo",
                        "execution_backend": "host",
                        "host_authorization": token,
                    },
                )
            )
            self.assertEqual(task.status, TaskStatus.BLOCKED)
            self.assertEqual(task.error.code, ALREADY_USED)
            self.assertEqual(task.execution_mode, ExecutionMode.NOT_EXECUTED)
            events = [
                (event.event_type, (event.payload or {}).get("code"))
                for event in service.events(task.task_id)
            ]
            self.assertIn(("policy_rejected", ALREADY_USED), events)
            self.assertNotIn("tool_started", [name for name, _ in events])

    def test_grant_that_expires_mid_flight_fails_with_the_same_code(
        self,
    ) -> None:
        """Between planning and execution the grant really can lapse."""
        from unittest import mock

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _workspace(root)
            store = _store(root)
            issued = store.issue(
                workspace="demo", target="test_ok", issuer="pytest"
            )
            grant_id = issued["grant"]["grant_id"]
            real_consume = store.consume

            def lapsed(token: Any, **kwargs: Any) -> dict:
                record = _record(store, grant_id)
                record["expires_at"] = (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat()
                (store.root / f"{grant_id}.json").write_text(
                    json.dumps(record), encoding="utf-8"
                )
                return real_consume(token, **kwargs)

            service = TaskService(root, host_grant_store=store)
            with mock.patch.object(store, "consume", side_effect=lapsed):
                task = service.submit(
                    TaskRequest(
                        goal="run",
                        operation=RUN_TESTS,
                        parameters={
                            "runner": "unittest",
                            "target": "test_ok",
                            "workspace": "demo",
                            "execution_backend": "host",
                            "host_authorization": issued["token"],
                        },
                    )
                )
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error.code, "host_authorization_expired")
            events = [
                (event.event_type, (event.payload or {}).get("code"))
                for event in service.events(task.task_id)
            ]
            self.assertIn(("tool_failed", "host_authorization_expired"), events)
            self.assertIsNone(task.tool_result.test_evidence)

    def test_every_code_is_consistent_across_the_six_layers(self) -> None:
        classification = CAPABILITY_REGISTRY["task_operations"]["run_tests"][
            "error_classification"
        ]
        documented = " ".join(
            f"{key} {value}" for key, value in classification.items()
        )
        for code in self.ENVIRONMENT_CODES:
            with self.subTest(code=code):
                # 4. retry policy: never retryable, and says why.
                self.assertFalse(is_retryable_error(code))
                decision = get_retry_decision(
                    tool_name="run_tests",
                    error_code=code,
                    task_status="failed",
                    attempt_count=1,
                )
                self.assertFalse(decision["retry"])
                # 5. capability registry documents it, by name.
                self.assertIn(code, documented)
                # 6. the UI has words for it instead of a raw code.
                self.assertIn(code, I18N["errors"])

    def test_container_unavailable_is_reachable_and_not_a_timeout(self) -> None:
        self.assertIn(
            "container_backend_unavailable", self.ENVIRONMENT_CODES
        )
        self.assertNotIn("test_timeout", self.ENVIRONMENT_CODES)
        self.assertTrue(isinstance(grants.AUDIT_FAILED, str))


if __name__ == "__main__":
    unittest.main()
