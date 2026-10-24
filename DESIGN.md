# Design

## Source of truth

Status: Active

Date: 2026-10-21

Product surfaces:

- Simplified Chinese Zevorinth read-only workbench;
- opt-in local control center v1 with real task creation, auditable task notes,
  confirmed clarification, unstarted-task revision, queued cancellation and
  controlled artifact start.

Evidence reviewed:

- `README.md`: current CLI/API capabilities, assurance limits, task states and recovery boundaries.
- `docs/phase-3.74-system-acceptance-baseline.md`: missing UI, lifecycle gaps and product scope.
- `docs/phase-3.90-exact-text-execution.md`: verified artifact execution and readback evidence.
- `src/zevorinth/models.py`: authoritative task, event, artifact and verification fields.
- `src/zevorinth/cli.py`: existing status, events, audit, resume and recovery commands.
- `src/zevorinth/service.py`: lifecycle audit, recovery preview and task execution boundaries.
- `src/zevorinth/workbench_static/`: existing Simplified Chinese workbench HTML,
  CSS, JavaScript and display mappings.
- `tests/test_workbench.py`: read-only behavior, workbench routing, projection,
  safety and localization regressions.
- `src/zevorinth/capabilities.py`: conditional read-only `web_ui`, fixed recovery
  and general-recovery limits.

No frontend framework or external visual dependency was present; the workbench
uses the Python standard library and vanilla browser assets.

## Brand

Personality: restrained, forensic, calm and explicit about uncertainty.

Trust signals:

- “只读”在页面上持续可见。
- Task state comes from the authoritative backend and is never inferred by the client.
- Missing, stale or unverified evidence is shown as such.
- Artifact hashes and verifier reasons use monospace and remain selectable.

Avoid:

- autonomous-agent theatre, chat bubbles as the primary interaction, sci-fi dashboards;
- celebratory success decoration before independent verification;
- hiding limitations behind friendly summaries;
- color-only status communication;
- recovery controls in v1.

## Product goals

Goals:

- Let a user discover tasks without already knowing a task ID.
- Explain what happened through authoritative status, events, verification and recovery preview data.
- Surface items requiring human attention, including stale recovery approvals.
- Preview only artifacts registered to a task.
- Provide useful evidence for deciding later lifecycle or recovery investment.
- Answer an outstanding clarification through a server-validated, explicitly
  confirmed action.
- Create a real persisted task through a server-validated, idempotent form
  without conflating creation with execution.
- Append a task-scoped note to the audit trail without treating ordinary prose
  as a lifecycle instruction.
- Revise the goal or parameter patch of a task that has not produced execution
  evidence.
- Cancel a queued task that has never acquired execution ownership.
- Start a queued deterministic artifact task through the existing execution,
  verification, and persistence path.

Non-goals:

- starting `run_tests` from the control center, retrying, cancelling after
  execution starts, resuming, recovering, overwriting or deleting tasks;
- arbitrary file browsing or arbitrary database queries;
- a second task state store;
- live model evaluation;
- multi-user authentication, remote hosting or multi-host support;
- replacing the CLI or Python API.

Success signals:

- task list, detail, timeline and attention items load from existing persisted state;
- artifact preview works without accepting file paths from the browser;
- unavailable or corrupt data degrades visibly without affecting execution;
- browser requests produce no task, event or workspace mutation;
- users can explain a task outcome from one screen.

## Personas and jobs

Primary persona: a local operator supervising a bounded Zevorinth alpha.

Jobs:

- “Show me what ran recently and what needs me.”
- “Explain why this task failed or stopped.”
- “Show the verified artifact and its evidence without searching the filesystem.”
- “Show what recovery would involve without accidentally executing it.”
- “Let me inspect evidence while the engine remains safe if the UI closes.”

Context: localhost, desktop-first, single user, occasional narrow-window use.

## Information architecture

Root route `/`:

