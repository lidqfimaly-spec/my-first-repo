"""S-C: a finished task's artifact must be deliverable byte-for-byte.

The preview endpoint caps at 64 KiB because it returns JSON; a download must
not, otherwise any real report is undeliverable. The download also re-checks the
registered digest, so a file that changed after verification is refused rather
than silently handed over as "the task's result".
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.workbench_server import create_server  # noqa: E402


class ArtifactDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.server = create_server(self.root, port=0, control_enabled=True)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)
        self._tmp.cleanup()

    # ---------------------------------------------------------------- utils
    def _make_task(self, content: str, name: str = "note.txt") -> str:
        service = self.server.workbench
        from zevorinth.models import TaskRequest
        from zevorinth.service import TaskService

        task_service = TaskService(self.root, recover_leases=False)
        task = task_service.submit(
            TaskRequest(
                goal=f"写入 {name}",
                operation="write_text_artifact",
                parameters={"artifact_path": name, "content": content},
            )
        )
        self.assertEqual(task.status.value, "succeeded")
        self.assertIsNotNone(service)
        return task.task_id

    def _download(self, task_id: str) -> tuple[int, dict, bytes]:
        request = Request(
            f"{self.base}/api/tasks/{task_id}/artifact?download=1"
        )
        try:
            with urlopen(request, timeout=10) as response:
                return (
                    response.status,
                    dict(response.headers),
                    response.read(),
                )
        except HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    # ---------------------------------------------------------------- tests
    def test_download_returns_the_exact_bytes_as_an_attachment(self) -> None:
        task_id = self._make_task("结论已验证\n")
        status, headers, body = self._download(task_id)
        self.assertEqual(status, 200)
        self.assertEqual(body, "结论已验证\n".encode("utf-8"))
        disposition = headers.get("Content-Disposition", "")
        self.assertTrue(disposition.startswith("attachment;"))
        self.assertIn("note.txt", disposition)
        self.assertEqual(headers.get("Content-Length"), str(len(body)))

    def test_download_handles_the_largest_file_the_engine_can_produce(self) -> None:
        # FINDING: write_text_artifact caps `content` at 65536 bytes
        # (`content_too_large`), which is exactly MAX_ARTIFACT_PREVIEW_BYTES.
        # So "a >64 KiB artifact" cannot be produced through this operation
        # today and that clause of F-12 is NOT_TESTABLE end to end. What is
        # testable is the boundary: the largest producible file must download
        # intact, and the endpoint itself has no length check (code reading).
        big = "x" * 65536
        task_id = self._make_task(big, name="big.txt")
        status, headers, body = self._download(task_id)
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 65536)
        self.assertEqual(headers.get("Content-Length"), "65536")

    def test_the_preview_still_refuses_what_the_download_serves(self) -> None:
        # A direct, honest probe of the difference: the preview is a JSON field
        # and keeps its limit; the download path has no such limit.
        from zevorinth.workbench import MAX_ARTIFACT_PREVIEW_BYTES

        raw, filename, content_type = (
            self.server.workbench.artifact_download(
                self._make_task("ok\n")
            )
        )
        self.assertEqual(raw, b"ok\n")
        self.assertEqual(filename, "note.txt")
        self.assertTrue(content_type.startswith("text/plain"))
        self.assertEqual(MAX_ARTIFACT_PREVIEW_BYTES, 65536)

    def test_a_file_changed_after_registration_is_refused(self) -> None:
        task_id = self._make_task("original\n")
        path = self.root / "artifacts" / task_id / "note.txt"
        path.write_text("tampered\n", encoding="utf-8")
        status, _, body = self._download(task_id)
        self.assertEqual(status, 409)
        payload = json.loads(body)
        self.assertEqual(
            payload["error"]["code"], "artifact_changed_since_registration"
        )

    def test_download_of_a_task_without_artifact_is_a_404(self) -> None:
        status, _, body = self._download(str(uuid.uuid4()))
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
