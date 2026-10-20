"""Provision isolation evidence for one (workspace, target) pair.

Runs the seven boundary probes inside the container, records what the kernel
allowed/denied, binds the record to the exact command the caller will run,
and stores it.  The executor still only *consumes* verified evidence: this
module never runs the test itself.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .evidence_store import EvidenceStore
from .isolation import (
    _live_image_digest,
    _live_runtime_identity,
    assert_container_command_safe,
    build_container_command,
)
from .isolation_evidence import PROBE_BOUNDARIES, build_evidence
from .local_test_runner import resolve_workspace


PROBE_SCRIPT = r"""
r(){ printf '%s=%s\n' "$1" "$2"; }
cat /workspace/inside.txt >/dev/null 2>&1 && r read_inside allowed || r read_inside failed
echo ok > /workspace/probe-written.txt 2>/dev/null && r write_inside allowed || r write_inside failed
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


class ProvisioningError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def provision_evidence(
    root: Path,
    *,
    image: str,
    workspace_name: str,
    target: str,
    request_id: str,
    evidence_id: str,
    ttl_seconds: float = 900.0,
) -> dict[str, Any]:
    workspace = resolve_workspace(root, workspace_name)
    (workspace / "inside.txt").write_text("inside\n", encoding="utf-8")
    probe_command = build_container_command(
        image=image, workspace=workspace, argv=["/bin/sh", "-c", PROBE_SCRIPT]
    )
    assert_container_command_safe(probe_command)
    completed = subprocess.run(
        probe_command, capture_output=True, text=True, timeout=120
    )
    observed = dict(
        line.split("=", 1)
        for line in completed.stdout.splitlines()
        if "=" in line
    )
    if observed != EXPECTED:
        raise ProvisioningError(
            "boundary_probe_failed", json.dumps(observed, sort_keys=True)
        )
    argv = ["python", "-B", "-m", "unittest", "-q", target]
    run_command = build_container_command(
        image=image, workspace=workspace, argv=argv
    )
    evidence = build_evidence(
        environment_id="local",
        runtime_identity=_live_runtime_identity(),
        image_digest=_live_image_digest(image),
        container_config_digest=hashlib.sha256(
            " ".join(run_command).encode("utf-8")
        ).hexdigest(),
        request_id=request_id,
        command_digest=hashlib.sha256(
            json.dumps(argv, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        probes=[
            {
                "probe_id": f"provision-{boundary}",
                "boundary": boundary,
                "status": "PASS",
                "expected_result": EXPECTED[boundary],
                "observed_result": observed[boundary],
                "exit_code": completed.returncode,
                "evidence_artifact_ids": [f"probe:{boundary}"],
            }
            for boundary in PROBE_BOUNDARIES
        ],
        evidence_id=evidence_id,
        ttl_seconds=ttl_seconds,
    )
    stored = EvidenceStore(root).save_evidence(evidence)
    return {
        "status": stored["status"],
        "evidence_id": evidence["evidence_id"],
        "image_digest": evidence["image_digest"],
        "observed": observed,
    }
