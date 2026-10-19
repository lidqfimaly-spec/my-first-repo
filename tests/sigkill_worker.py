"""Subprocess helper for the real SIGKILL recovery test."""

from __future__ import annotations

import json
import signal
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import Task, TaskRequest  # noqa: E402
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


def main() -> int:
    runtime_root = Path(sys.argv[1])

    def wait_for_sigkill(stage: str, task: Task) -> None:
        if stage != "after_write_before_verification":
            return
        print(
            json.dumps(
                {
                    "task_id": task.task_id,
                    "lease_expires_at": task.lease_expires_at,
                    "artifact": task.artifact.to_dict() if task.artifact else None,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        while True:
            signal.pause()

    service = TaskService(runtime_root, fault_hook=wait_for_sigkill)
    service.submit(
        TaskRequest(
            goal="prove SIGKILL recovery",
            operation=WRITE_TEXT_ARTIFACT,
            parameters={
                "artifact_path": "sigkill-proof.txt",
                "content": "survives SIGKILL before verification\n",
            },
        )
    )
    return 24


if __name__ == "__main__":
    raise SystemExit(main())
