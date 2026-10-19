"""Subprocess helper that exits abruptly after the post-write checkpoint."""

from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import Task, TaskRequest  # noqa: E402
from zevorinth.planner import WRITE_TEXT_ARTIFACT  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


def main() -> int:
    runtime_root = Path(sys.argv[1])

    def exit_after_write(stage: str, task: Task) -> None:
        if stage != "after_write_before_verification":
            os._exit(41)
        print(task.task_id, flush=True)
        os._exit(23)

    service = TaskService(runtime_root, fault_hook=exit_after_write)
    service.submit(
        TaskRequest(
            goal="prove process restart recovery",
            operation=WRITE_TEXT_ARTIFACT,
            parameters={
                "artifact_path": "process-proof.txt",
                "content": "survives a process exit\n",
            },
        )
    )
    return 24


if __name__ == "__main__":
    raise SystemExit(main())
