"use strict";

const state = {
  mode: "all",
  status: "",
  offset: 0,
  tasks: [],
  nextOffset: null,
  selectedTaskId: null,
  attentionItems: [],
  loading: false,
};

const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  for (const id of [
    "refresh-button",
    "global-message",
    "metric-all",
    "metric-attention",
    "metric-progress",
    "metric-verified",
    "show-all",
    "show-attention",
    "status-filter",
    "task-count-label",
    "task-list",
    "load-more",
    "task-detail",
  ]) {
    elements[id] = document.getElementById(id);
  }
  bindEvents();
  loadAll();
});

function bindEvents() {
  elements["refresh-button"].addEventListener("click", loadAll);
  elements["show-all"].addEventListener("click", () => setMode("all"));
  elements["show-attention"].addEventListener("click", () => setMode("attention"));
  elements["status-filter"].addEventListener("change", () => {
    state.status = elements["status-filter"].value;
    reloadTasks();
  });
  elements["load-more"].addEventListener("click", () => loadTasks(false));
}

async function api(path) {
  const response = await fetch(path, {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    payload = null;
  }
  if (!response.ok) {
    const error = payload && payload.error ? payload.error : {};
    throw new Error(error.message || `Request failed (${response.status})`);
  }
  return payload;
}

async function loadAll() {
  setBusy(true);
  elements["global-message"].textContent = "";
  const results = await Promise.allSettled([
    loadTasks(true),
    loadAttention(),
  ]);
  const failures = results.filter((item) => item.status === "rejected");
  if (failures.length) {
    const messages = failures.map((item) => item.reason.message);
    elements["global-message"].textContent = messages.join(" · ");
  }
  setBusy(false);
}

async function reloadTasks() {
  state.offset = 0;
  state.tasks = [];
  state.nextOffset = null;
  await loadTasks(true);
}

async function loadTasks(reset) {
  if (reset) {
    state.offset = 0;
    state.tasks = [];
    renderTaskLoading();
  }
  const params = new URLSearchParams({
    limit: "40",
    offset: String(state.offset),
    mode: state.mode,
  });
  if (state.status) params.set("status", state.status);
  try {
    const payload = await api(`/api/tasks?${params.toString()}`);
    state.tasks = reset ? payload.tasks : state.tasks.concat(payload.tasks);
    state.offset = payload.page.offset + payload.tasks.length;
    state.nextOffset = payload.page.next_offset;
    renderMetrics(payload.counts || {});
    renderTaskList();
  } catch (error) {
    if (reset) {
      renderTaskError(error.message);
    }
    throw error;
  }
}

async function loadAttention() {
  try {
    const payload = await api("/api/attention?limit=50");
    state.attentionItems = payload.items || [];
    elements["metric-attention"].textContent = String(payload.count ?? state.attentionItems.length);
    if (!state.selectedTaskId) renderEmptyDetail();
  } catch (error) {
    elements["metric-attention"].textContent = "?";
    throw error;
  }
}

function setMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  state.status = "";
  elements["status-filter"].value = "";
  elements["show-all"].classList.toggle("is-active", mode === "all");
  elements["show-attention"].classList.toggle("is-active", mode === "attention");
  elements["show-all"].setAttribute("aria-pressed", String(mode === "all"));
  elements["show-attention"].setAttribute("aria-pressed", String(mode === "attention"));
  reloadTasks().catch(showGlobalError);
}

function renderMetrics(counts) {
  const values = Object.values(counts);
  const total = values.reduce((sum, value) => sum + Number(value || 0), 0);
  elements["metric-all"].textContent = String(total);
  const active = Number(counts.running || 0) + Number(counts.verifying || 0) + Number(counts.queued || 0);
  elements["metric-progress"].textContent = String(active);
  elements["metric-verified"].textContent = String(counts.succeeded || 0);
}

