"""Subprocess worker for concurrent bounded recovery tests."""

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
    report = TaskService(root).recover_prepublish_publish_lock_failure(
        task_id,
        authorized=True,
    )
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
