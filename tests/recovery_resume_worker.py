"""Subprocess worker for concurrent task resume."""

from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.service import TaskService  # noqa: E402


def main() -> int:
    root = Path(sys.argv[1])
    task_id = sys.argv[2]
    timestamp = sys.argv[3]
    service = TaskService(root, clock=lambda: timestamp)
    task = service.resume(task_id)
    print(
        json.dumps(
            {
                "status": task.status.value,
                "phase": task.phase,
                "error": task.error.code if task.error else None,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
