"use strict";

let I18N = null;

const UI_FALLBACK = {
  task_list_unavailable: "任务列表不可用",
  task_unavailable: "任务不可用",
  task_detail_error: "无法读取任务详情",
  read_only: "只读",
};

const state = {
  mode: "all",
  status: "",
  search: "",
  offset: 0,
  tasks: [],
  nextOffset: null,
  selectedTaskId: null,
  attentionItems: [],
  loading: false,
  detailRequestGeneration: 0,
  listRequestGeneration: 0,
  controlSession: null,
  controlSessionError: null,
  pendingControlRequests: loadPendingControlRequests(),
};

const elements = {};

document.addEventListener("DOMContentLoaded", async () => {
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
    "task-search-form",
    "task-search",
    "load-more",
    "task-detail",
    "mode-badge",
    "footer-copy",
    "task-create-panel",
    "create-task-form",
    "create-task-copy",
    "create-goal-label",
    "create-goal",
    "create-operation-label",
    "create-operation",
    "create-parameters",
    "create-task-preview",
    "create-confirm",
    "create-confirm-label",
    "create-only",
    "create-and-submit",
    "create-task-status",
  ]) {
    elements[id] = document.getElementById(id);
  }
  bindEvents();
  try {
    await loadI18n();
  } catch (error) {
    elements["global-message"].textContent =
      "中文界面文案加载失败，未显示任务数据。";
    renderTaskError(error);
    renderDetailError(null, error);
    return;
  }
  initializeCreateTaskForm();
  try {
    await loadControlSession();
  } catch (error) {
    state.controlSessionError = displayError(error);
    showGlobalError(error);
  }
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
  elements["task-search-form"].addEventListener("submit", (event) => {
    event.preventDefault();
    state.search = elements["task-search"].value.trim();
    reloadTasks().catch(showGlobalError);
  });
  elements["load-more"].addEventListener("click", () => loadTasks(false));
}

function loadPendingControlRequests() {
  try {
    const raw = sessionStorage.getItem(
      "zevorinth.pending-control-requests.v1",
    );
    const parsed = raw ? JSON.parse(raw) : {};
    const pending = new Map();
    for (const [taskId, requests] of Object.entries(parsed)) {
      if (typeof taskId !== "string" || !requests) continue;
      if (typeof requests === "string") {
        pending.set(taskId, new Map([[requests, true]]));
        continue;
      }
      const taskRequests = new Map(
        Object.entries(requests).filter(
          ([requestId]) => typeof requestId === "string",
        ),
      );
      if (taskRequests.size) pending.set(taskId, taskRequests);
    }
    return pending;
  } catch (_) {
    return new Map();
  }
}

function persistPendingControlRequests() {
  try {
    sessionStorage.setItem(
      "zevorinth.pending-control-requests.v1",
      JSON.stringify(Object.fromEntries(
        [...state.pendingControlRequests].map(
          ([taskId, requests]) => [
            taskId,
            Object.fromEntries(requests),
          ],
        ),
      )),
    );
  } catch (_) {
    return;
  }
}

function rememberPendingControlRequest(taskId, requestId, action = "unknown") {
  if (!taskId || !requestId) return;
  const requests = state.pendingControlRequests.get(taskId) || new Map();
  requests.set(requestId, action);
  state.pendingControlRequests.set(taskId, requests);
  persistPendingControlRequests();
}

function forgetPendingControlRequest(taskId, requestId) {
  if (!taskId || !requestId) return;
  const requests = state.pendingControlRequests.get(taskId);
  if (!requests) return;
  requests.delete(requestId);
  if (!requests.size) state.pendingControlRequests.delete(taskId);
  persistPendingControlRequests();
}

function pendingControlRequestCount(action = null) {
  let count = 0;
  for (const requests of state.pendingControlRequests.values()) {
    for (const pendingAction of requests.values()) {
      if (action === null || pendingAction === action) count += 1;
    }
  }
  return count;
}

function loadPendingTaskCreation() {
  try {
    return sessionStorage.getItem(
      "zevorinth.pending-task-creation.v1",
    );
  } catch (_) {
    return null;
  }
}

function rememberPendingTaskCreation(requestId) {
  try {
    sessionStorage.setItem(
      "zevorinth.pending-task-creation.v1",
      requestId,
    );
  } catch (_) {
    return;
  }
}

function forgetPendingTaskCreation() {
  try {
    sessionStorage.removeItem("zevorinth.pending-task-creation.v1");
  } catch (_) {
    return;
  }
}

