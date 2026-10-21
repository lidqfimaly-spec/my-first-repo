from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReleaseReadinessTests(unittest.TestCase):
    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return environment

    def test_module_cli_and_doctor_are_machine_readable(self) -> None:
        version = subprocess.run(
            [sys.executable, "-m", "zevorinth", "--version"],
            cwd=PROJECT_ROOT,
            env=self._environment(),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertTrue(version.stdout.strip())

        doctor = subprocess.run(
            [sys.executable, "-m", "zevorinth", "doctor", "--json"],
            cwd=PROJECT_ROOT,
            env=self._environment(),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        report = json.loads(doctor.stdout)
        self.assertEqual(report["schema_version"], 4)
        self.assertTrue(report["capabilities"]["task_runtime"])
        self.assertFalse(report["deployment_scope"]["distributed_execution"])

    def test_fresh_home_initializes_v4_and_errors_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = self._environment()
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "zevorinth",
                    "--home",
                    temp_dir,
                    "run",
                    "--goal",
                    "release readiness",
                    "--artifact-path",
                    "release.txt",
                    "--content",
                    "ready",
                    "--json",
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            task = json.loads(run.stdout)
            self.assertEqual(task["status"], "succeeded")
            self.assertTrue(
                (
                    Path(temp_dir)
                    / "artifacts"
                    / task["task_id"]
                    / "release.txt"
                ).is_file()
            )
            connection = sqlite3.connect(
                Path(temp_dir) / "data" / "zevorinth.sqlite3"
            )
            try:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(version, "4")

            missing = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "zevorinth",
                    "--home",
                    temp_dir,
                    "status",
                    "missing-task",
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(missing.returncode, 2)
            self.assertIn("task not found", missing.stderr)
            self.assertNotIn("Traceback", missing.stderr)

    def test_package_entrypoint_and_data_contract(self) -> None:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        self.assertEqual(
            project["scripts"]["zevorinth"],
            "zevorinth.cli:main",
        )
        self.assertGreaterEqual(
            tuple(int(part) for part in project["requires-python"].removeprefix(">=").split(".")),
            (3, 11),
        )
        self.assertEqual(
            tomllib.loads(
                (PROJECT_ROOT / "pyproject.toml").read_text()
            )["tool"]["setuptools"]["package-data"]["zevorinth"],
            ["evals/*.json", "workbench_static/*"],
        )


if __name__ == "__main__":
    unittest.main()
