"""S2: workspace product entry.

Ten acceptance scenarios from the review, plus the boundary that matters most:

    creating a workspace is not authorization to execute in it.

Everything here goes through the real loopback HTTP surface with the real
control token and Origin checks, so a passing test means the feature works for
a user, not that a helper function works in isolation.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.planner import RUN_TESTS  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.workbench import ReadOnlyWorkbench  # noqa: E402
from zevorinth.workbench_server import create_server  # noqa: E402

PASSING = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        self.assertTrue(True)\n"
)


class _ServerCase(unittest.TestCase):
    """Loopback server with control enabled, one temp project root."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = create_server(self.root, port=0, control_enabled=True)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"
        self.origin = self.base
        self.token = self.server.control_token or ""

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    # -- http helpers --------------------------------------------------
    def _get(self, path: str, *, token: str | None = None, origin=None):
        headers = {}
        if token is not None:
            headers["X-Zevorinth-Control-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        request = Request(f"{self.base}{path}", headers=headers, method="GET")
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def _post(self, path: str, payload, *, token=None, origin=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Zevorinth-Control-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        data = payload if isinstance(payload, bytes) else json.dumps(
            payload
        ).encode()
        request = Request(
            f"{self.base}{path}", data=data, headers=headers, method="POST"
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def _list(self):
        return self._get(
            "/control/api/workspaces", token=self.token, origin=self.origin
        )

    def _create(self, name):
        return self._post(
            "/control/api/workspaces",
            {"name": name},
            token=self.token,
            origin=self.origin,
        )


class WorkspaceListingTests(_ServerCase):
    def test_listing_requires_the_control_token(self) -> None:
        status, payload = self._get("/control/api/workspaces")
        self.assertIn(status, {401, 403})
        self.assertIn("error", payload)

    def test_listing_works_without_an_origin_header(self) -> None:
        """The browser sends no Origin on a same-origin GET.

        Regression guard: requiring Origin here made the list unreachable from
        the page it exists for, while every test passed because the tests sent
        the header by hand.
        """
        root = self.root / "workspaces"
        root.mkdir(parents=True)
        (root / "demo").mkdir()
        status, payload = self._get(
            "/control/api/workspaces", token=self.token
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in payload["workspaces"]],
                         ["demo"])

    def test_listing_still_requires_the_token_without_an_origin(self) -> None:
        status, _ = self._get("/control/api/workspaces")
        self.assertIn(status, {401, 403})

    def test_creation_still_requires_the_matching_origin(self) -> None:
        status, _ = self._post(
            "/control/api/workspaces",
            {"name": "demo"},
            token=self.token,
            origin="http://attacker.example",
        )
        self.assertEqual(status, 403)
        self.assertFalse((self.root / "workspaces" / "demo").exists())

    def test_listing_a_missing_root_is_empty_and_creates_nothing(self) -> None:
        status, payload = self._list()
        self.assertEqual(status, 200)
        self.assertFalse(payload["root_exists"])
        self.assertEqual(payload["workspaces"], [])
        self.assertFalse((self.root / "workspaces").exists())

    def test_listing_reports_counts_and_no_absolute_paths(self) -> None:
        workspace = self.root / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        (workspace / "test_ok.py").write_text(PASSING, encoding="utf-8")
        (workspace / "notes.txt").write_text("hello", encoding="utf-8")
        status, payload = self._list()
        self.assertEqual(status, 200)
        self.assertTrue(payload["root_exists"])
        entry = payload["workspaces"][0]
        self.assertEqual(entry["name"], "demo")
        self.assertEqual(entry["relative_path"], "workspaces/demo")
        self.assertEqual(entry["file_count"], 2)
        self.assertTrue(entry["has_test_files"])
        self.assertGreater(entry["size_bytes"], 0)
        self.assertIsNotNone(entry["last_modified"])
        self.assertNotIn(
            str(self.root),
            json.dumps(payload),
            "the listing must not leak the project root path",
        )

    def test_listing_skips_symlinks_instead_of_following_them(self) -> None:
        root = self.root / "workspaces"
        root.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("do not read", encoding="utf-8")
        (root / "linked").symlink_to(outside)
        (root / "real").mkdir()
        (root / "loose.txt").write_text("not a workspace", encoding="utf-8")
        status, payload = self._list()
        self.assertEqual(status, 200)
        names = [item["name"] for item in payload["workspaces"]]
        self.assertEqual(names, ["real"])
        skipped = {item["name"]: item["reason"] for item in payload["skipped"]}
        self.assertEqual(skipped["linked"], "symlink")
        self.assertEqual(skipped["loose.txt"], "not_a_directory")


class WorkspaceCreationTests(_ServerCase):
    def test_creation_returns_a_verifiable_result(self) -> None:
        status, payload = self._create("demo")
        self.assertEqual(status, 201)
        self.assertEqual(payload["name"], "demo")
        self.assertEqual(payload["relative_path"], "workspaces/demo")
        self.assertTrue(payload["exists"])
        self.assertFalse(payload["is_symlink"])
        self.assertTrue(payload["resolves_inside_workspace_root"])
        # The claim above must be checkable against the filesystem.
        created = Path(payload["absolute_path"])
        self.assertTrue(created.is_dir())
        self.assertEqual(
            created, (self.root / "workspaces" / "demo").resolve()
        )
        self.assertEqual(
            stat.S_IMODE(created.stat().st_mode) & 0o077,
            0,
            "a new workspace must not be group/world accessible",
        )
        self.assertFalse(payload["execution_authorized"])

    def test_a_created_workspace_is_immediately_listed(self) -> None:
        self._create("demo")
        _, payload = self._list()
        self.assertEqual([item["name"] for item in payload["workspaces"]],
                         ["demo"])

    def test_duplicate_creation_is_refused_with_conflict(self) -> None:
        self.assertEqual(self._create("demo")[0], 201)
        status, payload = self._create("demo")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "workspace_already_exists")

    def test_a_file_with_the_name_is_refused_not_overwritten(self) -> None:
        root = self.root / "workspaces"
        root.mkdir(parents=True)
        (root / "taken").write_text("important", encoding="utf-8")
        status, payload = self._create("taken")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "workspace_already_exists")
        self.assertEqual((root / "taken").read_text(), "important")

    def test_path_traversal_and_absolute_names_are_rejected(self) -> None:
        cases = (
            "../escape",
            "a/b",
            "..",
            ".",
            "/etc",
            "with space",
            "hidden.name",
            "",
            "x" * 65,
            "null\x00byte",
        )
        for name in cases:
            with self.subTest(name=name):
                status, payload = self._create(name)
                self.assertEqual(status, 400, payload)
                self.assertEqual(
                    payload["error"]["code"], "invalid_workspace_name"
                )
        # Nothing was created: the workspace root may not even exist yet, and
        # a partial attempt must not leave a directory behind.
        workspaces = self.root / "workspaces"
        self.assertEqual(
            list(workspaces.iterdir()) if workspaces.exists() else [], []
        )
        self.assertFalse((self.root.parent / "escape").exists())

    def test_a_symlinked_root_is_refused(self) -> None:
        outside = self.root / "elsewhere"
        outside.mkdir()
        (self.root / "workspaces").symlink_to(outside)
        status, payload = self._create("demo")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "unsafe_workspace_root")
        self.assertFalse((outside / "demo").exists())

    def test_creation_requires_token_origin_and_a_body(self) -> None:
        status, _ = self._post("/control/api/workspaces", {"name": "demo"})
        self.assertIn(status, {401, 403})
        status, _ = self._post(
            "/control/api/workspaces",
            {"name": "demo"},
            token=self.token,
            origin="http://attacker.example",
        )
        self.assertEqual(status, 403)
        status, payload = self._post(
            "/control/api/workspaces",
            b"{not json",
            token=self.token,
            origin=self.origin,
        )
        self.assertEqual(status, 400)
        self.assertFalse((self.root / "workspaces" / "demo").exists())

    def test_two_concurrent_creations_produce_one_winner(self) -> None:
        results: list[int] = []
        lock = threading.Lock()

        def attempt() -> None:
            status, _ = self._create("race")
            with lock:
                results.append(status)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(sorted(results), [201, 409])


