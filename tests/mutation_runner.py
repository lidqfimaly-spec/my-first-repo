"""Run fixed, isolated mutation checks without third-party dependencies."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
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


def _normalize_patches(case: dict[str, Any]) -> list[dict[str, str]]:
    if "patches" in case:
        patches = case["patches"]
        if not isinstance(patches, list) or not patches:
            raise ValueError(f"mutation {case.get('id')} has empty patches")
        return [
            {
                "path": str(patch["path"]),
                "old": str(patch["old"]),
                "new": str(patch["new"]),
            }
            for patch in patches
        ]
    return [
        {
            "path": str(case["path"]),
            "old": str(case["old"]),
            "new": str(case["new"]),
        }
    ]


def _copy_project(target: Path) -> None:
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", target / "pyproject.toml")
    shutil.copytree(PROJECT_ROOT / "src", target / "src")
    shutil.copytree(PROJECT_ROOT / "tests", target / "tests")


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


def _evaluate_mutation(
    case: dict[str, Any],
    *,
    command_override: list[str] | None = None,
) -> dict[str, Any]:
    patches = _normalize_patches(case)
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _copy_project(root)
        mutated_lines: list[dict[str, Any]] = []
        contents: dict[Path, str] = {}
        for patch in patches:
            path = root / patch["path"]
            original = contents.get(path) or path.read_text(encoding="utf-8")
            occurrences = original.count(patch["old"])
            if occurrences != 1:
                return {
                    "id": case["id"],
                    "expected": case.get("expected", "killed"),
                    "result": "invalid",
                    "reason": (
                        f"patch target count is {occurrences} "
                        f"for {patch['path']}"
                    ),
                }
            index = original.index(patch["old"])
            start_line = original[:index].count("\n") + 1
            mutated = original.replace(patch["old"], patch["new"], 1)
            contents[path] = mutated
            mutated_lines.append(
                {
                    "path": patch["path"],
                    "start_line": start_line,
                    "end_line": start_line + patch["new"].count("\n"),
                }
            )
        for path, mutated in contents.items():
            path.write_text(mutated, encoding="utf-8")
        compile_result = _run(
            [
                sys.executable,
                "-m",
                "py_compile",
                *[str(path) for path in contents],
            ],
            cwd=root,
        )
        if compile_result["exit_code"] != 0:
            return {
                "id": case["id"],
                "expected": case.get("expected", "killed"),
                "result": "invalid",
                "reason": "mutated file does not compile",
                "compile": compile_result,
            }
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
        scope = "in_process" if executed["executed"] else "unverified"
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
                # Child processes are not traced, so this is never claimed.
                "executed_in_child_process": False,
                "child_trace_supported": False,
                "trace_missing": bool(executed.get("trace_missing")),
            },
            "test": test_result,
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
    unexpected = [
        item
        for item in results
        if item.get("expected") == "killed"
        and item["result"] != "killed"
    ]
    unverified = [
        item["id"]
        for item in results
        if item.get("execution_evidence", {}).get("scope") != "in_process"
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
        "execution_evidence_counts": {
            "in_process": len(results) - len(unverified),
            "unverified": len(unverified),
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
