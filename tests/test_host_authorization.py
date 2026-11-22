"""Independent acceptance tests for one-time host execution grants (S1-1).

These tests are the evidence package for the claim "host execution requires
an explicit, single-use, short-lived, scope-bound human authorization".  They
deliberately exercise the failure directions (forged, expired, cross
workspace, cross target, replayed) before the happy path, because a grant
that only works when used correctly is not an authorization mechanism.

The mutation test at the bottom of ``LockAtomicityTests`` removes the
exclusive lock and shows two consumers both winning; that is what proves the
lock is load bearing rather than decorative.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth import host_authorization as grants  # noqa: E402
from zevorinth import local_test_runner  # noqa: E402
from zevorinth.host_authorization import (  # noqa: E402
    ALREADY_USED,
    EXPIRED,
    INVALID,
    REQUIRED,
    REVOKED,
    SCOPE_MISMATCH,
    HostAuthorizationError,
    HostGrantStore,
)


def _store(root: Path) -> HostGrantStore:
    return HostGrantStore(root / "host-grants")


def _issue(
    store: HostGrantStore,
    *,
    workspace: str = "demo",
    target: str = "test_demo",
    ttl_minutes: int = 30,
    now: datetime | None = None,
) -> dict:
    return store.issue(
        workspace=workspace,
        target=target,
        issuer="pytest",
        ttl_minutes=ttl_minutes,
        now=now,
    )


def _audit_events(store: HostGrantStore) -> list[dict]:
    path = store.root / "audit.jsonl"
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def _record(store: HostGrantStore, grant_id: str) -> dict:
    raw = (store.root / f"{grant_id}.json").read_text(encoding="utf-8")
    return json.loads(raw)


def _code(callable_) -> str:
    try:
        callable_()
    except HostAuthorizationError as exc:
        return exc.code
    return "no_error"


class GrantIssueTests(unittest.TestCase):
    """Issuance: shape, storage hygiene, and input rejection."""

    def test_issued_token_matches_the_runner_token_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _store(Path(temp_dir))
            issued = _issue(store)
            token = issued["token"]
            self.assertRegex(token, grants._TOKEN_PATTERN)
            # The runner validates the parameter before the store sees it, so
            # the two patterns must agree or a valid grant is unreachable.
            self.assertIsNotNone(
                local_test_runner.AUTH_TOKEN_PATTERN.fullmatch(token)
            )

    def test_secret_is_never_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _store(Path(temp_dir))
            issued = _issue(store)
            secret = issued["token"].split(".", 1)[1]
            on_disk = (store.root / f"{issued['grant']['grant_id']}.json")
            contents = on_disk.read_text(encoding="utf-8")
            self.assertNotIn(secret, contents)
            self.assertIn("secret_sha256", contents)
            listed = store.list_grants()
            self.assertEqual(len(listed), 1)
            self.assertNotIn("secret_sha256", listed[0])
            self.assertNotIn("token", listed[0])

    def test_issue_rejects_out_of_scope_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _store(Path(temp_dir))
            cases = [
                {"workspace": "../etc", "target": "test_demo"},
                {"workspace": "", "target": "test_demo"},
                {"workspace": "demo", "target": "test.demo../x"},
                {"workspace": "demo", "target": ""},
            ]
            for case in cases:
                with self.subTest(case=case):
                    self.assertEqual(
                        _code(
                            lambda case=case: store.issue(
                                workspace=case["workspace"],
                                target=case["target"],
                                issuer="pytest",
                            )
                        ),
                        INVALID,
                    )
            self.assertEqual(
                _code(lambda: store.issue(
                    workspace="demo", target="test_demo", issuer="   "
                )),
                INVALID,
            )
            for ttl in (0, 121, -1):
                with self.subTest(ttl=ttl):
                    self.assertEqual(
                        _code(
                            lambda ttl=ttl: store.issue(
                                workspace="demo",
                                target="test_demo",
                                issuer="pytest",
                                ttl_minutes=ttl,
                            )
                        ),
                        INVALID,
                    )
            self.assertEqual(
                _code(lambda: store.issue(
                    workspace="demo", target="test_demo", issuer="pytest",
                    ttl_minutes=True,
                )),
                INVALID,
            )

    def test_scope_patterns_stay_in_sync_with_the_runner(self) -> None:
        self.assertEqual(
            grants._GRANT_WORKSPACE_PATTERN.pattern,
            local_test_runner.WORKSPACE_PATTERN.pattern,
        )
        self.assertEqual(
            grants._GRANT_TARGET_PATTERN.pattern,
            local_test_runner.TARGET_PATTERN.pattern,
        )


class GrantStorageTests(unittest.TestCase):
    """Storage hygiene: owner-only files, private root, outside the repo."""

    def test_root_and_records_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = _store(root)
            issued = _issue(store)
            self.assertEqual(stat.S_IMODE(store.root.stat().st_mode), 0o700)
            record = store.root / f"{issued['grant']['grant_id']}.json"
            self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
            audit = store.root / "audit.jsonl"
            self.assertEqual(stat.S_IMODE(audit.stat().st_mode), 0o600)

    def test_default_root_is_outside_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = _store(root)
            _issue(store)
            inside = grants.grant_root_permissions(
                store, project_root=root
            )
            self.assertFalse(inside["outside_repository"])
            outside = grants.grant_root_permissions(
                store, project_root=Path("/opt/some/other/project")
            )
            self.assertTrue(outside["outside_repository"])
            self.assertEqual(outside["mode"], "0o700")

    def test_default_root_lives_outside_any_project_tree(self) -> None:
        default_root = grants.default_grant_root()
        self.assertEqual(default_root.name, "host-grants")
        self.assertEqual(default_root.parent.name, ".zevorinth")


class GrantRejectionTests(unittest.TestCase):
    """Every way an authorization can fail must fail closed and distinctly."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.store = _store(self.root)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_missing_token_requires_authorization(self) -> None:
        for missing in (None, "", "   "):
            with self.subTest(missing=missing):
                self.assertEqual(
                    _code(
                        lambda missing=missing: self.store.consume(
                            missing, workspace="demo", target="test_demo"
                        )
                    ),
                    REQUIRED,
                )

    def test_malformed_token_is_invalid(self) -> None:
        for token in ("local-maintenance", "abc.def", "x" * 130, ".."):
            with self.subTest(token=token):
                self.assertEqual(
                    _code(
                        lambda token=token: self.store.consume(
                            token, workspace="demo", target="test_demo"
                        )
                    ),
                    INVALID,
                )

    def test_forged_secret_is_indistinguishable_from_an_unknown_grant(
        self,
    ) -> None:
        issued = _issue(self.store)
        grant_id = issued["grant"]["grant_id"]
        forged = f"{grant_id}.{'0' * 64}"
        unknown = f"{'1' * 32}.{'0' * 64}"
        messages = set()
        for token in (forged, unknown):
            with self.subTest(token=token):
                try:
                    self.store.consume(
                        token, workspace="demo", target="test_demo"
                    )
                except HostAuthorizationError as exc:
                    self.assertEqual(exc.code, INVALID)
                    messages.add(exc.message)
                else:  # pragma: no cover - fail closed guard
                    self.fail("a forged token was accepted")
        # No oracle: a forged secret must not reveal whether the grant exists.
        self.assertEqual(len(messages), 1)

    def test_expired_grant_is_rejected(self) -> None:
        issued_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        issued = _issue(self.store, ttl_minutes=1, now=issued_at)
        self.assertEqual(
            _code(
                lambda: self.store.consume(
                    issued["token"],
                    workspace="demo",
                    target="test_demo",
                    now=issued_at + timedelta(minutes=2),
                )
            ),
            EXPIRED,
        )
        events = [item["event"] for item in _audit_events(self.store)]
        self.assertIn("expired", events)

    def test_cross_workspace_consume_is_rejected(self) -> None:
        issued = _issue(self.store, workspace="demo", target="test_demo")
        self.assertEqual(
            _code(
                lambda: self.store.consume(
                    issued["token"], workspace="other", target="test_demo"
                )
            ),
            SCOPE_MISMATCH,
        )
        # The rejected attempt must not have burned the grant.
        consumed = self.store.consume(
            issued["token"], workspace="demo", target="test_demo"
        )
        self.assertEqual(consumed["status"], "consumed")

    def test_cross_target_consume_is_rejected(self) -> None:
        issued = _issue(self.store, workspace="demo", target="test_demo")
        self.assertEqual(
            _code(
                lambda: self.store.consume(
                    issued["token"],
                    workspace="demo",
                    target="test_demo.Other",
                )
            ),
            SCOPE_MISMATCH,
        )
        # A prefix of the granted target is not the granted target.
        self.assertEqual(
            _code(
                lambda: self.store.consume(
                    issued["token"], workspace="demo", target="test"
                )
            ),
            SCOPE_MISMATCH,
        )

    def test_consumed_grant_cannot_be_replayed(self) -> None:
        issued = _issue(self.store)
        self.store.consume(
            issued["token"], workspace="demo", target="test_demo"
        )
        self.assertEqual(
            _code(
                lambda: self.store.consume(
                    issued["token"], workspace="demo", target="test_demo"
                )
            ),
            ALREADY_USED,
        )

    def test_verify_does_not_consume_but_consume_does(self) -> None:
        issued = _issue(self.store)
        for _ in range(3):
            verified = self.store.verify(
                issued["token"], workspace="demo", target="test_demo"
            )
            self.assertEqual(verified["status"], "issued")
        consumed = self.store.consume(
            issued["token"], workspace="demo", target="test_demo"
        )
        self.assertEqual(consumed["status"], "consumed")
        self.assertEqual(
            consumed["consumed_workspace"], "demo"
        )
        self.assertEqual(consumed["consumed_target"], "test_demo")

    def test_verify_rejects_the_same_ways_consume_does(self) -> None:
        issued = _issue(self.store, workspace="demo", target="test_demo")
        cases = {
            REQUIRED: lambda: self.store.verify(
                None, workspace="demo", target="test_demo"
            ),
            INVALID: lambda: self.store.verify(
                "nope", workspace="demo", target="test_demo"
            ),
            SCOPE_MISMATCH: lambda: self.store.verify(
                issued["token"], workspace="other", target="test_demo"
            ),
        }
        for expected, call in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(_code(call), expected)
        issued_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stale = _issue(self.store, ttl_minutes=1, now=issued_at)
        self.assertEqual(
            _code(
                lambda: self.store.verify(
                    stale["token"],
                    workspace="demo",
                    target="test_demo",
                    now=issued_at + timedelta(minutes=5),
                )
            ),
            EXPIRED,
        )


class GrantRevocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.store = _store(Path(self._temp.name))

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_revoked_grant_cannot_be_consumed(self) -> None:
        issued = _issue(self.store)
        grant_id = issued["grant"]["grant_id"]
        revoked = self.store.revoke(grant_id, issuer="operator")
        self.assertEqual(revoked["revoked_by"], "operator")
        self.assertEqual(
            _code(
                lambda: self.store.consume(
                    issued["token"], workspace="demo", target="test_demo"
                )
            ),
            REVOKED,
        )

    def test_revoke_is_idempotent_and_records_the_first_operator(self) -> None:
        issued = _issue(self.store)
        grant_id = issued["grant"]["grant_id"]
        first = self.store.revoke(grant_id, issuer="operator-a")
        second = self.store.revoke(grant_id, issuer="operator-b")
        self.assertEqual(second["revoked_at"], first["revoked_at"])
        self.assertEqual(second["revoked_by"], "operator-a")
        revoked_events = [
            item
            for item in _audit_events(self.store)
            if item["event"] == "revoked"
        ]
        self.assertEqual(len(revoked_events), 1)

    def test_revoke_rejects_a_malformed_grant_id(self) -> None:
        for bad in ("", "zzz", "A" * 32, "0" * 31):
            with self.subTest(bad=bad):
                self.assertEqual(
                    _code(lambda bad=bad: self.store.revoke(bad, issuer="op")),
                    INVALID,
                )

    def test_revoke_of_an_unknown_grant_is_invalid(self) -> None:
        self.assertEqual(
            _code(lambda: self.store.revoke("f" * 32, issuer="op")),
            INVALID,
        )