class CreationIsNotExecutionTests(_ServerCase):
    """The boundary the review called out explicitly."""

    def test_a_created_workspace_does_not_authorize_host_execution(self) -> None:
        status, created = self._create("demo")
        self.assertEqual(status, 201)
        workspace = Path(created["absolute_path"])
        (workspace / "test_ok.py").write_text(PASSING, encoding="utf-8")

        service = TaskService(self.root)
        task = service.submit(
            TaskRequest(
                goal="run the tests I just created a workspace for",
                operation=RUN_TESTS,
                parameters={
                    "runner": "unittest",
                    "target": "test_ok",
                    "workspace": "demo",
                    "execution_backend": "host",
                },
            )
        )
        # Creating the directory is not permission to run code in it.
        self.assertEqual(task.status, TaskStatus.BLOCKED)
        self.assertEqual(task.error.code, "host_authorization_required")
        self.assertEqual(task.execution_mode.value, "not_executed")
        self.assertIsNone(task.tool_result)

    def test_created_workspace_resolves_through_the_runner_parser(self) -> None:
        _, created = self._create("demo")
        from zevorinth.local_test_runner import resolve_workspace

        resolved = resolve_workspace(self.root, "demo")
        self.assertEqual(str(resolved), created["absolute_path"])

    def test_task_detail_reports_its_workspace(self) -> None:
        self._create("demo")
        service = TaskService(self.root)
        task = service.submit(
            TaskRequest(
                goal="inspect",
                operation="inspect_workspace",
                parameters={"workspace": "demo"},
            )
        )
        workbench = ReadOnlyWorkbench(self.root)
        detail = workbench.task_detail(task.task_id)
        self.assertEqual(detail["task"]["workspace"], "demo")

    def test_workspace_association_survives_a_restart(self) -> None:
        self._create("demo")
        service = TaskService(self.root)
        task = service.submit(
            TaskRequest(
                goal="inspect",
                operation="inspect_workspace",
                parameters={"workspace": "demo"},
            )
        )
        # A fresh service and a fresh workbench read the same persisted task.
        ReadOnlyWorkbench(self.root)
        detail = ReadOnlyWorkbench(self.root).task_detail(task.task_id)
        self.assertEqual(detail["task"]["workspace"], "demo")
        listed = ReadOnlyWorkbench(self.root).list_tasks()
        self.assertIn(
            task.task_id,
            [item["task_id"] for item in listed["tasks"]],
        )
        self.assertEqual(
            next(
                item["workspace"]
                for item in listed["tasks"]
                if item["task_id"] == task.task_id
            ),
            "demo",
        )


