"""Run tests and coverage on the same interpreter, fully offline.

Phase 3.11 measured coverage with a Python 3.12 tool while the test suite
ran on Python 3.14.  The numbers happened to match, but that is an
unnecessary explanation burden for a release gate.

``coverage`` ships a pure-Python tracer that is used whenever the compiled
``tracer`` extension cannot be imported.  This runner copies an existing
local ``coverage`` installation into a temporary shim, drops the compiled
extensions, and runs the real test suite under the *current* interpreter.
Nothing is downloaded.

Usage:

    python3 -m tests.coverage_runner \
        --coverage-home <venv-or-site-packages> \
        --output artifacts/audits/phase-3.12-coverage
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CoverageEnvironmentError(RuntimeError):
    pass


def resolve_site_packages(coverage_home: Path) -> Path:
    """Accept a venv, a lib dir, or a site-packages directory."""
    candidates = [
        coverage_home,
        coverage_home / "lib",
    ]
    candidates.extend(sorted(coverage_home.glob("lib/python3*/site-packages")))
    for candidate in candidates:
        if (candidate / "coverage" / "__init__.py").is_file():
            return candidate
    raise CoverageEnvironmentError(
        f"no coverage package found under {coverage_home}"
    )


def build_shim(site_packages: Path, target: Path) -> Path:
    """Copy coverage into ``target`` without compiled extensions."""
    source = site_packages / "coverage"
    if not (source / "__init__.py").is_file():
        raise CoverageEnvironmentError(f"not a coverage package: {source}")
    destination = target / "coverage"
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.so", "*.pyd", "*.dylib"),
    )
    return target


def coverage_version(shim_root: Path) -> str | None:
    completed = subprocess.run(
        [sys.executable, "-c", "import coverage; print(coverage.__version__)"],
        capture_output=True,
        text=True,
        env=_env([shim_root]),
        cwd=PROJECT_ROOT,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _env(
    pythonpath: list[Path],
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = {
        **os.environ,
        "PYTHONPATH": ":".join(str(item) for item in pythonpath),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if extra:
        env.update(extra)
    return env


def _run(
    command: list[str],
    *,
    pythonpath: list[Path],
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=_env(pythonpath, extra_env),
    )
    return {
        "command": command,
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }


def run_coverage(
    *,
    coverage_home: Path,
    output: Path,
    test_targets: list[str] | None = None,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    site_packages = resolve_site_packages(coverage_home)
    with tempfile.TemporaryDirectory() as temp_dir:
        shim_root = build_shim(site_packages, Path(temp_dir))
        version = coverage_version(shim_root)
        if version is None:
            raise CoverageEnvironmentError(
                "coverage is not importable on this interpreter"
            )
        data_file = Path(temp_dir) / ".coverage"
        json_path = output / "coverage.json"
        targets = test_targets or ["discover", "-s", "tests"]
        if targets and targets[0] == "discover":
            unittest_args = targets
        else:
            unittest_args = list(targets)
        run = _run(
            [
                sys.executable,
                "-m",
                "coverage",
                "run",
                "--branch",
                "--source=src/zevorinth",
                "-m",
                "unittest",
                *unittest_args,
            ],
            pythonpath=[shim_root, PROJECT_ROOT / "src"],
            extra_env={"COVERAGE_FILE": str(data_file)},
        )
        if run["exit_code"] != 0:
            return {
                "status": "FAIL",
                "stage": "test_run",
                "coverage_version": version,
                "python_version": sys.version.split()[0],
                "run": run,
            }
        report = _run(
            [
                sys.executable,
                "-m",
                "coverage",
                "json",
                "-o",
                str(json_path),
                "-q",
            ],
            pythonpath=[shim_root, PROJECT_ROOT / "src"],
            extra_env={"COVERAGE_FILE": str(data_file)},
        )
        if report["exit_code"] != 0:
            return {
                "status": "FAIL",
                "stage": "coverage_report",
                "coverage_version": version,
                "python_version": sys.version.split()[0],
                "run": run,
                "report": report,
            }
    gate_path = output / "coverage-gate.json"
    gate = _run(
        [
            sys.executable,
            "tests/coverage_gate.py",
            "--coverage-json",
            str(json_path),
            "--output",
            str(gate_path),
        ],
        pythonpath=[PROJECT_ROOT / "src"],
    )
    gate_result = (
        json.loads(gate_path.read_text(encoding="utf-8"))
        if gate_path.is_file()
        else None
    )
    summary = {
        "status": (
            "PASS"
            if gate["exit_code"] == 0
            and isinstance(gate_result, dict)
            and gate_result.get("status") == "PASS"
            else "FAIL"
        ),
        "stage": "gate",
        "python_version": sys.version.split()[0],
        "coverage_version": version,
        "coverage_shim_source": str(site_packages),
        "same_interpreter": True,
        "coverage_json": str(json_path),
        "coverage_json_sha256": hashlib.sha256(
            json_path.read_bytes()
        ).hexdigest(),
        "coverage_gate_json": str(gate_path),
        "coverage_gate_sha256": (
            hashlib.sha256(gate_path.read_bytes()).hexdigest()
            if gate_path.is_file()
            else None
        ),
        "run": run,
        "gate": gate,
    }
    (output / "coverage-run.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage-home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("tests", nargs="*", default=None)
    args = parser.parse_args()
    result = run_coverage(
        coverage_home=args.coverage_home,
        output=args.output,
        test_targets=args.tests or None,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
