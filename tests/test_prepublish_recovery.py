"""Phase 3.76: one real pre-publication failure recovery path."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tests.test_acceptance import FailingWriter, request  # noqa: E402
from zevorinth.cli import main  # noqa: E402
from zevorinth.models import TaskStatus  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402


WORKER = PROJECT_ROOT / "tests" / "prepublish_recovery_worker.py"


def _hold_publish_lock(lock_path: str, entered, release) -> None:
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        entered.set()
        release.wait(timeout=15)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class PrepublishRecoveryTests(unittest.TestCase):
    def _synthetic_publish_lock_failure(self, root: Path):
        service = TaskService(root)
        task = service.enqueue(request())
        claim = service._claim(
            task.task_id,
            allowed=service.RUNNABLE_STATUSES,
        )
        self.assertTrue(claim.acquired)
        claimed = claim.task
        self.assertIsNotNone(claimed.lease_token)
        failed = service._finish_tool_failure(
            claimed,
            claimed.lease_token,
            "publish_lock_timeout",
            "timed out waiting for the artifact publish lock",
            service.clock(),
            tool_name="write_text_artifact",
        )
        self.assertEqual(failed.status, TaskStatus.FAILED)
        self.assertEqual(failed.error.code, "publish_lock_timeout")
        return service, failed

    def _failed_publish_lock_task(self, root: Path):
        service = TaskService(root, publish_lock_timeout_seconds=0.1)
        task = service.enqueue(request())
        locks_dir = service.artifacts_root / ".locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_path = locks_dir / f"{task.task_id}.lock"
        context = multiprocessing.get_context("spawn")
        entered = context.Event()
        release = context.Event()
        holder = context.Process(
            target=_hold_publish_lock,
            args=(str(lock_path), entered, release),
        )
        holder.start()
        try:
            self.assertTrue(entered.wait(timeout=10))
            failed = service.execute_task(task.task_id)
        finally:
            release.set()
            holder.join(timeout=10)
            if holder.is_alive():
                holder.kill()
                holder.join(timeout=5)
        self.assertEqual(failed.status, TaskStatus.FAILED)
        self.assertEqual(failed.error.code, "publish_lock_timeout")
        self.assertIsNone(failed.artifact)
        self.assertIsNone(failed.verification)
        task_dir = service.artifacts_root / failed.task_id
        self.assertFalse((task_dir / "result.txt").exists())
        staging = task_dir / ".staging"
        self.assertFalse(staging.exists() and any(staging.iterdir()))
        return service, failed

    def test_recovery_creates_one_verified_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, failed = self._failed_publish_lock_task(root)
            source_before = service.require_task(failed.task_id).to_dict()
            source_events = [
                event.to_dict()
                for event in service.events(failed.task_id)
            ]

            report = service.recover_prepublish_publish_lock_failure(
                failed.task_id,
                authorized=True,
            )

            self.assertEqual(report["status"], "recovered", report)
            self.assertFalse(report["safe_to_retry"])
            self.assertFalse(report["security_certification"])
            self.assertEqual(report["assurance_level"], "L1")
            replacement = report["replacement_task"]
            self.assertEqual(replacement["status"], "succeeded")
            self.assertNotEqual(replacement["task_id"], failed.task_id)
            self.assertNotEqual(replacement["attempt_id"], failed.attempt_id)
            self.assertEqual(
                replacement["verification"]["reason"],
                "artifact_verified",
            )
            artifact = (
                service.artifacts_root
                / replacement["task_id"]
                / "result.txt"
            )
            self.assertEqual(artifact.read_text(), "verified content\n")
            self.assertEqual(
                service.require_task(failed.task_id).to_dict(),
                source_before,
            )
            self.assertEqual(
                [event.to_dict() for event in service.events(failed.task_id)],
                source_events,
            )
            created = service.events(replacement["task_id"])[0]
            self.assertEqual(created.event_type, "task_created")
            self.assertEqual(
                created.payload["recovery"]["source_task_id"],
                failed.task_id,
            )

            repeated = service.recover_prepublish_publish_lock_failure(
                failed.task_id,
                authorized=True,
            )
            self.assertEqual(repeated["status"], "already_recovered")
            self.assertEqual(
                repeated["replacement_task_id"],
                replacement["task_id"],
            )
            with sqlite3.connect(service.database_path) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0]
            self.assertEqual(count, 2)

    def test_unauthorized_and_noneligible_failures_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, failed = self._failed_publish_lock_task(root)
            unauthorized = service.recover_prepublish_publish_lock_failure(
                failed.task_id
            )
            self.assertEqual(unauthorized["status"], "rejected")
            self.assertEqual(unauthorized["code"], "recovery_unauthorized")
            with sqlite3.connect(service.database_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM tasks"
                    ).fetchone()[0],
                    1,
                )

            other_root = root / "other"
            other = TaskService(other_root, writer=FailingWriter())
            ineligible = other.submit(request())
            report = other.recover_prepublish_publish_lock_failure(
                ineligible.task_id,
                authorized=True,
            )
            self.assertEqual(report["status"], "rejected")
            self.assertEqual(
                report["code"], "recovery_error_not_eligible"
            )

    def test_target_or_staging_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, failed = self._failed_publish_lock_task(root)
            target = service.artifacts_root / failed.task_id / "result.txt"
            target.write_text("operator-owned\n", encoding="utf-8")
            report = service.recover_prepublish_publish_lock_failure(
                failed.task_id,
                authorized=True,
            )
            self.assertEqual(report["status"], "rejected")
            self.assertEqual(report["code"], "recovery_target_exists")
            self.assertEqual(
                target.read_text(encoding="utf-8"), "operator-owned\n"
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, failed = self._failed_publish_lock_task(root)
            staging = service.artifacts_root / failed.task_id / ".staging"
            staging.mkdir(exist_ok=True)
            (staging / "leftover.part").write_text("partial", encoding="utf-8")
            report = service.recover_prepublish_publish_lock_failure(
                failed.task_id,
                authorized=True,
            )
            self.assertEqual(report["status"], "rejected")
            self.assertEqual(report["code"], "recovery_staging_not_clean")

    def test_replacement_failure_is_not_retried_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, failed = self._failed_publish_lock_task(root)
            service.writer = FailingWriter()
            first = service.recover_prepublish_publish_lock_failure(
                failed.task_id,
                authorized=True,
            )
            self.assertEqual(first["status"], "recovery_failed")
            self.assertEqual(
                first["replacement_task"]["status"], "failed"
            )
            second = service.recover_prepublish_publish_lock_failure(
                failed.task_id,
                authorized=True,
            )
            self.assertEqual(second["status"], "recovery_failed")
            self.assertEqual(
                second["replacement_task_id"],
                first["replacement_task_id"],
            )
            with sqlite3.connect(service.database_path) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0]
            self.assertEqual(count, 2)

    def test_concurrent_recovery_reuses_one_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, failed = self._failed_publish_lock_task(root)
            reports = []
            errors = []

            def recover() -> None:
                try:
                    reports.append(
                        service.recover_prepublish_publish_lock_failure(
                            failed.task_id,
                            authorized=True,
                        )
                    )
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=recover) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            self.assertFalse(errors, errors)
            self.assertEqual(len(reports), 2)
            self.assertTrue(
                all(
                    item["status"]
                    in {
                        "recovered",
                        "already_recovered",
                        "recovery_in_progress",
                    }
                    for item in reports
                )
            )
            self.assertEqual(
                len({item["replacement_task_id"] for item in reports}), 1
            )
            with sqlite3.connect(service.database_path) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0]
            self.assertEqual(count, 2)

    def test_two_processes_create_one_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, failed = self._failed_publish_lock_task(root)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            command = [
                sys.executable,
                str(WORKER),
                str(root),
                failed.task_id,
            ]
            processes = [
                subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                for _ in range(2)
            ]
            reports = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr)
                reports.append(json.loads(stdout.strip()))

            self.assertEqual(
                len({item["replacement_task_id"] for item in reports}), 1
            )
            self.assertTrue(
                all(
                    item["status"]
                    in {
                        "recovered",
                        "already_recovered",
                        "recovery_in_progress",
                    }
                    for item in reports
                )
            )
            with sqlite3.connect(service.database_path) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM tasks"
                ).fetchone()[0]
            self.assertEqual(count, 2)
            replacement_id = reports[0]["replacement_task_id"]
            artifact = (
                service.artifacts_root
                / replacement_id
                / "result.txt"
            )
            self.assertEqual(artifact.read_text(), "verified content\n")

    def test_cli_requires_confirmation_and_reports_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, failed = self._failed_publish_lock_task(root)
            denied = StringIO()
            with redirect_stdout(denied):
                code = main(
                    [
                        "--home",
                        str(root),
                        "recover-prepublish",
                        failed.task_id,
                        "--json",
                    ]
                )
            self.assertEqual(code, 4)
            denied_report = json.loads(denied.getvalue())
            self.assertEqual(denied_report["status"], "rejected")

            recovered = StringIO()
            with redirect_stdout(recovered):
                code = main(
                    [
                        "--home",
                        str(root),
                        "recover-prepublish",
                        failed.task_id,
                        "--confirm",
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            report = json.loads(recovered.getvalue())
            self.assertEqual(report["status"], "recovered")
            self.assertEqual(
                report["replacement_task"]["status"], "succeeded"
            )
            self.assertFalse(report["security_certification"])

    def test_unit_recovery_contract_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, failed = self._synthetic_publish_lock_failure(
                Path(temp_dir)
            )
            denied = service.recover_prepublish_publish_lock_failure(
                failed.task_id
            )
            self.assertEqual(denied["status"], "rejected")
            self.assertEqual(denied["code"], "recovery_unauthorized")

    def test_unit_ineligible_error_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TaskService(Path(temp_dir), writer=FailingWriter())
            failed = service.submit(request())
            report = service.recover_prepublish_publish_lock_failure(
                failed.task_id,
                authorized=True,
            )
            self.assertEqual(
                report["code"], "recovery_error_not_eligible"
            )

    def test_unit_target_and_staging_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, failed = self._synthetic_publish_lock_failure(
                Path(temp_dir)
            )
            target = service.artifacts_root / failed.task_id / "result.txt"
            target.parent.mkdir(parents=True)
            target.write_text("operator-owned\n", encoding="utf-8")
            report = service.recover_prepublish_publish_lock_failure(
                failed.task_id,
                authorized=True,
            )
            self.assertEqual(report["code"], "recovery_target_exists")

        with tempfile.TemporaryDirectory() as temp_dir:
            service, failed = self._synthetic_publish_lock_failure(
                Path(temp_dir)
            )
            staging = service.artifacts_root / failed.task_id / ".staging"
            staging.mkdir(parents=True)
            (staging / "leftover.part").write_text(
                "partial", encoding="utf-8"
            )
            report = service.recover_prepublish_publish_lock_failure(
                failed.task_id,
                authorized=True,
            )
            self.assertEqual(
                report["code"], "recovery_staging_not_clean"
            )

    def test_unit_replacement_failure_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, failed = self._synthetic_publish_lock_failure(
                Path(temp_dir)
            )
            service.writer = FailingWriter()
            report = service.recover_prepublish_publish_lock_failure(
                failed.task_id,
                authorized=True,
            )
            self.assertEqual(report["status"], "recovery_failed")

    def test_unit_recovered_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, failed = self._synthetic_publish_lock_failure(
                Path(temp_dir)
            )
            report = service.recover_prepublish_publish_lock_failure(
                failed.task_id,
                authorized=True,
            )
            self.assertEqual(report["status"], "recovered")
            self.assertFalse(report["safe_to_retry"])
            self.assertEqual(report["assurance_level"], "L1")

    def test_unit_cli_confirmation_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service, failed = self._synthetic_publish_lock_failure(root)
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--home",
                        str(root),
                        "recover-prepublish",
                        failed.task_id,
                        "--json",
                    ]
                )
            self.assertEqual(code, 4)
            self.assertEqual(
                json.loads(output.getvalue())["code"],
                "recovery_unauthorized",
            )


if __name__ == "__main__":
    unittest.main()
