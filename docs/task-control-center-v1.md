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

Evidence runs must enter through `_run-tests` or an explicitly reviewed test-ID
whitelist. Raw `unittest discover` is not an approved evidence command because
it bypasses the blocked-ID predicate. Execution has a second fail-closed guard:
the selected collection is rejected before the unittest runner starts if any
ID matches the blocked container/Docker/live/online module or term policy.

`RUN_DIR` is unique for every invocation and must be new and empty. Both
commands use that same variable so the summary always reads the current run.
When an accepted summary requires historical provenance, pass the predecessor
digest supplied by an independent trusted configuration:

```bash
--trusted-previous-sha256 "$TRUSTED_PREVIOUS_SHA256"
--trusted-previous-candidate-snapshot \
  /trusted/location/final-candidate-snapshot-previous.json
```

Omitting that anchor is a supported fail-closed downgrade: semantic history may
be inspected, but merge acceptance remains unavailable.
The digest identifies the predecessor bytes. The separately supplied candidate
snapshot is read as its own external evidence entity and then passed through
the same authoritative candidate-payload validation used by the current
candidate validator. The fixed repository root independently rebuilds the
candidate mode, Git state, status hash, fingerprint, manifest and complete file
set; the snapshot must match those rebuilt values. A valid Git OID or a
self-consistent SHA-256 value is not enough. If a predecessor working-tree or
historical state cannot be reconstructed from the trusted current repository,
the snapshot is not treated as a trusted predecessor and acceptance fails
closed. A caller-provided file does not become an independent provenance root
merely because its hash was recomputed.

The output directory is opened from the already pinned repository root through
no-follow directory descriptors. Every run artifact, including the previous
summary snapshot, candidate identity, both test results and logs, mutation
cases, mutation report, metadata, bootstrap/error status and acceptance status,
is written through that held directory descriptor as staging evidence. These
writes reject symlink and escaping path components, but they are not a proof
that a same-principal directory rename is impossible. Formal publication uses
the separate custody capability described below. Test and mutation child
processes write to a fresh private temporary directory; the parent reads those
files through a separately pinned descriptor and removes the directory on
success and every handled failure path.

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

The manifest repository and each lane `target_repository` must equal the exact
canonical repository path string selected by the caller. Relative paths,
`..` spellings, `/tmp` aliases and symlinks to the same or a different
repository are rejected; resolving an alias to prove equality is not accepted.

### Candidate identity v2

Candidate snapshots use
`schema_version=task-control-center-candidate-identity-v2` and distinguish:

- `working_tree`: relevant files changed relative to the current HEAD;
- `committed_range`: files changed from an explicit `base_head` to
  `target_head`;
- `bootstrap`: an initialization record where historical acceptance evidence
  is not yet available.

Each snapshot records `target_head`, `reviewed_head`, `code_fingerprint`,
`file_count`, `file_manifest_sha256`, `evidence_status`, and the complete
path/SHA-256 manifest. A working-tree or committed candidate must contain at
least one file. A committed candidate also requires the current HEAD to equal
`target_head`, `base_head` to be an ancestor of `target_head`, and a worktree
clean outside ignored `.idea/**` content. The file set comes from
`git diff base_head target_head`, never from `git diff HEAD`.

The candidate scope is all tracked files plus non-ignored untracked files. YAML,
INI, TOML, build configuration, test configuration, resources and unknown
extensions are included by default. Explicit exclusions cover Git metadata,
`.idea/`, virtual environments, generated artifacts, logs, temporary files,
tool caches, bytecode and known secret-like names or suffixes. A working-tree
change to a protected or explicitly excluded path is recorded under
`excluded_changes` rather than silently disappearing. `candidate_scope` records
the inclusion policy, exclusions and `unknown_extension_policy=include`.

`bootstrap` cannot produce `PASS_EVIDENCE_INTEGRITY` by itself. Its identity
status is `BOOTSTRAP_INCOMPLETE`, and missing historical inputs must remain
visible rather than being replaced with defaults.

### Review binding

When a reviewed worktree is later committed without content changes, a
`review-binding-*.json` record may prove file-path and SHA-256 equivalence. The
record preserves the original review HEAD, report and machine-result hashes,
reviewed file manifest, target commit file manifest, code fingerprint,
generation timestamp, generator version, statuses and uncovered scope.

The original review reports are never rewritten. A binding succeeds only when
both reviewed file sets exactly match the committed candidate and every
path/SHA-256 pair is identical. It may describe content equivalence, but it
sets `acceptance_supported=false` and `merge_ready=false`; browser execution,
container/live execution, real-model calls and current test/mutation evidence
must be validated separately.

### Evidence bundle

A portable historical evidence bundle uses
`schema_version=task-control-center-evidence-bundle-v2`. Its manifest names a
self-contained root and records:

- the acceptance policy;
- candidate snapshot, base, reviewed and target HEAD identities;
- the original review manifest, reports and machine results;
- the content-equivalence review binding;
- the previous stable summary;
- all three frozen evidence inputs with status and expected/observed hashes;
- browser evidence with candidate binding, status and reason;
- runtime verification status, scopes and reason;
- a role-tagged file manifest.

