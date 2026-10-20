"""Independent-process worker for atomic approval claim tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.recovery_preview import (  # noqa: E402
    RecoveryApprovalStore,
    RecoveryPreview,
)


def main() -> int:
    root = Path(sys.argv[1])
    approval_id = sys.argv[2]
    preview_path = Path(sys.argv[3])
    action = sys.argv[4]
    target = sys.argv[5]
    preview = RecoveryPreview.from_dict(
        json.loads(preview_path.read_text(encoding="utf-8"))
    )
    result = RecoveryApprovalStore(root).claim(
        approval_id,
        preview,
        action=action,
        target=target,
        now="2026-10-20T00:01:00.000+00:00",
    )
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
