"""Backend detection and unified boundary acceptance."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.isolation import (  # noqa: E402
    BOUNDARY_CHECKS,
    SANDBOX_EXEC,
    detect_backends,
    isolation_available,
    run_protected,
    verify_boundaries,
)


class BackendDetectionTests(unittest.TestCase):
    def test_detection_reports_evidence_state_per_backend(self) -> None:
        backends = detect_backends()
        self.assertEqual(
            set(backends), {"seatbelt", "docker", "podman", "low_priv_user"}
        )
        for name, entry in backends.items():
            self.assertIn(entry["state"], {
                "probe_required", "not_executed", "blocked",
            }, name)
            # Presence of a binary must never imply verification.
            self.assertNotEqual(entry["state"], "verified", name)
            self.assertIn("limitation", entry)

    def test_low_privilege_user_is_never_claimed_available(self) -> None:
        self.assertFalse(detect_backends()["low_priv_user"]["available"])


class SandboxReadEvidenceTests(unittest.TestCase):
    def test_restricted_reads_abort_even_a_trivial_binary(self) -> None:
        """Documents *why* read confinement is blocked on this platform."""
        if not isolation_available():
            self.skipTest("sandbox-exec is not available")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "ws"
            workspace.mkdir()
            inside = workspace / "in.txt"
            inside.write_text("inside", encoding="utf-8")
            profile = (
                "(version 1)\n(deny default)\n(allow process*)\n"
                "(allow file-read-metadata)\n"
                f'(allow file-read* (subpath "{workspace}"))\n'
            )
            completed = subprocess.run(
                [SANDBOX_EXEC, "-p", profile, "/bin/cat", str(inside)],
                capture_output=True,
                text=True,
            )
        # Not a denial and not a success: the process aborted, so no
        # kernel-level read boundary can be demonstrated this way.
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            completed.returncode, {134, 71, -6},
            f"unexpected exit {completed.returncode}",
        )


class BoundaryMatrixTests(unittest.TestCase):
    def test_matrix_never_verifies_without_all_checks(self) -> None:
        if not isolation_available():
            self.skipTest("sandbox-exec is not available")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "ws"
            workspace.mkdir()
            decoy = root / "outside.txt"
            decoy.write_text("x", encoding="utf-8")
            credential = root / "cred.txt"
            credential.write_text("placeholder", encoding="utf-8")
            result = verify_boundaries(
                "seatbelt", workspace, decoy, credential
            )
        self.assertIn(result["state"], {"verified", "partial", "failed", "blocked", "unverified"})
        if result["state"] != "verified":
            self.assertTrue(result["unverified_boundaries"])
            blocked = run_protected(
                ["/bin/cat", str(credential)],
                workspace=workspace,
                probe={"state": result["state"], "reason": result["reason"]},
            )
            self.assertFalse(blocked["process_started"])

    def test_unavailable_backend_is_not_executed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "ws"
            workspace.mkdir()
            decoy = root / "o.txt"
            decoy.write_text("x")
            credential = root / "c.txt"
            credential.write_text("y")
            result = verify_boundaries(
                "docker", workspace, decoy, credential
            )
        self.assertEqual(result["state"], "not_executed")
        self.assertEqual(
            set(result["checks"]), set(BOUNDARY_CHECKS)
        )


if __name__ == "__main__":
    unittest.main()