function renderTaskLoading() {
  elements["task-list"].replaceChildren();
  for (let index = 0; index < 4; index += 1) {
    const card = el("div", { className: "loading-card" });
    card.append(el("div", { className: "loading-line" }));
    card.append(el("div", { className: "loading-line short-line" }));
    elements["task-list"].append(card);
  }
  elements["load-more"].hidden = true;
}

function renderTaskError(message) {
  const stateBlock = el("div", { className: "state-block" });
  stateBlock.append(el("strong", { textContent: "Task list unavailable" }));
  stateBlock.append(el("span", { textContent: message }));
  elements["task-list"].replaceChildren(stateBlock);
  elements["task-count-label"].textContent = "";
  elements["load-more"].hidden = true;
}

function renderTaskList() {
  elements["task-list"].replaceChildren();
  if (!state.tasks.length) {
    const stateBlock = el("div", { className: "state-block" });
    stateBlock.append(el("strong", {
      textContent: state.mode === "attention"
        ? "Nothing needs attention"
        : "No tasks recorded",
    }));
    stateBlock.append(el("span", {
      textContent: state.mode === "attention"
        ? "No persisted task currently matches the attention statuses."
        : "Create a task through the CLI, then refresh this workbench.",
    }));
    elements["task-list"].append(stateBlock);
  } else {
    for (const task of state.tasks) {
      const row = el("button", {
        className: `task-row${task.task_id === state.selectedTaskId ? " is-selected" : ""}`,
        type: "button",
      });
      row.dataset.taskId = task.task_id;
      const top = el("div", { className: "task-row-top" });
      top.append(el("span", { className: "task-meta mono", textContent: task.operation || "planning" }));
      top.append(statusBadge(task.status));
      row.append(top);
      row.append(el("span", { className: "task-goal", textContent: task.goal || "(goal unavailable)" }));
      const meta = el("div", { className: "task-meta" });
      meta.append(el("span", { textContent: task.phase || "unknown phase" }));
      meta.append(el("time", {
        className: "timestamp",
        dateTime: task.updated_at,
        textContent: shortTime(task.updated_at),
      }));
      row.append(meta);
      if (task.attention) {
        row.append(el("div", { className: "attention-label", textContent: task.attention.label }));
      }
      row.addEventListener("click", () => openTask(task.task_id, row));
      elements["task-list"].append(row);
    }
  }
  elements["task-count-label"].textContent = `${state.tasks.length} shown`;
  elements["load-more"].hidden = state.nextOffset === null;
}

async function openTask(taskId, sourceRow) {
  state.selectedTaskId = taskId;
  for (const row of elements["task-list"].querySelectorAll(".task-row")) {
    row.classList.toggle("is-selected", row.dataset.taskId === taskId);
  }
  if (sourceRow && window.matchMedia("(max-width: 719px)").matches) {
    elements["task-detail"].scrollIntoView({ block: "start" });
  }
  renderDetailLoading();
  try {
    const detail = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    renderTaskDetail(detail);
  } catch (error) {
    renderDetailError(taskId, error.message);
  }
}

function renderDetailLoading() {
  const fragment = document.createDocumentFragment();
  const header = el("div", { className: "detail-header" });
  header.append(el("p", { className: "eyebrow", textContent: "Loading task" }));
  header.append(el("div", { className: "loading-line header-loading" }));
  fragment.append(header);
  const block = el("div", { className: "state-block inset-block" });
  block.append(el("strong", { textContent: "Reading persisted evidence" }));
  block.append(el("span", { textContent: "This request is read-only." }));
  fragment.append(block);
  elements["task-detail"].replaceChildren(fragment);
}

function renderDetailError(taskId, message) {
  const fragment = document.createDocumentFragment();
  const header = el("div", { className: "detail-header" });
  header.append(el("p", { className: "eyebrow", textContent: "Task unavailable" }));
  header.append(el("p", { className: "detail-id", textContent: taskId }));
  fragment.append(header);
  const block = el("div", { className: "state-block inset-block" });
  block.append(el("strong", { textContent: "Could not read task detail" }));
  block.append(el("span", { textContent: message }));
  fragment.append(block);
  elements["task-detail"].replaceChildren(fragment);
}

