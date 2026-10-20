"""Read-boundary evidence and process-tree lifecycle verification."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.isolation import (  # noqa: E402
    build_profile,
    classify,
    isolation_available,
    run_probe,
    run_protected,
)
from zevorinth.local_test_runner import _terminate  # noqa: E402


MARKER = f"ZV-ORPHAN-MARKER-3-23-{os.getpid()}-{uuid4().hex[:8]}"


def _marker_pids() -> list[int]:
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        return []
    completed = subprocess.run(
        [pgrep, "-f", MARKER], capture_output=True, text=True
    )
    if completed.returncode != 0:
        return []
    return [
        int(line) for line in completed.stdout.split() if line.strip().isdigit()
    ]


def _leftovers() -> int | None:
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        return None
    completed = subprocess.run(
        [pgrep, "-f", MARKER], capture_output=True, text=True
    )
    if completed.returncode not in (0, 1):
        return None
    return len([line for line in completed.stdout.splitlines() if line.strip()])


class ReadBoundaryMatrixTests(unittest.TestCase):
    def test_read_boundary_matrix_is_recorded_not_assumed(self) -> None:
        if not isolation_available():
            self.skipTest("sandbox-exec is not available")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "ws"
            workspace.mkdir()
            inside = workspace / "inside.txt"
            inside.write_text("inside", encoding="utf-8")
            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            credential = root / "credential-placeholder.txt"
            credential.write_text("not-a-real-secret", encoding="utf-8")
            os.symlink(outside, workspace / "escape-link")
            os.symlink(inside, root / "inbound-link")

            probe = run_probe(workspace, outside)
            results = probe["results"]
            self.assertTrue(results, probe)
            # Recorded observations, not assumptions.
            self.assertEqual(results["write_inside"], "allowed")
            self.assertEqual(results["write_outside"], "denied")
            self.assertEqual(results["network"], "denied")
            # Read confinement is not enforced by the working profile.
            self.assertEqual(results["read_outside"], "allowed")
            self.assertEqual(probe["state"], "partial")
            self.assertIn(
                "read_outside_denied", probe["unverified_boundaries"]
            )
            self.assertNotEqual(probe["state"], "verified")
            blocked = run_protected(
                ["/bin/cat", str(credential)],
                workspace=workspace,
                probe=probe,
            )
            self.assertFalse(blocked["process_started"])

    def test_classify_never_verifies_a_partial_profile(self) -> None:
        partial = {
            "read_inside": "allowed",
            "read_outside": "allowed",
            "write_inside": "allowed",
            "write_outside": "denied",
            "network": "denied",
        }
        state, missing = classify(partial)
        self.assertEqual(state, "partial")
        self.assertEqual(missing, ["read_outside_denied"])

    def test_classify_fails_when_workspace_write_is_denied(self) -> None:
        state, _missing = classify(
            {
                "read_inside": "allowed",
                "read_outside": "denied",
                "write_inside": "denied",
                "write_outside": "denied",
                "network": "denied",
            }
        )
        self.assertEqual(state, "failed")

    def test_profile_does_not_allow_global_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = build_profile(Path(temp_dir))
        self.assertIn("(subpath", profile)
        self.assertNotIn("(allow file-write*)\n", profile)


class ProcessLifecycleTests(unittest.TestCase):
    def _spawn_tree(self) -> subprocess.Popen:
        script = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', "
            f"'import time; time.sleep(60)  # {MARKER}'])\n"
            "time.sleep(60)\n"
        )
        return subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @staticmethod
    def _wait_for(predicate, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def test_timeout_termination_leaves_no_descendants(self) -> None:
        before = _leftovers()
        if before is None:
            self.skipTest("pgrep is not available to verify descendants")
        # Clear stragglers from an earlier run of this very test (identified
        # by the unique marker, never by a broad pattern).
        for pid in _marker_pids():
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        self.assertTrue(
            self._wait_for(lambda: (_leftovers() or 0) == 0),
            "pre-existing marker process left over from an earlier run",
        )
        process = self._spawn_tree()
        self.assertTrue(
            self._wait_for(lambda: (_leftovers() or 0) >= 2),
            "descendant process did not appear",
        )
        terminated = _terminate(process)
        self.assertTrue(terminated)
        self.assertIsNotNone(process.returncode)
        self.assertTrue(
            self._wait_for(lambda: (_leftovers() or 0) == 0),
            "descendant processes survived termination",
        )

    def test_unknown_detection_is_not_reported_as_zero(self) -> None:
        # When the detector is unavailable the metric must be "unknown",
        # which the helper expresses as ``None`` rather than ``0``.
        with unittest.mock.patch("shutil.which", return_value=None):
            self.assertIsNone(_leftovers())


if __name__ == "__main__":
    unittest.main()
