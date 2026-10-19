from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.cli import doctor_report, main  # noqa: E402


class CliTests(unittest.TestCase):
    def test_doctor_reports_phase_one_without_overstating_model_support(self) -> None:
        report = doctor_report(PROJECT_ROOT)

        self.assertEqual(report["phase"], 1)
        self.assertEqual(report["project_root"], str(PROJECT_ROOT.resolve()))
        self.assertEqual(report["runtime_dependencies"], [])
        self.assertTrue(report["capabilities"]["task_runtime"])
        self.assertTrue(report["capabilities"]["result_verification"])
        self.assertFalse(report["capabilities"]["model_provider"])

    def test_doctor_json_is_machine_readable(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(["doctor", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["project"], "Zevorinth")
        self.assertEqual(payload["phase"], 1)

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


if __name__ == "__main__":
    unittest.main()
