from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.event_store import EventStore  # noqa: E402
from zevorinth.isolation import (  # noqa: E402
    inventory_quarantine,
    isolation_available,
    run_probe,
    run_protected,
)
from zevorinth.models import TaskRequest  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


def _written(root: Path):
    service = TaskService(root)
    task = service.submit(
        TaskRequest(
            goal="write",
            operation="write_text_artifact",
            parameters={"artifact_path": "a.txt", "content": "hi\n"},
        )
    )
    return service, task, root / "artifacts" / task.task_id


class DurableIntentGateTests(unittest.TestCase):
    def test_store_failure_blocks_every_filesystem_effect(self) -> None:
        class DownStore(EventStore):
            def record_event_and_update(self, *args, **kwargs):
                raise RuntimeError("down")

            def list_events(self, *args, **kwargs):
                raise RuntimeError("down")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, task_dir = _written(root)
            service.store = DownStore(service.database_path)
            record = service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            self.assertEqual(record["status"], "unverified")
            self.assertEqual(record["code"], "store_unavailable")
            self.assertTrue((task_dir / "a.txt").exists())
            self.assertFalse((task_dir / ".quarantine").exists())

    def test_intent_write_failure_blocks_quarantine(self) -> None:
        class IntentFails(EventStore):
            def record_event_and_update(self, *args, **kwargs):
                raise RuntimeError("intent write failed")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, task_dir = _written(root)
            service.store = IntentFails(service.database_path)
            record = service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            self.assertEqual(record["status"], "unverified")
            self.assertEqual(record["code"], "intent_not_persisted")
            self.assertFalse(record["intent_persisted"])
            self.assertIs(record["intent_reconciled"], False)
            self.assertTrue((task_dir / "a.txt").exists())
            self.assertFalse((task_dir / ".quarantine").exists())

    def test_intent_persisted_before_any_file_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, task_dir = _written(root)
            service.cleanup_workspace_file(
                task.task_id, "a.txt", enabled=True, authorized=True
            )
            events = [e.event_type for e in service.events(task.task_id)]
            self.assertIn("cleanup_intent", events)
            self.assertLess(
                events.index("cleanup_intent"),
                events.index("cleanup_completed"),
            )


class IsolationBoundaryTests(unittest.TestCase):
    def test_probe_distinguishes_kernel_denial_from_unenforced(self) -> None:
        if not isolation_available():
            self.skipTest("sandbox-exec is not available on this platform")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "ws"
            workspace.mkdir()
            decoy = root / "decoy.txt"
            decoy.write_text("secret", encoding="utf-8")
            result = run_probe(workspace, decoy)
        self.assertIn(result["state"], {"verified", "partial", "blocked", "failed"})
        if result["results"]:
            # The working profile denies writes outside and the network.
            self.assertEqual(result["results"]["write_outside"], "denied")
            self.assertEqual(result["results"]["network"], "denied")
            self.assertEqual(result["results"]["write_inside"], "allowed")
            if result["results"]["read_outside"] == "allowed":
                # Read confinement is not enforced, so it must not be verified.
                self.assertEqual(result["state"], "partial")
                self.assertIn(
                    "read_outside_denied", result["unverified_boundaries"]
                )
        if result["state"] != "verified":
            self.assertFalse(run_protected(
                ["/bin/echo", "hi"],
                workspace=workspace,
                probe=result,
            )["process_started"])

    def test_protected_execution_never_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            marker = workspace / "must-not-exist.txt"
            record = run_protected(
                ["/bin/sh", "-c", f"touch {marker}"],
                workspace=workspace,
                probe={"state": "partial", "reason": "read not confined"},
            )
            self.assertEqual(record["isolation_state"], "partial")
            self.assertFalse(record["process_started"])
            self.assertFalse(record["profile_applied"])
            self.assertIsNone(record["exit_code"])
            self.assertFalse(marker.exists())

    def test_protected_execution_rejects_blocked_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record = run_protected(
                ["/bin/echo", "hi"],
                workspace=Path(temp_dir),
                probe={"state": "blocked", "reason": "sandbox-exec missing"},
            )
        self.assertFalse(record["process_started"])
        self.assertEqual(record["isolation_state"], "blocked")

    def test_unisolated_class_is_explicit(self) -> None:
        from zevorinth.isolation import UNISOLATED_ALLOWED_OPERATIONS

        self.assertEqual(
            UNISOLATED_ALLOWED_OPERATIONS, frozenset({"run_tests"})
        )

    def test_inventory_is_read_only_and_empty_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, task, task_dir = _written(root)
            self.assertEqual(inventory_quarantine(task_dir), [])
            (task_dir / ".quarantine" / "op-1").mkdir(parents=True)
            (task_dir / ".quarantine" / "op-1" / "a.txt").write_text("x")
            entries = inventory_quarantine(task_dir)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["operation_id"], "op-1")
            self.assertFalse(entries[0]["retention_candidate"])
            self.assertTrue(
                (task_dir / ".quarantine" / "op-1" / "a.txt").exists()
            )


if __name__ == "__main__":
    unittest.main()