- persistent header: product name, read-only state, refresh;
- overview counts: all tasks, needs attention, in progress, verified;
- desktop two-column layout: task index and selected task detail;
- mobile: task index and detail are stacked, with a sticky “Back to tasks” action in detail.

Task index:

- all tasks / needs attention toggle;
- status filter;
- ordered by `updated_at` descending from backend;
- list item: operation, concise goal, status, phase, updated time, attention label.

Task detail:

1. Summary: goal, status, phase, operation, timestamps, error.
2. Attention callout: whether user response is required or the task is view-only.
3. Evidence: artifact path, size, registered and observed SHA-256, verifier outcome.
4. Artifact preview: explicit “Load preview” action; no automatic content request.
5. Recovery preview: read-only actions, preconditions, risk, expected result and verification.
6. Event timeline: sequence, timestamp, event type, safe payload summary.
7. Lifecycle audit: completeness, assurance, authenticity and certification limits.
8. Control actions: only when served by the opt-in control center; action
   eligibility, field input, explicit confirmation and server result status.
9. Task creation: a real persisted queue entry with a preview of the goal,
   operation, parameters and stage boundaries; create-only and
   create-and-submit remain distinct outcomes.
10. Task conversation: task-scoped notes and clarification evidence with an
    explicit non-instruction boundary; ordinary prose never changes the task.

Attention Inbox:

- `awaiting_clarification`: requires answer;
- `interrupted`: review required;
- `blocked`: view and diagnose;
- `failed` / `planning_failed`: terminal failure, view only;
- stale or consuming recovery approvals: manual review;
- recovery preview candidates: view-only explanation in v1.

There is no separate settings or recovery-execution screen. Task creation and
submission are sections of the control center, not a second state store.

## Design principles

1. Backend is the only truth. Labels and colors reflect authoritative fields.
2. Read-only means no hidden mutations. Every route is `GET` or `HEAD`.
3. Evidence before summary. Hashes, verifier reasons and lifecycle limits remain visible.
4. Uncertainty is a first-class state. Missing data is never rendered as success.
5. Progressive disclosure. Content previews and raw event details load on demand.
6. Dense but quiet. Favor scanning and comparison over decorative cards.

Tradeoffs:

- More visible technical evidence can feel advanced; it is appropriate for the local alpha.
- A two-column layout is efficient on desktop but requires a deliberate mobile stack.
- No polling in v1 keeps load predictable; users refresh manually.

## Visual language

Concept: an evidence ledger crossed with an operations console.

Color:

- canvas: warm paper `#f4f1e8`;
- surface: `#fffdf7`;
- ink: `#1f2520`;
- muted ink: `#657069`;
- border: `#d6d1c4`;
- accent/verified: deep green `#246344`;
- attention: amber `#a55b13`;
- danger: rust `#9d2f25`;
- information: slate blue `#355f7d`.

Typography:

- system sans stack with PingFang SC, Hiragino Sans GB, Microsoft YaHei and
  Noto Sans CJK SC fallbacks for prose and controls;
- system monospace stack with CJK fallbacks for task IDs, hashes, raw identifiers
  and timestamps;
- display sizes: 32/24/18px; body 15px; metadata 12–13px;
- no externally loaded fonts.

Spacing: 4px base; common steps 4, 8, 12, 16, 24, 32.

Shape: 2px and 6px radii; thin borders; restrained shadows only for sticky surfaces.

Elevation: mostly flat; selected task and detail header may use a subtle border accent.

Motion: 120–180ms color/opacity transitions only; disable under reduced motion.

Imagery/iconography: no raster imagery; small inline symbols for status and disclosure.

## Components

