"""Build and verify a release artifact without network access."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_source(target: Path) -> None:
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", target / "pyproject.toml")
    shutil.copy2(PROJECT_ROOT / "README.md", target / "README.md")
    shutil.copytree(PROJECT_ROOT / "src", target / "src")


def _build_environment_report(build_python: Path, cwd: Path) -> dict[str, Any]:
    code = r"""
import hashlib
import json
import sys
from pathlib import Path
import build
import setuptools
import wheel

packages = {}
for name, module in (
    ("setuptools", setuptools),
    ("wheel", wheel),
    ("build", build),
):
    package_root = Path(module.__file__).parent
    entries = []
    for path in sorted(package_root.rglob("*")):
        if not path.is_file():
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = "unreadable"
        entries.append(
            f"{path.relative_to(package_root)}:{digest}"
        )
    manifest = hashlib.sha256(
        "\n".join(sorted(entries)).encode("utf-8")
    ).hexdigest()
    packages[name] = {
        "version": str(getattr(module, "__version__", "unknown")),
        "file_manifest_sha256": manifest,
        "package_path": str(package_root),
    }
print(json.dumps({"python": sys.version, "packages": packages}))
"""
    result = _run([str(build_python), "-c", code], cwd=cwd)
    if result["exit_code"] != 0:
        raise RuntimeError(result["stderr"])
    return json.loads(result["stdout"])


def _wheel_resource_check(wheel: Path) -> dict[str, Any]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    required = {
        "evals/planner_cases.json",
        "evals/online_pilot_v1.json",
        "evals/online_retest_v1.json",
    }
    present = {
        name.split("zevorinth/", 1)[1]
        for name in names
        if "zevorinth/evals/" in name
    }
    return {
        "wheel_names": names,
        "required_evals_present": required.issubset(present),
        "present_evals": sorted(present),
    }


def _sdist_resource_check(sdist: Path) -> dict[str, Any]:
    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
    required = {
        "planner_cases.json",
        "online_pilot_v1.json",
        "online_retest_v1.json",
    }
    present = {
        Path(name).name
        for name in names
        if "/src/zevorinth/evals/" in name
    }
    return {
        "names": names,
        "required_evals_present": required.issubset(present),
        "present_evals": sorted(present),
    }


def run_release_check(
    *,
    build_python: Path,
    target_python: Path,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    commands: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = root / "source"
        source.mkdir()
        _copy_source(source)
        build_environment = _build_environment_report(build_python, source)

        build = _run(
            [
                str(build_python),
                "-m",
                "build",
                "--no-isolation",
                "--wheel",
                "--sdist",
                "--outdir",
                str(output_dir),
            ],
            cwd=source,
            timeout=120,
        )
        commands.append(build)
        if build["exit_code"] != 0:
            return {
                "status": "failed",
                "stage": "build",
                "build_environment": build_environment,
                "commands": commands,
            }

        wheels = sorted(output_dir.glob("zevorinth-*.whl"))
        sdists = sorted(output_dir.glob("zevorinth-*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            return {
                "status": "failed",
                "stage": "artifact_discovery",
                "wheels": [str(item) for item in wheels],
                "sdists": [str(item) for item in sdists],
                "commands": commands,
            }
        wheel = wheels[0]
        sdist = sdists[0]
        resource_check = _wheel_resource_check(wheel)
        sdist_resource_check = _sdist_resource_check(sdist)

        target = root / "target"
        venv = _run(
            [str(target_python), "-m", "venv", str(target)],
            cwd=root,
        )
        commands.append(venv)
        if venv["exit_code"] != 0:
            return {
                "status": "failed",
                "stage": "venv",
                "commands": commands,
            }
        target_python_path = target / "bin" / "python"
        install = _run(
            [
                str(target_python_path),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                str(wheel),
            ],
            cwd=root,
            timeout=60,
        )
        commands.append(install)
        if install["exit_code"] != 0:
            return {
                "status": "failed",
                "stage": "install",
                "build_environment": build_environment,
                "wheel": str(wheel),
                "wheel_sha256": _sha256(wheel),
                "sdist": str(sdist),
                "sdist_sha256": _sha256(sdist),
                "resource_check": resource_check,
                "commands": commands,
            }

        entrypoint = target / "bin" / "zevorinth"
        version = _run([str(entrypoint), "--version"], cwd=root)
        doctor = _run([str(entrypoint), "doctor", "--json"], cwd=root)
        commands.extend([version, doctor])
        doctor_report = (
            json.loads(doctor["stdout"])
            if doctor["exit_code"] == 0
            else {}
        )

        home = root / "home"
        task_run = _run(
            [
                str(entrypoint),
                "--home",
                str(home),
                "run",
                "--goal",
                "packaging acceptance",
                "--artifact-path",
                "packaged.txt",
                "--content",
                "installed wheel",
                "--json",
            ],
            cwd=root,
            timeout=30,
        )
        commands.append(task_run)
        task = json.loads(task_run["stdout"]) if task_run["exit_code"] == 0 else {}
        artifact = (
            home / "artifacts" / str(task.get("task_id")) / "packaged.txt"
            if task.get("task_id")
            else None
        )
        database = home / "data" / "zevorinth.sqlite3"
        schema_version = None
        if database.exists():
            connection = sqlite3.connect(database)
            try:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                schema_version = row[0] if row else None
            finally:
                connection.close()

        import_check = _run(
            [
                str(target_python_path),
                "-c",
                (
                    "from importlib.resources import files;"
                    "p=files('zevorinth.evals').joinpath('planner_cases.json');"
                    "print(p.is_file())"
                ),
            ],
            cwd=root,
        )
        commands.append(import_check)
        target_version = _run(
            [
                str(target_python_path),
                "-c",
                "import sys;print(sys.version)",
            ],
            cwd=root,
        )
        commands.append(target_version)
        pip_check = _run(
            [
                str(target_python_path),
                "-m",
                "pip",
                "check",
            ],
            cwd=root,
        )
        commands.append(pip_check)

        checks = {
            "wheel_resource_complete": resource_check[
                "required_evals_present"
            ],
            "sdist_resource_complete": sdist_resource_check[
                "required_evals_present"
            ],
            "version_exit_zero": version["exit_code"] == 0,
            "doctor_exit_zero": doctor["exit_code"] == 0,
            "doctor_schema_v4": doctor_report.get("schema_version") == 4,
            "minimal_task_succeeded": task.get("status") == "succeeded",
            "artifact_present": bool(artifact and artifact.is_file()),
            "artifact_content_matches": bool(
                artifact
                and artifact.is_file()
                and artifact.read_text() == "installed wheel"
            ),
            "database_schema_v4": schema_version == "4",
            "resource_import_exit_zero": import_check["exit_code"] == 0,
            "resource_import_true": import_check["stdout"].strip() == "True",
            "pip_check_exit_zero": pip_check["exit_code"] == 0,
        }
        status = "passed" if all(checks.values()) else "failed"
        return {
            "status": status,
            "stage": "complete",
            "build_environment": build_environment,
            "target_python": str(target_python),
            "wheel": str(wheel),
            "wheel_sha256": _sha256(wheel),
            "sdist": str(sdist),
            "sdist_sha256": _sha256(sdist),
            "runtime_dependencies": [],
            "target_python_version": target_version["stdout"].strip(),
            "resource_check": resource_check,
            "sdist_resource_check": sdist_resource_check,
            "checks": checks,
            "schema_version": schema_version,
            "task_id": task.get("task_id"),
            "artifact": str(artifact) if artifact else None,
            "task_status": task.get("status"),
            "commands": commands,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-python", type=Path, required=True)
    parser.add_argument("--target-python", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "packaging" / "phase-3.8",
    )
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    report = run_release_check(
        build_python=args.build_python,
        target_python=args.target_python,
        output_dir=args.output_dir,
    )
    report_path = args.report or args.output_dir / "release-check.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
