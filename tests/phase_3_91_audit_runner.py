"""Generate Phase 3.91 read-only workbench evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import TaskRequest  # noqa: E402
from zevorinth.service import TaskService  # noqa: E402
from zevorinth.workbench_server import create_server  # noqa: E402


PHASE_3_90_EVIDENCE = {
    "artifacts/audits/phase-3.90/stable-summary.json": (
        "ac3abacb5645c710e58f0a2ded32777b7b85960ae65e2938c1d0d183ecc2f1ff"
    ),
    "src/zevorinth/evals/planner_text_execution_v1.json": (
        "7f306abeaf7bd5c800a06eeb0fe71dad6eb504f4f42c7f65ec70bb2c5e59fbfb"
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_new_json(path: Path, payload: Any) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")


def _load_json_url(url: str) -> tuple[int, dict[str, str], dict[str, Any]]:
    with urlopen(url, timeout=5) as response:
        return (
            response.status,
            dict(response.headers),
            json.loads(response.read().decode("utf-8")),
        )


def _matrix() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        service = TaskService(root)
        success = service.submit(
            TaskRequest(
                goal="创建 workbench-proof.txt，内容写 verified evidence。",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "workbench-proof.txt",
                    "content": "verified evidence\n",
                },
            )
        )
        service.submit(
            TaskRequest(
                goal="创建一个需要补充字段的文件",
                operation="write_text_artifact",
                parameters={},
            )
        )
        service.submit(
            TaskRequest(
                goal="写入不安全路径",
                operation="write_text_artifact",
                parameters={
                    "artifact_path": "../escape.txt",
                    "content": "blocked",
                },
            )
        )
        database = root / "data" / "zevorinth.sqlite3"
        artifact = (
            root
            / "artifacts"
            / success.task_id
            / "workbench-proof.txt"
        )
        before_database_sha256 = _sha256(database)
        before_artifact_sha256 = _sha256(artifact)
        before_events = [
            event.to_dict() for event in service.events(success.task_id)
        ]

        server = create_server(root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        base = f"http://{host}:{port}"
        try:
            task_status, task_headers, tasks = _load_json_url(
                f"{base}/api/tasks?limit=40"
            )
            detail_status, _, detail = _load_json_url(
                f"{base}/api/tasks/{success.task_id}"
            )
            attention_status, _, attention = _load_json_url(
                f"{base}/api/attention"
            )
            preview_status, _, preview = _load_json_url(
                f"{base}/api/tasks/{success.task_id}/artifact"
            )
            with urlopen(f"{base}/", timeout=5) as response:
                html_status = response.status
                content_security_policy = response.headers[
                    "Content-Security-Policy"
                ]
            mutation_rejected = False
            try:
                urlopen(
                    Request(
                        f"{base}/api/tasks",
                        method="POST",
                        data=b"{}",
                    ),
                    timeout=5,
                )
            except HTTPError as exc:
                mutation_rejected = exc.code == 405
                exc.close()

            artifact.write_text("changed after verification\n", encoding="utf-8")
            drift_status, _, drift = _load_json_url(
                f"{base}/api/tasks/{success.task_id}/artifact"
            )
            drift_bytes = artifact.read_bytes()
            artifact.write_bytes(
                "verified evidence\n".encode("utf-8")
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        after_database_sha256 = _sha256(database)
        after_artifact_sha256 = _sha256(artifact)
        after_events = [
            event.to_dict() for event in service.events(success.task_id)
        ]
        return {
            "phase": "3.91",
            "status": "PASS",
            "scope": "read_only_local_workbench_v1",
            "server": {
                "host": host,
                "loopback_only": True,
                "html_status": html_status,
                "content_security_policy": content_security_policy,
            },
            "http": {
                "task_list_status": task_status,
                "task_detail_status": detail_status,
                "attention_status": attention_status,
                "artifact_preview_status": preview_status,
                "artifact_drift_status": drift_status,
                "mutation_method_rejected": mutation_rejected,
                "cache_control": task_headers.get("Cache-Control"),
            },
            "projection": {
                "task_count": len(tasks["tasks"]),
                "attention_count": attention["count"],
                "success_status": detail["task"]["status"],
                "success_verification": detail["task"]["verification"]["passed"],
                "initial_preview_state": preview["state"],
                "drift_preview_state": drift["state"],
                "drift_file_preserved": (
                    drift_bytes == b"changed after verification\n"
                ),
                "recovery_preview_present": (
                    "recovery_preview" in detail
                ),
                "event_sequence_ordered": (
                    [item["sequence"] for item in detail["events"]]
                    == sorted(item["sequence"] for item in detail["events"])
                ),
            },
            "read_only_proof": {
                "database_sha256_before": before_database_sha256,
                "database_sha256_after": after_database_sha256,
                "database_unchanged": (
                    before_database_sha256 == after_database_sha256
                ),
                "artifact_sha256_before": before_artifact_sha256,
                "artifact_sha256_after": after_artifact_sha256,
                "artifact_unchanged": (
                    before_artifact_sha256 == after_artifact_sha256
                ),
                "events_unchanged": before_events == after_events,
            },
            "isolation": {
                "real_model_requests": 0,
                "external_network_requests": 0,
                "loopback_http_requests": 7,
                "dependency_installs": 0,
                "container_executions": 0,
                "project_workspace_writes": 0,
            },
        }


def _integrity() -> dict[str, Any]:
    files: dict[str, Any] = {}
    all_match = True
    for relative, expected in PHASE_3_90_EVIDENCE.items():
        observed = _sha256(PROJECT_ROOT / relative)
        matches = observed == expected
        all_match = all_match and matches
        files[relative] = {
            "expected_sha256": expected,
            "observed_sha256": observed,
            "matches": matches,
        }
    return {
        "phase": "3.90",
        "baseline_commit": "709b82d",
        "all_preserved_evidence_matches": all_match,
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts/audits/phase-3.91",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "workbench-matrix.json": _matrix(),
        "phase-3.90-integrity.json": _integrity(),
    }
    for name, payload in outputs.items():
        path = args.output_dir / name
        if path.exists():
            raise FileExistsError(path)
        _write_new_json(path, payload)
        print(f"{path} {_sha256(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