function renderTaskDetail(detail) {
  const task = detail.task;
  const audit = detail.lifecycle_audit || {};
  const preview = detail.recovery_preview || {};
  const fragment = document.createDocumentFragment();

  const header = el("header", { className: "detail-header" });
  header.append(el("a", {
    className: "button mobile-back",
    href: "#task-list-heading",
    textContent: "Back to tasks",
  }));
  const heading = el("div", { className: "detail-heading" });
  const headingText = el("div");
  headingText.append(el("p", { className: "eyebrow", textContent: task.operation || "Plan only" }));
  headingText.append(el("h2", { textContent: task.goal || "(goal unavailable)" }));
  headingText.append(el("p", { className: "detail-id", textContent: task.task_id }));
  heading.append(headingText);
  heading.append(statusBadge(task.status));
  header.append(heading);
  fragment.append(header);

  const grid = el("div", { className: "detail-grid" });
  const facts = el("dl", { className: "fact-strip" });
  facts.append(fact("Phase", task.phase || "unknown"));
  facts.append(fact("Planner", task.planner_mode || "unknown"));
  facts.append(fact("Execution", task.execution_mode || "unknown"));
  facts.append(fact("Updated", shortTime(task.updated_at)));
  grid.append(facts);

  grid.append(attentionCallout(task, audit));
  grid.append(summaryPanel(task));

  const artifactAndVerification = el("section", { className: "panel" });
  artifactAndVerification.append(el("h3", { textContent: "Result and evidence" }));
  artifactAndVerification.append(el("p", {
    className: "panel-copy",
    textContent: "Registered evidence is shown separately from current file observations. Loading a preview never changes the artifact.",
  }));
  const evidenceGrid = el("div", { className: "evidence-grid" });
  evidenceGrid.append(artifactEvidenceCard(task));
  evidenceGrid.append(verificationCard(task));
  artifactAndVerification.append(evidenceGrid);
  if (task.artifact) {
    artifactAndVerification.append(previewControl(task));
  }
  grid.append(artifactAndVerification);

  grid.append(recoveryPanel(preview));
  grid.append(eventPanel(detail.events || []));
  grid.append(auditPanel(audit));

  const back = el("a", { className: "button button-secondary", href: "#task-list-heading", textContent: "Back to tasks" });
  grid.append(back);
  fragment.append(grid);
  elements["task-detail"].replaceChildren(fragment);
  elements["task-detail"].focus({ preventScroll: true });
}

function summaryPanel(task) {
  const section = el("section", { className: "panel" });
  section.append(el("h3", { textContent: "Task record" }));
  const list = el("ul", { className: "evidence-list spaced-list" });
  list.append(evidenceRow("Task ID", task.task_id));
  list.append(evidenceRow("Attempt", task.attempt_id));
  list.append(evidenceRow("Claim generation", String(task.claim_generation)));
  list.append(evidenceRow("Created", task.created_at));
  list.append(evidenceRow("Updated", task.updated_at));
  if (task.missing_fields && task.missing_fields.length) {
    list.append(evidenceRow("Missing fields", task.missing_fields.join(", ")));
  }
  if (task.clarification_questions && task.clarification_questions.length) {
    list.append(evidenceRow("Questions", task.clarification_questions.join(" · ")));
  }
  if (task.error) {
    list.append(evidenceRow("Error", `${task.error.code}: ${task.error.message}`));
  }
  section.append(list);
  return section;
}

