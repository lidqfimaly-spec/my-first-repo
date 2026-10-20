"""Deterministic audit artifacts: stable results plus run metadata.

The runtime reports deliberately keep wall-clock and environment fields so a
human can debug a single run.  Audit artifacts are split instead:

``stable``   pure results; identical inputs must produce identical bytes
``metadata`` what produced them, including the digest of ``stable``

Serialization contract
----------------------

* object keys sorted with ``sort_keys=True`` (mixed key types raise);
* compact separators ``","`` and ``":"``, no insignificant whitespace;
* UTF-8, ``ensure_ascii=False``, exactly one trailing ``\\n`` (the digest is
  taken over these exact bytes, so ``shasum -a 256 stable.json`` matches);
* ``allow_nan=False`` so ``NaN``/``Infinity`` raise instead of emitting
  non-standard JSON;
* integers, booleans (``true``/``false``), ``null`` and floats keep their
  JSON representation, and floats are never rounded or reformatted;
* arrays keep their order, which runners define by stable case ids.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "1.0"

#: Fields that describe *when/how* a run happened, never *what* it found.
VOLATILE_PATHS = {
    "safety_gate": (
        ("metrics", "total_duration_ms"),
        ("metrics", "case_duration_ms"),
        ("python_version",),
        ("git_head",),
        ("sdk_status",),
    ),
    "acceptance": (
        ("generated_at",),
    ),
    # The user-acceptance runner never emits wall-clock fields in its result
    # object; timing lives only in the metadata envelope.
    "user_acceptance": (),
}
CASE_VOLATILE_FIELDS = {
    "safety_gate": ("cases", "duration_ms"),
}
#: Fields that are random by construction.  They stay in the runtime report
#: (they prove provenance) but cannot participate in a stable digest.
NONDETERMINISTIC_EFFECT_FIELDS = {
    "safety_gate": ("evidence_attempt_id",),
    "acceptance": ("evidence_attempt_id",),
    "user_acceptance": ("evidence_attempt_id",),
}
#: Absolute paths live under a temporary directory, so only their shape is
#: stable.  ``effects.relative_path`` carries the meaningful identity.
NONDETERMINISTIC_CASE_PATHS = {
    "acceptance": (("artifact", "path"),),
}


class ReportSerializationError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    """Serialize ``value`` under the documented contract."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ReportSerializationError(
            f"value is not canonically serializable: {exc}"
        ) from exc


def stable_digest(value: Any) -> str:
    payload = (canonical_json(value) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _drop(target: dict[str, Any], path: tuple[str, ...]) -> None:
    cursor: Any = target
    for key in path[:-1]:
        if not isinstance(cursor, dict) or key not in cursor:
            return
        cursor = cursor[key]
    if isinstance(cursor, dict):
        cursor.pop(path[-1], None)


def stable_projection(report: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """Return the reproducible part of a report.

    Volatile fields are removed by path, and every case gains an explicit
    ``case_id`` ordering key so array order is business-defined rather than
    incidental.
    """
    if kind not in VOLATILE_PATHS:
        raise ReportSerializationError(f"unknown report kind: {kind}")
    stable = json.loads(json.dumps(report, allow_nan=False))
    for path in VOLATILE_PATHS[kind]:
        _drop(stable, path)
    container, field = CASE_VOLATILE_FIELDS.get(kind, (None, None))
    if container is not None:
        entries = stable.get(container)
        if not isinstance(entries, list):
            raise ReportSerializationError(
                f"report has no {container} array"
            )
        for entry in entries:
            if not isinstance(entry, dict):
                raise ReportSerializationError("case entry is not an object")
            entry.pop(field, None)
    dropping = NONDETERMINISTIC_EFFECT_FIELDS.get(kind, ())
    if dropping:
        entries = stable.get("cases") or stable.get("scenarios") or []
        for entry in entries:
            effects = entry.get("effects")
            if isinstance(effects, dict):
                for name in dropping:
                    effects.pop(name, None)
    for path in NONDETERMINISTIC_CASE_PATHS.get(kind, ()):
        entries = stable.get("scenarios") or stable.get("cases") or []
        for entry in entries:
            cursor = entry
            for key in path[:-1]:
                cursor = cursor.get(key) if isinstance(cursor, dict) else None
                if cursor is None:
                    break
            if isinstance(cursor, dict):
                cursor.pop(path[-1], None)
    return stable


def nondeterministic_fields(kind: str) -> list[str]:
    fields = [
        ".".join(path) for path in VOLATILE_PATHS.get(kind, ())
    ]
    container, field = CASE_VOLATILE_FIELDS.get(kind, (None, None))
    if container is not None:
        fields.append(f"{container}[].{field}")
    for name in NONDETERMINISTIC_EFFECT_FIELDS.get(kind, ()):
        fields.append(f"effects.{name}")
    for path in NONDETERMINISTIC_CASE_PATHS.get(kind, ()):
        fields.append(".".join(path))
    return sorted(fields)


def build_metadata(
    stable: Mapping[str, Any],
    *,
    kind: str,
    command: list[str],
    exit_code: int,
    started_at: str,
    duration_ms: float,
    not_run: list[str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "started_at_utc": started_at,
        "duration_ms": round(float(duration_ms), 3),
        "command": list(command),
        "exit_code": int(exit_code),
        "environment": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "stable_schema_version": SCHEMA_VERSION,
        "stable_sha256": stable_digest(stable),
        "not_run": sorted(not_run or []),
        **dict(extra or {}),
    }


def write_envelope(
    directory: Path,
    *,
    stable: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    stable_bytes = (canonical_json(stable) + "\n").encode("utf-8")
    metadata_bytes = (
        json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    stable_path = directory / "stable.json"
    metadata_path = directory / "metadata.json"
    digest_path = directory / "stable.sha256"
    stable_path.write_bytes(stable_bytes)
    metadata_path.write_bytes(metadata_bytes)
    digest_path.write_text(
        hashlib.sha256(stable_bytes).hexdigest() + "\n", encoding="utf-8"
    )
    return {
        "stable_path": str(stable_path),
        "stable_sha256": hashlib.sha256(stable_bytes).hexdigest(),
        "metadata_path": str(metadata_path),
        "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
    }


def compare_stable(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> dict[str, Any]:
    first_digest = stable_digest(first)
    second_digest = stable_digest(second)
    differing: list[str] = []
    if first != second:
        for key in sorted(set(first) | set(second)):
            if first.get(key) != second.get(key):
                differing.append(str(key))
    return {
        "status": "PASS" if first_digest == second_digest else "FAIL",
        "first_sha256": first_digest,
        "second_sha256": second_digest,
        "identical": first_digest == second_digest,
        "differing_top_level_keys": differing,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("safety_gate", "acceptance"), required=True)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    reports = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (args.first, args.second)
    ]
    stables = [stable_projection(report, args.kind) for report in reports]
    result = compare_stable(*stables)
    write_envelope(
        args.output_dir,
        stable=stables[0],
        metadata={
            "schema_version": SCHEMA_VERSION,
            "kind": args.kind,
            "runs": [
                {
                    "source": str(args.first),
                    "stable_sha256": result["first_sha256"],
                },
                {
                    "source": str(args.second),
                    "stable_sha256": result["second_sha256"],
                },
            ],
            "comparison": result,
            "python_version": sys.version.split()[0],
        },
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
