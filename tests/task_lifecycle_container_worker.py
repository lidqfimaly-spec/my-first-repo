"""Subprocess worker for container post-test/pre-verification recovery."""

from __future__ import annotations

import json
import signal
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.service import TaskService  # noqa: E402


def main() -> int:
    root = Path(sys.argv[1])
    task_id = sys.argv[2]
    timestamp = sys.argv[3]

    def pause_after_test(stage: str, task) -> None:
        if stage != "after_test_before_verification":
            return
        print(
            json.dumps(
                {"stage": stage, "task_id": task.task_id},
                sort_keys=True,
            ),
            flush=True,
        )
        while True:
            signal.pause()

    service = TaskService(
        root,
        clock=lambda: timestamp,
        fault_hook=pause_after_test,
    )
    service.execute_task(task_id)
    return 24


if __name__ == "__main__":
    raise SystemExit(main())