function attentionCallout(task, audit) {
  const callout = el("div", {
    className: `callout${task.status === "failed" || task.status === "planning_failed" || task.integrity_attention ? " callout-danger" : ""}`,
  });
  const attention = task.integrity_attention || task.attention;
  if (attention) {
    callout.append(el("strong", { textContent: attention.label }));
    callout.append(el("p", {
      textContent: attention.requires_user_answer
        ? "This task is waiting for user input. This workbench is read-only, so answer through the existing conversation or CLI workflow."
        : attention.kind === "artifact_integrity"
          ? "The persisted verification remains historical evidence, but the artifact no longer matches its registered SHA-256 or size."
          : "Review the persisted evidence below. This workbench cannot resume, retry or recover the task.",
    }));
  } else {
    callout.className = "callout callout-info";
    callout.append(el("strong", {
      textContent: audit.ok ? "No immediate attention required" : "Lifecycle evidence is unverified",
    }));
    callout.append(el("p", {
      textContent: audit.ok
        ? "The persisted lifecycle audit is internally consistent. This is not source authentication."
        : "The lifecycle audit does not currently support a fully verified conclusion. Review the evidence below.",
    }));
  }
  return callout;
}

function artifactEvidenceCard(task) {
  const card = el("article", { className: "evidence-card" });
  card.append(el("h4", { textContent: "Artifact" }));
  if (!task.artifact) {
    card.append(el("p", { className: "panel-copy", textContent: "No artifact is registered for this task." }));
    return card;
  }
  const list = el("ul", { className: "evidence-list" });
  list.append(evidenceRow("Path", task.artifact.relative_path));
  list.append(evidenceRow("Size", `${task.artifact.byte_count} bytes`));
  list.append(evidenceRow("SHA-256", task.artifact.sha256));
  list.append(evidenceRow("Attempt", task.artifact.attempt_id || "not recorded"));
  if (task.artifact_observation) {
    list.append(evidenceRow("Current", task.artifact_observation.state));
    list.append(evidenceRow(
      "Current SHA-256",
      task.artifact_observation.observed_sha256 || "unavailable",
    ));
  }
  card.append(list);
  return card;
}

function verificationCard(task) {
  const card = el("article", { className: "evidence-card" });
  card.append(el("h4", { textContent: "Independent verification" }));
  if (!task.verification) {
    card.append(el("p", { className: "panel-copy", textContent: "No verification record exists." }));
    return card;
  }
  const artifactMatches = !task.artifact_observation
    || task.artifact_observation.matches_registration;
  const currentlyVerified = task.verification.passed && artifactMatches;
  const badge = el("span", {
    className: `status-badge ${currentlyVerified ? "status-verified" : "status-failed"}`,
    textContent: currentlyVerified ? "currently verified" : "not currently verified",
  });
  card.append(badge);
  const list = el("ul", { className: "evidence-list spaced-list" });
  list.append(evidenceRow("Verifier", task.verification.verifier_name));
  list.append(evidenceRow("Reason", task.verification.reason));
  list.append(evidenceRow("Checked", task.verification.checked_at));
  card.append(list);
  return card;
}

function previewControl(task) {
  const wrap = el("div");
  const heading = el("div", { className: "preview-heading spaced-heading" });
  heading.append(el("span", { className: "eyebrow", textContent: "Controlled artifact preview" }));
  const button = el("button", { className: "button", type: "button", textContent: "Load preview" });
  heading.append(button);
  wrap.append(heading);
  const target = el("div");
  target.id = "artifact-preview-result";
  wrap.append(target);
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "Loading…";
    try {
      const preview = await api(task.artifact.preview_url);
      target.replaceChildren(renderArtifactPreview(preview));
    } catch (error) {
      const block = el("div", { className: "state-block spaced-top" });
      block.append(el("strong", { textContent: "Preview unavailable" }));
      block.append(el("span", { textContent: error.message }));
      target.replaceChildren(block);
    } finally {
      button.disabled = false;
      button.textContent = "Reload preview";
    }
  });
  return wrap;
}

