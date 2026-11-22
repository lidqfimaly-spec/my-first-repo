from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.capabilities import CAPABILITY_REGISTRY  # noqa: E402
from zevorinth.models import TaskStatus  # noqa: E402
from zevorinth.proposal_candidates import (  # noqa: E402
    PROPOSAL_SCHEMA_VERSION,
    ProposalCandidateError,
    ProposalSessionIssuer,
    ScriptedProposalProvider,
    code_task_steps,
    proposal_context_sha256,
    validate_proposal_candidate,
)
from zevorinth.service import TaskService  # noqa: E402


NONCE = "proposal-session-0001"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_workspace(root: Path) -> tuple[Path, str]:
    workspace = root / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    source = "VALUE = 1\n\ndef value():\n    return VALUE\n"
    (workspace / "sample.py").write_text(source, encoding="utf-8")
    (workspace / "test_sample.py").write_text(
        "import unittest\nimport sample\n\n"
        "class SampleTests(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        self.assertEqual(sample.value(), 2)\n",
        encoding="utf-8",
    )
    return workspace, source


def envelope(kind: str, context: dict[str, object], body: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "kind": kind,
        "provider": "scripted-proposal-test-v1",
        "session_nonce": NONCE,
        "context_sha256": proposal_context_sha256(context),
        "body": body,
    }


def validate_with_provider(
    root: Path,
    candidate: dict[str, object],
    *,
    kind: str,
    provider: ScriptedProposalProvider,
    context: dict[str, object],
):
    issuer = ProposalSessionIssuer(ttl_seconds=60.0)
    session = issuer.issue(provider=provider, kind=kind, context=context)
    bound = dict(candidate)
    bound["provider"] = provider.name
    bound["session_nonce"] = session.session_id
    bound["context_sha256"] = proposal_context_sha256(context)
    return validate_proposal_candidate(
        root,
        bound,
        expected_kind=kind,
        provider=provider,
        session=session,
        issuer=issuer,
        context=context,
    )