- `AppShell`: header, overview metrics, responsive layout.
- `TaskFilters`: list mode toggle and status select.
- `TaskList` / `TaskRow`: loading, empty, error, selected and attention variants.
- `TaskSummary`: status, phase, operation, request goal, timestamps and error.
- `AttentionCallout`: answer-required, review-required, view-only variants.
- `EvidencePanel`: artifact and verification facts, including unverified/missing states.
- `ArtifactPreview`: request-on-demand text preview with stale/mismatch state.
- `RecoveryPreview`: read-only action cards and risk labels.
- `EventTimeline`: ordered events with safe summaries.
- `LifecycleAuditPanel`: assurance, completeness, authenticity and certification.
- `ControlActionPanel`: opt-in server-validated actions, confirmation checkbox,
  stale-state error and authoritative refresh after completion.
- `StatusBadge`, `RiskBadge`, `Metric`, `EmptyState`, `ErrorState`.

Token ownership: CSS custom properties in the workbench stylesheet. No parallel design system.

## Accessibility

- target WCAG 2.2 AA;
- semantic `header`, `nav`, `main`, `aside`, `section`, headings and lists;
- visible focus rings using a 2px accent outline;
- status labels include text, not only color;
- minimum 4.5:1 contrast for body text and 3:1 for large text/UI borders;
- all interactive elements keyboard reachable;
- `aria-live` for loading, errors and preview completion;
- timeline is an ordered list with event type before payload;
- status filter has an explicit label;
- reduced-motion media query disables transitions;
- no auto-playing motion, sound or timed update.

## Responsive behavior

Desktop ≥ 1024px:

- left task list 340–380px;
- right detail fluid up to 960px;
- sticky detail summary within the content column.

Tablet 720–1023px:

- narrower two-column layout;
- evidence facts wrap into two columns.

Mobile < 720px:

- single column;
- task list shown first;
- selecting a task scrolls/focuses the detail heading;
- “Back to tasks” link appears above detail;
- action controls remain full-width and touch targets at least 44px.

## Interaction states

- Initial load: skeleton rows and detail placeholder.
- Empty task store: explicit “No tasks recorded yet” with CLI creation hint.
- Empty attention inbox: “Nothing needs attention” without implying all history is verified.
- Store unavailable: retryable error banner; no invented state.
- Task not found: 404 view with task ID preserved.
- Missing events or malformed payload: warning badge, no success inference.
- Artifact missing/modified: preview shows mismatch and current/registered hashes.
- Disconnected request: inline error, page remains read-only and unchanged.
- Slow request: bounded loading state; no request retries in v1.
- No JavaScript: static read-only explanation and fallback text, no false data.
- Controlled action submitting: disable the button, announce progress and never
  report success until the server returns a completed action.
- Controlled action rejected: show the server error and require a fresh preview.

## Content voice

Tone: direct, specific and non-anthropomorphic.

Language: Simplified Chinese for all fixed interface copy. Raw model/user text,
backend error messages and machine identifiers remain unchanged and must be
visually identified as original backend information rather than translated.

Terminology:

- 固定术语统一使用“任务”“事件”“任务产物”“验证结果”“恢复方案预览”。
- “需要你补充信息”只用于 clarification。
- “需要人工复核”用于中断、阻止或证据不一致。
- “仅查看”用于终态失败和恢复方案预览。
- “当前已通过验证”只在持久化 verifier 通过且当前产物仍匹配登记证据时使用。
- “当前未通过验证”用于缺失、过期或不一致证据。

Microcopy rules:

- state facts before advice;
- show error codes beside friendly descriptions;
- never say “completed successfully” when only planning or tool execution succeeded;
- mention read-only and assurance limits near controls/evidence.
- known status, phase, event, verification and error codes receive Chinese display
  labels while original identifiers stay available for diagnosis;
- unknown codes show a Chinese fallback plus the raw identifier, never a guessed
  translation.

## Implementation constraints

Framework: Python standard library HTTP server plus vanilla HTML/CSS/JavaScript.

Data access:

- SQLite opened read-only with `mode=ro`;
- query-only and bounded page size;
- no task mutations, lease recovery or schema migration from workbench requests;
- artifact preview only through `task.artifact.relative_path` and existing safe reader;
- no arbitrary filesystem paths, SQL or shell input.