function renderArtifactPreview(preview) {
  const frame = el("div", { className: "preview-frame" });
  const meta = el("div", { className: "preview-meta" });
  meta.append(el("span", { textContent: `state=${preview.state}` }));
  meta.append(el("span", { textContent: `bytes=${preview.observed.byte_count}` }));
  meta.append(el("span", { textContent: `sha256=${preview.observed.sha256}` }));
  frame.append(meta);
  if (preview.state === "modified_since_registration") {
    const warning = el("p", {
      className: "callout callout-danger",
      textContent: "Current bytes no longer match the registered artifact evidence. This file must not be treated as verified.",
    });
    frame.append(warning);
  }
  frame.append(el("pre", { textContent: preview.content }));
  return frame;
}

function recoveryPanel(preview) {
  const section = el("section", { className: "panel" });
  section.append(el("h3", { textContent: "Recovery preview" }));
  section.append(el("p", {
    className: "panel-copy",
    textContent: "Read-only preview. Actions are shown for review only and cannot be executed from this workbench.",
  }));
  const actions = Array.isArray(preview.actions) ? preview.actions : [];
  if (!actions.length) {
    section.append(el("div", { className: "state-block", textContent: "No recovery actions are proposed." }));
    return section;
  }
  const list = el("ul", { className: "action-list" });
  for (const action of actions) {
    const card = el("li", { className: "action-card" });
    const head = el("div", { className: "action-head" });
    head.append(el("span", { className: "action-name", textContent: action.action }));
    head.append(riskBadge(action.risk || "unknown"));
    card.append(head);
    const details = el("div", { className: "action-detail" });
    details.append(el("p", { textContent: action.operation || "" }));
    details.append(el("p", { textContent: `Target: ${action.target || "unknown"}` }));
    details.append(el("p", { textContent: `Expected: ${action.expected_result || "not specified"}` }));
    details.append(el("p", { textContent: `Verification: ${action.verification || "not specified"}` }));
    if (Array.isArray(action.preconditions) && action.preconditions.length) {
      details.append(el("p", { textContent: `Preconditions: ${action.preconditions.join(" · ")}` }));
    }
    card.append(details);
    list.append(card);
  }
  section.append(list);
  return section;
}

function eventPanel(events) {
  const section = el("section", { className: "panel" });
  section.append(el("h3", { textContent: "Event timeline" }));
  section.append(el("p", {
    className: "panel-copy",
    textContent: "Order follows persisted event sequence. Missing events are not inferred to mean success.",
  }));
  if (!events.length) {
    section.append(el("div", { className: "state-block", textContent: "No events were returned for this task." }));
    return section;
  }
  const list = el("ol", { className: "event-list" });
  for (const event of events) {
    const item = el("li", { className: "event-item" });
    item.append(el("span", { className: "event-sequence", textContent: `#${event.sequence}` }));
    item.append(el("div", {}, [
      el("div", { className: "event-type", textContent: event.event_type }),
      el("time", { className: "event-time", dateTime: event.occurred_at, textContent: shortTime(event.occurred_at) }),
    ]));
    const payloadWrap = el("div", { className: "event-payload-wrap" });
    const payloadText = summarizePayload(event.payload_summary);
    if (payloadText) payloadWrap.append(el("p", { className: "event-payload", textContent: payloadText }));
    item.append(payloadWrap);
    list.append(item);
  }
  section.append(list);
  return section;
}

function auditPanel(audit) {
  const section = el("section", { className: "panel" });
  section.append(el("h3", { textContent: "Lifecycle audit" }));
  const status = el("p", {
    className: audit.ok ? "callout callout-info" : "callout callout-danger",
    textContent: audit.ok
      ? `Audit ${audit.assurance_level || "unknown"}: ${audit.event_completeness?.state || "unknown completeness"}`
      : `Audit failed closed: ${(audit.issues || []).map((issue) => issue.code || issue).join(", ") || "unverified"}`,
  });
  section.append(status);
  const list = el("ul", { className: "evidence-list spaced-list" });
  list.append(evidenceRow("Assurance", audit.assurance_level || "unknown"));
  list.append(evidenceRow("Authenticity", audit.authenticity || "not_authenticated"));
  list.append(evidenceRow("Security certification", String(audit.security_certification ?? false)));
  list.append(evidenceRow("Trust root", audit.trust_root || "none"));
  section.append(list);
  return section;
}