async function loadI18n() {
  const response = await fetch("/static/i18n.zh-CN.json", {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`中文文案资源加载失败（HTTP ${response.status}）`);
  }
  const payload = await response.json();
  if (
    payload.language !== "zh-CN"
    || !payload.statuses
    || !payload.events
    || !payload.ui
  ) {
    throw new Error("中文文案资源结构无效");
  }
  I18N = payload;
  document.title = ui("page_title");
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = {
    Accept: "application/json",
    ...(options.headers || {}),
  };
  const request = {
    method,
    headers,
    cache: "no-store",
  };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, request);
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    payload = null;
  }
  if (!response.ok) {
    const rawError = payload && payload.error ? payload.error : {};
    const error = new Error(
      rawError.message || `请求失败（HTTP ${response.status}）`,
    );
    error.code = rawError.code || "http_error";
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function loadControlSession() {
  try {
    const session = await api("/control/api/session");
    if (
      session.contract_version === "task-control-v1"
      &&
      session.enabled === true
      && typeof session.token === "string"
      && session.token
    ) {
      state.controlSession = session;
      elements["mode-badge"].textContent = ui("controlled_actions_badge");
      elements["mode-badge"].title = ui("controlled_actions_title");
      elements["mode-badge"].classList.add("control-mode");
      elements["footer-copy"].textContent = ui("control_footer");
      elements["task-create-panel"].hidden = false;
    } else {
      const error = new Error(ui("control_session_invalid"));
      error.code = "control_session_invalid";
      throw error;
    }
  } catch (error) {
    if (error.status !== 404) throw error;
  }
}

let createTaskRequestId = loadPendingTaskCreation();
let createSubmitTaskId = null;

function initializeCreateTaskForm() {
  const goal = elements["create-goal"];
  const operation = elements["create-operation"];
  const confirmation = elements["create-confirm"];
  const createOnly = elements["create-only"];
  const createAndSubmit = elements["create-and-submit"];
  const status = elements["create-task-status"];
  let submitTracker;
  elements["create-task-copy"].textContent = ui("create_task_copy");
  elements["create-goal-label"].textContent = ui("create_goal_label");
  elements["create-operation-label"].textContent = ui("create_operation_label");
  elements["create-confirm-label"].textContent = ui("create_confirm_label");
  createOnly.textContent = ui("create_task_only");
  createAndSubmit.textContent = ui("create_task_and_submit");

  const update = () => {
    renderCreateParameters();
    const valid = Boolean(goal.value.trim()) && confirmation.checked;
    const pendingSubmit = submitTracker.pending()
      || pendingControlRequestCount("start_queued_task") > 0
      || Boolean(createTaskRequestId);
    createOnly.disabled = !valid || pendingSubmit;
    createAndSubmit.disabled = !valid
      || pendingSubmit
      || operation.value !== "write_text_artifact";
    createAndSubmit.title = operation.value === "write_text_artifact"
      ? ""
      : ui("create_run_tests_create_only");
    renderCreateTaskPreview();
  };
  submitTracker = createControlRequestTracker(
    elements["create-task-form"],
    () => createSubmitTaskId,
    update,
    "start_queued_task",
  );
  const checkCreateRequest = el("button", {
    className: "button button-quiet control-status-action",
    type: "button",
    textContent: ui("create_query_original_request"),
    hidden: !createTaskRequestId,
  });
  const discardCreateRequest = el("button", {
    className: "button button-quiet control-status-action",
    type: "button",
    textContent: ui("abandon_original_request"),
    hidden: !createTaskRequestId,
  });
  elements["create-task-form"].append(checkCreateRequest, discardCreateRequest);
  checkCreateRequest.addEventListener("click", async () => {
    if (!createTaskRequestId) return;
    checkCreateRequest.disabled = true;
    try {
      const detail = await api(
        `/api/tasks/${encodeURIComponent(createTaskRequestId)}`,
      );
      forgetPendingTaskCreation();
      createTaskRequestId = null;
      checkCreateRequest.hidden = true;
      discardCreateRequest.hidden = true;
      await loadAll();
      await openTask(detail.task.task_id, null);
    } catch (error) {
      if (error.code === "task_not_found") {
        status.textContent = ui("create_original_request_not_found");
      } else {
        status.textContent = ui("create_query_original_error", {
          message: displayError(error),
        });
      }
    } finally {
      checkCreateRequest.disabled = false;
    }
  });
  discardCreateRequest.addEventListener("click", () => {
    forgetPendingTaskCreation();
    createTaskRequestId = null;
    checkCreateRequest.hidden = true;
    discardCreateRequest.hidden = true;
    status.textContent = ui("original_request_abandoned");
    update();
  });
  goal.addEventListener("input", update);
  operation.addEventListener("change", update);
  confirmation.addEventListener("change", update);

  elements["create-task-form"].addEventListener("submit", async (event) => {
    event.preventDefault();
    if (createOnly.disabled) return;
    const submitter = event.submitter || createOnly;
    const submitMode = submitter.value === "submit" ? "submit" : "create";
    const parameters = createParameters();
    createTaskRequestId = createTaskRequestId || crypto.randomUUID();
    rememberPendingTaskCreation(createTaskRequestId);
    const selectedBeforeCreate = state.selectedTaskId;
    createOnly.disabled = true;
    createAndSubmit.disabled = true;
    status.textContent = ui("create_task_submitting");
    let created;
    try {
      created = await api("/control/api/tasks", {
        method: "POST",
        headers: {
          "X-Zevorinth-Control-Token": state.controlSession.token,
        },
        body: {
          request_id: createTaskRequestId,
          confirmed: true,
          goal: goal.value.trim(),
          operation: operation.value,
          parameters,
        },
      });
      status.textContent = ui("create_task_created", {
        task: created.task_id,
        status: labelStatus(created.task_status),
      });
      forgetPendingTaskCreation();
      createTaskRequestId = null;
      checkCreateRequest.hidden = true;
      discardCreateRequest.hidden = true;
    } catch (error) {
      status.textContent = ui("create_task_error", {
        message: displayError(error),
      });
      if (createTaskRequestId) {
        status.textContent += ` ${ui("create_task_retry_same_request")}`;
        checkCreateRequest.hidden = false;
        discardCreateRequest.hidden = false;
      }
      return;
    } finally {
      update();
    }
    if (submitMode === "submit") {
      createSubmitTaskId = created.task_id;
      await submitCreatedTask(
        created.task_id,
        status,
        submitTracker,
        update,
      );
    }
    await loadAll();
    if (state.selectedTaskId === selectedBeforeCreate) {
      await openTask(created.task_id, null);
    }
  });
  update();
}

function renderCreateParameters() {
  const operation = elements["create-operation"].value;
  const host = elements["create-parameters"];
  const values = createParameterValues();
  host.replaceChildren();
  if (operation === "write_text_artifact") {
    host.append(createInputField("artifact_path", ui("artifact_path"), values.artifact_path));
    host.append(createInputField("content", ui("artifact_content"), values.content, true));
    host.append(createInputField("supplement", ui("create_supplement"), values.supplement, true));
    host.append(createInputField("constraints", ui("create_constraints"), values.constraints, true));
    host.append(createInputField("expected_artifact", ui("create_expected_artifact"), values.expected_artifact, true));
    host.append(createInputField("acceptance", ui("create_acceptance"), values.acceptance, true));
  } else {
    host.append(createInputField("workspace", labelField("workspace"), values.workspace));
    host.append(createInputField("target", labelField("test_target"), values.target));
    host.append(el("p", {
      className: "audit-boundary",
      textContent: ui("create_run_tests_not_startable"),
    }));
  }
}

function createInputField(name, labelText, value = "", multiline = false) {
  const label = el("label", { className: "form-field" });
  label.append(el("span", { textContent: labelText }));
  const input = multiline
    ? el("textarea", { rows: "4" })
    : el("input", { type: "text" });
  input.dataset.createParameter = name;
  input.value = value;
  input.autocomplete = "off";
  input.addEventListener("input", renderCreateTaskPreview);
  label.append(input);
  return label;
}

function createParameterValues() {
  const values = {
    artifact_path: "task-output.txt",
    content: "",
    workspace: "",
    target: "",
    supplement: "",
    constraints: "",
    expected_artifact: "",
    acceptance: "",
  };
  for (const input of elements["create-parameters"].querySelectorAll(
    "[data-create-parameter]",
  )) {
    values[input.dataset.createParameter] = input.value;
  }
  return values;
}

function createParameters() {
  const operation = elements["create-operation"].value;
  const values = createParameterValues();
  if (operation === "write_text_artifact") {
    const parameters = {};
    if (values.artifact_path.trim()) {
      parameters.artifact_path = values.artifact_path.trim();
    }
    if (values.content !== "") parameters.content = values.content;
    for (const field of [
      "supplement",
      "constraints",
      "expected_artifact",
      "acceptance",
    ]) {
      if (values[field].trim()) parameters[field] = values[field].trim();
    }
    return parameters;
  }
  const parameters = { runner: "unittest" };
  if (values.workspace.trim()) parameters.workspace = values.workspace.trim();
  if (values.target.trim()) parameters.target = values.target.trim();
  return parameters;
}

function renderCreateTaskPreview() {
  const goal = elements["create-goal"].value.trim();
  const operation = elements["create-operation"].value;
  const parameters = createParameters();
  const host = elements["create-task-preview"];
  if (!goal) {
    host.textContent = ui("create_preview_empty");
    return;
  }
  host.replaceChildren(
    el("strong", { textContent: ui("create_preview_title") }),
    el("p", { textContent: `${ui("create_goal_label")}：${goal}` }),
    el("p", { textContent: `${ui("create_operation_label")}：${labelOperation(operation)}` }),
    el("pre", { textContent: JSON.stringify(parameters, null, 2) }),
    el("p", {
      className: "audit-boundary",
      textContent: ui("create_stage_boundary"),
    }),
  );
}

async function submitCreatedTask(taskId, status, tracker, onStateChange) {
  let preview;
  try {
    preview = await api(
      `/control/api/tasks/${encodeURIComponent(taskId)}/actions`,
    );
  } catch (error) {
    status.textContent = ui("create_submit_preview_error", {
      task: taskId,
      message: displayError(error),
    });
    onStateChange();
    return;
  }
  const start = (preview.actions || []).find(
    (action) => action.action === "start_queued_task",
  );
  if (!start) {
    status.textContent += ` ${ui("create_task_no_start")}`;
    return;
  }
  tracker.begin();
  const requestId = tracker.requestId();
  try {
    const result = await api(
      `/control/api/tasks/${encodeURIComponent(taskId)}/actions`,
      {
        method: "POST",
        headers: {
          "X-Zevorinth-Control-Token": state.controlSession.token,
        },
        body: {
          action: "start_queued_task",
          request_id: requestId,
          confirmed: true,
          expected_updated_at: preview.task_updated_at,
          expected_plan_version: preview.plan_version,
        },
      },
    );
    tracker.succeed(ui("create_and_submit_result", {
      status: labelStatus(result.task_status),
    }));
  } catch (error) {
    tracker.fail(error, displayError(error));
    status.textContent = ui("create_and_submit_error", {
      task: taskId,
      message: displayError(error),
    });
    if (tracker.pending()) {
      status.textContent += ` ${ui("create_and_submit_query_hint")}`;
    }
  } finally {
    onStateChange();
  }
}

async function loadAll() {
  setBusy(true);
  elements["global-message"].textContent = "";
  const results = await Promise.allSettled([
    loadTasks(true),
    loadAttention(),
  ]);
  const messages = results
    .filter((item) => item.status === "rejected")
    .map((item) => displayError(item.reason));
  if (state.controlSessionError) {
    messages.unshift(state.controlSessionError);
  }
  if (messages.length) {
    elements["global-message"].textContent = messages
      .join(ui("global_error_separator"));
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
    state.listRequestGeneration += 1;
    state.offset = 0;
    state.tasks = [];
    renderTaskLoading();
  }
  const generation = state.listRequestGeneration;
  const params = new URLSearchParams({
    limit: "40",
    offset: String(state.offset),
    mode: state.mode,
  });
  if (state.status) params.set("status", state.status);
  if (state.search) params.set("q", state.search);
  try {
    const payload = await api(`/api/tasks?${params.toString()}`);
    if (generation !== state.listRequestGeneration) return;
    state.tasks = reset ? payload.tasks : state.tasks.concat(payload.tasks);
    state.offset = payload.page.offset + payload.tasks.length;
    state.nextOffset = payload.page.next_offset;
    renderMetrics(payload.counts || {});
    renderTaskList();
  } catch (error) {
    if (reset) {
      renderTaskError(error);
    }
    throw error;
  }
}

async function loadAttention() {
  try {
    const payload = await api("/api/attention?limit=50");
    state.attentionItems = payload.items || [];
    elements["metric-attention"].textContent = String(
      payload.count ?? state.attentionItems.length,
    );
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
  elements["show-attention"].setAttribute(
    "aria-pressed",
    String(mode === "attention"),
  );
  reloadTasks().catch(showGlobalError);
}

function renderMetrics(counts) {
  const values = Object.values(counts);
  const total = values.reduce((sum, value) => sum + Number(value || 0), 0);
  elements["metric-all"].textContent = String(total);
  const active =
    Number(counts.running || 0)
    + Number(counts.verifying || 0)
    + Number(counts.queued || 0);
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

function renderTaskError(error) {
  const stateBlock = el("div", { className: "state-block" });
  stateBlock.append(el("strong", { textContent: ui("task_list_unavailable") }));
  stateBlock.append(el("span", { textContent: displayError(error) }));
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
        ? ui("empty_attention_title")
        : ui("empty_tasks_title"),
    }));
    stateBlock.append(el("span", {
      textContent: state.mode === "attention"
        ? ui("empty_attention_body")
        : ui("empty_tasks_body"),
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
      top.append(el("span", {
        className: "task-meta mono",
        textContent: labelOperation(task.operation),
      }));
      top.append(statusBadge(task.status));
      row.append(top);
      row.append(el("span", {
        className: "task-goal",
        textContent: task.goal || ui("empty_goal"),
      }));
      const meta = el("div", { className: "task-meta" });
      meta.append(el("span", { textContent: labelPhase(task.phase) }));
      meta.append(el("time", {
        className: "timestamp",
        dateTime: task.updated_at,
        textContent: shortTime(task.updated_at),
      }));
      row.append(meta);
      if (task.attention) {
        row.append(el("div", {
          className: "attention-label",
          textContent: labelAttention(task.attention),
        }));
      }
      row.addEventListener("click", () => openTask(task.task_id, row));
      elements["task-list"].append(row);
    }
  }
  elements["task-count-label"].textContent = ui(
    "shown_tasks",
    { count: state.tasks.length },
  );
  elements["load-more"].hidden = state.nextOffset === null;
}

async function openTask(taskId, sourceRow) {
  const generation = ++state.detailRequestGeneration;
  state.selectedTaskId = taskId;
  for (const row of elements["task-list"].querySelectorAll(".task-row")) {
    row.classList.toggle("is-selected", row.dataset.taskId === taskId);
  }
  if (sourceRow && window.matchMedia("(max-width: 719px)").matches) {
    elements["task-detail"].scrollIntoView({ block: "start" });
  }
  renderDetailLoading();
  try {
    let controlError = null;
    const controlRequest = state.controlSession
      ? api(`/control/api/tasks/${encodeURIComponent(taskId)}/actions`).catch(
        (error) => {
          controlError = error;
          return null;
        },
      )
      : Promise.resolve(null);
    const [detail, controlPreview] = await Promise.all([
      api(`/api/tasks/${encodeURIComponent(taskId)}`),
      controlRequest,
    ]);
    if (
      generation !== state.detailRequestGeneration
      || state.selectedTaskId !== taskId
    ) return;
    renderTaskDetail(detail, controlPreview, controlError);
  } catch (error) {
    if (
      generation !== state.detailRequestGeneration
      || state.selectedTaskId !== taskId
    ) return;
    renderDetailError(taskId, error);
  }
}

function renderDetailLoading() {
  const fragment = document.createDocumentFragment();
  const header = el("div", { className: "detail-header" });
  header.append(el("p", { className: "eyebrow", textContent: ui("loading_task") }));
  header.append(el("div", { className: "loading-line header-loading" }));
  fragment.append(header);
  const block = el("div", { className: "state-block inset-block" });
  block.append(el("strong", { textContent: ui("loading_task_body") }));
  block.append(el("span", { textContent: ui("read_only") }));
  fragment.append(block);
  elements["task-detail"].replaceChildren(fragment);
}

function renderDetailError(taskId, error) {
  const fragment = document.createDocumentFragment();
  const header = el("div", { className: "detail-header" });
  header.append(el("p", { className: "eyebrow", textContent: ui("task_unavailable") }));
  if (taskId) header.append(el("p", { className: "detail-id", textContent: taskId }));
  fragment.append(header);
  const block = el("div", { className: "state-block inset-block" });
  block.append(el("strong", { textContent: ui("task_detail_error") }));
  block.append(el("span", { textContent: displayError(error) }));
  fragment.append(block);
  elements["task-detail"].replaceChildren(fragment);
}

function renderTaskDetail(detail, controlPreview = null, controlError = null) {
  const task = detail.task;
  const audit = detail.lifecycle_audit || {};
  const preview = detail.recovery_preview || {};
  const fragment = document.createDocumentFragment();

  const header = el("header", { className: "detail-header" });
  header.append(el("a", {
    className: "button mobile-back",
    href: "#task-list-heading",
    textContent: ui("back_to_tasks"),
  }));
  const heading = el("div", { className: "detail-heading" });
  const headingText = el("div");
  headingText.append(el("p", {
    className: "eyebrow",
    textContent: labelOperation(task.operation),
  }));
  headingText.append(el("h2", { textContent: task.goal || ui("empty_goal") }));
  headingText.append(el("p", { className: "detail-id", textContent: task.task_id }));
  heading.append(headingText);
  heading.append(statusBadge(task.status));
  header.append(heading);
  fragment.append(header);

  const grid = el("div", { className: "detail-grid" });
  const facts = el("dl", { className: "fact-strip" });
  facts.append(fact(ui("phase"), labelPhase(task.phase)));
  facts.append(fact(ui("planner"), labelPlannerMode(task.planner_mode)));
  facts.append(fact(ui("execution"), labelExecutionMode(task.execution_mode)));
  facts.append(fact(ui("updated"), shortTime(task.updated_at)));
  grid.append(facts);

  grid.append(statusGuidance(task, controlPreview));
  grid.append(attentionCallout(task, audit));
  grid.append(summaryPanel(task));
  if (state.controlSession) {
    const controlHost = el("div");
    grid.append(controlHost);
    if (controlError) {
      const block = el("div", { className: "state-block" });
      block.append(el("strong", { textContent: ui("control_actions") }));
      block.append(el("span", { textContent: displayError(controlError) }));
      controlHost.replaceChildren(block);
    } else {
      renderControlActions(task.task_id, controlHost, controlPreview);
    }
  }

  const artifactAndVerification = el("section", { className: "panel" });
  artifactAndVerification.append(el("h3", { textContent: ui("result_evidence") }));
  artifactAndVerification.append(el("p", {
    className: "panel-copy",
    textContent: ui("result_evidence_copy"),
  }));
  const evidenceGrid = el("div", { className: "evidence-grid" });
  evidenceGrid.append(artifactEvidenceCard(task));
  evidenceGrid.append(verificationCard(task));
  artifactAndVerification.append(evidenceGrid);
  if (task.artifact) artifactAndVerification.append(previewControl(task));
  grid.append(artifactAndVerification);

  grid.append(recoveryPanel(preview));
  grid.append(conversationPanel(detail.events || []));
  grid.append(eventPanel(detail.events || []));
  grid.append(auditPanel(audit));

  grid.append(el("a", {
    className: "button button-secondary",
    href: "#task-list-heading",
    textContent: ui("back_to_tasks"),
  }));
  fragment.append(grid);
  elements["task-detail"].replaceChildren(fragment);
  elements["task-detail"].focus({ preventScroll: true });
}

function statusGuidance(task, controlPreview) {
  const meta = statusMeta(task.status);
  let nextStep = meta.next_step;
  const availableActions = Array.isArray(controlPreview?.actions)
    ? controlPreview.actions.map((action) => action.action)
    : [];
  const actionable = [
    "answer_clarification",
    "revise_unstarted_task",
    "start_queued_task",
    "cancel_queued_task",
  ];
  const controlActions = new Set(
    availableActions.filter((action) => actionable.includes(action)),
  );
  if (state.controlSession && controlActions.has("answer_clarification")) {
    nextStep = ui("control_clarification_next_step");
  } else if (state.controlSession && task.status === "interrupted") {
    nextStep = ui("control_interrupted_next_step");
  } else if (state.controlSession && controlActions.size > 0) {
    const labels = {
      revise_unstarted_task: ui("control_revision_title"),
      start_queued_task: ui("control_start_title"),
      cancel_queued_task: ui("control_cancel_title"),
    };
    nextStep = ui("control_available_actions_next_step", {
      actions: [...controlActions]
        .map((action) => labels[action] || ui("unknown"))
        .join(ui("list_separator")),
    });
  }
  const section = el("section", { className: "panel status-guidance" });
  section.append(el("h3", { textContent: ui("status_explanation") }));
  section.append(el("p", { textContent: meta.description }));
  section.append(el("p", {
    className: "next-step",
    textContent: `${ui("next_step")}：${nextStep}`,
  }));
  return section;
}

async function recoverStaleControlPreview(error, taskId, message) {
  const staleCodes = [
    "task_status_changed",
    "task_version_changed",
    "task_definition_changed",
    "task_clarification_changed",
    "task_revision_conflict",
    "task_claim_not_acquired",
    "task_claim_recovery_required",
    "task_cancel_not_allowed",
    "task_revision_not_allowed",
  ];
  if (!staleCodes.includes(error.code)) return false;
  elements["global-message"].textContent = message;
  await loadTasks(true);
  if (state.selectedTaskId === taskId) await openTask(taskId, null);
  return true;
}

async function refreshAfterControl(taskId) {
  await Promise.allSettled([loadTasks(true), loadAttention()]);
  if (state.selectedTaskId === taskId) await openTask(taskId, null);
}

function summaryPanel(task) {
  const section = el("section", { className: "panel" });
  section.append(el("h3", { textContent: ui("task_record") }));
  const list = el("ul", { className: "evidence-list spaced-list" });
  list.append(evidenceRow(ui("task_id"), task.task_id));
  list.append(evidenceRow(ui("attempt"), task.attempt_id));
  list.append(evidenceRow(ui("claim_generation"), String(task.claim_generation)));
  list.append(evidenceRow(ui("plan_version"), String(task.plan_version)));
  if (task.plan_revisions?.length) {
    list.append(evidenceRow(
      ui("plan_revisions"),
      ui("plan_revision_count", {
        count: task.plan_revision_count ?? task.plan_revisions.length,
        version: task.plan_version,
      }),
    ));
  } else if (task.plan_revision_count) {
    list.append(evidenceRow(
      ui("plan_revisions"),
      ui("plan_revision_count", {
        count: task.plan_revision_count,
        version: task.plan_version,
      }),
    ));
  }
  list.append(evidenceRow(ui("created"), shortTime(task.created_at)));
  list.append(evidenceRow(ui("updated"), shortTime(task.updated_at)));
  if (task.missing_fields?.length) {
    list.append(evidenceRow(
      ui("missing_fields"),
      task.missing_fields.map(labelField).join("、"),
    ));
  }
  if (task.clarification_questions?.length) {
    list.append(evidenceRow(ui("questions"), task.clarification_questions.join("；")));
  }
  if (task.error) {
    list.append(evidenceRow(
      ui("error"),
      `${labelError(task.error.code)}（${task.error.code}）`,
    ));
    list.append(evidenceRow(
      ui("backend_message"),
      task.error.message,
    ));
  }
  section.append(list);

  if (task.definition_metadata?.length) {
    const metadata = el("div", { className: "plan-summary" });
    metadata.append(el("h4", {
      textContent: ui("task_definition_metadata"),
    }));
    metadata.append(el("p", {
      className: "audit-boundary",
      textContent: ui("definition_metadata_boundary"),
    }));
    const facts = el("dl", { className: "fact-strip metadata-facts" });
    for (const item of task.definition_metadata) {
      facts.append(fact(
        ui(`definition_metadata_${item.field}`),
        item.value,
      ));
    }
    metadata.append(facts);
    section.append(metadata);
  }

  if (task.creation_preview?.classification === "clarification") {
    const preview = el("div", { className: "callout callout-info" });
    preview.append(el("strong", {
      textContent: ui("creation_clarification_preview"),
    }));
    preview.append(el("p", {
      textContent: ui("creation_clarification_fields", {
        fields: (task.creation_preview.missing_fields || [])
          .map(labelField)
          .join("、"),
      }),
    }));
    if (task.creation_preview.questions?.length) {
      preview.append(el("p", {
        textContent: task.creation_preview.questions.join("；"),
      }));
    }
    section.append(preview);
  }

  if (task.plan) {
    const plan = el("div", { className: "plan-summary" });
    plan.append(el("h4", { textContent: ui("plan_summary") }));
    const steps = el("ol", { className: "plan-steps" });
    for (const step of task.plan.steps || []) {
      const item = el("li");
      item.append(el("strong", {
        textContent: `${ui("plan_step", { id: step.step_id })}：${labelOperation(step.tool_name)}`,
      }));
      item.append(el("p", {
        textContent: `${ui("expected_result_raw")}：${step.expected_result || ui("unavailable")}`,
      }));
      steps.append(item);
    }
    plan.append(steps);
    section.append(plan);
  }
  return section;
}

function attentionCallout(task, audit) {
  const attention = task.integrity_attention || task.attention;
  const callout = el("div", {
    className: `callout${task.status === "failed" || task.status === "planning_failed" || task.integrity_attention ? " callout-danger" : ""}`,
  });
  if (attention) {
    callout.append(el("strong", { textContent: labelAttention(attention) }));
    let body = ui("review_evidence_no_action");
    if (attention.kind === "clarification") {
      body = state.controlSession
        ? ui("control_clarification_next_step")
        : ui("waiting_for_clarification");
    }
    if (attention.kind === "artifact_integrity") body = ui("preview_drift_warning");
    callout.append(el("p", { textContent: body }));
    return callout;
  }
  callout.className = "callout callout-info";
  callout.append(el("strong", {
    textContent: audit.ok
      ? "当前阶段没有需要立即处理的事项"
      : "生命周期证据尚未通过审计",
  }));
  callout.append(el("p", {
    textContent: audit.ok
      ? "生命周期审计在当前适用规则下通过；这不代表来源认证或更高安全等级。"
      : "请查看生命周期审计中的问题；工作台不会自动修复或重试。",
  }));
  return callout;
}

function artifactEvidenceCard(task) {
  const card = el("article", { className: "evidence-card" });
  card.append(el("h4", { textContent: ui("artifact") }));
  if (!task.artifact) {
    card.append(el("p", { className: "panel-copy", textContent: ui("no_artifact") }));
    return card;
  }
  const list = el("ul", { className: "evidence-list" });
  list.append(evidenceRow(ui("artifact_path"), task.artifact.relative_path));
  list.append(evidenceRow(ui("artifact_size"), `${task.artifact.byte_count} 字节`));
  list.append(evidenceRow(ui("artifact_sha"), task.artifact.sha256));
  list.append(evidenceRow(
    ui("attempt_id"),
    task.artifact.attempt_id || ui("not_recorded"),
  ));
  if (task.artifact_observation) {
    list.append(evidenceRow(
      ui("artifact_current_state"),
      labelArtifactState(task.artifact_observation.state),
    ));
    list.append(evidenceRow(
      ui("artifact_current_sha"),
      task.artifact_observation.observed_sha256 || ui("unavailable"),
    ));
  }
  card.append(list);
  return card;
}

function verificationCard(task) {
  const card = el("article", { className: "evidence-card" });
  card.append(el("h4", { textContent: ui("verification") }));
  if (!task.verification) {
    card.append(el("p", { className: "panel-copy", textContent: ui("no_verification") }));
    return card;
  }
  const artifactMatches =
    !task.artifact_observation || task.artifact_observation.matches_registration;
  const currentlyVerified = task.verification.passed && artifactMatches;
  card.append(el("span", {
    className: `status-badge ${currentlyVerified ? "status-verified" : "status-failed"}`,
    textContent: currentlyVerified
      ? ui("currently_verified")
      : ui("not_currently_verified"),
  }));
  const list = el("ul", { className: "evidence-list spaced-list" });
  list.append(evidenceRow(ui("verifier"), task.verification.verifier_name));
  list.append(evidenceRow(
    ui("verification_historical_result"),
    task.verification.passed ? ui("passed") : ui("not_passed"),
  ));
  list.append(evidenceRow(
    ui("verification_reason"),
    labelVerification(task.verification.reason),
  ));
  list.append(evidenceRow(ui("checked_at"), shortTime(task.verification.checked_at)));
  card.append(list);
  return card;
}

function previewControl(task) {
  const wrap = el("div");
  const heading = el("div", { className: "preview-heading spaced-heading" });
  heading.append(el("span", {
    className: "eyebrow",
    textContent: ui("preview_label"),
  }));
  const button = el("button", {
    className: "button",
    type: "button",
    textContent: ui("load_preview"),
  });
  heading.append(button);
  wrap.append(heading);
  const target = el("div");
  target.id = "artifact-preview-result";
  wrap.append(target);
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = ui("refreshing");
    try {
      const preview = await api(task.artifact.preview_url);
      target.replaceChildren(renderArtifactPreview(preview));
    } catch (error) {
      const block = el("div", { className: "state-block spaced-top" });
      block.append(el("strong", { textContent: ui("preview_unavailable") }));
      block.append(el("span", { textContent: displayError(error) }));
      target.replaceChildren(block);
    } finally {
      button.disabled = false;
      button.textContent = ui("reload_preview");
    }
  });
  return wrap;
}

