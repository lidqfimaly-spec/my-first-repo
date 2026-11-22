# Zevorinth V1 User Journey Acceptance Plan

Status: Active acceptance baseline

Date: 2026-11-11

## 1. Purpose

This plan defines what must be proven before Zevorinth can claim that a user
can complete a representative V1 task from natural language through verified
delivery. It tests the product journey, not only the underlying engine slices.

The plan does not claim that any scenario below has passed. Current evidence
remains:

```text
ENGINE_SLICE_ACCEPTANCE=PASS
LATEST_FULL_OFFICIAL_SUITE=1270/1270
POST_FULL_RUN_TARGETED_REGRESSION=148/148
LATEST_FULL_RERUN=INTERRUPTED
V1_USER_JOURNEY_COMPLETE=False
PRODUCTION_TRUST=False
A79_NOT_STARTED
```

## Current execution status

```text
V1J-FILE-01=PASS
V1J-CODE-01=PENDING
V1J-RESEARCH-01=PENDING
V1J-COMPLEX-01=PENDING
```

The `V1J-FILE-01` retest evidence is recorded in
`artifacts/audits/v1j-file/20261110T211309Z`. The earlier FAIL record remains
intact in `artifacts/audits/v1j-file/20261110T205047Z`.

## 2. Acceptance layers

The following layers must be reported separately.

| Layer | Question | Required evidence |
| --- | --- | --- |
| Engine safety | Do state, policy, leases, tools and verification behave correctly for fixed inputs? | Automated unit, concurrency, crash and negative-control tests |
| Product journey | Can a user complete a real task through the local product surface without internal schema knowledge? | Browser/HTTP E2E transcript, screenshots where useful, task/event/artifact evidence |
| Real-model quality | Can a configured model understand open-ended goals, request useful clarification, plan competently and recover? | Authorized live-model run with task-level scoring and human calibration |

A lower layer never substitutes for a higher layer. Scripted-provider success
does not prove real-model quality. A model response does not prove execution
success. A task label does not replace artifact and verifier evidence.

## 3. Common user-journey contract

Every V1 journey must satisfy all of the following:

1. Start from a natural-language goal entered in the local control center.
2. Show the interpreted goal, assumptions, task class and missing information.
3. Never invent required digests, source quotes, patches or plan steps.
4. Show preferences, limits and expected risks before mutation.
5. Require explicit confirmation before task creation and before execution.
6. Display authoritative states without requiring task IDs, lease IDs or event
   sequence numbers from the user.
7. Preserve durable task, event, attempt and evidence records.
8. Reopen and continue correctly after the control center is restarted.
9. Prevent duplicate side effects after repeated resume or completion requests.
10. Deliver an inspectable result with independent verification.

## 4. Core V1 journeys

### V1J-FILE-01: create, revise and deliver a report

User goal:

> 创建一份简短的项目说明，保存为 report.md；完成后把结论重复两遍。

Required journey:

1. Natural-language preview identifies a file task.
2. File name and content are extracted or clarified in guided fields.
3. User reviews assumptions and confirms task creation.
4. User explicitly starts the task.
5. The system creates and independently verifies the first artifact.
6. The user continues from the verified artifact and requests an append or
   replace version.
7. The system creates a new task-scoped version with source digest and lineage.
8. The final artifact, verifier result and both version identities are visible.

Required assertions:

- Source artifact remains byte-for-byte unchanged.
- Final artifact content exactly matches the confirmed revision.
- Source and result SHA-256 values are recorded.
- Missing content and unsafe paths fail before side effects.
- Repeating the completion request does not create another version.

Evidence:

- `V1J-FILE-01-E1`: preview response and confirmed field values.
- `V1J-FILE-01-E2`: task and event sequence for source creation.
- `V1J-FILE-01-E3`: source artifact bytes and verifier evidence.
- `V1J-FILE-01-E4`: continuation request, lineage and final artifact evidence.
- `V1J-FILE-01-E5`: repeated-request idempotency result.

### V1J-CODE-01: inspect, analyze, approve, edit and test

Test fixture:

- Registered workspace containing one failing unittest.
- One source file whose current digest is available from inspection.
- One candidate patch that makes the test pass.

User goal:

> 检查示例项目为什么测试失败，提出修复，验证通过后运行测试。

Required journey:

1. Natural-language preview routes to the code journey.
2. The system inspects the workspace and displays bounded file evidence.
3. A scripted or configured proposal provider produces an untrusted candidate
   patch with `old_text`, `new_text`, rationale and proposed test.
