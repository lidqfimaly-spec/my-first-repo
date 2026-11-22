"""F-13/F-14/F-15: artifact lineage and "keep working on this".

The product gap: a finished artifact could be read and downloaded, but a user
who wanted to change one character had to compute a SHA-256 by hand and type
it into a task definition. That is not a product, it is an API test.

What is checked here:

* the lineage projection says where a derived artifact came from, and stays
  silent for a first-generation one;
* "continue from this" is offered only when the engine would actually accept
  it — a verified, unchanged artifact from a succeeded task — and refuses with
  a reason otherwise;
* the whole path works over HTTP: create from the prefilled payload, start it,
  and the resulting artifact records the source it came from.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.service import TaskService  # noqa: E402
from zevorinth.models import TaskRequest, TaskStatus  # noqa: E402
from zevorinth.workbench import ReadOnlyWorkbench  # noqa: E402
from zevorinth.workbench_server import create_server  # noqa: E402


class _ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir(parents=True)
        self.service = TaskService(self.root)
        self.server = create_server(self.root, port=0, control_enabled=True)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"
        self.token = self._get("/control/api/session")["token"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def _get(self, path: str):
        request = Request(
            self.base + path,
            headers={
                "X-Zevorinth-Control-Token": getattr(self, "token", ""),
                "Origin": self.base,
            },
        )
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def _post(self, path: str, body: dict):
        request = Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Zevorinth-Control-Token": self.token,
                "Origin": self.base,
            },
        )
        with urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())

    def _write_task(self, name: str, content: str):
        return self.service.submit(
            TaskRequest(
                goal=f"write {name}",
                operation="write_text_artifact",
                parameters={"artifact_path": name, "content": content},
            )
        )


class LineageProjectionTests(_ServerCase):
    def test_a_first_generation_artifact_has_no_lineage(self) -> None:
        task = self._write_task("plain.txt", "first\n")
        detail = ReadOnlyWorkbench(self.root).task_detail(task.task_id)
        self.assertIsNone(detail["task"]["lineage"])

    def test_a_derived_artifact_records_its_source(self) -> None:
        source = self._write_task("base.txt", "base\n")
        derived = self.service.submit(
            TaskRequest(
                goal="derive",
                operation="modify_text_artifact",
                parameters={
                    "source_task_id": source.task_id,
                    "artifact_path": "base.txt",
                    "expected_sha256": source.artifact.sha256,
                    "content": "base\nextra\n",
                    "mode": "append",
                    "output_path": "base-v2.txt",
                },
            )
        )
        self.assertEqual(derived.status, TaskStatus.SUCCEEDED)
        detail = ReadOnlyWorkbench(self.root).task_detail(derived.task_id)
        lineage = detail["task"]["lineage"]
        self.assertIsNotNone(lineage)
        self.assertEqual(lineage["source_task_id"], source.task_id)
        self.assertEqual(lineage["source_artifact_path"], "base.txt")
        self.assertEqual(lineage["source_sha256"], source.artifact.sha256)
        self.assertEqual(lineage["mode"], "append")
        self.assertEqual(
            lineage["result_sha256"], derived.artifact.sha256
        )
        self.assertEqual(
            lineage["result_byte_count"], derived.artifact.byte_count
        )


class ContinuationOfferTests(_ServerCase):
    def test_a_verified_artifact_offers_continuation_with_the_digest(
        self,
    ) -> None:
        task = self._write_task("note.txt", "hello\n")
        detail = ReadOnlyWorkbench(self.root).task_detail(task.task_id)
        continuation = detail["task"]["continuation"]
        self.assertTrue(continuation["available"])
        self.assertEqual(continuation["operation"], "modify_text_artifact")
        self.assertEqual(continuation["source_task_id"], task.task_id)
        self.assertEqual(continuation["artifact_path"], "note.txt")
        # The whole point: the user never types this value.
        self.assertEqual(
            continuation["expected_sha256"], task.artifact.sha256
        )
        self.assertEqual(continuation["suggested_output_path"], "note-v2.txt")
        self.assertEqual(continuation["modes"], ["replace", "append"])

    def test_a_changed_artifact_withdraws_the_offer(self) -> None:
        task = self._write_task("note.txt", "hello\n")
        target = (
            self.root / "artifacts" / task.task_id / "note.txt"
        )
        target.write_text("someone else wrote this\n", encoding="utf-8")
        detail = ReadOnlyWorkbench(self.root).task_detail(task.task_id)
        continuation = detail["task"]["continuation"]
        self.assertFalse(continuation["available"])
        self.assertEqual(
            continuation["reason"], "artifact_changed_since_registration"
        )

    def test_a_task_without_an_artifact_offers_nothing(self) -> None:
        detail_task = self.service.enqueue(
            TaskRequest(
                goal="nothing yet",
                operation="write_text_artifact",
                parameters={"artifact_path": "x.txt", "content": "x"},
            )
        )
        detail = ReadOnlyWorkbench(self.root).task_detail(detail_task.task_id)
        continuation = detail["task"]["continuation"]
        self.assertFalse(continuation["available"])
        self.assertEqual(continuation["reason"], "no_artifact")


class ContinueOverHTTPTests(_ServerCase):
    def _continue(self, source_task_id: str, detail: dict, body: dict):
        return self._post(
            "/control/api/tasks",
            {
                "request_id": str(uuid.uuid4()),
                "confirmed": True,
                "goal": "在 note.txt 基础上再改一版",
                "operation": "modify_text_artifact",
                "parameters": {
                    "source_task_id": source_task_id,
                    "artifact_path": detail["task"]["continuation"][
                        "artifact_path"
                    ],
                    "expected_sha256": detail["task"]["continuation"][
                        "expected_sha256"
                    ],
                    "output_path": detail["task"]["continuation"][
                        "suggested_output_path"
                    ],
                    **body,
                },
            },
        )

    def test_the_prefilled_payload_is_accepted_and_runs(self) -> None:
        source = self._write_task("note.txt", "hello\n")
        detail = self._get(f"/api/tasks/{source.task_id}")
        status, created = self._continue(
            source.task_id, detail, {"content": "hello again\n"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["task_status"], "queued")

        self._post(
            f"/control/api/tasks/{created['task_id']}/dispatch",
            {"request_id": str(uuid.uuid4())},
        )
        finished = self._wait(created["task_id"])
        self.assertEqual(finished["task"]["status"], "succeeded")
        self.assertEqual(
            finished["task"]["artifact"]["relative_path"], "note-v2.txt"
        )
        written = (
            self.root
            / "artifacts"
            / created["task_id"]
            / "note-v2.txt"
        )
        self.assertEqual(written.read_text(encoding="utf-8"), "hello again\n")
        lineage = finished["task"]["lineage"]
        self.assertEqual(lineage["source_task_id"], source.task_id)
        # And the new artifact can itself be continued from.
        self.assertTrue(finished["task"]["continuation"]["available"])

    def test_a_stale_digest_is_refused_by_the_engine(self) -> None:
        source = self._write_task("note.txt", "hello\n")
        detail = self._get(f"/api/tasks/{source.task_id}")
        (
            self.root / "artifacts" / source.task_id / "note.txt"
        ).write_text("tampered\n", encoding="utf-8")
        status, created = self._continue(
            source.task_id, detail, {"content": "next\n"}
        )
        self.assertEqual(status, 201)
        self._post(
            f"/control/api/tasks/{created['task_id']}/dispatch",
            {"request_id": str(uuid.uuid4())},
        )
        finished = self._wait(created["task_id"])
        self.assertEqual(finished["task"]["status"], "blocked")
        self.assertEqual(
            finished["task"]["error"]["code"], "source_artifact_changed"
        )

    def _wait(self, task_id: str, attempts: int = 100) -> dict:
        import time

        for _ in range(attempts):
            detail = self._get(f"/api/tasks/{task_id}")
            if detail["task"]["status"] in {
                "succeeded",
                "failed",
                "blocked",
                "partial",
                "cancelled",
            }:
                return detail
            time.sleep(0.1)
        raise AssertionError("task did not reach a terminal status")


class DownstreamChainTests(_ServerCase):
    def test_a_derived_task_appears_downstream_of_its_source(self) -> None:
        source = self._write_task("base.txt", "base\n")
        derived = self.service.submit(
            TaskRequest(
                goal="derive",
                operation="modify_text_artifact",
                parameters={
                    "source_task_id": source.task_id,
                    "artifact_path": "base.txt",
                    "expected_sha256": source.artifact.sha256,
                    "content": "base\nextra\n",
                    "mode": "append",
                },
            )
        )
        detail = ReadOnlyWorkbench(self.root).task_detail(source.task_id)
        downstream = detail["task"]["derived_tasks"]
        self.assertEqual(
            [item["task_id"] for item in downstream], [derived.task_id]
        )
        self.assertEqual(downstream[0]["artifact_path"], "base.txt")
        self.assertTrue(downstream[0]["verified"])

    def test_an_unrelated_task_writing_the_same_name_is_not_downstream(
        self,
    ) -> None:
        source = self._write_task("same.txt", "one\n")
        other = self._write_task("same.txt", "two\n")
        detail = ReadOnlyWorkbench(self.root).task_detail(source.task_id)
        self.assertEqual(detail["task"]["derived_tasks"], [])
        self.assertNotEqual(other.task_id, source.task_id)

    def test_a_task_with_no_chain_reports_no_lineage_block(self) -> None:
        task = self._write_task("alone.txt", "alone\n")
        detail = ReadOnlyWorkbench(self.root).task_detail(task.task_id)
        self.assertIsNone(detail["task"]["lineage"])
        self.assertEqual(detail["task"]["derived_tasks"], [])


if __name__ == "__main__":
    unittest.main()