function renderArtifactPreview(preview) {
  const frame = el("div", { className: "preview-frame" });
  const meta = el("div", { className: "preview-meta" });
  meta.append(el("span", {
    textContent: `${ui("artifact_current_state")}：${labelArtifactState(preview.state)}`,
  }));
  meta.append(el("span", {
    textContent: `${ui("artifact_size")}：${preview.observed.byte_count} 字节`,
  }));
  meta.append(el("span", {
    textContent: `SHA-256：${preview.observed.sha256}`,
  }));
  frame.append(meta);
  if (preview.state === "modified_since_registration") {
    frame.append(el("p", {
      className: "callout callout-danger",
      textContent: ui("preview_drift_warning"),
    }));
  }
  frame.append(el("pre", { textContent: preview.content }));
  return frame;
}

function recoveryPanel(preview) {
  const section = el("section", { className: "panel" });
  section.append(el("h3", { textContent: ui("recovery_preview") }));
  section.append(el("p", {
    className: "panel-copy",
    textContent: ui("recovery_copy"),
  }));
  const actions = Array.isArray(preview.actions) ? preview.actions : [];
  if (!actions.length) {
    section.append(el("div", {
      className: "state-block",
      textContent: ui("no_recovery_actions"),
    }));
    return section;
  }
  const list = el("ul", { className: "action-list" });
  for (const action of actions) {
    const card = el("li", { className: "action-card" });
    const head = el("div", { className: "action-head" });
    head.append(el("span", {
      className: "action-name",
      textContent: labelRecoveryAction(action.action),
    }));
    head.append(riskBadge(action.risk || "unknown"));
    card.append(head);
    const details = el("div", { className: "action-detail" });
    details.append(el("p", {
      textContent: recoveryActionGuidance(action.action),
    }));
    details.append(el("p", {
      textContent: `${ui("current_state")}：${labelRecoveryState(action.current_state)}`,
    }));
    details.append(el("p", {
      textContent: `${ui("target")}：${action.target || ui("unavailable")}`,
    }));
    details.append(el("p", {
      textContent: `${ui("expected")}（后端原文）：${action.expected_result || ui("unavailable")}`,
    }));
    details.append(el("p", {
      textContent: `${ui("verification_method")}（后端原文）：${action.verification || ui("unavailable")}`,
    }));
    if (Array.isArray(action.preconditions) && action.preconditions.length) {
      details.append(el("p", {
        textContent: `${ui("preconditions")}（后端原文）：${action.preconditions.join("；")}`,
      }));
    }
    card.append(details);
    list.append(card);
  }
  section.append(list);
  return section;
}