4. Deterministic validation confirms the current digest, unique match, safe path
   and supported test target.
5. The task pauses at the exact edit approval step.
6. The user approves or rejects that exact step.
7. On approval, the system applies one atomic edit with a backup.
8. The system independently verifies bytes and runs the approved test backend.
9. The final view shows the change summary, test result and remaining limits.

Required assertions:

- No edit occurs before approval.
- A candidate cannot supply host authorization.
- A stale digest or ambiguous match fails closed.
- Two concurrent edits cannot both succeed from the same source version.
- Restarting after the edit but before verification resumes by verification and
  never reapplies the edit.
- Rejected approval produces no workspace change.

Allowed completion variants:

- `V1J-CODE-01D`: deterministic/scripted proposal provider.
- `V1J-CODE-01L`: configured live-model proposal provider, reported separately.

Evidence:

- `V1J-CODE-01-E1`: workspace inspection evidence before editing.
- `V1J-CODE-01-E2`: frozen proposal candidate, session identity and validation.
- `V1J-CODE-01-E3`: pending approval state.
- `V1J-CODE-01-E4`: source/result digest, backup and atomic edit evidence.
- `V1J-CODE-01-E5`: test evidence and host/container authorization identity.
- `V1J-CODE-01-E6`: final workspace observation and diff summary.

### V1J-RESEARCH-01: local source-preserving report

Test fixture:

- Registered workspace with at least three UTF-8 local source documents.
- One source becomes unavailable during a negative variant.
- Questions and candidate findings supported by exact quotes.

User goal:

> 根据本地资料研究这个主题，生成一份带来源和引用的报告。

Required journey:

1. Natural-language preview routes to the research journey.
2. The system asks for registered workspace, source paths, title, output path
   and findings/citations when they are not safely inferable.
3. Proposal validation checks every citation quote against source bytes.
4. The user reviews sources, limitations and uncertainty labels.
5. The report is created only after confirmation and explicit start.
6. The system independently rereads every source and the final report.
7. The report shows source hashes, exact excerpts, uncertainty and the
   local-consistency-only provenance limit.

Required assertions:

- Fabricated or undeclared citations block before report creation.
- Sensitive paths, symlinks and source drift fail closed.
- Missing sources produce truthful failure or partial evidence, never an
  apparently complete report.
- The report does not claim source truth or network verification.
- Repeated start or recovery does not publish a second report.

Allowed completion variants:

- `V1J-RESEARCH-01D`: scripted or caller-provided validated findings.
- `V1J-RESEARCH-01L`: configured live-model synthesis, reported separately.

Evidence:

- `V1J-RESEARCH-01-E1`: guided research request and assumptions.
- `V1J-RESEARCH-01-E2`: source inventory and digests.
- `V1J-RESEARCH-01-E3`: proposal session and citation-validation result.
- `V1J-RESEARCH-01-E4`: final report and report digest.
- `V1J-RESEARCH-01-E5`: independent source/citation revalidation.

### V1J-COMPLEX-01: dependent plan, failure, replan and delivery

Test target:

A three-step plan where:

- step A creates a verified source;
- step B depends on A and has a controllable failure;
- step C is independent or depends on revised B.

User goal:

> 先创建来源文件，再完成依赖它的处理；如果中间失败，保留已完成成果并让我
> 决定是否修改后续步骤。

Required journey:

1. The system routes to complex-plan guidance and validates the bounded DAG.
2. The user confirms the plan, step dependencies and approval boundaries.
3. Step A completes and its artifact is independently verified.
4. Step B fails or pauses at an approval boundary.
5. Completed A evidence remains visible and is never repeated.
6. The user revises only unfinished steps.
7. The revised plan resumes and creates the parent-owned final delivery.
8. Partial failure and recovery are visible throughout.

Required assertions:

- Cycles, unknown dependencies, undeclared references and more than eight steps
  are rejected before execution.
- Pause/replan races cannot overwrite an active execution claim.
- An approval is bound to the exact pending step and current plan version.
- Restart recovers expired waiting-state claims without losing the waiting
  state.
- Parent final artifact evidence resolves under the parent task and is available
  through the workbench artifact route.
- No completed child side effect is repeated after replan or recovery.

Evidence:

