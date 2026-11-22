"""Deterministic, source-preserving local research report generation."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .artifact_tool import (
    MAX_ARTIFACT_BYTES,
    ArtifactToolError,
    validate_artifact_name,
)
from .local_test_runner import TestExecutionError, resolve_workspace
from .workspace_inspection import (
    WorkspaceInspectionError,
    normalize_workspace_relative,
)


RESEARCH_REPORT_TOOL = "research_report"
RESEARCH_SCHEMA_VERSION = "local-research-report-v1"
MAX_RESEARCH_SOURCES = 20
MAX_RESEARCH_SOURCE_BYTES = 512 * 1024
MAX_RESEARCH_TOTAL_BYTES = 4 * 1024 * 1024
MAX_RESEARCH_FINDINGS = 40
MAX_RESEARCH_CITATIONS_PER_FINDING = 8
MAX_RESEARCH_TEXT_CHARS = 8_000
MAX_RESEARCH_QUOTE_CHARS = 2_000
RESEARCH_CONFIDENCE_LABELS = frozenset(
    {"supported", "inference", "uncertain"}
)


class ResearchWorkflowError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_research_parameters(
    project_root: Path,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    allowed = {
        "workspace",
        "sources",
        "title",
        "output_path",
        "findings",
    }
    unknown = set(parameters) - allowed
    if unknown:
        raise ResearchWorkflowError(
            "unsupported_parameter",
            f"unknown research parameters: {sorted(unknown)}",
        )
    workspace_name = parameters.get("workspace")
    if not isinstance(workspace_name, str):
        raise ResearchWorkflowError(
            "invalid_workspace",
            "workspace must be a registered workspace name",
        )
    try:
        workspace = resolve_workspace(project_root, workspace_name)
    except TestExecutionError as exc:
        raise ResearchWorkflowError(exc.code, exc.message) from exc

    raw_sources = parameters.get("sources")
    if (
        not isinstance(raw_sources, list)
        or not raw_sources
        or len(raw_sources) > MAX_RESEARCH_SOURCES
    ):
        raise ResearchWorkflowError(
            "invalid_research_sources",
            f"sources must contain 1 to {MAX_RESEARCH_SOURCES} paths",
        )
    sources: list[str] = []
    seen_sources: set[str] = set()
    for raw_source in raw_sources:
        try:
            source_path = normalize_workspace_relative(
                raw_source,
                allow_empty=False,
            )
        except WorkspaceInspectionError as exc:
            raise ResearchWorkflowError(exc.code, exc.message) from exc
        if source_path in seen_sources:
            raise ResearchWorkflowError(
                "duplicate_research_source",
                f"source is listed more than once: {source_path}",
            )
        seen_sources.add(source_path)
        sources.append(source_path)

    title = parameters.get("title")
    if (
        not isinstance(title, str)
        or not title.strip()
        or title != title.strip()
        or len(title) > 300
        or "\x00" in title
        or "\r" in title
        or "\n" in title
    ):
        raise ResearchWorkflowError(
            "invalid_research_title",
            "title must be a non-empty single-line string of at most 300 characters",
        )
    try:
        output_path = validate_artifact_name(parameters.get("output_path"))
    except ArtifactToolError as exc:
        raise ResearchWorkflowError(exc.code, exc.message) from exc
    if not output_path.lower().endswith(".md"):
        raise ResearchWorkflowError(
            "invalid_research_output",
            "research output_path must end with .md",
        )

    raw_findings = parameters.get("findings")
    if (
        not isinstance(raw_findings, list)
        or not raw_findings
        or len(raw_findings) > MAX_RESEARCH_FINDINGS
    ):
        raise ResearchWorkflowError(
            "invalid_research_findings",
            f"findings must contain 1 to {MAX_RESEARCH_FINDINGS} items",
        )
    findings: list[dict[str, Any]] = []
    for index, raw_finding in enumerate(raw_findings):
        if not isinstance(raw_finding, Mapping):
            raise ResearchWorkflowError(
                "invalid_research_finding",
                f"finding {index} must be an object",
            )
        if set(raw_finding) - {"statement", "confidence", "citations"}:
            raise ResearchWorkflowError(
                "invalid_research_finding",
                f"finding {index} has unknown fields",
            )
        statement = raw_finding.get("statement")
        if (
            not isinstance(statement, str)
            or not statement.strip()
            or len(statement) > MAX_RESEARCH_TEXT_CHARS
            or "\x00" in statement
        ):
            raise ResearchWorkflowError(
                "invalid_research_finding",
                f"finding {index} statement is invalid",
            )
        confidence = raw_finding.get("confidence")
        if confidence not in RESEARCH_CONFIDENCE_LABELS:
            raise ResearchWorkflowError(
                "invalid_research_confidence",
                "confidence must be supported, inference, or uncertain",
            )
        raw_citations = raw_finding.get("citations")
        if (
            not isinstance(raw_citations, list)
            or not raw_citations
            or len(raw_citations) > MAX_RESEARCH_CITATIONS_PER_FINDING
        ):
            raise ResearchWorkflowError(
                "invalid_research_citations",
                f"finding {index} must have 1 to "
                f"{MAX_RESEARCH_CITATIONS_PER_FINDING} citations",
            )
        citations: list[dict[str, str]] = []
        for raw_citation in raw_citations:
            if not isinstance(raw_citation, Mapping):
                raise ResearchWorkflowError(
                    "invalid_research_citation",
                    f"finding {index} citation must be an object",
                )
            if set(raw_citation) != {"source_path", "quote"}:
                raise ResearchWorkflowError(
                    "invalid_research_citation",
                    f"finding {index} citation schema is invalid",
                )
            try:
                source_path = normalize_workspace_relative(
                    raw_citation.get("source_path"),
                    allow_empty=False,
                )
            except WorkspaceInspectionError as exc:
                raise ResearchWorkflowError(exc.code, exc.message) from exc
            if source_path not in seen_sources:
                raise ResearchWorkflowError(
                    "citation_source_not_declared",
                    f"citation source is not in sources: {source_path}",
                )
            quote = raw_citation.get("quote")
            if (
                not isinstance(quote, str)
                or not quote
                or len(quote) > MAX_RESEARCH_QUOTE_CHARS
                or "\x00" in quote
            ):
                raise ResearchWorkflowError(
                    "invalid_research_quote",
                    f"finding {index} citation quote is invalid",
                )
            citations.append(
                {"source_path": source_path, "quote": quote}
            )
        findings.append(
            {
                "statement": statement.strip(),
                "confidence": confidence,
                "citations": citations,
            }
        )
    return {
        "workspace": workspace_name,
        "workspace_path": workspace,
        "sources": sources,
        "title": title,
        "output_path": output_path,
        "findings": findings,
    }


def build_research_report(
    project_root: Path,
    parameters: Mapping[str, Any],
    *,
    generated_at: str,
) -> tuple[str, dict[str, Any]]:
    """Read local sources, verify citations, and render deterministic Markdown."""
    values = validate_research_parameters(project_root, parameters)
    workspace = values["workspace_path"]
    source_records: list[dict[str, Any]] = []
    source_bytes: dict[str, bytes] = {}
    total_bytes = 0
    for index, source_path in enumerate(values["sources"], start=1):
        raw = _read_source(workspace, source_path)
        total_bytes += len(raw)
        if total_bytes > MAX_RESEARCH_TOTAL_BYTES:
            raise ResearchWorkflowError(
                "research_sources_too_large",
                f"sources exceed {MAX_RESEARCH_TOTAL_BYTES} bytes",
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResearchWorkflowError(
                "research_source_not_utf8",
                f"source is not UTF-8 text: {source_path}",
            ) from exc
        source_bytes[source_path] = raw
        source_records.append(
            {
                "source_id": f"S{index}",
                "path": source_path,
                "byte_count": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "text": text,
            }
        )
    source_by_path = {item["path"]: item for item in source_records}
    findings: list[dict[str, Any]] = []
    for finding_index, finding in enumerate(values["findings"], start=1):
        citations: list[dict[str, Any]] = []
        for citation in finding["citations"]:
            source = source_by_path[citation["source_path"]]
            quote = citation["quote"]
            if quote not in source["text"]:
                raise ResearchWorkflowError(
                    "research_citation_not_found",
                    f"finding {finding_index} quote is absent from "
                    f"{source['path']}",
                )
            citations.append(
                {
                    "source_id": source["source_id"],
                    "source_path": source["path"],
                    "source_sha256": source["sha256"],
                    "quote": quote,
                }
            )
        findings.append(
            {
                "finding_id": f"F{finding_index}",
                "statement": finding["statement"],
                "confidence": finding["confidence"],
                "citations": citations,
            }
        )

    lines = [
        f"# {values['title']}",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Workspace: `{values['workspace']}`",
        "- Scope: local registered-workspace sources only",
        "- Network access: disabled",
        "- Provenance limit: local internal consistency only; authenticity is not independently certified",
        "",
        "## Sources",
        "",
    ]
    for source in source_records:
        lines.append(
            f"- **{source['source_id']}** `{source['path']}` - "
            f"{source['byte_count']} bytes - SHA-256 `{source['sha256']}`"
        )
    lines.extend(["", "## Findings", ""])
    for finding in findings:
        lines.append(
            f"### {finding['finding_id']} "
            f"[{finding['confidence'].upper()}]"
        )
        lines.extend(["", finding["statement"], ""])
        for citation in finding["citations"]:
            lines.append(
                f"- [{citation['source_id']}] "
                f"`{citation['source_path']}` "
                f"SHA-256 `{citation['source_sha256']}`"
            )
            for quote_line in citation["quote"].splitlines() or [""]:
                lines.append(f"  > {quote_line}")
        lines.append("")
    lines.extend(
        [
            "## Limitations",
            "",
            "- This report was generated from local text supplied in a registered workspace.",
            "- Citation verification proves that a quoted string occurs in the recorded source bytes.",
            "- It does not prove that the source itself is true, current, complete, or authentic.",
            "- No network or browser collection was performed.",
            "",
        ]
    )
    report = "\n".join(lines)
    encoded = report.encode("utf-8")
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise ResearchWorkflowError(
            "research_report_too_large",
            f"report exceeds {MAX_ARTIFACT_BYTES} bytes",
        )
    evidence = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "title": values["title"],
        "workspace": values["workspace"],
        "generated_at": generated_at,
        "output_path": values["output_path"],
        "sources": [
            {
                "source_id": source["source_id"],
                "path": source["path"],
                "byte_count": source["byte_count"],
                "sha256": source["sha256"],
            }
            for source in source_records
        ],
        "findings": findings,
        "report_byte_count": len(encoded),
        "report_sha256": hashlib.sha256(encoded).hexdigest(),
        "network_access": False,
        "provenance": "local_workspace_internal_consistency",
    }
    return report, evidence


def verify_research_report(
    project_root: Path,
    parameters: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
    *,
    expected_generated_at: str,
) -> tuple[bool, str]:
    if evidence is None:
        return False, "research_evidence_missing"
    try:
        report, expected = build_research_report(
            project_root,
            parameters,
            generated_at=expected_generated_at,
        )
    except ResearchWorkflowError:
        return False, "research_sources_unavailable"
    if evidence.get("schema_version") != RESEARCH_SCHEMA_VERSION:
        return False, "research_evidence_schema_mismatch"
    for field in (
        "title",
        "workspace",
        "generated_at",
        "output_path",
        "sources",
        "findings",
        "report_byte_count",
        "report_sha256",
        "network_access",
        "provenance",
    ):
        if evidence.get(field) != expected.get(field):
            return False, "research_evidence_mismatch"
    if hashlib.sha256(report.encode("utf-8")).hexdigest() != expected["report_sha256"]:
        return False, "research_report_digest_mismatch"
    return True, "research_report_verified"


def _read_source(workspace: Path, relative_path: str) -> bytes:
    current = workspace
    for component in PurePosixPath(relative_path).parts:
        current = current / component
        if current.is_symlink():
            raise ResearchWorkflowError(
                "unsafe_research_source",
                "research source path contains a symbolic link",
            )
    if not current.is_file():
        raise ResearchWorkflowError(
            "research_source_missing",
            f"research source does not exist: {relative_path}",
        )
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise ResearchWorkflowError(
            "unsafe_research_source",
            "research source cannot be resolved safely",
        ) from exc
    if workspace not in resolved.parents:
        raise ResearchWorkflowError(
            "unsafe_research_source",
            "research source escapes the registered workspace",
        )
    fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        result = os.fstat(fd)
        if not stat.S_ISREG(result.st_mode):
            raise ResearchWorkflowError(
                "unsafe_research_source",
                "research source is not a regular file",
            )
        if result.st_size > MAX_RESEARCH_SOURCE_BYTES:
            raise ResearchWorkflowError(
                "research_source_too_large",
                f"research source exceeds {MAX_RESEARCH_SOURCE_BYTES} bytes",
            )
        chunks: list[bytes] = []
        remaining = MAX_RESEARCH_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_RESEARCH_SOURCE_BYTES:
            raise ResearchWorkflowError(
                "research_source_too_large",
                f"research source exceeds {MAX_RESEARCH_SOURCE_BYTES} bytes",
            )
        return raw
    finally:
        os.close(fd)
