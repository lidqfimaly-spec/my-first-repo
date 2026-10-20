"""Subprocess worker for provisioning cleanup concurrency/checkpoints."""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.service import TaskService  # noqa: E402


def main() -> int:
    root = Path(sys.argv[1])
    task_id = sys.argv[2]
    mode = sys.argv[3]
    service = TaskService(root)
    if mode == "race":
        start_file = Path(sys.argv[4])
        deadline = time.monotonic() + 10
        while not start_file.exists():
            if time.monotonic() >= deadline:
                raise SystemExit("start barrier timed out")
            time.sleep(0.01)
        record = service.cleanup_provisioning_artifact(
            task_id,
            "probe-written.txt",
            enabled=True,
            authorized=True,
        )
        print(json.dumps(record, sort_keys=True), flush=True)
        return 0

    stage = sys.argv[4]

    def checkpoint(current: str) -> None:
        if current != stage:
            return
        intent = next(
            event
            for event in service.events(task_id)
            if event.event_type == "cleanup_intent"
        )
        print(
            json.dumps(
                {
                    "stage": current,
                    "operation_id": intent.payload["operation_id"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
        while True:
            signal.pause()

    service.cleanup_provisioning_artifact(
        task_id,
        "probe-written.txt",
        enabled=True,
        authorized=True,
        checkpoint=checkpoint,
    )
    return 24


if __name__ == "__main__":
    raise SystemExit(main())