class ProposalCandidateTests(unittest.TestCase):
    def test_capability_is_registered_without_execution_authority(self) -> None:
        details = CAPABILITY_REGISTRY["capabilities"][
            "proposal_candidate_validation"
        ]
        self.assertEqual(details["status"], "available")
        self.assertTrue(details["verified"])
        self.assertIn(
            "grants no execution authority",
            " ".join(details["limitations"]),
        )

    def test_scripted_code_candidate_validates_without_side_effect_and_materializes_with_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace, source = make_workspace(root)
            before = (workspace / "sample.py").read_bytes()
            context = {"goal": "change value", "workspace": "demo"}
            body = {
                "goal": "change the sample value",
                "workspace": "demo",
                "edit": {
                    "workspace": "demo",
                    "path": "sample.py",
                    "expected_sha256": sha256_text(source),
                    "old_text": "VALUE = 1",
                    "new_text": "VALUE = 2",
                },
                "rationale": "the test expects two",
            }
            provider = ScriptedProposalProvider(envelope("code", context, body))
            candidate = provider.propose(
                kind="code",
                goal="change value",
                context=context,
                session_nonce=NONCE,
            )
            proposal = validate_with_provider(
                root,
                candidate,
                kind="code",
                provider=provider,
                context=context,
            )

            self.assertEqual((workspace / "sample.py").read_bytes(), before)
            self.assertEqual(proposal.kind, "code")
            self.assertEqual(
                proposal.provenance["validation"],
                "DETERMINISTIC_CANDIDATE_ONLY",
            )

            service = TaskService(root)
            waiting = service.submit_code_proposal(proposal)
            self.assertEqual(waiting.status, TaskStatus.AWAITING_APPROVAL)
            self.assertEqual(waiting.pending_approval_step_id, "edit")
            self.assertEqual((workspace / "sample.py").read_bytes(), before)
            finished = service.approve_step(
                waiting.task_id, "edit", approved=True
            )
            self.assertEqual(finished.status, TaskStatus.SUCCEEDED, finished.error)
            self.assertIn(
                "VALUE = 2",
                (workspace / "sample.py").read_text(encoding="utf-8"),
            )
            created = service.events(waiting.task_id)[0]
            self.assertEqual(
                created.payload["creation_preview"]["proposal_provenance"],
                proposal.provenance,
            )

    def test_validated_body_is_deeply_detached_from_provider_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, source = make_workspace(root)
            context = {"goal": "change value"}
            candidate = envelope(
                "code",
                context,
                {
                    "goal": "change value",
                    "workspace": "demo",
                    "edit": {
                        "workspace": "demo",
                        "path": "sample.py",
                        "expected_sha256": sha256_text(source),
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                    },
                },
            )
            provider = ScriptedProposalProvider(candidate)
            proposal = validate_with_provider(
                root,
                candidate,
                kind="code",
                provider=provider,
                context=context,
            )

            candidate["body"]["edit"]["new_text"] = "VALUE = 999"
            edit_step = code_task_steps(proposal)[1]

            self.assertEqual(
                edit_step["parameters"]["new_text"],
                "VALUE = 2",
            )
            with self.assertRaises(TypeError):
                proposal.body["edit"]["new_text"] = "VALUE = 999"

    def test_candidate_cannot_authorize_host_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, source = make_workspace(root)
            context = {"goal": "change value"}
            candidate = envelope(
                "code",
                context,
                {
                    "goal": "change value",
                    "workspace": "demo",
                    "edit": {
                        "workspace": "demo",
                        "path": "sample.py",
                        "expected_sha256": sha256_text(source),
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                    },
                    "test_target": "test_sample",
                },
            )
            provider = ScriptedProposalProvider(candidate)
            proposal = validate_with_provider(
                root,
                candidate,
                kind="code",
                provider=provider,
                context=context,
            )
            steps = code_task_steps(proposal)
            test_parameters = steps[-1]["parameters"]
            self.assertEqual(
                test_parameters["execution_backend"],
                "container",
            )
            self.assertNotIn("host_authorization", test_parameters)
            with self.assertRaisesRegex(
                ProposalCandidateError,
                "separate trusted authorization",
            ):
                code_task_steps(proposal, test_backend="host")

    def test_code_candidate_rejects_tampered_digest_path_and_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace, source = make_workspace(root)
            (workspace / "ambiguous.py").write_text(
                "x = 1\nx = 1\n",
                encoding="utf-8",
            )
            context = {"goal": "change value"}
            expected_context = proposal_context_sha256(context)
            cases = [
                (
                    {
                        "workspace": "demo",
                        "path": "sample.py",
                        "expected_sha256": "0" * 64,
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                    },
                    "source_file_changed",
                ),
                (
                    {
                        "workspace": "demo",
                        "path": "../escape.py",
                        "expected_sha256": sha256_text(source),
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                    },
                    "invalid_workspace_path",
                ),
                (
                    {
                        "workspace": "demo",
                        "path": "ambiguous.py",
                        "expected_sha256": sha256_text("x = 1\nx = 1\n"),
                        "old_text": "x = 1",
                        "new_text": "x = 2",
                    },
                    "text_match_count",
                ),
            ]
            for edit, code in cases:
                with self.subTest(code=code):
                    candidate = envelope(
                        "code",
                        context,
                        {
                            "goal": "change value",
                            "workspace": "demo",
                            "edit": edit,
                        },
                    )
                    provider = ScriptedProposalProvider(candidate)
                    with self.assertRaises(ProposalCandidateError) as raised:
                        validate_with_provider(
                            root,
                            candidate,
                            kind="code",
                            provider=provider,
                            context=context,
                        )
                    self.assertEqual(raised.exception.code, code)

    def test_envelope_identity_mismatch_and_candidate_tampering_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, source = make_workspace(root)
            context = {"goal": "change value"}
            candidate = envelope(
                "code",
                context,
                {
                    "goal": "change value",
                    "workspace": "demo",
                    "edit": {
                        "workspace": "demo",
                        "path": "sample.py",
                        "expected_sha256": sha256_text(source),
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                    },
                },
            )
            provider = ScriptedProposalProvider(candidate)
            candidate["provider"] = provider.name
            candidate["context_sha256"] = proposal_context_sha256(context)
            for mutation, code in (
                (("session_nonce", "wrong"), "proposal_nonce_mismatch"),
                (("context_sha256", "0" * 64), "proposal_context_mismatch"),
                (("provider", "other"), "proposal_provider_mismatch"),
                (("unexpected", True), "invalid_proposal_fields"),
            ):
                forged = dict(candidate)
                forged[mutation[0]] = mutation[1]
                with self.subTest(code=code):
                    issuer = ProposalSessionIssuer(ttl_seconds=60.0)
                    session = issuer.issue(
                        provider=provider,
                        kind="code",
                        context=context,
                    )
                    if mutation[0] != "session_nonce":
                        forged["session_nonce"] = session.session_id
                    with self.assertRaises(ProposalCandidateError) as raised:
                        validate_proposal_candidate(
                            root,
                            forged,
                            expected_kind="code",
                            provider=provider,
                            session=session,
                            issuer=issuer,
                            context=context,
                        )
                    self.assertEqual(raised.exception.code, code)

    def test_proposal_session_replay_expiry_provider_and_context_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, source = make_workspace(root)
            context = {"goal": "change value"}
            body = {
                "goal": "change value",
                "workspace": "demo",
                "edit": {
                    "workspace": "demo",
                    "path": "sample.py",
                    "expected_sha256": sha256_text(source),
                    "old_text": "VALUE = 1",
                    "new_text": "VALUE = 2",
                },
            }
            provider = ScriptedProposalProvider(envelope("code", context, body))
            issuer = ProposalSessionIssuer(ttl_seconds=60.0)
            session = issuer.issue(provider=provider, kind="code", context=context)
            candidate = dict(provider._response)
            candidate["session_nonce"] = session.session_id
            candidate["context_sha256"] = session.context_sha256
            first = validate_proposal_candidate(
                root,
                candidate,
                expected_kind="code",
                provider=provider,
                session=session,
                issuer=issuer,
                context=context,
            )
            self.assertEqual(first.kind, "code")
            with self.assertRaisesRegex(
                ProposalCandidateError,
                "expired, or already consumed",
            ):
                validate_proposal_candidate(
                    root,
                    candidate,
                    expected_kind="code",
                    provider=provider,
                    session=session,
                    issuer=issuer,
                    context=context,
                )

            other = ScriptedProposalProvider(candidate)
            wrong_provider_session = issuer.issue(
                provider=other, kind="code", context=context
            )
            with self.assertRaisesRegex(
                ProposalCandidateError,
                "provider does not own",
            ):
                validate_proposal_candidate(
                    root,
                    candidate,
                    expected_kind="code",
                    provider=provider,
                    session=wrong_provider_session,
                    issuer=issuer,
                    context=context,
                )

            drift_session = issuer.issue(
                provider=provider, kind="code", context=context
            )
            with self.assertRaisesRegex(
                ProposalCandidateError,
                "context no longer matches",
            ):
                issuer.consume(
                    drift_session,
                    provider=provider,
                    kind="code",
                    context={"goal": "different"},
                )

            now = [100.0]
            expiring = ProposalSessionIssuer(
                ttl_seconds=1.0,
                clock=lambda: now[0],
            )
            expired = expiring.issue(
                provider=provider, kind="code", context=context
            )
            now[0] = 102.0
            with self.assertRaisesRegex(
                ProposalCandidateError,
                "expired",
            ):
                expiring.consume(
                    expired,
                    provider=provider,
                    kind="code",
                    context=context,
                )

    def test_fabricated_research_candidate_is_rejected_before_report_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspaces" / "research"
            workspace.mkdir(parents=True)
            (workspace / "source.txt").write_text(
                "Verified systems read results from disk.\n",
                encoding="utf-8",
            )
            context = {"goal": "summarize sources"}
            body = {
                "goal": "summarize local source",
                "workspace": "research",
                "sources": ["source.txt"],
                "title": "Local source summary",
                "output_path": "report.md",
                "findings": [
                    {
                        "statement": "Fabricated statement",
                        "confidence": "supported",
                        "citations": [
                            {
                                "source_path": "source.txt",
                                "quote": "quote that is absent",
                            }
                        ],
                    }
                ],
            }
            candidate = envelope("research", context, body)
            provider = ScriptedProposalProvider(candidate)
            with self.assertRaises(ProposalCandidateError) as raised:
                validate_with_provider(
                    root,
                    candidate,
                    kind="research",
                    provider=provider,
                    context=context,
                )
            self.assertEqual(
                raised.exception.code,
                "research_citation_not_found",
            )
            self.assertFalse(
                any((root / "artifacts").glob("*/report.md"))
                if (root / "artifacts").exists()
                else False
            )

    def test_valid_research_candidate_materializes_traceable_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspaces" / "research"
            workspace.mkdir(parents=True)
            source = "Verified systems read results from disk.\n"
            (workspace / "source.txt").write_text(source, encoding="utf-8")
            context = {"goal": "summarize sources"}
            candidate = envelope(
                "research",
                context,
                {
                    "goal": "summarize local source",
                    "workspace": "research",
                    "sources": ["source.txt"],
                    "title": "Local source summary",
                    "output_path": "report.md",
                    "findings": [
                        {
                            "statement": "Verification reads persisted results.",
                            "confidence": "supported",
                            "citations": [
                                {
                                    "source_path": "source.txt",
                                    "quote": "read results from disk",
                                }
                            ],
                        }
                    ],
                },
            )
            provider = ScriptedProposalProvider(candidate)
            proposal = validate_with_provider(
                root,
                candidate,
                kind="research",
                provider=provider,
                context=context,
            )
            service = TaskService(root)
            task = service.submit_research_proposal(proposal)
            self.assertEqual(task.status, TaskStatus.SUCCEEDED, task.error)
            report = (
                root / "artifacts" / task.task_id / "report.md"
            ).read_text(encoding="utf-8")
            self.assertIn("read results from disk", report)
            self.assertIn(sha256_text(source), report)


if __name__ == "__main__":
    unittest.main()
