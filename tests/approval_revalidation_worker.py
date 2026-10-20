"""Subprocess worker for stale approval revalidation."""

from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.recovery_preview import (  # noqa: E402
    RecoveryApprovalStore,
    RecoveryPreview,
    validate_recovery_approval,
)


def main() -> int:
    root = Path(sys.argv[1])
    approval_id = sys.argv[2]
    preview_path = Path(sys.argv[3])
    approval = RecoveryApprovalStore(root).load(approval_id)
    preview = RecoveryPreview.from_dict(
        json.loads(preview_path.read_text(encoding="utf-8"))
    )
    verdict = validate_recovery_approval(approval, preview)
    print(json.dumps(verdict, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