Every bundle path is relative to the bundle root. Absolute paths, `..`,
backslashes, `.idea`, `.env*`, symbolic links, Unicode/case aliases, duplicate
roles and undeclared files are rejected. The manifest itself and its parent
components cannot traverse a symbolic link. Unknown schema fields, duplicate
JSON keys and non-finite JSON numbers fail closed.

All production entry points preserve the caller-supplied bundle path until
validation. Validation rejects symbolic links in the manifest path and any
parent component before adding it to a valid result. File reads begin from an
opened root directory and walk every parent component with directory file
descriptors and `O_NOFOLLOW`; the final component is opened relative to the
checked descriptor, verified as a regular file, and read from the same
descriptor. A platform without the required directory-descriptor primitives
fails closed.

The Bundle root is opened once from the filesystem root using no-follow
directory components. Manifest parsing, size enforcement and SHA-256 use the
same opened file descriptor. The root's device/inode identity is pinned for the
rest of validation, so replacing the root path with a symlink or a different
real directory fails closed. Candidate snapshots, review manifests, review
bindings and browser evidence reuse the protected evidence-root path and
identity; they never re-resolve it. Final Bundle enumeration runs from the
pinned directory descriptor, not `rglob()` or other path-based traversal.
The repository/evidence root uses the same filesystem-anchor walk for every
subsequent read.

`TrustedEvidenceRoot` owns the opened directory FD and device/inode identity.
Its lifetime covers a complete public nested-validation call, or the complete
validation, summary construction, summary recomputation and status-write
lifecycle in `build_summary()`/`write_summary()`. A closed root object fails
closed. Nested validation cannot fall back to a caller-provided path after the
root object has been released.

Repository and external Bundle roots are separate trusted objects. Entry
points pin the repository first, then select and pin the Bundle. Both identities
remain active through final acceptance. Replacement at any of those stages
fails closed. If a `TrustedEvidenceRoot` identity check fails during
construction, the newly opened FD is closed before the original exception is
re-raised; repeated failures do not accumulate descriptors.

After the repository root is pinned, the evidence tool does not call
`Path.resolve()` on that root again. Outputs are converted to root-relative
paths and use directory-descriptor staging; formal publication is gated by the
custody capability rather than by an assumed atomic ancestry guarantee.

The public candidate-snapshot, review-binding and summary writers require an
explicit persistent-publication custody capability before creating directories,
temporary files, summaries or acceptance status. The current macOS runtime
reports this capability as `UNSUPPORTED`, so those public writers fail closed
before entering a check-to-`replace` window.

`TrustedEvidenceRoot.write_bytes_atomic()` is reserved for publication and
requires custody. Internal `_write_bytes_staging()` is used only by controlled
staging paths; such writes are not formal publication and cannot support
`merge_ready=true`. The previous trust-anchor checks remain useful for detecting
moves before staging, but they are not described as atomic ancestry fencing.
Formal acceptance remains `NOT_VERIFIED` until an independent custody mechanism
can prove the durable output contract.

On macOS, `/etc`, `/tmp` and `/var` are normalized only to the corresponding
immutable `/private` system aliases. Other symbolic-link components are
rejected. Trust anchors are the filesystem root, the current process
credentials, and the caller-selected initial repository or Bundle path. This
does not defend against a same-principal writer changing trusted directory
contents or against an unsigned Bundle with a malicious provenance.

The validator reads every referenced file and recomputes its SHA-256; a hash
declared only by the bundle is never trusted. The candidate snapshot, review
reports, machine results, review binding and browser evidence are independently
validated again. Candidate identity, file manifests and code fingerprints use
the same NFC/case-folded path identity and no-follow regular-file reader.

The previous summary is also validated semantically, not merely as a JSON object
with a non-empty incident list. Current schema-v2 summaries require complete
typed code, test, mutation, frozen-evidence, review, acceptance-gate, candidate
identity and validation-input records. Candidate/code fields must agree; the
recorded reviewed status must match the declared acceptance; successful
summaries must have no blocking or unverified scopes; replacement-reason hashes
must bind the superseded-summary reason; and the superseded digest must occur in
a non-empty, duplicate-free evidence-history chain. Complete legacy schema-v1
stable summaries remain supported through a separate closed validator. Unknown
schemas, missing fields and contradictory acceptance fields fail closed before
the previous summary can participate in current acceptance.
The v2 acceptance state is checked as one state machine. `DIRECT_CLEAR` requires
a current direct review, content-equivalent clearance requires the matching
policy and binding, `UNVERIFIED` cannot accompany acceptance, and unverified
scope, blocking reasons, runtime, merge readiness and publication custody must
all agree. Recomputing every hash does not bypass these semantic checks.
Accepted content-equivalent history must include a complete top-level review
binding. Accepted `special_review` history must include its path and SHA-256,
`merge_ready=true`, no blocking reasons, no uncovered scope, and nested
code/architecture states matching the authoritative review rollup. V2 history
must also record `review_status.review_gate` and it must equal the acceptance
gate. `special_review.candidate_acceptance` is derived from the authoritative
resolution: direct clearance records `INDEPENDENT_REVIEW_CLEAR`, while
content-equivalent clearance records `CONTENT_EQUIVALENT_REVIEW_CLEAR`.
The historical review-binding record must also carry a unique-typed
`unverified_scope` array and every item must be present in the accepted gate's
authoritative `unverified_scopes`. Since an accepted gate cannot contain
unverified scopes, a legacy or rewritten history cannot hide non-empty binding
scope behind an empty summary field.
Each completed lane must carry the full reviewed-file manifest, exact candidate
target HEAD and fingerprint, `completed=true`, a matching conclusion, required
coverage, and report/result identities. A lane-local uncovered scope must be
projected into the authoritative rollup; an empty top-level scope cannot hide
it.