class _HostRunCase(_ServerCase):
    """A runnable workspace plus a real single-use host grant for it."""

    def _grant(self):
        from zevorinth.host_authorization import HostGrantStore

        store = HostGrantStore(self.root / "host-grants")
        issued = store.issue(
            workspace="demo", target="test_ok", issuer="pytest"
        )
        return store, issued["token"]

    def _submit_host_test(self, store, token):
        service = TaskService(self.root, host_grant_store=store)
        return service.submit(
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

    def _ready_workspace(self) -> Path:
        self._create("demo")
        workspace = self.root / "workspaces" / "demo"
        (workspace / "test_ok.py").write_text(PASSING, encoding="utf-8")
        return workspace


class PathRaceTests(_HostRunCase):
    """Creation checks one moment; use happens at another.

    Three separate questions, deliberately answered separately rather than
    rolled into one "the workspace is safe" claim:

    1. Is the path that creation returned trusted at use time?  No — the runner
       re-resolves by *name*, so a stale path can never outlive its object.
    2. If the object at that name becomes a link, does use refuse?  Yes, and
       before the grant is spent.
    3. Is there still a window between resolving and ``chdir``?  Yes. The last
       test here proves it stays open, so the fence cannot be described as
       TOCTOU-proof.
    """

    def test_use_ignores_the_creation_result_and_resolves_by_name(self) -> None:
        """Containment is the guarantee, not content identity.

        A directory that is swapped for a *different real directory* still
        resolves inside the root, so it runs — at the name, using the object
        that is there now, not the one creation saw. That is the intended
        boundary: the workspace is user-supplied content that the fence never
        hashes, so an actor able to replace the directory could equally replace
        ``test_ok.py`` inside it.
        """
        status, _created = self._create("demo")
        self.assertEqual(status, 201)
        original = self.root / "workspaces" / "demo"
        (original / "test_ok.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        print('OLD_RAN')\n",
            encoding="utf-8",
        )
        # A concurrent actor swaps the whole directory for another one.
        os.rename(original, self.root / "workspaces" / "demo-moved")
        replacement = self.root / "workspaces" / "demo"
        os.mkdir(replacement, 0o700)
        (replacement / "test_ok.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        print('NEW_RAN')\n",
            encoding="utf-8",
        )

        store, token = self._grant()
        task = self._submit_host_test(store, token)
        self.assertEqual(task.status, TaskStatus.SUCCEEDED, task.error)
        evidence = task.tool_result.test_evidence
        self.assertEqual(evidence.exit_code, 0)
        self.assertIn("NEW_RAN", evidence.stdout_excerpt)
        # The object creation saw was never the one that ran.
        self.assertNotIn("OLD_RAN", evidence.stdout_excerpt)
        self.assertEqual(evidence.workspace, "demo")

    def test_a_symlink_staying_inside_the_root_is_still_refused(self) -> None:
        """Escaping the root is not the only thing a link can do wrong.

        ``demo -> real`` resolves inside the root, so a containment-only check
        would pass it. The runner refuses the link itself, which keeps the
        name-to-object mapping one-to-one.
        """
        from zevorinth.local_test_runner import (
            TestExecutionError,
            resolve_workspace,
        )

        root = self.root / "workspaces"
        root.mkdir(parents=True)
        (root / "real").mkdir(mode=0o700)
        (root / "demo").symlink_to(root / "real")
        with self.assertRaises(TestExecutionError) as caught:
            resolve_workspace(self.root, "demo")
        self.assertEqual(caught.exception.code, "unsafe_workspace")

    def test_the_resolve_to_exec_window_is_still_open(self) -> None:
        """Known, unfixed TOCTOU window — recorded, not papered over.

        Between ``resolve_workspace`` returning and ``Popen(cwd=...)`` running,
        the path can be replaced by a symlink and the child follows it. This
        test *demonstrates the gap on purpose*: if it ever fails, the gap has
        been closed and this test must be inverted into a refusal assertion.

        Preconditions, which is why this is recorded rather than fixed here:
        the actor must already have write access to ``workspaces/`` as the
        server's own OS user (new workspaces are 0700), i.e. it is inside the
        trust boundary. Closing it needs a child-side ``fchdir`` on a passed
        directory descriptor, which changes the spawn contract and the recorded
        command; that is a separate change, not part of S2.
        """
        import subprocess

        from zevorinth import local_test_runner as ltr

        workspace = self._ready_workspace()
        store, token = self._grant()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "test_ok.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        print('OUTSIDE_RAN')\n",
            encoding="utf-8",
        )
        real_popen = subprocess.Popen

        def substituting_popen(*args, **kwargs):
            target = Path(kwargs["cwd"])
            os.rename(target, str(target) + "-moved")
            target.symlink_to(outside)
            return real_popen(*args, **kwargs)

        with mock.patch.object(
            ltr.subprocess, "Popen", new=substituting_popen
        ):
            task = self._submit_host_test(store, token)
        evidence = task.tool_result.test_evidence
        self.assertIn(
            "OUTSIDE_RAN",
            evidence.stdout_excerpt,
            "the window appears closed — invert this test into a refusal check",
        )
        # The evidence names a workspace that no longer denotes where the code
        # ran. That mismatch is the actual harm: the record stays confident.
        self.assertEqual(evidence.workspace, "demo")
        self.assertTrue((self.root / "workspaces" / "demo").is_symlink())
        # Authorization is still not reusable even when the fence is raced.
        self.assertEqual(store.list_grants()[0]["status"], "consumed")
        self.assertNotEqual(workspace, outside)


