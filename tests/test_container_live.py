"""Live container boundary verification (runs only where a runtime exists)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.isolation import (  # noqa: E402
    _live_image_digest,
    _live_runtime_identity,
    assert_container_command_safe,
    build_container_command,
    container_runtime_state,
    run_protected_container,
)
from zevorinth.isolation_evidence import (  # noqa: E402
    build_evidence,
    verify_evidence,
)


IMAGE = "alpine:3.20"
PROBE = r"""
r(){ printf '%s=%s\n' "$1" "$2"; }
cat /workspace/inside.txt >/dev/null 2>&1 && r read_inside allowed || r read_inside failed
echo ok > /workspace/written.txt 2>/dev/null && r write_inside allowed || r write_inside failed
cat /host-outside-decoy.txt >/dev/null 2>&1 && r read_outside leaked || r read_outside denied
echo x > /outside.txt 2>/dev/null && r write_outside leaked || r write_outside denied
wget -T 2 -q -O /dev/null http://1.1.1.1 >/dev/null 2>&1 && r network allowed || r network denied
cat /root/.aws/credentials >/dev/null 2>&1 && r credential leaked || r credential denied
sh -c 'exit 0' >/dev/null 2>&1 && r subprocess allowed || r subprocess denied
"""
EXPECTED = {
    "read_inside": "allowed",
    "write_inside": "allowed",
    "read_outside": "denied",
    "write_outside": "denied",
    "network": "denied",
    "credential": "denied",
    "subprocess": "allowed",
}


def _runtime_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    if container_runtime_state()["state"] != "available_unverified":
        return False
    digest = _live_image_digest(IMAGE)
    return digest.startswith("alpine@")


class LiveContainerBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _runtime_ready():
            self.skipTest(
                "no reachable container runtime with a local alpine:3.20 image"
            )

    def test_real_boundaries_gate_and_tamper_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "inside.txt").write_text("inside\n", encoding="utf-8")
            (root / "host-outside-decoy.txt").write_text(
                "host-only\n", encoding="utf-8"
            )
            command = build_container_command(
                image=IMAGE, workspace=workspace, argv=["/bin/sh", "-c", PROBE]
            )
            assert_container_command_safe(command)
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=120
            )
            observed = dict(
                line.split("=", 1)
                for line in completed.stdout.splitlines()
                if "=" in line
            )
            self.assertEqual(observed, EXPECTED, completed.stderr)

            runtime = _live_runtime_identity()
            digest = _live_image_digest(IMAGE)
            config = hashlib.sha256(" ".join(command).encode()).hexdigest()
            evidence = build_evidence(
                environment_id="live-test",
                runtime_identity=runtime,
                image_digest=digest,
                container_config_digest=config,
                probes=[
                    {
                        "probe_id": f"live-{boundary}",
                        "boundary": boundary,
                        "status": "PASS",
                        "expected_result": want,
                        "observed_result": observed[boundary],
                        "exit_code": completed.returncode,
                        "evidence_artifact_ids": [f"stdout:{boundary}"],
                    }
                    for boundary, want in EXPECTED.items()
                ],
            )
            verdict = verify_evidence(
                evidence,
                expected_runtime=runtime,
                expected_image_digest=digest,
                expected_config_digest=config,
            )
            self.assertEqual(verdict["state"], "verified", verdict)

            gate = run_protected_container(
                workspace=workspace,
                argv=["/bin/sh", "-c", PROBE],
                image=IMAGE,
                boundary_result={"state": verdict["state"]},
                evidence=evidence,
            )
            self.assertTrue(gate["process_started"])
            self.assertEqual(gate["exit_code"], 0)

            tampered = json.loads(json.dumps(evidence))
            tampered["probes"][0]["status"] = "FAIL"
            blocked = run_protected_container(
                workspace=workspace,
                argv=["/bin/sh", "-c", PROBE],
                image=IMAGE,
                boundary_result={"state": "verified"},
                evidence=tampered,
            )
            self.assertFalse(blocked["process_started"])
            self.assertEqual(blocked["isolation_state"], "failed")


if __name__ == "__main__":
    unittest.main()
