"""Subprocess worker for controlled recovery execution and crash tests."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.controlled_recovery import (  # noqa: E402
    execute_controlled_restore,
)
from zevorinth.recovery_preview import (  # noqa: E402
    RecoveryApprovalStore,
    RecoveryPreview,
)


def main() -> int:
    mode = sys.argv[1]
    root = Path(sys.argv[2])
    approval_id = sys.argv[3]
    preview_path = Path(sys.argv[4])
    workspace = Path(sys.argv[5])
    ready_path = Path(sys.argv[6])
    preview = RecoveryPreview.from_dict(
        json.loads(preview_path.read_text(encoding="utf-8"))
    )

    def checkpoint(stage: str) -> None:
        expected = (
            "after_intent"
            if mode == "crash-before"
            else "after_side_effect"
        )
        if stage == expected:
            ready_path.write_text(stage, encoding="utf-8")
            time.sleep(60)

    result = execute_controlled_restore(
        RecoveryApprovalStore(root),
        approval_id,
        preview,
        workspace,
        now="2026-10-20T00:03:00.000+00:00",
        checkpoint=checkpoint if mode.startswith("crash") else None,
    )
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