Control actions:

- the existing read-only `/api` surface remains `GET`/`HEAD` only;
- actions use a separate `/control/api` surface available only through the
  `control-center` command;
- action POSTs require matching loopback Origin and a per-process CSRF token;
- the backend rechecks task status, updated timestamp, plan version and missing
  fields before execution;
- unstarted-task revisions also bind to a SHA-256 digest of the task definition;
- revisions reuse the existing atomic plan-revision path and never execute a tool;
- queued cancellation checks the expected plan version atomically and races with
  revision through the same version boundary;
- supported artifact start claims only at the expected plan version and records
  request and attempt provenance in `task_claimed`;
- clarification and controlled start reserve the request ID and acquire the
  exclusive SQLite task claim in one `BEGIN IMMEDIATE` transaction;
- revisions and queued cancellation use optimistic plan versions, and plan
  revision fails closed while any execution lease is present;
- action IDs are recorded as task events for replay detection and audit;
- ambiguous submissions can be queried by task and request ID through a
  token-protected read-only receipt endpoint;
- completion and failure receipts share one fail-closed classifier; conflicting
  evidence is `inconsistent` for both replay and query;
- clarification claims bind to the previewed plan version, while historical
  revision reconciliation uses the exact request/source ID rather than a
  bounded task-list projection;
- `interrupted` tasks are not offered revision because the lifecycle state
  machine does not permit a safe transition back to queued;
- the browser refetches authoritative task state after every action;
- task creation uses a client UUIDv4 as the real task ID, persists an idempotent
  definition digest and never executes a tool; only the separate
  create-and-submit path can request the existing controlled start;
- an unresolved create-and-submit start keeps its original request ID and
  manual receipt query, and blocks a second submission until reconciliation;
- `run_tests` tasks may be created but cannot be started from the control
  center; create-and-submit is disabled for that type rather than silently
  falling back to create-only, and server-side clarification is limited to
  artifact tasks so clarification cannot bypass the start boundary;
- task notes append audit evidence only and cannot change task definition,
  plan version or lifecycle state;
- no recovery, retry, in-progress cancellation or high-risk file action is
  enabled in control-center v1; only an unstarted queued task may be cancelled.

Localization:

- fixed copy lives in HTML/JavaScript;
- dynamic display mappings live in the static `i18n.zh-CN.json` resource;
- mappings are presentation-only and do not change API values, audit results,
  recovery decisions or persisted data;
- user and backend text is rendered with `textContent`, never HTML interpolation;
- visible timestamps use `zh-CN` formatting, while raw ISO timestamps remain in
  diagnostic fields.

Network:

- loopback bind only;
- no external assets, fonts, analytics or network requests;
- restrictive CSP, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`.

Performance:

- default page size 40, maximum 100;
- event payload summaries capped;
- no polling;
- server work is proportional to requested page and selected task.

Compatibility:

- current macOS/POSIX target;
- modern evergreen browser;
- Python ≥ 3.11.

Test expectations:

- repository/projection tests;
- HTTP integration tests using a real loopback server and temporary task root;
- read-only side-effect comparison before and after page/API requests;
- path traversal and method rejection;
- responsive and accessibility review through DOM inspection and screenshot smoke check when tooling is available.

## Open questions

- [ ] Should a later read-only v1.1 expose saved visual filters in the URL? Owner: product; impact: shareability and back/forward behavior.
- [ ] Should event payloads ever be expanded to full raw JSON? Owner: security/product; impact: forensic depth versus sensitive-data exposure.
- [ ] Which desktop shell or local browser integration is preferred? Owner: product; impact: packaging, not v1 information architecture.
- [ ] Should task-list auto-refresh be opt-in? Owner: product; impact: progress visibility versus predictable read-only load.
- [ ] What retention policy applies to task goals and artifact previews? Owner: security/product; impact: sensitive-data exposure and storage.
