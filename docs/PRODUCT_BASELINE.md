# Zevorinth Product Baseline

Status: Active product source of truth

Date: 2026-11-10

## Product mission

Zevorinth is a local-first, evidence-first general task-delivery agent
workspace. A user states a goal in natural language. Zevorinth clarifies only
when necessary, forms an inspectable bounded plan, executes real tools within
explicit permissions, verifies intermediate and final results, preserves
durable state, recovers safely after failure, and delivers inspectable
artifacts.

The long-term direction is a controlled value-creating intelligent system that
can support sustained projects, opportunity discovery, and real-world outcomes.
Wealth creation is the long-term value direction; it is not a claim that V1
will autonomously invest, trade, or generate income.

## V1 outcomes

V1 must prove representative capability across four task classes:

1. File tasks: read, create, modify, and verify files and reports.
2. Code tasks: inspect a workspace, analyze a problem, apply controlled edits,
   run approved tests, and deliver a verifiable result.
3. Research tasks: collect sources, preserve provenance, synthesize findings,
   and deliver a traceable report.
4. Complex planning tasks: execute dependent multi-step work with approval,
   pause, recovery, local replanning, and partial-success semantics.

V1 does not require unlimited tools, unrestricted autonomy, or complete
self-evolution.

## Architecture principles

- Local-first, modular monolith, one primary agent.
- The model understands and plans; the deterministic kernel owns permissions,
  execution, lifecycle state, and final verification.
- Every tool has a declared capability, permission boundary, result contract,
  and verification method.
- Every plan has limits, budgets, acceptance criteria, and a termination
  condition.
- High-risk actions require explicit authorization.
- Failure, uncertainty, non-execution, and partial success are first-class
  outcomes.
- Task state, events, checkpoints, and artifacts are durable and auditable.
- Self-improvement requires isolated evaluation, approval, staged rollout, and
  rollback.

## User experience

A user starts with a natural-language goal and does not need to understand
internal operation names, task identifiers, leases, event sequence numbers, or
DAG storage formats.

Zevorinth shows the user, when relevant:

- what the system understood;
- which assumptions or clarifications matter;
- the current plan and approval points;
- what is running, completed, blocked, or uncertain;
- the produced artifacts and their verification evidence;
- why a task failed and what can be done next.

Users can continue, revise, retry, pause, or cancel work through real
server-backed actions.

## Roadmap boundaries

Build capability in this order:

1. reliable local task delivery;
2. cross-task memory and a personal knowledge system;
3. research and opportunity discovery;
4. model routing and cost optimization;
5. controlled self-improvement and skill evolution;
6. long-running projects and value creation.

## Governance

- Safety research must not indefinitely delay product delivery.
- Historical test results do not verify the current worktree.
- Design completion, probe definitions, and document volume are not feature
  completion.
- New safety phases do not start automatically.
- Every phase is accepted through real user tasks and reproducible evidence.
- A78-S remains `REQUEST_CHANGES` as subordinate evidence-hardening research;
  it does not control the product roadmap.

## One-sentence definition

Zevorinth turns a user's natural-language goal into an inspectable, bounded,
authorized, verifiable, and recoverable real task delivered through a local
control surface.