function taskNoteForm(taskId, preview) {
  const form = el("form", { className: "control-form" });
  form.append(el("h4", { textContent: ui("task_conversation") }));
  form.append(el("p", {
    className: "panel-copy",
    textContent: ui("task_note_raw_notice"),
  }));
  const field = el("label", { className: "form-field" });
  field.append(el("span", { textContent: ui("task_note_placeholder") }));
  const message = el("textarea", {
    rows: "3",
    placeholder: ui("task_note_placeholder"),
  });
  message.required = true;
  field.append(message);
  form.append(field);
  const submit = el("button", {
    className: "button button-secondary",
    type: "submit",
    textContent: ui("task_note_submit"),
  });
  form.append(submit);
  const tracker = createControlRequestTracker(
    form,
    taskId,
    () => {},
    "add_task_note",
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!message.value.trim()) return;
    submit.disabled = true;
    submit.textContent = ui("task_note_sending");
    tracker.begin();
    try {
      const result = await api(
        `/control/api/tasks/${encodeURIComponent(taskId)}/actions`,
        {
          method: "POST",
          headers: {
            "X-Zevorinth-Control-Token": state.controlSession.token,
          },
          body: {
            action: "add_task_note",
            request_id: tracker.requestId(),
            confirmed: true,
            expected_updated_at: preview.task_updated_at,
            expected_plan_version: preview.plan_version,
            message: message.value.trim(),
          },
        },
      );
      tracker.succeed(ui("task_note_success"));
      tracker.status.dataset.controlResult = result.status;
      await refreshAfterControl(taskId);
    } catch (error) {
      const message = displayError(error);
      const statusUnknown = tracker.fail(error, message);
      if (await recoverStaleControlPreview(error, taskId, message)) return;
      submit.disabled = statusUnknown;
    } finally {
      submit.textContent = ui("task_note_submit");
    }
  });
  return form;
}