function renderEmptyDetail() {
  const fragment = document.createDocumentFragment();
  const empty = el("section", { className: "empty-detail" });
  empty.append(el("p", { className: "eyebrow", textContent: "Task detail" }));
  empty.append(el("h2", { textContent: "Attention inbox" }));
  empty.append(el("p", {
    textContent: "Items below are derived from authoritative task status and approval records. None of them can be changed from this read-only view.",
  }));
  const inbox = el("div", { className: "attention-inbox" });
  if (!state.attentionItems.length) {
    const block = el("div", { className: "state-block" });
    block.append(el("strong", { textContent: "Nothing needs attention" }));
    block.append(el("span", { textContent: "This does not certify that every task is successful." }));
    inbox.append(block);
  } else {
    const list = el("ul", { className: "attention-list" });
    for (const item of state.attentionItems) {
      const taskId = item.task_id;
      const row = el(taskId ? "button" : "div", {
        className: "attention-item",
        type: taskId ? "button" : undefined,
      });
      const text = el("div");
      text.append(el("strong", { textContent: item.attention?.label || "Review required" }));
      text.append(el("p", {
        textContent: taskId
          ? `${item.goal || "Task"} · ${item.status || "unknown status"}`
          : `Approval ${item.approval_id || "unavailable"}`,
      }));
      row.append(text);
      row.append(shortTime(item.updated_at || ""));
      if (taskId) row.addEventListener("click", () => openTask(taskId, null));
      list.append(row);
    }
    inbox.append(list);
  }
  empty.append(inbox);
  fragment.append(empty);
  elements["task-detail"].replaceChildren(fragment);
}

function statusBadge(status) {
  const safeStatus = String(status || "unknown").replace(/[^a-z0-9_-]/gi, "");
  return el("span", {
    className: `status-badge status-${safeStatus}`,
    textContent: status || "unknown",
  });
}

function riskBadge(risk) {
  return el("span", {
    className: `risk-badge risk-${risk}`,
    textContent: `${risk} risk`,
  });
}

function fact(label, value) {
  const wrap = el("div", { className: "fact" });
  wrap.append(el("dt", { textContent: label }));
  wrap.append(el("dd", { textContent: value || "unknown" }));
  return wrap;
}

function evidenceRow(label, value) {
  const row = el("li");
  row.append(el("span", { textContent: label }));
  row.append(el("span", { textContent: value || "—" }));
  return row;
}

function summarizePayload(payload) {
  if (!payload || typeof payload !== "object") return "";
  const parts = [];
  for (const [key, value] of Object.entries(payload)) {
    let text;
    if (value && typeof value === "object") {
      text = `${value.type || "object"}(${value.count ?? "?"})`;
    } else {
      text = String(value);
    }
    parts.push(`${key}=${text}`);
    if (parts.length >= 6) break;
  }
  return parts.join(" · ");
}

function shortTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function setBusy(busy) {
  state.loading = busy;
  elements["refresh-button"].disabled = busy;
  if (busy) elements["refresh-button"].textContent = "Refreshing…";
  else elements["refresh-button"].textContent = "Refresh";
}

function showGlobalError(error) {
  elements["global-message"].textContent = error.message || String(error);
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key === "textContent") node.textContent = value;
    else if (key === "className") node.className = value;
    else if (key === "style") node.setAttribute("style", value);
    else if (key === "type") node.setAttribute("type", value);
    else if (key === "dateTime") node.setAttribute("datetime", value);
    else node.setAttribute(key, value);
  }
  for (const child of children) node.append(child);
  return node;
}
