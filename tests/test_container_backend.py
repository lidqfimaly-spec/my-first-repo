"""Container backend configuration and fail-closed behaviour."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.isolation import (  # noqa: E402
    BOUNDARY_CHECKS,
    IsolationError,
    assert_container_command_safe,
    build_container_command,
    container_runtime_state,
    detect_backends,
    run_protected_container,
    verify_container_boundaries,
)


class ContainerCommandTests(unittest.TestCase):
    def _command(self, workspace: Path) -> list[str]:
        return build_container_command(
            image="python:3.13-slim",
            workspace=workspace,
            argv=["python", "-c", "print(1)"],
        )

    def test_hardened_flags_are_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            command = self._command(Path(temp_dir))
        joined = " ".join(command)
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn("--read-only", command)
        self.assertIn("no-new-privileges", joined)
        self.assertIn("--cap-drop", command)
        self.assertIn("65534:65534", command)
        self.assertIn("--pids-limit", command)
        self.assertIn("--memory", command)
        self.assertIn("--cpus", command)
        self.assertNotIn("--privileged", command)
        self.assertNotIn("docker.sock", joined)
        self.assertNotIn("-e", command)
        assert_container_command_safe(command)

    def test_workspace_is_the_only_mount(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            command = self._command(Path(temp_dir))
        mounts = [
            command[index + 1]
            for index, item in enumerate(command)
            if item == "--mount"
        ]
        self.assertEqual(len(mounts), 1)
        self.assertIn("dst=/workspace", mounts[0])

    def test_unsafe_variants_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = self._command(Path(temp_dir))
        variants = [
            [*base[:2], "--privileged", *base[2:]],
            [*base[: base.index("--network") + 2], "host"],
            [item if item != "none" else "bridge" for item in base],
            [*base, "-e", "HOME=/root"],
        ]
        for variant in variants:
            with self.assertRaises(IsolationError):
                assert_container_command_safe(
                    variant if "--network" in variant else base
                )

    def test_docker_socket_mount_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = self._command(Path(temp_dir))
        with self.assertRaises(IsolationError):
            assert_container_command_safe(
                [*base[:2], "-v", "/var/run/docker.sock:/var/run/docker.sock", *base[2:]]
            )

    def test_unpinned_image_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(IsolationError):
                build_container_command(
                    image="python", workspace=Path(temp_dir), argv=["true"]
                )


class ContainerRuntimeTests(unittest.TestCase):
    def test_runtime_state_is_reported_honestly(self) -> None:
        state = container_runtime_state()
        self.assertIn(
            state["state"],
            {
                "unavailable",
                "available_unverified",
                "initialization_failed",
                "blocked",
            },
        )
        # A reachable daemon is never "verified" by inspection alone.
        self.assertNotEqual(state["state"], "verified")

    def test_boundaries_are_blocked_without_a_verified_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            result = verify_container_boundaries(workspace=workspace)
            self.assertIn(result["state"], {"blocked", "unverified"})
            self.assertNotEqual(result["state"], "verified")
            self.assertEqual(set(result["checks"]), set(BOUNDARY_CHECKS))
            record = run_protected_container(
                workspace=workspace,
                argv=["true"],
                image="python:3.13-slim",
                boundary_result=result,
            )
        self.assertFalse(record["process_started"])
        self.assertFalse(record["profile_applied"])
        self.assertIsNone(record["exit_code"])

    def test_protected_container_refuses_unverified_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record = run_protected_container(
                workspace=Path(temp_dir),
                argv=["true"],
                image="python:3.13-slim",
                boundary_result={"state": "partial", "reason": "read open"},
            )
        self.assertEqual(record["isolation_state"], "partial")
        self.assertFalse(record["process_started"])

    def test_detect_backends_never_marks_container_verified(self) -> None:
        backends = detect_backends()
        self.assertNotEqual(backends["docker"]["state"], "verified")


if __name__ == "__main__":
    unittest.main()