class LockAtomicityTests(unittest.TestCase):
    """Single use must hold under concurrency, not just sequentially."""

    THREADS = 8

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.store = _store(Path(self._temp.name))
        self.issued = _issue(self.store)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _race(self) -> list[str]:
        barrier = threading.Barrier(self.THREADS)
        outcomes: list[str] = []
        lock = threading.Lock()

        def attempt() -> None:
            barrier.wait()
            try:
                self.store.consume(
                    self.issued["token"],
                    workspace="demo",
                    target="test_demo",
                )
            except HostAuthorizationError as exc:
                code = exc.code
            else:
                code = "consumed"
            with lock:
                outcomes.append(code)

        threads = [
            threading.Thread(target=attempt) for _ in range(self.THREADS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return outcomes

    def test_concurrent_consumers_produce_exactly_one_winner(self) -> None:
        outcomes = self._race()
        self.assertEqual(len(outcomes), self.THREADS)
        self.assertEqual(outcomes.count("consumed"), 1, outcomes)
        self.assertEqual(
            outcomes.count(ALREADY_USED), self.THREADS - 1, outcomes
        )
        record = _record(self.store, self.issued["grant"]["grant_id"])
        self.assertEqual(record["status"], "consumed")
        consumed_events = [
            item
            for item in _audit_events(self.store)
            if item["event"] == "consumed"
        ]
        self.assertEqual(len(consumed_events), 1)

    def test_concurrent_consumers_across_processes_produce_one_winner(
        self,
    ) -> None:
        script = (
            "import json,sys\n"
            "sys.path.insert(0, %r)\n"
            "from zevorinth.host_authorization import ("
            "HostGrantStore, HostAuthorizationError)\n"
            "store = HostGrantStore(%r)\n"
            "try:\n"
            "    store.consume(%r, workspace='demo', target='test_demo')\n"
            "except HostAuthorizationError as exc:\n"
            "    print(json.dumps({'code': exc.code}))\n"
            "else:\n"
            "    print(json.dumps({'code': 'consumed'}))\n"
        )
        results: list[str] = []
        procs = []
        for _ in range(4):
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        script
                        % (
                            str(PROJECT_ROOT / "src"),
                            str(self.store.root),
                            self.issued["token"],
                        ),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env={
                        **os.environ,
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "HOME": str(self._temp.name),
                    },
                )
            )
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err)
            results.append(json.loads(out.strip())["code"])
        self.assertEqual(results.count("consumed"), 1, results)
        self.assertEqual(
            results.count(ALREADY_USED), 3, results
        )

    def test_removing_the_lock_lets_two_consumers_both_win(self) -> None:
        """Mutation guard: the exclusive lock is load bearing.

        Without it both consumers read ``status == issued`` before either
        writes, and both report success.  Reproduced here deliberately so the
        lock cannot be simplified away later without a red test.
        """
        original_read = HostGrantStore._read_record
        barrier = threading.Barrier(2, timeout=15)

        class _NoLock:
            def __init__(self, _path: Path) -> None:
                pass

            def __enter__(self) -> "_NoLock":
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

        def racing_read(store_self, grant_id):  # type: ignore[no-untyped-def]
            record = original_read(store_self, grant_id)
            if grant_id == self.issued["grant"]["grant_id"]:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:  # pragma: no cover
                    pass
            return record

        outcomes: list[str] = []
        with mock.patch.object(grants, "_exclusive_lock", _NoLock), \
                mock.patch.object(
                    HostGrantStore, "_read_record", racing_read
                ):
            def attempt() -> None:
                try:
                    self.store.consume(
                        self.issued["token"],
                        workspace="demo",
                        target="test_demo",
                    )
                except HostAuthorizationError as exc:
                    outcomes.append(exc.code)
                else:
                    outcomes.append("consumed")

            threads = [threading.Thread(target=attempt) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(outcomes.count("consumed"), 2, outcomes)


class GrantAuditTests(unittest.TestCase):
    """The audit trail must answer who, what, when for every transition."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.store = _store(Path(self._temp.name))

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_lifecycle_transitions_are_all_recorded(self) -> None:
        first = _issue(self.store)
        self.store.consume(
            first["token"], workspace="demo", target="test_demo"
        )
        second = _issue(self.store)
        self.store.revoke(second["grant"]["grant_id"], issuer="operator")
        _code(
            lambda: self.store.consume(
                second["token"], workspace="demo", target="test_demo"
            )
        )
        _code(
            lambda: self.store.consume(
                f"{'2' * 32}.{'3' * 64}",
                workspace="demo",
                target="test_demo",
            )
        )
        events = _audit_events(self.store)
        kinds = [item["event"] for item in events]
        self.assertEqual(kinds.count("issued"), 2, kinds)
        self.assertIn("consumed", kinds)
        self.assertIn("revoked", kinds)
        self.assertIn("rejected", kinds)

        for entry in events:
            self.assertEqual(
                set(entry),
                {
                    "event",
                    "grant_id",
                    "workspace",
                    "target",
                    "issuer",
                    "reason",
                    "recorded_at",
                    "os_user",
                    "pid",
                },
            )
            self.assertIsNotNone(entry["recorded_at"])

    def test_rejected_entries_carry_the_reason_code(self) -> None:
        issued = _issue(self.store)
        _code(
            lambda: self.store.consume(
                issued["token"], workspace="demo", target="other"
            )
        )
        rejected = [
            item
            for item in _audit_events(self.store)
            if item["event"] == "rejected"
        ]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["reason"], SCOPE_MISMATCH)
        self.assertEqual(rejected[0]["workspace"], "demo")
        self.assertEqual(rejected[0]["target"], "other")

    def test_audit_never_contains_the_secret(self) -> None:
        issued = _issue(self.store)
        secret = issued["token"].split(".", 1)[1]
        self.store.consume(
            issued["token"], workspace="demo", target="test_demo"
        )
        contents = (self.store.root / "audit.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(secret, contents)


class InteractiveIssuerTests(unittest.TestCase):
    def test_issuance_refuses_a_non_interactive_process(self) -> None:
        class _NotATty:
            def isatty(self) -> bool:
                return False

        with mock.patch.object(grants.sys, "stdin", _NotATty()):
            self.assertEqual(
                _code(grants.require_interactive_issuer),
                "host_authorization_issuance_requires_tty",
            )

    def test_issuance_allows_an_interactive_terminal(self) -> None:
        class _ATty:
            def isatty(self) -> bool:
                return True

        with mock.patch.object(grants.sys, "stdin", _ATty()):
            self.assertIsNone(grants.require_interactive_issuer())


class HostExecutionGrantTests(unittest.TestCase):
    """The runner is the only place a grant is spent."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.workspace = self.root / "workspaces" / "demo"
        self.workspace.mkdir(parents=True)
        (self.workspace / "test_demo.py").write_text(
            "import unittest\n\n"
            "class DemoTests(unittest.TestCase):\n"
            "    def test_always_fails(self):\n"
            "        self.assertEqual(1, 2)\n",
            encoding="utf-8",
        )
        self.store = _store(self.root)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _parameters(self, token: str | None) -> dict:
        parameters = {
            "runner": "unittest",
            "target": "test_demo",
            "workspace": "demo",
            "execution_backend": "host",
            "timeout_seconds": 15,
            "max_output_bytes": 8192,
        }
        if token is not None:
            parameters["host_authorization"] = token
        return parameters

    def test_missing_grant_blocks_before_the_process_starts(self) -> None:
        with self.assertRaises(local_test_runner.TestExecutionError) as ctx:
            local_test_runner.run_unittest(
                project_root=self.root,
                parameters=self._parameters(None),
                grant_store=self.store,
            )
        self.assertEqual(ctx.exception.code, REQUIRED)

    def test_invalid_grant_is_refused_at_the_runner(self) -> None:
        with self.assertRaises(local_test_runner.TestExecutionError) as ctx:
            local_test_runner.run_unittest(
                project_root=self.root,
                parameters=self._parameters("local-maintenance"),
                grant_store=self.store,
            )
        self.assertEqual(ctx.exception.code, INVALID)

    def test_scope_mismatch_is_refused_at_the_runner(self) -> None:
        issued = _issue(self.store, workspace="demo", target="test_other")
        with self.assertRaises(local_test_runner.TestExecutionError) as ctx:
            local_test_runner.run_unittest(
                project_root=self.root,
                parameters=self._parameters(issued["token"]),
                grant_store=self.store,
            )
        self.assertEqual(ctx.exception.code, SCOPE_MISMATCH)

    def test_grant_is_spent_even_when_the_tests_fail(self) -> None:
        issued = _issue(self.store)
        result = local_test_runner.run_unittest(
            project_root=self.root,
            parameters=self._parameters(issued["token"]),
            grant_store=self.store,
        )
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual(result["host_grant_id"], issued["grant"]["grant_id"])
        self.assertEqual(result["host_grant_issuer"], "pytest")
        record = _record(self.store, issued["grant"]["grant_id"])
        self.assertEqual(record["status"], "consumed")
        # Never restored: a second run cannot reuse the spent grant.
        with self.assertRaises(local_test_runner.TestExecutionError) as ctx:
            local_test_runner.run_unittest(
                project_root=self.root,
                parameters=self._parameters(issued["token"]),
                grant_store=self.store,
            )
        self.assertEqual(ctx.exception.code, ALREADY_USED)

    def test_grant_store_defaults_outside_the_project_root(self) -> None:
        # When no store is injected the runner must still verify, using the
        # out-of-repository default, rather than dropping the check.
        with mock.patch.object(
            grants,
            "default_grant_root",
            lambda: self.root / "home-grants",
        ):
            with self.assertRaises(local_test_runner.TestExecutionError) as ctx:
                local_test_runner.run_unittest(
                    project_root=self.root,
                    parameters=self._parameters("a" * 32 + "." + "b" * 64),
                )
        self.assertEqual(ctx.exception.code, INVALID)


if __name__ == "__main__":
    unittest.main()