- `V1J-COMPLEX-01-E1`: validated plan graph and confirmation.
- `V1J-COMPLEX-01-E2`: per-step child task and verifier evidence.
- `V1J-COMPLEX-01-E3`: pause/approval/failure state and recovery record.
- `V1J-COMPLEX-01-E4`: replanned plan version and preserved completed steps.
- `V1J-COMPLEX-01-E5`: parent-owned final artifact and HTTP preview.

## 5. Cross-journey scenarios

| ID | Scenario | Required result |
| --- | --- | --- |
| V1J-X-01 | Code task pauses, control center restarts, user resumes | Exact pending state survives; no pre-approval edit |
| V1J-X-02 | Research source becomes unavailable | Explicit partial/failed state; no fabricated citation |
| V1J-X-03 | Complex task fails, user replans pending steps | Completed steps and artifacts retained; no duplicate side effect |
| V1J-X-04 | User changes the original goal before execution | Treated as revision/new authorization, not silent scope expansion |
| V1J-X-05 | Repeated recovery or completion requests | One authorized effect and one terminal result |
| V1J-X-06 | Proposal session expires or is replayed | Fail closed before task materialization |
| V1J-X-07 | Parent artifact is modified after completion | Workbench shows drift and does not claim current verification |

## 6. Control-center state presentation

| State | Required user-visible information | Allowed controls |
| --- | --- | --- |
| Draft preview | Interpreted goal, assumptions, missing fields, limits | Edit fields, confirm creation |
| Queued | Task class, confirmed plan summary, no execution claim | Start, revise, cancel |
| Running | Current step, completed steps, evidence summary | Request cooperative pause |
| Awaiting approval | Exact pending step, action, expected result, risk | Approve, reject, pause |
| Paused | Why paused, completed work, next step, restart safety | Resume, replan pending steps |
| Partial | Successful steps, failed/skipped steps, available artifacts | Review/replan or create follow-up |
| Failed/blocked | Exact error and remediation | Inspect, revise where safe, cancel |
| Succeeded | Final artifact, verifier result, source/diff/test evidence | Preview and continue from result |
| Interrupted | Unknown/uncertainty boundary and recovery requirement | Recover under existing safe rules |

No state may display success solely because a model response or child step
finished. The displayed status must come from authoritative task state plus
independent result verification.

## 7. Engine vs model acceptance

### Deterministic engine gate

Must pass with no network or live model:

- all four journey mechanisms;
- all negative controls;
- concurrency and crash recovery;
- UI routing and field guidance;
- proposal session expiry/replay/context separation.

### Live-model product gate

Must use a separately authorized model run and report:

- task-class classification accuracy;
- unnecessary clarification rate;
- useful missing-field identification;
- plan validity and intent fidelity;
- unsafe candidate block rate;
- accepted unsafe executable plans, which must remain 0;
- recovery/replan quality;
- user interventions and end-to-end success;
- cost and latency when measurable.

Live-model evidence cannot be inferred from scripted fixtures.

## 8. Overall V1 completion gate

V1 user journey completion requires all of the following:

1. The four deterministic end-to-end journeys pass through the product surface.
2. All cross-journey recovery and negative controls pass.
3. No scenario reports success without independent artifact/result evidence.
4. The latest official filtered suite passes after the final code change.
5. A frozen input manifest and final review evidence are recorded.
6. Known limitations are visible in the UI and release notes.
7. At least one real user task per class is completed and inspected.
8. Live-model claims are made only if the separate live-model gate passes.

Until then:

```text
V1_USER_JOURNEY_COMPLETE=false
PRODUCTION_TRUST=false
A79_NOT_STARTED
```

## 9. Primary progress metric

The product metric is:

> How many real tasks per week begin from a natural-language goal and end with
> an independently verified, user-usable deliverable?

Supporting metrics:

- unnecessary clarification rate;
- user correction count;
- first-pass delivery rate;
- false-success count, required to be zero;
- recovery success rate;
- duplicate-side-effect count, required to be zero;
- median completion time;
- model, token and infrastructure cost when available.

Token consumption is an investment metric, not evidence of user value.

## 10. Reproducibility and evidence discipline

Each journey must use a newly created temporary runtime home and workspace.
Evidence must include:

- exact request and confirmed interpretation;
- task and attempt identities;
- ordered event or state evidence;
- tool and verifier evidence;
- artifact paths and SHA-256 values;
- approval/replan/recovery records;
- browser/API transcript identity;
- code fingerprint and environment version;
- PASS, FAIL, NOT_RUN, or INCONCLUSIVE for every scenario.

A design document, probe definition, task label, or completed child step is not
an executed acceptance result.
