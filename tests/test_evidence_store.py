"""Evidence persistence: atomic writes, corruption detection, recovery."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.evidence_store import (  # noqa: E402
    EvidenceStore,
    EvidenceStoreError,
)
from zevorinth.isolation_evidence import (  # noqa: E402
    PROBE_BOUNDARIES,
    build_evidence,
    evidence_digest,
)


IDENTITY = {
    "environment_id": "env-1",
    "runtime_identity": "docker@29.2.1",
    "image_digest": "alpine@sha256:" + "a" * 64,
    "container_config_digest": "sha256:" + "b" * 64,
}
REQUEST_ID = "task-1/attempt-1"
COMMAND_DIGEST = "c" * 64


def _probes(status: str = "PASS") -> list[dict]:
    return [
        {
            "probe_id": f"p-{boundary}",
            "boundary": boundary,
            "status": status,
            "expected_result": "x",
            "observed_result": "x",
            "exit_code": 0,
            "evidence_artifact_ids": [boundary],
        }
        for boundary in PROBE_BOUNDARIES
    ]


def _evidence(evidence_id: str = "ev-1", ttl: float = 600.0) -> dict:
    return build_evidence(
        **IDENTITY,
        probes=_probes(),
        evidence_id=evidence_id,
        request_id=REQUEST_ID,
        command_digest=COMMAND_DIGEST,
        ttl_seconds=ttl,
    )


class EvidenceStoreTests(unittest.TestCase):
    def test_save_and_reload_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            evidence = _evidence()
            saved = store.save_evidence(evidence)
            self.assertEqual(saved["status"], "saved")
            reopened = EvidenceStore(Path(temp_dir))
            record = reopened.load_evidence("ev-1")
        self.assertEqual(record["request_id"], REQUEST_ID)
        self.assertEqual(record["digest"], evidence["digest"])

    def test_saving_identical_content_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            evidence = _evidence()
            store.save_evidence(evidence)
            again = store.save_evidence(evidence)
        self.assertEqual(again["status"], "unchanged")

    def test_conflicting_content_under_same_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            store.save_evidence(_evidence())
            changed = _evidence()
            changed["runtime_identity"] = "docker@30.0.0"
            changed["digest"] = evidence_digest(changed)
            with self.assertRaises(EvidenceStoreError) as ctx:
                store.save_evidence(changed)
        self.assertEqual(ctx.exception.code, "evidence_conflict")

    def test_corrupt_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            store.save_evidence(_evidence())
            (Path(temp_dir) / "evidence" / "ev-1.json").write_text("{broken")
            with self.assertRaises(EvidenceStoreError) as ctx:
                store.load_evidence("ev-1")
        self.assertEqual(ctx.exception.code, "evidence_corrupt")

    def test_truncated_record_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            store.save_evidence(_evidence())
            path = Path(temp_dir) / "evidence" / "ev-1.json"
            raw = path.read_bytes()
            path.write_bytes(raw[: len(raw) // 2])
            with self.assertRaises(EvidenceStoreError):
                store.load_evidence("ev-1")

    def test_digest_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            store.save_evidence(_evidence())
            path = Path(temp_dir) / "evidence" / "ev-1.json"
            record = json.loads(path.read_text())
            record["runtime_identity"] = "docker@tampered"
            path.write_text(json.dumps(record))
            with self.assertRaises(EvidenceStoreError) as ctx:
                store.load_evidence("ev-1")
        self.assertEqual(ctx.exception.code, "evidence_digest_mismatch")

    def test_missing_record_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(EvidenceStoreError) as ctx:
                EvidenceStore(Path(temp_dir)).load_evidence("absent")
        self.assertEqual(ctx.exception.code, "evidence_missing")

    def test_path_traversal_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            for bad in ("../escape", "a/b", "a.b", ""):
                with self.assertRaises(EvidenceStoreError):
                    store.load_evidence(bad)

    def test_store_directory_is_created_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            store.save_evidence(_evidence())
            mode = stat.S_IMODE(
                (Path(temp_dir) / "evidence").stat().st_mode
            )
        self.assertEqual(mode, 0o700)

    def test_no_temp_files_are_left_behind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            store.save_evidence(_evidence())
            leftovers = [
                p.name
                for p in (Path(temp_dir) / "evidence").iterdir()
                if p.name.startswith(".")
            ]
        self.assertEqual(leftovers, [])

    def test_unwritable_store_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "evidence"
            directory.mkdir()
            os.chmod(directory, 0o500)
            try:
                with self.assertRaises(EvidenceStoreError) as ctx:
                    EvidenceStore(Path(temp_dir)).save_evidence(_evidence())
            finally:
                os.chmod(directory, 0o700)
        self.assertIn(
            ctx.exception.code, {"store_write_failed", "store_unavailable"}
        )

    def test_invalidation_blocks_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            store.save_evidence(_evidence())
            store.invalidate_evidence("ev-1", "runtime upgraded")
            verdict = store.verify_stored_evidence(
                "ev-1",
                expected_runtime=IDENTITY["runtime_identity"],
                expected_image_digest=IDENTITY["image_digest"],
                expected_config_digest=IDENTITY["container_config_digest"],
                expected_request_id=REQUEST_ID,
                expected_command_digest=COMMAND_DIGEST,
            )
        self.assertEqual(verdict["state"], "blocked")
        self.assertIn("evidence_invalidated", verdict["reasons"])

    def test_reloaded_evidence_still_obeys_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            store.save_evidence(_evidence(ttl=60))
            verdict = EvidenceStore(Path(temp_dir)).verify_stored_evidence(
                "ev-1",
                expected_runtime=IDENTITY["runtime_identity"],
                expected_image_digest=IDENTITY["image_digest"],
                expected_config_digest=IDENTITY["container_config_digest"],
                expected_request_id=REQUEST_ID,
                expected_command_digest=COMMAND_DIGEST,
                now=time.time() + 3600,
            )
        self.assertEqual(verdict["state"], "expired")

    def test_reloaded_evidence_rechecks_live_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            store.save_evidence(_evidence())
            verdict = store.verify_stored_evidence(
                "ev-1",
                expected_runtime="docker@30.0.0",
                expected_image_digest=IDENTITY["image_digest"],
                expected_config_digest=IDENTITY["container_config_digest"],
                expected_request_id=REQUEST_ID,
                expected_command_digest=COMMAND_DIGEST,
            )
        self.assertEqual(verdict["state"], "blocked")

    def test_reloaded_evidence_rejects_other_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            store.save_evidence(_evidence())
            verdict = store.verify_stored_evidence(
                "ev-1",
                expected_runtime=IDENTITY["runtime_identity"],
                expected_image_digest=IDENTITY["image_digest"],
                expected_config_digest=IDENTITY["container_config_digest"],
                expected_request_id="task-2/attempt-1",
                expected_command_digest=COMMAND_DIGEST,
            )
        self.assertEqual(verdict["state"], "blocked")

    def test_corrupt_store_blocks_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            store.save_evidence(_evidence())
            (Path(temp_dir) / "evidence" / "ev-1.json").write_text("null")
            verdict = store.verify_stored_evidence(
                "ev-1",
                expected_runtime=IDENTITY["runtime_identity"],
                expected_image_digest=IDENTITY["image_digest"],
                expected_config_digest=IDENTITY["container_config_digest"],
            )
        self.assertEqual(verdict["state"], "blocked")

    def test_list_evidence_reports_damage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EvidenceStore(Path(temp_dir))
            store.save_evidence(_evidence("ev-1"))
            store.save_evidence(_evidence("ev-2"))
            (Path(temp_dir) / "evidence" / "ev-2.json").write_text("{")
            entries = {e["evidence_id"]: e for e in store.list_evidence()}
        self.assertEqual(entries["ev-1"]["status"], "stored")
        self.assertEqual(entries["ev-2"]["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
