# Task Control Center v1

## Product decision

The long-term frontend target is a **Task Control Center**: a unified surface for
task discovery, controlled execution, verification, recovery review, and tool
governance.

It is not currently a general infrastructure operations console. Model-provider
administration, host resource management, arbitrary workspace configuration,
and high-risk file mutation remain outside the current product boundary.

## Safety model

The existing read-only workbench remains unchanged:

- `/api` remains `GET`/`HEAD` only;
- mutation requests to `/api` remain `405`;
- SQLite remains read-only and query-only;
- artifact preview still uses registered artifact evidence;
- recovery preview remains non-executable.

Controlled operations use the separate `/control/api` surface exposed only by
the opt-in `control-center` command.

## Implemented actions

### Create a task

`POST /control/api/tasks` creates a real persisted task through the existing
`TaskService.enqueue` path. The client UUIDv4 request ID is the task ID.

- The request requires a matching loopback Origin, the per-process CSRF token,
  explicit confirmation, a bounded goal, operation and finite JSON parameters.
- Replaying the same ID and definition returns `already_exists` without
  creating another task.
- Reusing the ID with a different definition returns
  `409 task_creation_conflict`.
- The server persists the deterministic planning preview and an idempotent
  request digest. It does not invoke a tool.
- “创建排队任务” stops after creation. “创建并提交” refetches the current
  action preview and then requests the existing `start_queued_task` action.
- The create-and-submit start has its own retained request ID and manual receipt
  query. An unresolved start request blocks additional submissions rather than
  creating a second task or generating a replacement ID.
- Creation, planning and execution are separate stages. A `queued` task is not
  an executed task.

`run_tests` tasks can be created and may enter clarification, but the control
center intentionally disables create-and-submit and does not expose a start
action for that backend. Its container, isolation-evidence and authorization
contract remains outside this control-center version. `answer_clarification`
is server-side limited to `write_text_artifact`, so a `run_tests` clarification
cannot resume execution through this endpoint.

### Add a task note

`add_task_note` appends a bounded, sanitized `task_note_added` audit event.
Notes are task-scoped communication, not lifecycle instructions: they do not
change the task definition, plan version, status, lease, artifact or
verification evidence.

### Answer clarification

Eligibility:

- task is `awaiting_clarification`;
- planner mode is deterministic;
- all currently missing fields are supplied;
- the action preview is still current.

Preconditions:

- matching loopback `Origin`;
- per-process CSRF token;
- explicit confirmation;
- exact `request_id` UUID;
- expected `updated_at`;
- expected plan version;
- expected missing-field set.

Execution:

1. In one `BEGIN IMMEDIATE` transaction, validate the request ID/digest,
   task status and plan version, acquire the task lease, and persist
   requested/claimed evidence.
2. Call the pre-claimed `TaskService.resume`.
3. Let the normal planner, Policy, tool, verification, and persistence paths run.
4. Record `control_action_completed` or `control_action_failed`.
5. Reload the authoritative task and evidence in the browser.

The request reservation and task claim are one atomic write. If another control
process or CLI operation already owns the task, the request records a failed
receipt with `task_claim_not_acquired` and returns `409`. A lease whose expiry
has passed but whose lease metadata has not been released is exposed as
`lease_state=expired`; previews return `task_claim_recovery_required` and no
write actions. Expiry inspection never releases the lease, recovers the task,
or changes lifecycle state.

The action result means the action request completed, not that the task
succeeded. The final task status remains authoritative.

### Revise an unstarted task

Eligibility:

- the task has no execution evidence;
- the task is `queued`, `awaiting_clarification`, or `blocked`;
- no active or unreleased execution lease exists;
- the deterministic planner remains authoritative;
- the task definition digest and plan version still match the preview.

The action accepts a bounded patch containing `goal` and/or `parameters`.
Operation type is immutable in the control center. Parameters are merged by key,
so omitted existing parameters remain unchanged.

Execution:

1. Register `control_action_requested`.
2. Call the existing `TaskService.revise_plan`.
3. Apply the revision atomically using the expected plan version and request ID.
4. Record `control_action_completed` or `control_action_failed`.
5. Reload the authoritative request, plan, plan version, and events.

A revision never starts a task or invokes a tool. Concurrency is resolved by the
existing optimistic plan-version check; only one competing revision can commit.
`interrupted` tasks are not offered revision because the lifecycle state machine
does not permit a safe return to queued.
The request ID is recorded as the revision `source_id`, allowing a lost
completion event to be reconciled from the persisted `plan_revision`.