function conversationPanel(events) {
  const section = el("section", { className: "panel" });
  section.append(el("h3", { textContent: ui("task_conversation") }));
  section.append(el("p", {
    className: "panel-copy",
    textContent: ui("task_conversation_copy"),
  }));
  const conversationTypes = new Set([
    "task_note_added",
    "planning_clarification",
    "clarification_required",
    "clarification_updated",
  ]);
  const notes = events.filter(
    (event) => conversationTypes.has(event.event_type),
  );
  if (!notes.length) {
    section.append(el("p", {
      className: "panel-copy",
      textContent: ui("task_notes_empty"),
    }));
    return section;
  }
  const list = el("ol", { className: "event-list" });
  for (const event of notes) {
    const item = el("li", { className: "event-item" });
    item.append(el("span", {
      className: "event-type",
      textContent: labelEvent(event.event_type),
    }));
    item.append(el("span", {
      className: "timestamp",
      textContent: shortTime(event.occurred_at),
    }));
    item.append(el("p", {
      className: "event-payload",
      textContent: event.event_type === "task_note_added"
        ? event.payload_summary.message || ui("unavailable")
        : event.event_type === "clarification_updated"
          && event.payload_summary.provided_parameters
          ? JSON.stringify(event.payload_summary.provided_parameters, null, 2)
        : summarizePayload(event.payload_summary) || ui("unavailable"),
    }));
    list.append(item);
  }
  section.append(list);
  return section;
}

function eventPanel(events) {
  const section = el("section", { className: "panel" });
  section.append(el("h3", { textContent: ui("event_timeline") }));
  section.append(el("p", {
    className: "panel-copy",
    textContent: ui("event_copy"),
  }));
  if (!events.length) {
    section.append(el("div", {
      className: "state-block",
      textContent: ui("no_events"),
    }));
    return section;
  }
  const list = el("ol", { className: "event-list" });
  for (const event of events) {
    const item = el("li", { className: "event-item" });
    item.append(el("span", {
      className: "event-sequence",
      textContent: `#${event.sequence}`,
    }));
    item.append(el("div", {}, [
      el("div", {
        className: "event-type",
        textContent: labelEvent(event.event_type),
      }),
      el("time", {
        className: "event-time",
        dateTime: event.occurred_at,
        textContent: shortTime(event.occurred_at),
      }),
    ]));
    const payloadWrap = el("div", { className: "event-payload-wrap" });
    const payloadText = summarizePayload(event.payload_summary);
    if (payloadText) {
      payloadWrap.append(el("p", {
        className: "event-payload",
        textContent: payloadText,
      }));
    }
    item.append(payloadWrap);
    list.append(item);
  }
  section.append(list);
  return section;
}

function auditPanel(audit) {
  const section = el("section", { className: "panel" });
  section.append(el("h3", { textContent: ui("lifecycle_audit") }));
  const completeness = labelCompleteness(
    audit.event_completeness?.state || "not_assessed",
  );
  const statusText = audit.ok
    ? ui("audit_passed", {
      level: audit.assurance_level || "unknown",
      state: completeness,
    })
    : ui("audit_failed", {
      issues: (audit.issues || [])
        .map((item) => labelLifecycleIssue(item.code))
        .join("；") || ui("unknown"),
    });
  section.append(el("p", {
    className: audit.ok ? "callout callout-info" : "callout callout-danger",
    textContent: statusText,
  }));
  const list = el("ul", { className: "evidence-list spaced-list" });
  list.append(evidenceRow(ui("assurance"), audit.assurance_level || ui("unknown")));
  list.append(evidenceRow(
    ui("authenticity"),
    labelAuthenticity(audit.authenticity),
  ));
  list.append(evidenceRow(
    ui("security_certification"),
    audit.security_certification ? ui("state_yes") : ui("state_no"),
  ));
  list.append(evidenceRow(ui("trust_root"), labelTrustRoot(audit.trust_root)));
  const recovery = audit.task_recovery || {};
  list.append(evidenceRow(
    ui("manual_intervention"),
    recovery.manual_intervention ? ui("required") : ui("not_required"),
  ));
  list.append(evidenceRow(
    ui("recommendation"),
    audit.status === "awaiting_clarification"
      && recovery.recommendation === "manual_review"
      ? I18N?.recommendations?.awaiting_clarification
        || ui("awaiting_clarification")
      : labelRecommendation(recovery.recommendation),
  ));
  section.append(list);
  section.append(el("p", {
    className: "audit-boundary",
    textContent: ui("audit_boundary"),
  }));
  if (!audit.ok && Array.isArray(audit.issues) && audit.issues.length) {
    const issues = el("ul", { className: "evidence-list spaced-list" });
    for (const item of audit.issues) {
      issues.append(evidenceRow(
        labelLifecycleIssue(item.code),
        item.code || ui("unknown"),
      ));
    }
    section.append(issues);
  }
  return section;
}

