# Design

## Source of truth

Status: Active

Date: 2026-10-21

Product surface: Zevorinth local read-only workbench v1.

Evidence reviewed:

- `README.md`: current CLI/API capabilities, assurance limits, task states and recovery boundaries.
- `docs/phase-3.74-system-acceptance-baseline.md`: missing UI, lifecycle gaps and product scope.
- `docs/phase-3.90-exact-text-execution.md`: verified artifact execution and readback evidence.
- `src/zevorinth/models.py`: authoritative task, event, artifact and verification fields.
- `src/zevorinth/cli.py`: existing status, events, audit, resume and recovery commands.
- `src/zevorinth/service.py`: lifecycle audit, recovery preview and task execution boundaries.
- `src/zevorinth/capabilities.py`: `web_ui=not_implemented`, fixed recovery and general-recovery limits.

No existing frontend framework, visual system, screenshots or design tokens were present.

## Brand

Personality: restrained, forensic, calm and explicit about uncertainty.

Trust signals:

- “Read-only” is visible on every screen.
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

Non-goals:

- submitting, retrying, cancelling, resuming, recovering, overwriting or deleting tasks;
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

Attention Inbox:

- `awaiting_clarification`: requires answer;
- `interrupted`: review required;
- `blocked`: view and diagnose;
- `failed` / `planning_failed`: terminal failure, view only;
- stale or consuming recovery approvals: manual review;
- recovery preview candidates: view-only explanation in v1.

There is no separate settings, recovery-execution or task-submission screen.

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

- system sans stack for prose and controls;
- system monospace stack for task IDs, hashes, event types and timestamps;
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

## Content voice

Tone: direct, specific and non-anthropomorphic.

Terminology:

- “Task”, “event”, “artifact”, “verification”, “recovery preview”.
- “Needs your answer” only for clarification.
- “Review required” for interrupted, blocked or inconsistent evidence.
- “View only” for terminal failures and recovery previews.
- “Verified” only when the persisted verifier says passed and lifecycle audit supports it.
- “Unverified” for missing, stale or inconsistent evidence.

Microcopy rules:

- state facts before advice;
- show error codes beside friendly descriptions;
- never say “completed successfully” when only planning or tool execution succeeded;
- mention read-only and assurance limits near controls/evidence.

## Implementation constraints

Framework: Python standard library HTTP server plus vanilla HTML/CSS/JavaScript.

Data access:

- SQLite opened read-only with `mode=ro`;
- query-only and bounded page size;
- no task mutations, lease recovery or schema migration from workbench requests;
- artifact preview only through `task.artifact.relative_path` and existing safe reader;
- no arbitrary filesystem paths, SQL or shell input.

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
