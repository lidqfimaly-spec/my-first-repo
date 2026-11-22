# Proposal Candidate Validation

Status: Trusted validation interface implemented; live model provider disabled by default.

## Purpose

`proposal_candidates.py` defines the boundary between an untrusted analysis or
synthesis producer and the existing deterministic task runtime. A proposal
provider may be a scripted fixture, a local model, or another component, but its
output is only candidate data. It cannot edit a workspace, execute a test,
create a report, grant policy approval, or declare success.

## Envelope

Every candidate uses a closed schema:

```json
{
  "schema_version": "zevorinth-proposal-candidate-v1",
  "kind": "code",
  "provider": "provider-id",
  "session_nonce": "request-bound nonce",
  "context_sha256": "digest of the exact input context",
  "body": {}
}
```

Provider identity is bound through a trusted `ProposalSessionIssuer`. The
issuer creates a random, expiring, one-use session bound to the provider object,
kind, and canonical context digest. Validation consumes that session exactly
once; replay, expiry, wrong-provider use, and context drift fail closed.
Unknown fields, oversized input, provider/mode mismatches, stale sessions, and
context mismatches also fail closed.

## Code Candidates

A code body contains a goal, registered workspace, exact edit
(`path`, `expected_sha256`, `old_text`, `new_text`), optional rationale, and
optional unittest target.

Validated candidate data is detached and deeply frozen before it is returned.
Mutating the provider's original nested mappings or lists after validation
cannot change the bytes later materialized into a plan.

Validation reopens the current source through the existing workspace-edit
validator. It rejects stale digests, path escapes, symlinks, protected files,
invalid UTF-8, oversized input, and edits whose `old_text` does not occur
exactly once. Validation performs no write.

A validated code proposal materializes as:

```text
inspect_workspace
  -> approval-gated edit_text_file
  -> optional run_tests
```

The existing Policy, lease, backup, atomic edit, test authorization, and
independent Verifier still decide every side effect and final status.

A candidate can request that a test target exist, but it cannot grant host
execution authority. Proposal-derived test steps use the container backend by
default. A host backend requires a separate trusted service/UI argument with
explicit authorization before materialization.

## Research Candidates

A research body contains local registered sources, report metadata, and
findings. Every finding needs a confidence label and exact citations. Candidate
validation rebuilds the deterministic report in memory, using the existing
research validator, and rejects fabricated quotes, undeclared sources,
duplicates, sensitive paths, and unsupported source types.

Materialization uses the existing `research_report` task. The report is not
accepted merely because the proposal provider returned it: source hashes,
quotes, report bytes, and the final artifact are independently checked again.

## Provenance

Accepted candidates record:

- provider identity;
- request/session nonce;
- input-context SHA-256;
- full candidate SHA-256;
- deterministic validation classification.

This provenance is attached to the parent task's creation preview. It proves
which candidate bytes were validated, not that the provider or source is
truthful.

## Boundaries

- No live model or network provider is enabled by default.
- Prompt-like text in local sources remains data; it cannot grant authority.
- Candidate validation never produces a successful task by itself.
- Code execution remains bounded exact-text editing plus approved tests.
- Research remains local-source only and does not prove source authenticity.