function renderControlActions(taskId, host, preview) {
  if (!preview) {
    const block = el("div", { className: "state-block" });
    block.append(el("strong", { textContent: ui("control_actions") }));
    block.append(el("span", { textContent: ui("control_no_actions") }));
    host.replaceChildren(block);
    return;
  }
  const section = el("section", { className: "panel control-panel" });
  section.append(el("h3", { textContent: ui("control_actions") }));
  section.append(el("p", {
    className: "panel-copy",
    textContent: ui("control_actions_copy"),
  }));
  const pendingRequests = state.pendingControlRequests.get(taskId);
  for (const [pendingRequestId, pendingAction] of pendingRequests?.entries() || []) {
    const pendingForm = el("form", {
      className: "control-form pending-control-form",
    });
    pendingForm.append(el("h4", {
      textContent: ui("control_pending_receipt_title"),
    }));
    pendingForm.append(el("p", {
      className: "panel-copy",
      textContent: ui("control_pending_receipt_copy", {
        request: pendingRequestId,
      }),
    }));
    const tracker = createControlRequestTracker(
      pendingForm,
      taskId,
      () => {},
      pendingAction || "unknown",
    );
    tracker.restore(pendingRequestId);
    section.append(pendingForm);
  }
  if (!Array.isArray(preview.actions) || !preview.actions.length) {
    const reason = preview.unavailable_reason
      ? labelError(preview.unavailable_reason)
      : ui("control_no_actions");
    section.append(el("div", {
      className: "state-block",
      textContent: ui("control_unavailable", { reason }),
    }));
    section.append(el("p", {
      className: "audit-boundary",
      textContent: ui("control_read_only_api_note"),
    }));
    host.replaceChildren(section);
    return;
  }
  for (const action of preview.actions) {
    if (action.action === "answer_clarification") {
      section.append(
        clarificationActionForm(taskId, action, preview),
      );
    } else if (action.action === "revise_unstarted_task") {
      section.append(
        revisionActionForm(taskId, action, preview),
      );
    } else if (action.action === "cancel_queued_task") {
      section.append(
        cancelActionForm(taskId, action, preview),
      );
    } else if (action.action === "start_queued_task") {
      section.append(
        startActionForm(taskId, action, preview),
      );
    } else if (action.action === "add_task_note") {
      section.append(taskNoteForm(taskId, preview));
    }
  }
  section.append(el("p", {
    className: "audit-boundary",
    textContent: ui("control_read_only_api_note"),
  }));
  host.replaceChildren(section);
}

function createControlRequestTracker(
  form,
  taskId,
  onResolved = () => {},
  pendingAction = "unknown",
) {
  const status = el("p", {
    className: "control-status",
    role: "status",
    "aria-live": "polite",
  });
  form.append(status);
  const statusAction = el("button", {
    className: "button button-quiet control-status-action",
    type: "button",
    textContent: ui("control_query_status"),
    hidden: true,
  });
  const discardAction = el("button", {
    className: "button button-quiet control-status-action",
    type: "button",
    textContent: ui("abandon_original_request"),
    hidden: true,
  });
  form.append(statusAction, discardAction);
  let pendingRequestId = null;
  const resolveTaskId = () => (
    typeof taskId === "function" ? taskId() : taskId
  );
  statusAction.addEventListener("click", async () => {
    if (!pendingRequestId) return;
    statusAction.disabled = true;
    statusAction.textContent = ui("control_querying_status");
    try {
      const receipt = await api(
        (
          `/control/api/tasks/${encodeURIComponent(resolveTaskId())}/requests/`
          + encodeURIComponent(pendingRequestId)
        ),
        {
          headers: {
            "X-Zevorinth-Control-Token": state.controlSession.token,
          },
        },
      );
      if (receipt.state === "completed") {
        const resolvedTaskId = resolveTaskId();
        forgetPendingControlRequest(resolvedTaskId, pendingRequestId);
        pendingRequestId = null;
        statusAction.hidden = true;
        discardAction.hidden = true;
        status.textContent = ui("control_request_completed");
        elements["global-message"].textContent =
          ui("control_request_completed");
        await refreshAfterControl(resolvedTaskId);
        onResolved();
      } else if (receipt.state === "failed") {
        const resolvedTaskId = resolveTaskId();
        forgetPendingControlRequest(resolvedTaskId, pendingRequestId);
        pendingRequestId = null;
        statusAction.hidden = true;
        discardAction.hidden = true;
        const code = receipt.control_error_code || ui("unknown");
        status.textContent = ui("control_request_failed", {
          code: `${labelError(code)}（${code}）`,
        });
        onResolved();
      } else if (receipt.state === "inconsistent") {
        status.textContent = ui("control_request_inconsistent", {
          status: labelStatus(receipt.task_status),
        });
      } else if (receipt.state === "in_progress") {
        status.textContent = ui("control_request_in_progress", {
          status: labelStatus(receipt.task_status),
        });
      } else {
        status.textContent = ui("control_request_unknown", {
          status: labelStatus(receipt.task_status),
        });
      }
    } catch (error) {
      status.textContent = ui("control_query_error", {
        message: displayError(error),
      });
      if (error.code === "control_request_not_found") {
        status.textContent = ui("control_request_still_pending");
      }
    } finally {
      statusAction.disabled = false;
      statusAction.textContent = ui("control_query_status");
    }
  });
  discardAction.addEventListener("click", () => {
    if (!pendingRequestId) return;
    forgetPendingControlRequest(resolveTaskId(), pendingRequestId);
    pendingRequestId = null;
    statusAction.hidden = true;
    discardAction.hidden = true;
    status.textContent = ui("original_request_abandoned");
    onResolved();
  });
  return {
    status,
    begin() {
      status.textContent = "";
      statusAction.hidden = true;
      discardAction.hidden = true;
    },
    requestId() {
      pendingRequestId = pendingRequestId || crypto.randomUUID();
      rememberPendingControlRequest(
        resolveTaskId(),
        pendingRequestId,
        pendingAction,
      );
      discardAction.hidden = false;
      return pendingRequestId;
    },
    restore(requestId) {
      pendingRequestId = requestId;
      rememberPendingControlRequest(
        resolveTaskId(),
        requestId,
        pendingAction,
      );
      status.textContent = ui("control_request_restored");
      statusAction.hidden = false;
      discardAction.hidden = false;
    },
    succeed(message) {
      forgetPendingControlRequest(resolveTaskId(), pendingRequestId);
      pendingRequestId = null;
      statusAction.hidden = true;
      discardAction.hidden = true;
      status.textContent = message;
    },
    fail(error, message) {
      status.textContent = ui("control_error", { message });
      const unknown = error.status === undefined
        || error.status >= 500
        || error.code === "action_state_unknown"
        || error.code === "action_in_progress"
        || error.code === "action_receipt_inconsistent";
      statusAction.hidden = !unknown;
      discardAction.hidden = !unknown;
      if (!unknown) {
        forgetPendingControlRequest(resolveTaskId(), pendingRequestId);
        pendingRequestId = null;
      }
      return unknown;
    },
    pending() {
      return pendingRequestId !== null;
    },
  };
}

function clarificationActionForm(taskId, action, preview) {
  const form = el("form", { className: "control-form" });
  form.append(el("h4", {
    textContent: ui("control_answer_clarification"),
  }));
  form.append(el("p", {
    className: "panel-copy",
    textContent: ui("control_answer_copy"),
  }));
  const fields = {};
  for (const field of action.fields || []) {
    const fieldWrap = el("label", { className: "form-field" });
    fieldWrap.append(el("span", { textContent: labelField(field) }));
    const input = field === "content"
      ? el("textarea", { rows: "4" })
      : el("input", { type: "text" });
    input.required = true;
    input.autocomplete = "off";
    input.dataset.fieldName = field;
    fields[field] = input;
    fieldWrap.append(input);
    form.append(fieldWrap);
  }
  const confirmation = el("label", { className: "confirm-row" });
  const checkbox = el("input", { type: "checkbox" });
  confirmation.append(checkbox);
  confirmation.append(el("span", {
    textContent: ui("control_confirm_answer"),
  }));
  form.append(confirmation);
  const submit = el("button", {
    className: "button button-primary",
    type: "submit",
    textContent: ui("control_submit_answer"),
  });
  submit.disabled = true;
  checkbox.addEventListener("change", () => {
    submit.disabled = !checkbox.checked;
  });
  form.append(submit);
  const tracker = createControlRequestTracker(
    form,
    taskId,
    () => {},
    "answer_clarification",
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    tracker.begin();
    const parameters = {};
    for (const [field, input] of Object.entries(fields)) {
      if (!input.value) {
        input.focus();
        tracker.status.textContent =
          labelError("invalid_clarification_value");
        return;
      }
      parameters[field] = input.value;
    }
    submit.disabled = true;
    submit.textContent = ui("control_submitting");
    const requestId = tracker.requestId();
    try {
      const response = await api(
        `/control/api/tasks/${encodeURIComponent(taskId)}/actions`,
        {
          method: "POST",
          headers: {
            "X-Zevorinth-Control-Token": state.controlSession.token,
          },
          body: {
            action: action.action,
            request_id: requestId,
            confirmed: true,
            expected_updated_at: preview.task_updated_at,
            expected_plan_version: preview.plan_version,
            expected_missing_fields: action.fields,
            parameters,
          },
        },
      );
      tracker.succeed(ui("control_success"));
      tracker.status.dataset.controlResult = response.status;
      await refreshAfterControl(taskId);
    } catch (error) {
      const message = error.code === "task_status_changed"
        || error.code === "task_version_changed"
        || error.code === "task_clarification_changed"
        ? ui("control_state_changed")
        : displayError(error);
      const statusUnknown = tracker.fail(error, message);
      if (await recoverStaleControlPreview(error, taskId, message)) return;
      submit.disabled = statusUnknown || !checkbox.checked;
      submit.textContent = ui("control_submit_answer");
    }
  });
  return form;
}