### Cancel a queued task

Eligibility:

- task is `queued`;
- no execution claim has ever been acquired;
- execution mode remains `not_executed`;
- no artifact or tool result exists;
- the expected plan version is still current.

Execution:

1. Register `control_action_requested`.
2. Call the existing `TaskService.cancel`.
3. Cancel atomically only if the expected plan version is current and no active
   execution lease exists.
4. Record the control request ID in the `task_cancelled` event.
5. Record `control_action_completed` and reload authoritative state.

Cancellation and unstarted-task revision share the same optimistic plan version,
so they cannot both commit. A lost cancellation completion event is reconciled
from the persisted `task_cancelled` event with the same request ID.

### Start a queued artifact task

Eligibility is intentionally narrower than the general executor:

- task is `queued`;
- no execution claim has been acquired;
- task is deterministic and uses `write_text_artifact`;
- expected plan version remains current.

Execution:

1. In one `BEGIN IMMEDIATE` transaction, reserve the request ID, validate the
   expected plan version and task state, acquire the lease, and persist
   requested/claimed evidence.
2. Call `TaskService.execute_task` with the pre-claimed lease.
3. Record the control request ID, action and attempt ID in `task_claimed`.
4. Run the normal planner, Policy, artifact writer, and independent verifier.
5. Record the control completion and reload authoritative task and evidence.

The shared action result means execution was attempted and the backend returned;
the task status remains authoritative. Lost completion events may be reconciled
from the request-linked `task_claimed` event when the same attempt reached a
terminal state. Nonterminal attempts remain `in_progress`.

`run_tests` and other execution backends are not exposed through this action.

## Idempotency

Each request carries a UUIDv4 and a SHA-256 digest of the exact action content.

- request-ID reservation and task/version checks commit in one SQLite
  `BEGIN IMMEDIATE` transaction;
- each task can persist at most one `control_action_requested` event for a
  request ID, including across control-center processes;
- concurrent reuse with different content returns `action_request_conflict`
  before a second requested event or side effect;
- a completed request ID returns `already_processed`;
- a requested ID without a verified completion fails closed as
  `action_state_unknown`;
- reusing an ID with different content returns `action_request_conflict`;
- control actions are serialized within one control-center process.

Cross-process coordination is not used to merge actions in v1. If concurrent
processes compete for the same task, the SQLite claim permits at most one
executor and every losing control request fails closed.

`GET /control/api/tasks/{task_id}/requests/{request_id}` exposes the persisted
request receipt without changing task or artifact data. It requires the same
loopback Host and CSRF token. A request is reported as `completed` only when a
completion event exists, or when strictly request-linked action evidence is
sufficient for the documented reconciliation below. Task status alone is never
used to infer completion.
A requested action without a terminal control event returns `state=unknown` and
`manual_review_required=true`. For unstarted-task revisions, a persisted plan
revision with the same request ID is sufficient evidence to reconcile completion
and is reported with `reconciled_from=plan_revision`. A cancellation may similarly
reconcile from `reconciled_from=task_cancelled_event`. A control-started task may
reconcile a lost completion from its request-linked `task_claimed` event once the
same attempt is terminal (`reconciled_from=task_claimed_terminal`).

Receipts with conflicting action IDs, conflicting request digests, or both
completion and failure evidence are reported as `state=inconsistent` with
`manual_review_required=true`; they are never normalized into success.

## Endpoints

```text
GET  /control/api/session
POST /control/api/tasks
GET  /control/api/tasks/{task_id}/actions
GET  /control/api/tasks/{task_id}/requests/{request_id}
POST /control/api/tasks/{task_id}/actions
```

The existing read-only endpoints remain:

```text
GET  /api/tasks
GET  /api/attention
GET  /api/tasks/{task_id}
GET  /api/tasks/{task_id}/artifact
```

## Reproducible acceptance evidence

The offline non-container evidence flow is:

```bash
RUN_DIR="artifacts/audits/task-control-center-v1/final-validation-$(date -u +%Y%m%dT%H%M%SZ)"
PYTHONPATH=src python3 tests/task_control_center_evidence.py run \
  --root "$PWD" \
  --review-manifest artifacts/audits/task-control-center-v1/independent-review/current-review-manifest.json \
  --output-dir "$RUN_DIR"

PYTHONPATH=src python3 tests/task_control_center_evidence.py summarize \
  --root "$PWD" \
  --run-metadata "$RUN_DIR/run-metadata.json" \
  --output artifacts/audits/task-control-center-v1/stable-summary.json \
  --replace-reason "describe why the prior summary is superseded"
```