The normalized review object is validated again by the final acceptance gate,
not only by the historical-summary or manifest validators. This shared lane
validator requires an exact `target_repository`, reviewed files equal to the
candidate manifest, required coverage, coherent completion/conclusion/status,
and distinct report/result references. A directly supplied malformed review
summary therefore cannot reach `ACCEPTED_FOR_MERGE_REVIEW`.

For a direct target-head review, the final gate also reopens the review
manifest and all declared report/result files through a held evidence root,
recomputes their hashes and lane identity, and compares the result byte for
byte with the supplied normalized review summary. Deleting or replacing
evidence after an earlier manifest validation therefore causes the final gate
to fail closed.

The authoritative gate captures one immutable normalized review-evidence
snapshot and uses only that snapshot through decision formation. The snapshot
contains the validated manifest and lane identities, report/result hashes,
reviewed-file manifest, coverage and statuses. A decision therefore means
"authorized from this captured, validated evidence snapshot"; later mutation
of the source path does not retroactively alter that decision. A new decision
must capture evidence again and will reject a missing or changed source. This
semantic boundary replaces a check-then-authorize contract that would require
cross-filesystem locking; ordinary `exists()` or rehash checks after
authorization do not create such a lock.
Caller candidate, review and binding mappings are canonically frozen once.
Direct review validates the captured snapshot, compares it with the frozen
caller view, and then derives all status, coverage and resolution facts only
from the snapshot. Content-equivalent review derives its authoritative summary
from the revalidated binding and uses only that detached value. A stateful
Mapping cannot expose one status during validation and another during
authorization.

The final gate also requires a held `TrustedEvidenceRoot` for the repository.
It independently rebuilds the candidate snapshot from that root, including the
file count, file manifest, fingerprint and status hash, and compares the result
with the supplied candidate. The candidate object's `evidence_status` label or
a context copied from the candidate is not sufficient by itself.
Candidate construction enumerates the scope once, captures every included file
once into an immutable byte map, and derives per-file hashes, the changed-file
manifest and the full code fingerprint from that map. Candidate paths, Git
HEAD/status and every file's device/inode/mode/size/mtime/ctime must remain
stable through two full re-read/metadata passes. Every path component is walked
with `O_NOFOLLOW`; checking only the final component is insufficient. After the
first full pass, Git HEAD/status, branch, the complete candidate path set and the
changed set are checked again. A second full pass follows, then the same
Git/path/change checks are repeated as the final return boundary. The first pass
closes cross-file interleavings, including restored content; the later checks
close a path added while a full file pass is running. Each checkpoint must match
the captured generation; persistent path-set, intermediate-symlink, file or
Git-state changes observed at a checkpoint fail closed. A transient add-remove
or replace-restore ABA that occurs and is fully reversed between checkpoints is
not guaranteed to be detected. This is checkpoint-consistent bounded capture,
not continuous interval atomicity or a physical filesystem snapshot; writes by
an external actor that ignores coordination after the final boundary remain
outside the guarantee.

For accepted content-equivalent history, the review binding's `reviewed_head`
must equal `candidate_identity.reviewed_head`; a merely well-formed OID is not
sufficient. Legacy v1 history is bound to its own `code.head` and
`code.code_fingerprint`, while the caller-supplied trusted repository identity
must match each completed lane's `target_repository`. If the trusted repository
context is unavailable, legacy history may be parsed for inspection but cannot
participate in acceptance.

History acceptance additionally requires an external trusted predecessor
candidate snapshot. Its repository, target/reviewed HEADs, files, fingerprint,
status hash and manifest are independently validated and compared with the
history. Rehashing a rewritten predecessor and updating its digest is
insufficient when it does not match this separately supplied snapshot. Legacy
history remains readable without the snapshot but cannot grant acceptance.

Content-equivalent binding is validated at the final gate with the complete
binding schema and an actual evidence root. The gate revalidates the bound
review manifest, report/result entities and their hashes before comparing the
normalized binding: repository, candidate mode, reviewed/target HEADs,
fingerprint, file count, reviewed/target manifests and files, review lanes,
coverage, result/evidence status, and the non-authoritative
`acceptance_supported=false`/`merge_ready=false` assertions. Missing or
unreadable evidence, and any hidden unverified/uncovered scope, fail closed.
The binding's `base_head` must exactly equal the candidate's `base_head`;
target-tree equality is not a substitute for this authorization field.
The binding's validated `unverified_scope` is merged into the authoritative
gate `unverified_scopes`; it cannot disappear through serialization or an empty
review-summary list. A content-equivalence review may remain `CLEAR`, but any
non-empty unverified scope forces `NOT_VERIFIED`, adds
`review_binding_unverified_scope`, and keeps `merge_ready=false`.