function revisionActionForm(taskId, action, preview) {
  const form = el("form", { className: "control-form" });
  form.append(el("h4", {
    textContent: ui("control_revision_title"),
  }));
  form.append(el("p", {
    className: "panel-copy",
    textContent: ui("control_revision_copy"),
  }));
  form.append(el("p", {
    className: "control-immutable",
    textContent: ui("control_revision_operation", {
      operation: labelOperation(action.operation),
    }),
  }));
  const goalLabel = el("label", { className: "form-field" });
  goalLabel.append(el("span", { textContent: ui("control_revision_goal") }));
  const goal = el("input", { type: "text", autocomplete: "off" });
  goal.required = true;
  goal.value = action.current_goal || "";
  goalLabel.append(goal);
  form.append(goalLabel);
  const parameterLabel = el("label", { className: "form-field" });
  parameterLabel.append(el("span", {
    textContent: ui("control_revision_parameters"),
  }));
  const parameters = el("textarea", { rows: "7", spellcheck: "false" });
  parameters.value = "{}";
  parameterLabel.append(parameters);
  form.append(parameterLabel);
  form.append(el("p", {
    className: "field-hint",
    textContent: ui("control_revision_parameters_hint"),
  }));
  const confirmation = el("label", { className: "confirm-row" });
  const checkbox = el("input", { type: "checkbox" });
  confirmation.append(checkbox);
  confirmation.append(el("span", {
    textContent: ui("control_confirm_revision"),
  }));
  form.append(confirmation);
  const submit = el("button", {
    className: "button button-primary",
    type: "submit",
    textContent: ui("control_submit_revision"),
  });
  submit.disabled = true;
  checkbox.addEventListener("change", () => {
    submit.disabled = !checkbox.checked;
  });
  form.append(submit);
  const tracker = createControlRequestTracker(
    form,
    taskId,
    () => {},
    "revise_unstarted_task",
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    tracker.begin();
    if (!goal.value) {
      goal.focus();
      tracker.status.textContent = labelError("invalid_revision_goal");
      return;
    }
    let parameterPatch;
    try {
      parameterPatch = JSON.parse(parameters.value || "{}");
    } catch (_) {
      parameters.focus();
      tracker.status.textContent = labelError("invalid_revision_parameters");
      return;
    }
    if (
      !parameterPatch
      || typeof parameterPatch !== "object"
      || Array.isArray(parameterPatch)
    ) {
      parameters.focus();
      tracker.status.textContent = labelError("invalid_revision_parameters");
      return;
    }
    const changes = {};
    if (goal.value !== action.current_goal) changes.goal = goal.value;
    if (Object.keys(parameterPatch).length) {
      changes.parameters = parameterPatch;
    }
    if (!Object.keys(changes).length) {
      tracker.status.textContent = labelError("no_task_changes");
      return;
    }
    submit.disabled = true;
    submit.textContent = ui("control_submitting");
    const requestId = tracker.requestId();
    try {
      const response = await api(
        `/control/api/tasks/${encodeURIComponent(taskId)}/actions`,
        {
          method: "POST",
          headers: {
            "X-Zevorinth-Control-Token": state.controlSession.token,
          },
          body: {
            action: action.action,
            request_id: requestId,
            confirmed: true,
            expected_updated_at: preview.task_updated_at,
            expected_plan_version: preview.plan_version,
            expected_request_digest: action.request_digest,
            changes,
          },
        },
      );
      tracker.succeed(ui("control_revision_success"));
      tracker.status.dataset.controlResult = response.status;
      await refreshAfterControl(taskId);
    } catch (error) {
      const message = [
        "task_status_changed",
        "task_version_changed",
        "task_definition_changed",
        "task_revision_conflict",
      ].includes(error.code)
        ? ui("control_state_changed")
        : displayError(error);
      const statusUnknown = tracker.fail(error, message);
      if (await recoverStaleControlPreview(error, taskId, message)) return;
      submit.disabled = statusUnknown || !checkbox.checked;
      submit.textContent = ui("control_submit_revision");
    }
  });
  return form;
}

function cancelActionForm(taskId, action, preview) {
  const form = el("form", { className: "control-form" });
  form.append(el("h4", {
    textContent: ui("control_cancel_title"),
  }));
  form.append(el("p", {
    className: "panel-copy",
    textContent: ui("control_cancel_copy"),
  }));
  const confirmation = el("label", { className: "confirm-row" });
  const checkbox = el("input", { type: "checkbox" });
  confirmation.append(checkbox);
  confirmation.append(el("span", {
    textContent: ui("control_confirm_cancel"),
  }));
  form.append(confirmation);
  const submit = el("button", {
    className: "button button-danger",
    type: "submit",
    textContent: ui("control_submit_cancel"),
  });
  submit.disabled = true;
  checkbox.addEventListener("change", () => {
    submit.disabled = !checkbox.checked;
  });
  form.append(submit);
  const tracker = createControlRequestTracker(
    form,
    taskId,
    () => {},
    "cancel_queued_task",
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    tracker.begin();
    submit.disabled = true;
    submit.textContent = ui("control_submitting");
    const requestId = tracker.requestId();
    try {
      const response = await api(
        `/control/api/tasks/${encodeURIComponent(taskId)}/actions`,
        {
          method: "POST",
          headers: {
            "X-Zevorinth-Control-Token": state.controlSession.token,
          },
          body: {
            action: action.action,
            request_id: requestId,
            confirmed: true,
            expected_updated_at: preview.task_updated_at,
            expected_plan_version: preview.plan_version,
          },
        },
      );
      tracker.succeed(ui("control_cancel_success"));
      tracker.status.dataset.controlResult = response.status;
      await refreshAfterControl(taskId);
    } catch (error) {
      const message = [
        "task_status_changed",
        "task_version_changed",
        "task_cancel_conflict",
      ].includes(error.code)
        ? ui("control_state_changed")
        : displayError(error);
      const statusUnknown = tracker.fail(error, message);
      if (await recoverStaleControlPreview(error, taskId, message)) return;
      submit.disabled = statusUnknown || !checkbox.checked;
      submit.textContent = ui("control_submit_cancel");
    }
  });
  return form;
}

function startActionForm(taskId, action, preview) {
  const form = el("form", { className: "control-form" });
  form.append(el("h4", {
    textContent: ui("control_start_title"),
  }));
  form.append(el("p", {
    className: "panel-copy",
    textContent: ui("control_start_copy"),
  }));
  const confirmation = el("label", { className: "confirm-row" });
  const checkbox = el("input", { type: "checkbox" });
  confirmation.append(checkbox);
  confirmation.append(el("span", {
    textContent: ui("control_confirm_start"),
  }));
  form.append(confirmation);
  const submit = el("button", {
    className: "button button-primary",
    type: "submit",
    textContent: ui("control_submit_start"),
  });
  submit.disabled = true;
  checkbox.addEventListener("change", () => {
    submit.disabled = !checkbox.checked;
  });
  form.append(submit);
  const tracker = createControlRequestTracker(
    form,
    taskId,
    () => {},
    "start_queued_task",
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    tracker.begin();
    submit.disabled = true;
    submit.textContent = ui("control_starting");
    const requestId = tracker.requestId();
    try {
      const response = await api(
        `/control/api/tasks/${encodeURIComponent(taskId)}/actions`,
        {
          method: "POST",
          headers: {
            "X-Zevorinth-Control-Token": state.controlSession.token,
          },
          body: {
            action: action.action,
            request_id: requestId,
            confirmed: true,
            expected_updated_at: preview.task_updated_at,
            expected_plan_version: preview.plan_version,
          },
        },
      );
      tracker.succeed(ui("control_start_success"));
      tracker.status.dataset.controlResult = response.status;
      await refreshAfterControl(taskId);
    } catch (error) {
      const message = [
        "task_status_changed",
        "task_version_changed",
        "task_start_not_allowed",
      ].includes(error.code)
        ? ui("control_state_changed")
        : displayError(error);
      const statusUnknown = tracker.fail(error, message);
      if (await recoverStaleControlPreview(error, taskId, message)) return;
      submit.disabled = statusUnknown || !checkbox.checked;
      submit.textContent = ui("control_submit_start");
    }
  });
  return form;
}

