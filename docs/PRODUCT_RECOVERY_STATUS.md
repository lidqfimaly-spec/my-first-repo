# Zevorinth Product Recovery Status

Status: Current-state assessment after product-baseline reset

Date: 2026-11-10

## Current product position

Zevorinth has a credible local execution kernel. It is not yet the complete V1
general task-delivery workspace defined in `PRODUCT_BASELINE.md`.

```text
engine_slices=IMPLEMENTED_AND_TESTED_ON_CURRENT_HOST
engine_slice_acceptance=PASS
v1_user_journey=INCOMPLETE
production_trust=false
A79=NOT_STARTED
```

| Capability | Current state | Remaining work |
| --- | --- | --- |
| Durable task state and ordered events | Implemented and tested | Preserve as authoritative runtime truth |
| SQLite lease, fencing, budget, and crash recovery | Implemented and tested | Extend only where multi-step execution requires it |
| Independent artifact readback verification | Implemented and tested | Reuse for file modification and report delivery |
| Text artifact operations | Create, read and versioned append/replace are implemented and verified | Add richer edit and report composition workflows |
| Conditional `run_tests` and code workflow | Implemented with isolation requirements plus controlled workspace inspection and exact edit | Add autonomous problem analysis and patch proposal |
| Deterministic and LLM planning | Partially complete | Add explicit multi-step plans and task-type routing |
| Multi-step execution | Implemented as a trusted service API | Complete pause, approval, resume and replan through the control center and model planning |
| Approval and pause/resume | Service API implemented, product surface incomplete | Productize approval, pause, resume, and cancellation |
| Conversation and clarification | Partial | Add natural-language first use and assistant outcome messages |
| Local workbench and control center | Implemented but not validated as the complete user journey | Make guided creation the default local path |
| Research task support | Offline source-preserving slice implemented | Add a control-center entry flow and model-assisted synthesis whose claims still require citation verification |
| Cross-task memory | Not implemented | Add after V1 task delivery is reliable |
| Controlled self-improvement | Not implemented | Defer until evaluation and rollback are proven |
| L2/L3 evidence trust | Not implemented | Never claim without an independent trust root |

The code-task slice performs controlled exact-text replacement and verification;
it does not autonomously analyze a problem and author a patch. The research slice
verifies caller-authored findings and citations; it does not perform unrestricted
web research or autonomous synthesis.

## Recovery sequence

### R0: Restore a trustworthy engineering baseline

- Preserve all existing user changes.
- Establish canonical product documentation.
- Reconcile `doctor` and machine-readable capability truth.
- Validate the current worktree with focused and broad tests.
- Keep A78-S frozen as subordinate research.

Exit evidence: current candidate is understood, capability claims are truthful,
and verification gaps are explicit.

### R1: Complete the first real user journey

- Natural-language goal entry.
- Bounded plan and assumption preview.
- Clarification and confirmation.
- Authoritative execution progress.
- Verified artifact delivery.

Exit evidence: a new local user can create and retrieve a verified result
without knowing internal runtime fields or task IDs.

### R2: Complete verified file tasks

- Safe file read.
- New artifact creation.
- Controlled modification and continuation.
- Versioned, non-destructive delivery.
- Independent verification of the final artifact.

Exit evidence: create, modify, and continue workflows pass automated and
end-to-end local tests.

### R3: Complete bounded complex tasks

- Explicit plan graph with dependency and data-flow validation.
- Step-level verification.
- Budget and termination rules.
- Approval, pause, resume, and cancellation.
- Partial success and local replanning.

Exit evidence: dependent multi-step tasks complete without duplicate side
effects and preserve one authoritative state.

### R4: Complete code and research task slices

- Registered-workspace inspection and controlled code edits.
- Approved tests and static checks.
- Local source collection, provenance, excerpts, and citations.
- Traceable report delivery.

Exit evidence: representative code and research tasks produce inspectable,
verifiable outcomes within explicit permission boundaries.

### R5: Product acceptance

- Run a representative real-task evaluation set.
- Measure success, interventions, recovery, false success, latency, and cost.
- Publish reproducible acceptance evidence.
- Resolve independent code and architecture review blockers.

Exit evidence: V1 claims match observed behavior and no unsupported production
trust is claimed.

## Current constraints

- The pre-existing worktree contains substantial changes and must not be
  treated as a clean release candidate.
- `candidate_scope=INVALID_SCOPE` from the A78 process remains an evidence
  governance fact, not a product-stage success claim.
- Current implementation tests apply only when rerun against the current
  candidate.
- External live-model, container, and cross-platform behavior require separate
  environment-specific evidence.