For content-equivalence, the gate derives a complete normalized review summary
from the independently validated binding and requires canonical equality with
the supplied normalized summary. All lane report/result paths and hashes,
reviewed files, coverage, target identity, status and acceptance-derived labels
participate. Only the known manifest path `bundle/...` representation and the
equivalent direct/content-equivalent clear or blocked acceptance labels are
normalized; missing fields, unknown fields and evidence mismatch fail closed.

The public `evaluate_acceptance_gate()` is diagnostic and cannot self-assert
acceptance. Caller booleans or mappings for tests, frozen evidence, predecessor
trust and runtime are no longer accepted by its public signature. The
acceptance-producing path uses a private prerequisite proof issued only after
the complete run metadata, tests, mutation, frozen evidence, predecessor and
runtime validations have succeeded. Only that internal proof can reach the
state-machine decision.
The proof is bound to canonical candidate, review-summary and prerequisite
evidence digests and is signed by a process-local authority key. Reusing it for
another repository, HEAD, fingerprint, manifest, review identity or evidence
set, or reconstructing a modified proof after serialization, fails closed. The
old flag-only issuer/token interface no longer exists. Process-local signing
binds supported API authorization to validated evidence; it is not isolation
from arbitrary code execution in the same trusted process, which requires an
independent process or OS boundary.
Each issuance has a random handle and strong in-memory record. Consumption
prepares and validates canonical candidate/review/evidence values outside the
global lock, then checks issuance state and atomically changes `ACTIVE` to
`CONSUMED` under a short lock. Evidence mismatch, invalid proof data, signature
or canonicalization failure changes an ACTIVE record to `REVOKED`. `status()`
applies timeout and returns `EXPIRED` without waiting for consume. The public
diagnostic gate and formal summary producer both use the same structured proof
ownership transaction. The transaction holds the exact issued proof, requires
normal completion to consume it, revokes it on any exception before consumption,
and also revokes an unconsumed proof on an otherwise normal context exit. The
original exception is preserved; cleanup failure is attached as a note.
Before issuance, the transaction creates an unguessable owner token and binds it
to the eventual ACTIVE record. A failure after ACTIVE registration but before
the proof handle returns can therefore still be fenced and revoked by owner.
Revocation commits `REVOKED` before fallible clock/purge work; failed cleanup
does not return a revoked record to ACTIVE.
The owner token is a process-local bearer capability: possession of the token
value is sufficient for owner revocation. It is not independent issuer identity
authentication, and arbitrary same-process code remains outside the isolation
model. If adding an exception note fails, diagnostics fall back to a structured
`diagnostic_notes` attribute and must not replace the original exception.
Normal context exit always attempts owner cleanup; it does not use a fallible
status query as the precondition. A consumed proof remains terminal, while an
unconsumed proof is revoked. A status/clock failure therefore cannot skip owner
cleanup.
Consumed, revoked and expired proofs cannot authorize again.

Terminal records are normally retained for 60 seconds so replay attempts can
return an explicit terminal status, then are purged opportunistically when
issuing, revoking or querying record count. Status never purges the record whose
terminal state it is currently returning. With zero retention, the first
observation returns `EXPIRED`; a later query after cleanup returns `NOT_ACTIVE`,
while consume and revoke fail closed as "not active". Under capacity pressure,
terminal records may be purged earlier. The retained set is hard-capped at
1,024 records; if capacity remains occupied by ACTIVE proofs, new issuance
fails closed rather than dropping replay state. This is process-local replay
protection, not a durable cross-process or crash-persistent guarantee.

Semantic validity is not provenance authenticity. A caller-provided summary
digest and history array live inside the same untrusted input, so they cannot
prove that a predecessor really existed in an independently trusted history.
Acceptance therefore also requires an out-of-band
`trusted_previous_summary_sha256` supplied by the invoking caller or trusted
configuration. The value must match the actual previous-summary bytes. Without
that independent anchor, validation may still report the record as
`AVAILABLE_UNTRUSTED`, but the acceptance gate cannot become
`ACCEPTED_FOR_MERGE_REVIEW`. A wrong anchor fails immediately.

`UNKNOWN`, `NOT_VERIFIED`, `REQUEST_CHANGES`, `WATCH`, `BLOCKED` and incomplete
records are never upgraded merely because a bundle or binding exists.

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
  --candidate-mode committed_range \
  --base-head "$BASE_HEAD" \
  --target-head "$TARGET_HEAD" \
  --reviewed-head "$REVIEWED_HEAD" \
  --output artifacts/audits/task-control-center-v1/final-candidate-snapshot-<fingerprint-prefix>.json

PYTHONPATH=src python3 tests/task_control_center_evidence.py review-binding \
  --root "$PWD" \
  --review-manifest artifacts/audits/task-control-center-v1/independent-review/current-review-manifest.json \
  --base-head "$BASE_HEAD" \
  --target-head "$TARGET_HEAD" \
  --output artifacts/audits/task-control-center-v1/review-binding-<fingerprint-prefix>.json
```

On the current macOS runtime these commands fail with
`persistent publication ... UNSUPPORTED` before creating an output. They become
usable only when the runtime supplies a custody mechanism that can prove the
persistent-publication ancestry invariant.

The evidence runner requires an explicit historical summary input:

```text
--previous-summary artifacts/audits/task-control-center-v1/stable-summary.json
```

Alternatively, provide a self-contained bundle:

```text
--evidence-bundle /absolute/or/explicit/path/evidence-bundle.json
--acceptance-policy target_head_review_required
```

Bundle mode is mutually exclusive with the legacy `--review-manifest` and
`--review-binding` inputs. The same explicit policy must be supplied to
`summarize`. Validate a bundle without running tests with:

```text
PYTHONPATH=src python3 tests/task_control_center_evidence.py validate-bundle \
  --bundle /absolute/or/explicit/path/evidence-bundle.json
