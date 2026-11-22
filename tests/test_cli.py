from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.cli import doctor_report, main  # noqa: E402
from zevorinth.capabilities import CAPABILITY_REGISTRY  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ,
            {
                "ZEVORINTH_PLANNER": "deterministic",
                "OPENAI_API_KEY": "",
                "ZEVORINTH_MODEL": "",
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_doctor_reports_current_capabilities_without_overstating_model_support(self) -> None:
        report = doctor_report(PROJECT_ROOT)

        registry_phase = CAPABILITY_REGISTRY["product_stage"]
        self.assertEqual(report["phase"], registry_phase)
        self.assertEqual(report["hardening_phase"], registry_phase)
        self.assertIn(f"Phase {registry_phase}", report["notice"])
        for operation in CAPABILITY_REGISTRY["task_operations"]:
            self.assertIn(operation, report["task_types"])
            self.assertIn(operation, report["notice"])
        self.assertIn("trusted local task provenance", report["notice"])
        self.assertEqual(report["legacy_phase"], 2)
        self.assertEqual(report["project_root"], str(PROJECT_ROOT.resolve()))
        self.assertEqual(report["runtime_dependencies"], [])
        self.assertTrue(report["capabilities"]["task_runtime"])
        self.assertTrue(report["capabilities"]["write_text_artifact"])
        self.assertTrue(report["capabilities"]["run_tests"])
        self.assertTrue(report["capabilities"]["result_verification"])
        self.assertTrue(report["capabilities"]["sqlite_execution_leases"])
        self.assertFalse(report["capabilities"]["model_provider"])

    def test_doctor_json_is_machine_readable(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(["doctor", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["project"], "Zevorinth")
        self.assertEqual(
            payload["phase"], CAPABILITY_REGISTRY["product_stage"]
        )
        self.assertEqual(
            payload["capability_registry_version"],
            "capability-registry-v1",
        )

    def test_run_status_and_events_are_persisted_across_cli_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "--home",
                        temp_dir,
                        "run",
                        "--goal",
                        "create proof",
                        "--artifact-path",
                        "proof.txt",
                        "--content",
                        "cli proof\n",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            task = json.loads(output.getvalue())
            self.assertEqual(task["status"], "succeeded")
            self.assertEqual(task["execution_mode"], "real")
            artifact = Path(temp_dir) / "artifacts" / task["task_id"] / "proof.txt"
            self.assertEqual(artifact.read_text(), "cli proof\n")

            status_output = StringIO()
            with redirect_stdout(status_output):
                status_code = main(
                    [
                        "--home",
                        temp_dir,
                        "status",
                        task["task_id"],
                        "--json",
                    ]
                )
            self.assertEqual(status_code, 0)
            persisted = json.loads(status_output.getvalue())
            self.assertEqual(persisted["task_id"], task["task_id"])
            self.assertEqual(persisted["verification"]["reason"], "artifact_verified")

            events_output = StringIO()
            with redirect_stdout(events_output):
                events_code = main(
                    ["--home", temp_dir, "events", task["task_id"], "--json"]
                )
            self.assertEqual(events_code, 0)
            events = json.loads(events_output.getvalue())
            self.assertEqual(events[-1]["event_type"], "task_succeeded")
            self.assertEqual(
                [event["sequence"] for event in events],
                sorted(event["sequence"] for event in events),
            )

    def test_simulated_cli_run_reports_no_real_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "--home",
                        temp_dir,
                        "run",
                        "--goal",
                        "simulate",
                        "--artifact-path",
                        "sim.txt",
                        "--content",
                        "not written",
                        "--simulate",
                        "--json",
                    ]
                )
            task = json.loads(output.getvalue())
            self.assertEqual(exit_code, 3)
            self.assertEqual(task["status"], "simulated")
            self.assertEqual(task["execution_mode"], "simulated")
            self.assertIsNone(task["artifact"])
            self.assertFalse(
                (Path(temp_dir) / "artifacts" / task["task_id"] / "sim.txt").exists()
            )

    def test_missing_information_and_policy_block_have_distinct_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            clarification_output = StringIO()
            with redirect_stdout(clarification_output):
                clarification_code = main(
                    [
                        "--home",
                        temp_dir,
                        "run",
                        "--goal",
                        "missing content",
                        "--artifact-path",
                        "x.txt",
                        "--json",
                    ]
                )
            clarification = json.loads(clarification_output.getvalue())
            self.assertEqual(clarification_code, 3)
            self.assertEqual(clarification["status"], "awaiting_clarification")
            self.assertIn("content", clarification["missing_fields"])

            blocked_output = StringIO()
            with redirect_stdout(blocked_output):
                blocked_code = main(
                    [
                        "--home",
                        temp_dir,
                        "run",
                        "--goal",
                        "unsafe",
                        "--artifact-path",
                        "../escape.txt",
                        "--content",
                        "x",
                        "--json",
                    ]
                )
            blocked = json.loads(blocked_output.getvalue())
            self.assertEqual(blocked_code, 4)
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["execution_mode"], "not_executed")

    def test_unknown_task_returns_exit_two_without_secret_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stderr = StringIO()
            with redirect_stderr(stderr):
                exit_code = main(
                    ["--home", temp_dir, "status", "00000000-0000-0000-0000-000000000000"]
                )
            self.assertEqual(exit_code, 2)
            self.assertIn("task not found", stderr.getvalue())

    def test_sqlite_busy_returns_explicit_exit_six(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            TaskService(root, busy_timeout_ms=50)
            database = root / "data" / "zevorinth.sqlite3"
            blocker = sqlite3.connect(database, timeout=0.05)
            blocker.execute("PRAGMA journal_mode = WAL")
            blocker.execute("BEGIN IMMEDIATE")
            stderr = StringIO()
            try:
                with redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "--home",
                            temp_dir,
                            "--busy-timeout-ms",
                            "50",
                            "run",
                            "--goal",
                            "locked",
                            "--artifact-path",
                            "locked.txt",
                            "--content",
                            "locked",
                        ]
                    )
            finally:
                blocker.rollback()
                blocker.close()

            self.assertEqual(exit_code, 6)
            self.assertIn("storage or execution-claim error", stderr.getvalue())

    def test_llm_mode_without_configuration_fails_before_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "--home",
                        temp_dir,
                        "run",
                        "--planner",
                        "llm",
                        "--goal",
                        "Create a file.",
                        "--json",
                    ]
                )
            task = json.loads(output.getvalue())
            self.assertEqual(exit_code, 7)
            self.assertEqual(task["status"], "planning_failed")
            self.assertEqual(task["error"]["code"], "missing_api_key")
            self.assertEqual(task["execution_mode"], "not_executed")
            task_artifacts = Path(temp_dir) / "artifacts" / task["task_id"]
            self.assertFalse(task_artifacts.exists())

    def test_doctor_never_prints_api_key_value(self) -> None:
        sentinel = "test-only-secret-that-must-not-leak"
        output = StringIO()
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": sentinel,
                "ZEVORINTH_MODEL": "configured-test-model",
            },
            clear=False,
        ):
            with redirect_stdout(output):
                exit_code = main(["doctor", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertNotIn(sentinel, output.getvalue())
        self.assertTrue(payload["llm_planner"]["api_key_configured"])
        self.assertEqual(payload["llm_planner"]["model"], "configured-test-model")

    def _run_module_entry(self, module: str) -> subprocess.CompletedProcess:
        environment = {
            **os.environ,
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "ZEVORINTH_PLANNER": "deterministic",
            "OPENAI_API_KEY": "",
        }
        return subprocess.run(
            [sys.executable, "-m", module, "--version"],
            capture_output=True,
            text=True,
            env=environment,
            cwd=PROJECT_ROOT,
            timeout=60,
        )

    def test_package_entry_point_prints_one_version_line(self) -> None:
        completed = self._run_module_entry("zevorinth")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = [
            line
            for line in completed.stdout.splitlines()
            if line.strip()
        ]
        self.assertEqual(lines, ["0.3.0"])

    def test_cli_module_entry_matches_console_script(self) -> None:
        package = self._run_module_entry("zevorinth")
        module = self._run_module_entry("zevorinth.cli")
        self.assertEqual(module.returncode, 0, module.stderr)
        self.assertEqual(module.stdout, package.stdout)
        self.assertEqual(
            len([line for line in module.stdout.splitlines() if line.strip()]),
            1,
        )

    def test_cli_module_entry_has_no_side_effects_on_import(self) -> None:
        environment = {
            **os.environ,
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import zevorinth.cli, sys; print('ok')",
            ],
            capture_output=True,
            text=True,
            env=environment,
            cwd=PROJECT_ROOT,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")


class HostGrantCliTests(unittest.TestCase):
    """The three grant commands are the only way to authorize host execution."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.home = Path(self._temp.name)
        self._env = patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "ZEVORINTH_PLANNER": "deterministic",
                "OPENAI_API_KEY": "",
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._temp.cleanup)
        self.addCleanup(self._env.stop)

    def _run(self, *args: str) -> tuple[int, str]:
        out = StringIO()
        err = StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--home", str(self.home), *args])
        return code, out.getvalue() + err.getvalue()

    def _run_as_human(self, *args: str) -> tuple[int, str]:
        """Issue through a pseudo-terminal, as the operator would."""

        class _Tty:
            def isatty(self) -> bool:
                return True

        with patch.object(sys, "stdin", _Tty()):
            return self._run(*args)

    def test_issue_list_and_revoke_round_trip(self) -> None:
        code, output = self._run_as_human(
            "authorize-host-test",
            "--workspace",
            "demo",
            "--target",
            "test_demo",
            "--json",
        )
        self.assertEqual(code, 0, output)
        issued = json.loads(
            output[output.index("{"):]
        )
        grant_id = issued["grant"]["grant_id"]
        self.assertEqual(issued["grant"]["workspace"], "demo")
        self.assertEqual(issued["grant"]["target"], "test_demo")
        self.assertIn(".", issued["token"])

        code, listed = self._run("host-grants", "--json")
        self.assertEqual(code, 0, listed)
        payload = json.loads(listed[listed.index("{"):])
        self.assertEqual(payload["storage"]["mode"], "0o700")
        self.assertEqual(payload["grants"][0]["grant_id"], grant_id)
        # The secret never leaves the issuance call.
        self.assertNotIn(issued["token"].split(".", 1)[1], listed)

        code, revoked = self._run("revoke-host-test", grant_id, "--json")
        self.assertEqual(code, 0, revoked)
        record = json.loads(revoked[revoked.index("{"):])
        self.assertIsNotNone(record["revoked_at"])
        self.assertEqual(record["revoked_by"], record["issuer"])

    def test_revoke_unknown_grant_uses_the_authorization_exit_code(self) -> None:
        code, output = self._run(
            "revoke-host-test", "f" * 32, "--issuer", "operator"
        )
        self.assertEqual(code, 7, output)
        self.assertIn("host_authorization_invalid", output)

    def test_issue_refuses_a_non_interactive_process(self) -> None:
        code, output = self._run(
            "authorize-host-test",
            "--workspace",
            "demo",
            "--target",
            "test_demo",
        )
        self.assertEqual(code, 7, output)
        self.assertIn("host_authorization_issuance_requires_tty", output)

    def test_issue_rejects_invalid_scope(self) -> None:
        code, output = self._run_as_human(
            "authorize-host-test",
            "--workspace",
            "../outside",
            "--target",
            "test_demo",
        )
        self.assertEqual(code, 7, output)
        self.assertIn("host_authorization_invalid", output)


if __name__ == "__main__":
    unittest.main()
