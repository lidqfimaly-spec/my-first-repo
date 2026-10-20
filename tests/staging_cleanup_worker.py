"""Subprocess worker for a cleanup-after-unlink checkpoint."""

from __future__ import annotations

import json
import signal
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.event_store import EventStore  # noqa: E402
from zevorinth.models import EventType  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


class CheckpointStore(EventStore):
    def append_event(self, task_id, event):
        if event.event_type == EventType.STAGING_CLEANED.value:
            print(
                json.dumps(
                    {
                        "task_id": task_id,
                        "attempt_id": event.payload["attempt_id"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            while True:
                signal.pause()
        return super().append_event(task_id, event)


def main() -> int:
    root = Path(sys.argv[1])
    timestamp = sys.argv[2]
    service = TaskService(root, clock=lambda: timestamp)
    service.store = CheckpointStore(
        root / "data" / "zevorinth.sqlite3"
    )
    service.cleanup_staging(now=timestamp)
    return 24


if __name__ == "__main__":
    raise SystemExit(main())