```

If the argument is omitted or the file is unavailable, the runner writes
`previous-summary-status.json` with `candidate_mode=bootstrap`,
`evidence_status=BOOTSTRAP_INCOMPLETE`, a `missing_inputs` list,
`acceptance_supported=false` and `merge_ready=false`, then exits with an error
before running tests or mutation. A missing bundle is reported as
`NOT_AVAILABLE`; a present but conflicting bundle is `BLOCKED`.

The legacy `run` + `summarize` path with only `--review-manifest` and
`--previous-summary` can verify historical and test evidence but does not
provide a candidate-bound runtime input. It therefore remains at most
`PASS_EVIDENCE_INTEGRITY` with `merge_ready=false`; final review readiness
requires the external bundle or an equivalent validated runtime source.

Browser evidence is current only when its `candidate.head`,
`candidate.code_fingerprint` and candidate snapshot SHA-256 match the validated
candidate identity. A browser record without that binding, including the older
`e044eba...` response-loss artifact, is historical and cannot support the
current candidate.

Browser evidence uses the explicit
`task-control-center-browser-validation-v1` schema. Missing or unknown schema
versions, non-object values, unknown fields, incomplete scene sets and malformed
nested fields fail closed. HTTP status, tool-start counts, event counts and
viewport width require exact integer types; booleans, floats, numeric strings,
missing values and out-of-scenario values cannot be treated as valid success.

The browser declaration is not trusted. The validator evaluates the actual
recorded results for every required lease scenario, checks console errors and
preview read integrity, and derives `VALID_FOR_CANDIDATE`. A manifest claiming
validity for failed, missing or contradictory results is rejected. Runtime
`VERIFIED` remains limited to the implemented `browser` scope and requires at
least one declared browser record. When runtime verification is `VERIFIED`,
every declared record must be declared and evaluated as
`VALID_FOR_CANDIDATE`; one valid record cannot mask a failed, unavailable,
unknown, missing or ambiguous record. The reported valid-evidence count must
equal the complete declared browser record count.
Lease evidence must show active/expired/released behavior, no forged-operation
side effect, unchanged preview state and no mobile horizontal overflow.
Response-loss evidence must show retained request ID, completed same-request
reconciliation and no duplicate execution.

### Acceptance state machine

The final summary records these states separately:

```text
schema_version=task-control-center-evidence-v2
```

```text
candidate_identity
historical_review
content_equivalence_binding
current_target_independent_review
review_gate
tests
frozen_evidence
previous_summary
runtime_verification
evidence_integrity_status
acceptance_status
merge_ready
```

Two explicit policies are supported:

- `target_head_review_required` is the default. A historical content-equivalent
  review does not satisfy the current-target review gate.
- `content_equivalence_allowed` accepts a fully validated content-equivalent
  binding for the review gate, but still requires current tests, frozen
  evidence, the historical summary and candidate-bound runtime verification.

The trusted caller controls the policy. A bundle may repeat the declared policy
but cannot override the caller. A mismatch is rejected as `BLOCKED`; missing or
unknown policy values also fail closed.

`PASS_EVIDENCE_INTEGRITY` with `merge_ready=false` means the provided evidence
is internally valid while a required acceptance condition is still missing.
Only `ACCEPTED_FOR_MERGE_REVIEW` sets `merge_ready=true`, and it requires all of
the following:

- candidate identity verified;
- the review gate satisfied under the selected policy;
- both review conclusions `CLEAR`;
- no report or binding conflict;
- both test environments and mutation evidence pass;
- frozen evidence and the historical summary available;
- runtime verification `VERIFIED` for the current candidate.

In addition, every completed review lane supplies canonical `covered_scope` and
`uncovered_scope` arrays. Manifest and machine-result copies must agree. Any
non-empty uncovered scope is projected into the authoritative
`unverified_scopes` and prevents acceptance under both direct and
content-equivalence policies.

A binding never satisfies runtime verification, and historical evidence never
masks a current `REQUEST_CHANGES`, `WATCH`, `BLOCKED`, `NOT_VERIFIED` or
`NOT_AVAILABLE` state.

The summary is the sole acceptance authority. `acceptance-status.json` uses
`task-control-center-acceptance-status-v2` and declares
`authoritative=false`; it never carries a positive `merge_ready` value.
On the current platform, persistent-publication custody is `UNSUPPORTED`, so
`write_summary()` fails before invalidating status or creating a summary. No
formal success status can be exposed. A future platform-specific custody
provider may implement the existing lifecycle, but check-then-`replace` is not
accepted as proof of that capability.

Consumers must call `validate_summary_is_current()` or the non-authoritative
`validate_acceptance_status()` helper. A status file alone never grants
acceptance. Real process termination during these filesystem operations is
still `NOT_TESTED`; only deterministic write-failure and interrupted-commit
paths are covered by tests.

Every summary records a versioned `validation_input` containing the run
metadata path, its SHA-256, the explicit Bundle path and the generator version.
`validate_summary_is_current()` reads that metadata, re-runs the evidence and
acceptance gates, rebuilds the summary and requires byte-equivalent canonical
JSON. Correct candidate identity fields alone are insufficient. The
non-authoritative status file must additionally match the authoritative
summary's evidence-integrity and candidate-acceptance fields.

The replacement reason is supplied as an independent
`expected_replacement_reason` verification input and its SHA-256 is recorded in
`validation_input`. The reason embedded in the summary is compared against that
input and cannot authorize itself.

Test-result schema v3 flattens and freezes the selected collection before
execution, records the exact collection and every test's terminal outcome, and
derives counts from those records. Execution does not perform a second
discovery to write a different manifest. Duplicate, missing, extra or
unattributable IDs fail closed.

`subTest` is counted at parent-test-method granularity. Multiple subtest events
with the same terminal outcome merge into one parent outcome; conflicting
terminal outcomes fail closed. `TEST_EXECUTION_TRUST_SUPPORTED=false` is the
production default because candidate tests share the Python process with the
runner. The verifier detects changes to unittest runner, suite, test-case and
result primitives and fails closed; the authoritative gate records
`test_execution_trust=UNSUPPORTED` and keeps `merge_ready=false`.
`unexpectedSuccesses` is processed separately from `(test, detail)` result
entries. Each unexpected success retains its TestCase ID, is recorded as
`unexpected_success`, increments only `unexpected_successes`, and causes the
success gate to fail while result and log serialization still complete.

Mutation execution evidence has a closed schema for each declared scope.
`in_process` kills require a non-missing trace, executed mutated lines, a valid
failing test command and consistent boolean fields. Aggregate scope counts are
recomputed from result records. The validator then independently reruns every
selected mutation from a pinned project snapshot and requires the submitted
report to equal the rerun result. Mutually consistent self-asserted JSON hashes
are therefore not accepted as execution proof.

The repeat still executes the candidate-provided mutation runner, so it is an
isolated rerun rather than proof of runner authenticity.
`MUTATION_EXECUTION_TRUST_SUPPORTED=false` is the production default and the
authoritative gate records `mutation_execution_trust=UNSUPPORTED`.
Mutation patch paths must be normalized relative paths. The runner walks each
component with directory descriptors and `O_NOFOLLOW`, rejects absolute paths,
backslashes, drive syntax, `..`, symbolic links and special files, and verifies
file identity and digest again before replacement.
The mutation source project is copied through an independently opened,
no-follow source-root descriptor. Every directory and regular file is opened
relative to its held parent descriptor with `O_NOFOLLOW`; symbolic links,
multiply-linked files, special files, directory cycles and source-tree metadata
changes fail closed. One aggregate transaction holds `pyproject.toml`, `src` and
`tests`; it snapshots all component identities before copying, checks every
component and the complete tree identities afterwards, then reopens the project
root and each component to compare device/inode, and performs one final complete
held-tree/file identity pass before returning. The private staging tree is itself
written only from one immutable captured byte map, with source path-set and
generation checks before and after staging. A change between directory components
therefore rejects the whole project rather than mixing generations. The final
held-tree/file pass is the declared bounded return boundary; transient changes
after it are outside this function's guarantee and must be caught, if still
relevant, by the caller's later fingerprint checks.
The target root and single-file target parent are held directory descriptors.
Every created file and directory is recorded in a transaction ledger by relative
path plus device/inode. Cleanup operates through held descriptors and removes only
ledger entries whose current identity still matches. Missing, replaced or
unreadable entries are preserved and reported as residuals. Creation starts with
a pending intent record; if creation succeeds but identity lookup or ledger
finalization fails, the path remains pending and is preserved/reported as a
residual rather than treated as absent. Replacing a target path therefore cannot
redirect writes.
Directories are created under an unpredictable temporary name, opened as a held
directory FD, identified with `fstat`, entered in the ledger from that held
identity, and only then committed to the requested target name with a final
name-to-held-identity check. On macOS the commit uses
`renameatx_np(RENAME_EXCL)`, so an existing target is never replaced in the
check-to-commit window. Other platforms, or a system without that primitive,
fail closed instead of falling back to ordinary `rename`. A successful copy of
an empty source tree retains the target root. The tree target root records its
pending intent before `mkdir` and is revalidated through its held FD after
creation. Failed copies leave no usable partial project.
Cleanup also requires an exclusive private staging contract: the target/staging
root is held, owned by the current effective user, and has no group/other
permissions. On macOS, held-FD ACL inspection must show no extended ACL; an
unsupported or indeterminate ACL capability fails closed. This includes ACLs
inherited by the mutation staging root from a parent directory. The staging root
is created, opened, and identity-pinned first; owner, mode, and ACL checks finish
before any payload byte is written. Later writes use that same held root. Only a
documented `ENOENT` result means no extended ACL; `NULL/errno=0`, query errors,
and indeterminate capability fail closed. The tree target root is rechecked
through its held FD after creation. Every remaining FD is closed on exit, even
when an earlier close raises `KeyboardInterrupt` or `SystemExit`. Close failures
are attached to the existing primary exception; when the primary operation
succeeded, close failure makes the operation fail and reports output according
to final filesystem state. Descriptor-close diagnostics and object residuals are
separate: a successfully deleted object is not reported as preserved merely
because a later descriptor close failed. POSIX has no atomic compare-and-unlink
here. A same-principal writer
replacing an internal entry between identity observation and unlink is outside
the supported threat model; observed mismatches are preserved and diagnosed.
The mutation machine result exposes `residual.status`, `residual.paths`,
`fd_close_states`, `unresolved_fds`, and diagnostic-access errors, so
uncleanable objects and descriptor ownership remain queryable after the internal
exception is converted to an invalid mutation result. `UNKNOWN` is used when
object existence or ownership cannot be proven; it is never collapsed to `NONE`
or `PRESERVED`.
`UNKNOWN` takes precedence over a diagnostic path list: a path can remain useful
without proving the transaction-owned object is present. Committed snapshot and
tree output remains visible in residual reporting across later close or
asynchronous failures. After an attempted replacement, final target bytes and
mode are re-observed through the held directory FD; only confirmed old bytes
mean uncommitted, confirmed new bytes mean committed residual, and any
indeterminate result means `UNKNOWN`. Tree copy rejects source/target equality,
containment, and an existing target before creating output.

Namespace commit and durability are separate facts. Once native `os.replace`
returns successfully, the transaction namespace is `COMMITTED` even if a later
directory `fsync`, observation, or cleanup operation fails. `durability_state`
is `CONFIRMED` only after the required directory sync succeeds; otherwise it is
`NOT_ATTEMPTED` or `UNKNOWN`. `target_state` records the final observed path
contract as `ABSENT`, `UNCOMMITTED`, `COMMITTED`, `CHANGED`, or `UNKNOWN`.
A later external replacement therefore does not rewrite the fact that this
transaction's namespace operation committed. Native replace and the
transaction-state commit update execute in one SIGINT-shielded region. The
caller's local assignment is not the source of truth: if interruption occurs
before that assignment, exception handling reads the already-recorded
transaction commit fact. If the platform cannot establish the required SIGINT
mask, replacement fails closed before `os.replace()` starts. A pending
`KeyboardInterrupt` or mask-restore failure is preserved as a secondary
diagnostic while the original operation error remains primary.

Evidence staging returns success only when held-FD observation proves the exact
committed contract, including content and mode. An observed `UNCOMMITTED` or
`UNKNOWN` result fails closed. Exception translation copies transaction state,
residual paths, unknown state and close diagnostics to the publicly propagated
top-level exception rather than leaving them only on `__cause__`.
The evidence CLI exposes those diagnostics with `--json-errors`. A failure writes
one `task-control-center-error-v1` object to stderr and exits with status `2`.
`transaction_state` is restricted to `COMMITTED`, `UNCOMMITTED`, or `UNKNOWN`;
residual state is a separate `PRESENT`, `ABSENT`, or `UNKNOWN` value. The object
also carries residual paths, close diagnostics, the primary exception when one
is available, and exception notes. It additionally reports
`durability_state`, `target_state`, `fd_close_states`, `unresolved_fds`,
`signal_mask_state`, `secondary_exceptions`, context-exception information, and
diagnostic-access errors. A residual or signal-state getter failure, an invalid
signal-state type, and a cause/context getter failure all degrade to `UNKNOWN`
without removing the primary error. If normal diagnostic serialization itself
fails, the CLI emits a fixed
`UNKNOWN` fallback object and still fails; it does not print a clean result or
silently downgrade the failure.

Every native `open`/`dup`, initial dot-open, child directory open, temporary FD
and traversal FD is entered into an ownership record before SIGINT mask
restoration. Each record retains the original device/inode and a private
duplicate anchor; bare integer FD lists are not accepted as close authority.
Staging ownership is transferred through a context whose original owner remains
responsible until the caller accepts the yielded object. Snapshot and evidence
staging temporary paths are treated as pending before creation and are closed or
reported according to observed final state. Before the first native close, the
registered device/inode must still match and an advisory lock acquired through
the private anchor must also be acquirable through the current numeric FD. The
anchor itself must still be observable at the registered identity. A closed,
replaced or unobservable anchor, or failure to prove that the anchor and owner
share one open file description, retains both numeric FDs and reports
`UNKNOWN` / `REUSED_OR_REPLACED`; cleanup never closes an uncertain anchor. A
new open of the same inode does not share that open-file-description lock and is
classified `UNKNOWN` or `REUSED_OR_REPLACED` without being closed. A different
identity is also rejected before close. Lock capability failure, missing ownership records,
and observation failure become `UNKNOWN`; these states never cause a second
numeric-FD close. Registry mutation, single-FD cleanup, bulk cleanup, and anchor
transfer are serialized by a process-local re-entrant retirement lock. A failed
primary close retains the anchor and ownership record, removes the FD from the
automatic cleanup queue, and requires explicit later disposition instead of an
immediate same-number retry. Required unlock failures retain the FD and are
reported rather than ignored. Executable ownership is retired while the outcome
remains in the public exception as `fd_close_states` and `unresolved_fds`; path
cleanup success cannot erase live-resource uncertainty.
This also applies when `close()` raises `EINTR` or another `OSError`: the
exception alone does not prove closure. On POSIX the result is platform and
error dependent, so the implementation relies on post-call FD identity
observation instead of retrying the numeric descriptor unconditionally.
Close failures accumulate into `close_diagnostics` instead of replacing an
earlier diagnostic from the same cleanup path.
These mechanics do not enable mutation execution trust:
`MUTATION_EXECUTION_TRUST_SUPPORTED` remains false.

Test counts require exact integers and reject booleans. A successful result
must have zero failed, error, expected-failure and unexpected-success counts.
Skipped required control tests fail closed. A summary is rebuilt and validated
before its formal path is published. On this platform the missing custody
capability stops the writer before summary or status publication, so no later
failure can expose a false `COMPLETED` state.

Resource limits are explicit: JSON evidence is limited to 4 MiB and 64 nesting
levels, individual evidence files to 64 MiB, and a bundle to 512 declared files.
These bounds limit malformed-input resource exhaustion. Parent-path symbolic
link replacement is handled by re-resolving each relative component from the
opened root descriptor with `O_NOFOLLOW`; only the same opened file descriptor
is used for type checks, size limits, hashing and content reads. Absolute root
paths are opened component-by-component from the filesystem root, and Bundle
root device/inode identity is pinned during validation. Nested validators use
the protected root identity, and final file enumeration walks relative
directory descriptors. Every absolute-directory open path retains ownership of
all not-yet-transferred descriptors through a `BaseException` path, including
`KeyboardInterrupt` and `SystemExit`; normal return transfers only the final
directory FD to its caller. During each real open and ownership registration,
the absolute-directory helpers block SIGINT only in the calling thread and
restore that thread's previous mask only after the descriptor is registered, so
ordinary SIGINT delivery to that thread occurs after ownership exists.
`pthread_sigmask` does not alter other threads and is not a guarantee against
SIGTERM, SIGKILL, process crashes, or arbitrary `BaseException`.
Arbitrary Python callables that deliberately hide a
successfully opened descriptor and raise before returning it cannot be repaired
by this Python-level ownership model. The residual boundary
is trust in the filesystem root, process credentials, concurrent writes inside
an already trusted root, and the caller's initial root selection.
JSON decimal parsing rejects every non-finite result, including exponent
overflow such as `1e999` and `-1e999`; literal `NaN` and infinities are also
rejected before evidence objects can reach canonical serialization.

Test-result schema v3 records a discovery manifest generated by the current
blocked-test predicate. Validation independently reruns discovery and requires
the exact sorted discovered, selected and filtered ID sets, the rules-version
hash and the result manifest hash to agree. Every discovered test must appear in
exactly one of selected or filtered; filtered IDs must satisfy the current
blocked predicate; skipped IDs must be unique selected tests; and required
control tests cannot be skipped or omitted. Counts remain necessary but are not
treated as proof of discovery completeness.

The runner flattens one selected collection before execution, then records that
same collection and one terminal outcome for every test ID. Discovery,
execution and counts are derived from that collection; the evidence writer does
not perform a second discovery after execution. Missing, duplicate, extra or
unattributable IDs fail closed.

Test execution and rediscovery share one environment initializer that inserts
the same repository and `src` roots into `sys.path`. Loader-generated
`_FailedTest` records, import failures and discovery process failures are
rejected rather than represented as ordinary test IDs.

Mutation evidence is independently rebuilt from the pinned current
`tests/mutation_cases.json`. The validator re-applies the versioned blocked-test
filter, compares exact selected and filtered case IDs with the submitted cases
file, requires exactly one killed result for every selected ID, rejects unknown,
duplicate or missing IDs, and rechecks the source hash after validation. Counts
and self-declared filtered totals are not treated as coverage proof.
Each result is additionally checked against a closed execution-evidence schema.
In-process kills require a non-missing trace, executed mutated lines, a real
failing test command and mutually consistent scope flags. Aggregates are
recomputed from per-result records, after which the selected cases are executed
again from a fresh pinned project snapshot; acceptance requires the submitted
report to match that rerun exactly.

Candidate fingerprints and Git-backed candidate enumeration read candidate
bytes relative to the opened candidate-root descriptor. Git, test-discovery,
test-execution and mutation children are not launched with a path-valued
`cwd`. The parent opens and verifies the repository directory FD, passes that FD
to the child, and the child calls `fchdir()` on the FD before exec. On the
supported macOS/POSIX runtime this pins the child to the selected directory
inode; swapping and restoring the repository pathname during execution cannot
cause the child to scan a replacement repository. A platform lacking
`fchdir`/`pass_fds` fails closed.

Committed-range discovery uses NUL-delimited `git diff -z` output. It therefore
preserves Unicode, spaces and embedded newlines without Git's quoted-path
escaping, while the existing traversal, absolute-path, protected-path and
symbolic-link checks remain in force.

For content-equivalence acceptance, the gate records
`current_target_review_resolution=CONTENT_EQUIVALENT_CLEAR`; that scope is
considered satisfied by the validated content-equivalence binding rather than
listed as unverified. Direct target-head review records `DIRECT_CLEAR`.
Unresolved paths cannot coexist with accepted status.

Hash and schema validation prove internal consistency, not provenance. Bundles
are currently unsigned, so a malicious producer could construct a fully
self-consistent false bundle. Acceptance still requires a trusted evidence
source or independent human review; hashes are not a cryptographic proof of
origin.

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