function renderEmptyDetail() {
  const fragment = document.createDocumentFragment();
  const empty = el("section", { className: "empty-detail" });
  empty.append(el("p", { className: "eyebrow", textContent: ui("task_detail") }));
  empty.append(el("h2", { textContent: ui("attention_inbox") }));
  empty.append(el("p", { textContent: ui("attention_copy") }));
  const inbox = el("div", { className: "attention-inbox" });
  if (!state.attentionItems.length) {
    const block = el("div", { className: "state-block" });
    block.append(el("strong", { textContent: ui("nothing_attention_title") }));
    block.append(el("span", { textContent: ui("nothing_attention_body") }));
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
      text.append(el("strong", {
        textContent: labelAttention(item.attention),
      }));
      let detail = ui("unknown");
      if (taskId) {
        detail = `${item.goal || ui("empty_goal")} · ${labelStatus(item.status)}`;
      } else if (item.approval_id) {
        detail = `${labelApprovalState(item.approval_status)} · ${item.approval_id}`;
      }
      text.append(el("p", { textContent: detail }));
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
    textContent: labelStatus(status),
  });
}

function riskBadge(risk) {
  return el("span", {
    className: `risk-badge risk-${risk}`,
    textContent: labelRisk(risk),
  });
}

function fact(label, value) {
  const wrap = el("div", { className: "fact" });
  wrap.append(el("dt", { textContent: label }));
  wrap.append(el("dd", { textContent: value || ui("unknown") }));
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
      const count = value.count ?? "?";
      text = value.type === "list"
        ? ui("list_summary", { count })
        : ui("object_summary", { count });
    } else {
      text = displayPayloadValue(key, value);
    }
    parts.push(`${labelPayloadKey(key)}：${text}`);
    if (parts.length >= 6) break;
  }
  return parts.join(" · ");
}

function displayPayloadValue(key, value) {
  if (typeof value === "boolean") return value ? ui("state_yes") : ui("state_no");
  if (value === null || value === undefined) {
    return key.endsWith("_error_code") ? ui("none") : ui("unknown");
  }
  if (typeof value === "string") {
    if (key === "operation" || key === "tool_name") return labelOperation(value);
    if (key === "action") return labelControlAction(value);
    if (key === "planner_mode") return labelPlannerMode(value);
    if (key === "intended_execution_mode") return labelExecutionMode(value);
    if (key === "previous_status") return labelStatus(value);
    if (key.endsWith("_status")) return labelStatus(value);
    if (key === "code") return `${labelError(value)}（${value}）`;
    if (isTimestampKey(key)) return shortTime(value);
    return value;
  }
  return String(value);
}

function labelStatus(raw) {
  const known = I18N?.statuses?.[raw];
  if (known) return known.label;
  return `${ui("unknown_status")}（${raw || ui("unknown")}）`;
}

function statusMeta(raw) {
  return I18N?.statuses?.[raw] || {
    label: `${ui("unknown_status")}（${raw || ui("unknown")}）`,
    description: "当前状态未在中文映射中登记。",
    next_step: "请保留原始状态标识并查看事件与错误详情。",
  };
}

function labelPhase(raw) {
  if (!raw) return ui("unknown_phase");
  const exact = I18N?.phases?.[raw];
  if (exact) return exact;
  for (const [prefix, template] of Object.entries(I18N?.phase_prefixes || {})) {
    if (raw.startsWith(prefix)) {
      return template.replace("{suffix}", raw.slice(prefix.length));
    }
  }
  return `${ui("unknown_phase")}（${raw}）`;
}

function labelEvent(raw) {
  const known = I18N?.events?.[raw];
  return known || `${ui("unknown_event")}（${raw || ui("unknown")}）`;
}

function labelOperation(raw) {
  if (!raw) return ui("plan_only");
  const known = I18N?.operations?.[raw];
  return known || `${ui("unknown_operation")}（${raw}）`;
}

function labelPlannerMode(raw) {
  const known = I18N?.planner_modes?.[raw];
  return known || `${ui("unknown")}（${raw || "unknown"}）`;
}

function labelExecutionMode(raw) {
  const known = I18N?.execution_modes?.[raw];
  return known || `${ui("unknown")}（${raw || "unknown"}）`;
}

function labelVerification(raw) {
  const known = I18N?.verification_reasons?.[raw];
  return known || `${ui("unknown_verification")}（${raw || ui("unknown")}）`;
}

function labelArtifactState(raw) {
  const known = I18N?.artifact_states?.[raw];
  return known || `${ui("unknown_artifact_state")}（${raw || ui("unknown")}）`;
}

function labelRisk(raw) {
  const known = I18N?.risk_levels?.[raw];
  return known || `${ui("unknown")}（${raw || "unknown"}）`;
}

function labelAttention(attention) {
  if (!attention) return ui("nothing_attention_title");
  const kind = attention.kind;
  return I18N?.attention_kinds?.[kind]
    || `${ui("unknown")}（${kind || "attention"}）`;
}

function labelRecoveryAction(raw) {
  const known = I18N?.recovery_actions?.[raw];
  return known || `${ui("unknown")}（${raw || "unknown"}）`;
}

function labelControlAction(raw) {
  const known = I18N?.control_actions?.[raw];
  return known || `${ui("unknown")}（${raw || "unknown"}）`;
}

function recoveryActionGuidance(raw) {
  if (raw === "none") {
    return "当前没有适用的自动恢复动作；只查看持久化证据即可。";
  }
  if (raw === "manual_review") {
    return "请人工复核持久化证据和事件；不要直接重试或恢复。";
  }
  if (raw === "manual_reconcile_cleanup") {
    return "请人工核对清理意图、终态事件和受控产物状态。";
  }
  return "请根据后端原文判断该恢复预览动作；工作台不能执行它。";
}

function labelCompleteness(raw) {
  const known = I18N?.completeness_states?.[raw];
  return known || `${ui("unknown_completeness")}（${raw || "unknown"}）`;
}

function labelRecommendation(raw) {
  const known = I18N?.recommendations?.[raw];
  return known || `${ui("unknown_recommendation")}（${raw || "none"}）`;
}

function labelAuthenticity(raw) {
  const known = I18N?.authenticity?.[raw];
  return known || `${ui("unknown_authenticity")}（${raw || "unknown"}）`;
}

function labelTrustRoot(raw) {
  const known = I18N?.trust_roots?.[raw];
  return known || `${ui("unknown_trust_root")}（${raw || "unknown"}）`;
}

function labelLifecycleIssue(raw) {
  const known = I18N?.lifecycle_issues?.[raw];
  return known || `${ui("unknown")}（${raw || "unknown"}）`;
}

function labelApprovalState(raw) {
  const known = I18N?.approval_states?.[raw];
  return known || `${ui("unknown")}（${raw || "unknown"}）`;
}

function labelRecoveryState(raw) {
  const known = I18N?.recovery_states?.[raw];
  if (known) return known;
  return labelStatus(raw);
}

function labelPayloadKey(raw) {
  return I18N?.payload_keys?.[raw] || ui("unknown_payload_key", { key: raw });
}

function labelField(raw) {
  const known = I18N?.fields?.[raw];
  return known
    ? `${known}（${raw}）`
    : `${ui("unknown")}（${raw || "unknown"}）`;
}

function labelError(raw) {
  return I18N?.errors?.[raw] || `${ui("unknown_error")}（${raw || "unknown"}）`;
}

function displayError(error) {
  if (!(error instanceof Error)) return String(error);
  if (error.code) {
    return `${labelError(error.code)}（${error.code}）：${error.message}`;
  }
  return error.message;
}

function isTimestampKey(key) {
  return (
    key.endsWith("_at")
    || key.endsWith("_time")
    || key === "created_at"
    || key === "updated_at"
  );
}

function shortTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function setBusy(busy) {
  state.loading = busy;
  elements["refresh-button"].disabled = busy;
  elements["refresh-button"].textContent = busy
    ? ui("refreshing")
    : ui("refresh");
}

function showGlobalError(error) {
  elements["global-message"].textContent = displayError(error);
}

function ui(key, values = {}) {
  const template = I18N?.ui?.[key] ?? UI_FALLBACK[key] ?? key;
  return Object.entries(values).reduce(
    (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
    template,
  );
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key === "textContent") node.textContent = value;
    else if (key === "className") node.className = value;
    else if (key === "type") node.setAttribute("type", value);
    else if (key === "dateTime") node.setAttribute("datetime", value);
    else node.setAttribute(key, value);
  }
  for (const child of children) node.append(child);
  return node;
}