The runner:

- excludes test IDs containing container, Docker, live, or online terms;
- records both Python environments, code fingerprint before/after, Git state,
  test IDs, counts, raw logs, and file hashes;
- filters mutation cases using the same execution boundary;
- snapshots the previous stable summary so repeated generation from the same
  inputs is byte-for-byte deterministic;
- verifies the current code fingerprint, all evidence hashes, count identities,
  required control tests, frozen evidence hashes, and review-channel status;
- refuses missing/corrupt inputs, mismatched counts or hashes, stale code,
  unsafe output paths, missing review reports, zero-file reviews, wrong review
  HEADs or code fingerprints, review input paths under `.idea` or `.env*`,
  reports without an explicit conclusion, report/machine-result conclusion
  disagreement, incomplete candidate file coverage, and evidence that silently
  omits review status;
- preserves `REQUEST CHANGES`, `WATCH`, `BLOCK`, and `UNAVAILABLE`; only two
  completed `CLEAR` lanes can set merge readiness.

`RUN_DIR` is unique for every invocation and must be new and empty. Both
commands use that same variable so the summary always reads the current run.

The review manifest uses
`schema_version=task-control-center-independent-review-v1` and contains
`repository`, `head`, `code_fingerprint`, plus `code_review` and
`architecture_review` objects. A completed lane records its normalized
`status`, `conclusion`, `files_reviewed`, report path and SHA-256, target
repository/HEAD/code fingerprint, covered scope, and a machine-readable result
JSON with its own SHA-256. The manifest and result JSON must agree on status,
conclusion, target identity, file count, report hash, and covered scope. An
unavailable lane records `status=UNAVAILABLE`, `completed=false`,
`files_reviewed=0`, and a reason.

Completed Markdown reports must include:

```text
Independent review conclusion: CLEAR|REQUEST_CHANGES|WATCH|BLOCK|UNAVAILABLE
```

For a completed code-review lane, `PASS` is accepted as the report spelling of
`CLEAR`; for architecture review, `BLOCKED` is normalized to `BLOCK`. The
normalized report conclusion must equal both manifest and machine-result status.
The reviewed-file set must match the changed candidate exactly, including
untracked source, test, and documentation files. Generate the candidate snapshot
with:

```sh
PYTHONPATH=src python3 tests/task_control_center_evidence.py snapshot \
  --root "$PWD" \
  --output artifacts/audits/task-control-center-v1/final-candidate-snapshot-<fingerprint-prefix>.json
```

## Browser behavior

- The `control-center` page identifies itself as “受控操作”.
- The normal `workbench` page remains “只读”.
- The action form appears only when a valid control session exists.
- A real task-creation form exposes goal, operation and parameters with a
  submit-time preview. Create-only and create-and-submit are separate buttons.
- `run_tests` shows create-only behavior explicitly; its submit button is
  disabled until a future backend contract is separately reviewed.
- Task notes are displayed in a task conversation section and labelled as
  audit-only communication.
- The submit button is disabled until the confirmation checkbox is selected.
- Unstarted tasks expose a goal and parameter-patch form; operation type remains
  immutable and the action never executes the task.
- Unstarted queued tasks expose a separate terminal cancellation form with its
  own explicit confirmation.
- Supported artifact tasks expose a separate execution form that explicitly
  states it may create task artifacts and will not retry automatically.
- An ambiguous response exposes a manual “查询操作结果” action; it does not
  retry automatically.
- The page does not report success based on a button click; it reloads task
  status, events, artifact evidence, and verification after the server response.
- All dynamic text is rendered with `textContent`.

## Not enabled

- retry;
- starting `run_tests` or other unsupported execution backends;
- resume from general failures;
- cancellation after execution has started;
- recovery execution;
- editing after execution evidence;
- tool or permission governance;
- file overwrite or deletion;
- background workers, queues, polling, or push notifications.

## Future layers

1. controlled test execution and retry contracts;
2. recovery approval and execution;
3. tool, skill, policy, and workspace governance;
4. high-risk actions with explicit confirmation and post-action verification.

Each layer requires its own backend contract, tests, authorization checks, and
rollback/stop conditions.