class CreationRollbackSafetyTests(unittest.TestCase):
    """Rollback must delete the object this request created, not the path.

    A path is not an identity: by the time a rollback runs, the directory we
    made may have been removed and replaced by something else. Deleting by
    name would then destroy someone else's data.
    """

    def setUp(self) -> None:
        from unittest import mock

        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ws_root = self.root / "workspaces"
        self.ws_root.mkdir()
        self.mock = mock

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _create(self, name: str):
        from zevorinth.workspaces import WorkspaceError, create_workspace

        try:
            return create_workspace(self.root, name), None
        except WorkspaceError as exc:
            return None, exc

    def test_rollback_removes_only_the_directory_it_created(self) -> None:
        from zevorinth.local_test_runner import TestExecutionError

        with self.mock.patch(
            "zevorinth.workspaces.resolve_workspace",
            side_effect=TestExecutionError(
                "unsafe_workspace", "resolver disagreed"
            ),
        ):
            created, error = self._create("demo")
        self.assertIsNone(created)
        self.assertEqual(error.code, "unsafe_workspace")
        self.assertIn("rollback: removed", error.message)
        self.assertFalse((self.ws_root / "demo").exists())

    def test_rollback_leaves_a_replaced_directory_alone(self) -> None:
        from zevorinth.local_test_runner import TestExecutionError

        decoy = self.root / "decoy"
        decoy.mkdir()
        (decoy / "important.txt").write_text("keep me", encoding="utf-8")

        def replace_then_fail(*args, **kwargs):
            # Simulate a concurrent actor replacing our directory.
            os.rmdir(self.ws_root / "demo")
            os.rename(decoy, self.ws_root / "demo")
            raise TestExecutionError("unsafe_workspace", "resolver disagreed")

        with self.mock.patch(
            "zevorinth.workspaces.resolve_workspace",
            side_effect=replace_then_fail,
        ):
            created, error = self._create("demo")
        self.assertIsNone(created)
        self.assertIn(
            "replaced_by_another_object", error.message
        )
        # The replacement survived, with its content.
        self.assertTrue((self.ws_root / "demo" / "important.txt").exists())
        self.assertEqual(
            (self.ws_root / "demo" / "important.txt").read_text("utf-8"),
            "keep me",
        )

    def test_rollback_never_follows_a_symlink_replacement(self) -> None:
        from zevorinth.local_test_runner import TestExecutionError

        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("do not delete", encoding="utf-8")

        def swap_to_symlink(*args, **kwargs):
            os.rmdir(self.ws_root / "demo")
            (self.ws_root / "demo").symlink_to(outside)
            raise TestExecutionError("unsafe_workspace", "resolver disagreed")

        with self.mock.patch(
            "zevorinth.workspaces.resolve_workspace",
            side_effect=swap_to_symlink,
        ):
            created, error = self._create("demo")
        self.assertIsNone(created)
        self.assertIn("replaced_by_symlink", error.message)
        self.assertTrue((outside / "secret.txt").exists())
        self.assertTrue((self.ws_root / "demo").is_symlink())

    def test_a_swap_that_still_resolves_is_still_detected(self) -> None:
        """A different real directory at the same name resolves fine; the
        inode check is what catches it."""
        decoy = self.root / "decoy"
        decoy.mkdir()
        (decoy / "marker.txt").write_text("other owner's data", encoding="utf-8")

        def swap_and_resolve(*args, **kwargs):
            os.rmdir(self.ws_root / "demo")
            os.rename(decoy, self.ws_root / "demo")
            return (self.ws_root / "demo").resolve()

        with self.mock.patch(
            "zevorinth.workspaces.resolve_workspace",
            side_effect=swap_and_resolve,
        ):
            created, error = self._create("demo")
        self.assertIsNone(created)
        self.assertIn("replaced by a different object", error.message)
        self.assertTrue((self.ws_root / "demo" / "marker.txt").exists())


class WorkspaceInvalidationTests(_HostRunCase):
    """A workspace can disappear or change after a task was bound to it."""

    def test_a_deleted_workspace_blocks_the_task_without_spending_the_grant(
        self,
    ) -> None:
        import shutil

        workspace = self._ready_workspace()
        store, token = self._grant()
        shutil.rmtree(workspace)
        task = self._submit_host_test(store, token)
        self.assertEqual(task.status, TaskStatus.BLOCKED)
        self.assertEqual(task.error.code, "workspace_missing")
        self.assertEqual(store.list_grants()[0]["status"], "issued")

    def test_a_workspace_replaced_by_a_symlink_is_refused_at_use(self) -> None:
        import shutil

        workspace = self._ready_workspace()
        store, token = self._grant()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "test_ok.py").write_text(PASSING, encoding="utf-8")
        shutil.rmtree(workspace)
        workspace.symlink_to(outside)
        task = self._submit_host_test(store, token)
        self.assertEqual(task.status, TaskStatus.BLOCKED)
        self.assertEqual(task.error.code, "unsafe_workspace")
        self.assertEqual(store.list_grants()[0]["status"], "issued")
        self.assertIsNone(task.tool_result)

    def test_permission_loss_is_reported_as_unreadable_not_missing(
        self,
    ) -> None:
        workspace = self._ready_workspace()
        store, token = self._grant()
        os.chmod(workspace, 0o000)
        try:
            task = self._submit_host_test(store, token)
        finally:
            os.chmod(workspace, 0o700)
        # "Cannot look" is not "is not there": the file is still on disk.
        self.assertEqual(task.status, TaskStatus.BLOCKED)
        self.assertEqual(task.error.code, "workspace_unreadable")
        self.assertNotEqual(task.error.code, "test_target_missing")
        # Refused before execution, so nothing was spent.
        self.assertEqual(store.list_grants()[0]["status"], "issued")
        self.assertIsNone(task.tool_result)
        self.assertTrue((workspace / "test_ok.py").exists())


class RestartConsistencyTests(_ServerCase):
    def test_the_workspace_list_survives_a_server_restart(self) -> None:
        self._create("demo")
        self._create("second")
        before = [
            item["name"]
            for item in self._list()[1]["workspaces"]
        ]
        # Restart: a brand new server on the same project root.
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.server = create_server(self.root, port=0, control_enabled=True)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"
        self.origin = self.base
        self.token = self.server.control_token or ""
        after = [item["name"] for item in self._list()[1]["workspaces"]]
        self.assertEqual(before, after)
        self.assertEqual(after, ["demo", "second"])


if __name__ == "__main__":
    unittest.main()
