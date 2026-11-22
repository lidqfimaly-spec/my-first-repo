# P0 · Zevorinth 工程真实现状审查

`[P0_REALITY_AUDIT · 2026-11-22 · 只读审查]`

> 本文件只记录**实际读到、实际跑到**的东西。凡未验证的一律标 `UNKNOWN` / `NOT_TESTABLE`。
> 证据优先级：实跑 > 事件与产物 > 源码调用路径 > 自动化测试 > 接口 > 设计文档。

---

## 0. 审查范围与访问确认

| 项 | 实际值 |
| --- | --- |
| 项目根 | `/Users/lidequan/PythonProject/Zevorinth`（`pwd` 实测） |
| 版本 | `zevorinth 0.3.0`（`pyproject.toml:8`） |
| 首次进入时间 | 2026-11-22 01:5x（本轮） |
| 文件总数（排除 `.idea/`、`.git/`、`.venv/`、`__pycache__`） | 1 227 |
| 被禁目录 | `.idea/` —— 全程未读取、未枚举、未哈希 |
| 是否只读 | 是。项目内**唯一新增** = `docs/product-research/p0-baseline/**`；未改任何源码、配置、数据、冻结证据 |
| 运行时数据落点 | 全部隔离在 `/tmp/zv-audit-home`（HTTP 服务 `--home`）与 `/tmp/zv-audit-eval`（引擎直测） |

**没有独立的 "zh" 项目。** 本机 `~/PythonProject` 下其他目录未被当作 Zevorinth 审查对象。

---

## 1. 技术栈（实测）

| 层 | 实际实现 | 证据 |
| --- | --- | --- |
| 语言/运行时 | Python ≥3.11（本机 `.venv` 3.12/3.13 均可跑） | `pyproject.toml:9` |
| 运行时依赖 | **零**（`dependencies = []`） | `pyproject.toml:11` |
| 可选依赖 | `openai>=3.35.1,<4`（仅 `[llm]` 额外组） | `pyproject.toml:14` |
| HTTP 服务 | 标准库 `http.server.ThreadingHTTPServer`，手写正则路由 | `workbench_server.py:12,120-121` |
| 持久化 | SQLite（单文件 `data/zevorinth.sqlite3`，WAL） | `event_store.py` |
| 前端 | 原生 HTML/CSS/JS，无构建、无框架、无 npm | `workbench_static/` 4 个文件 |
| 部署 | 单机 loopback（`127.0.0.1` 硬约束） | `workbench_server.py:47,1604-1614` |

`FACT`：这是一个**零第三方运行时依赖**的单机 Python 应用。优点是可移植、可审计；代价是所有能力都得自己写（含路由、模板、状态机、锁）。

---

## 2. 模块结构与真实分工

`src/zevorinth/` 共 **57 个 `.py` 顶层模块** + 2 个子包（`evals/`、`workbench_static/`，后者 4 个前端文件），
按下表职能分工。下表只列与产品/引擎路径相关的模块，**不是全量清单**；未列出的
`planner_*`（7 个精度评测模块）、`*_evaluation.py`、`proposal_candidates.py`、`workspace_snapshot.py` 等
多为离线评测或未接入路径的模块。

| 职能 | 模块（行数级） | 是否在产品路径上 |
| --- | --- | --- |
| 执行内核 | `service.py`（7 180 行 / 264 KB） | **是**（核心） |
| 事件与状态 | `event_store.py`（约 3 000 行 / 121 KB）、`state_machine.py`、`task_lifecycle.py`、`models.py` | **是** |
| 规划 | `planner.py`（确定性，8 种操作）、`llm_planner.py`（模型，2 种操作）、`planning_validation.py` | 确定性**是**；LLM 规划器**不在产品路径** |
| 多步 | `multi_step.py` + `service._execute_multi_step_plan` | **引擎是**；产品面**无法启动**（见 §5.3） |
| 策略/围栏 | `policy.py`、`artifact_tool.py`、`workspace_edit.py`、`workspace_inspection.py`、`research_workflow.py`、`shell_tool.py`、`local_test_runner.py` | **是** |
| 验证 | `verifier.py`（写/读/测试）、各工具自带 `verify_*` | **是** |
| 恢复 | `controlled_recovery.py`、`retry_policy.py`、`lease_renewal.py`、`recovery_preview.py` | 部分：重试/租约**是**；审批**无生产入口** |
| 产品面 | `workbench_server.py`（1 713 行）、`workbench_control.py`（2 140 行）、`workbench.py`（1 195 行）、`conversation_service.py`（901 行） | **是** |
| 记忆 | `memory_service.py` | 仅 CRUD API，**不被规划/执行读取** |
| 评测/审计 | `safety_gate.py`、`evaluation.py`、`product_evaluation.py`、`code_edit_baseline.py`、`planner_quality.py` 等 14 个 | **不在运行时路径**（CLI 或测试专用） |
| 隔离 | `isolation.py`（macOS Seatbelt） | **无调用方**（见 §6.4） |

`INFERENCE`：`service.py` 单文件 7 180 行承担编排、工具分派、多步、恢复、验证聚合，是后续所有产品能力的共同瓶颈——任何新增工具都要改这个文件的分派链。

---

## 3. 启动入口（实测可用）

```bash
# 只读工作台（不初始化数据库）
PYTHONPATH=src python -m zevorinth --home <ROOT> workbench --port 8765
# 控制中心（可写，创建/审批/暂停）
PYTHONPATH=src python -m zevorinth --home <ROOT> control-center --port 8766
```

- 本轮实跑：`control-center --port 8791 --home /tmp/zv-audit-home`，`GET /` → 200，`GET /control/api/session` → 200。
- 只读模式**不初始化 SQLite**；库不存在时所有 `/api/*` 返回 503 `task_store_unavailable`（`workbench.py:85-91`）。
- `serve_workbench` 签名里没有 `host` 参数 → 无法绑定非 loopback（`workbench_server.py:1630-1640`）。

`FACT`：首次使用只读模式（`workbench`）会看到"任务列表不可用 / task_store_unavailable"，没有任何引导说明"先用 control-center 初始化"。这是默认首跑体验，不是异常路径。

---

## 4. 产品面：服务端与前端

### 4.1 路由

手写正则 + if/elif，共 **29 个可命中 (method, path)**（含 4 个静态资源）：

| 组 | 端点 | 备注 |
| --- | --- | --- |
| 页面 | `GET /`、`/index.html`、`/static/{app.js,styles.css,i18n.zh-CN.json}` | 静态先行 |
| 只读任务 | `GET /api/tasks`、`/api/tasks/{id}`、`/api/tasks/{id}/events/stream`（SSE）、`/api/tasks/{id}/artifact`、`/api/attention` | |
| 控制（写） | `POST /control/api/preview`、`POST /control/api/tasks`、`GET/POST /control/api/tasks/{id}/actions`、`GET /control/api/tasks/{id}/requests/{rid}`、`POST /control/api/tasks/{id}/retry` | 需 Origin + 进程内 token |
| 会话 | `GET/POST /control/api/conversations`、`GET /control/api/conversations/{id}`、`GET/POST /control/api/conversations/{id}/messages` | |
| 工作区 | `GET/POST /control/api/workspaces` | 只有列出与创建 |
| 记忆 | `GET/POST /control/api/memories`、`GET /control/api/memories/{id}`、`POST .../correct`、`DELETE .../{id}` | **前端零引用** |
| 会话元信息 | `GET /control/api/session` | 下发 token 与能力声明 |

**不存在**：`/health`、`/metrics`、`/version`、`/settings`、`/config`；运行中任务取消；产物下载；任务/会话/工作区删除；URL 深链。

鉴权：写路由要求 `Origin` 为 loopback 且 `X-Zevorinth-Control-Token` 匹配进程内随机 token（`workbench_server.py:1265-1299`）。`GET /control/api/session` 不校验且**直接返回该 token**，响应自述 `"authentication":"none"`（`workbench_server.py:1356-1358`）——这是刻意的本机信任边界，不是遗漏。

### 4.2 前端

4 个文件、无构建：`index.html`（235 行）、`app.js`（3 940 行 / 130 KB）、`styles.css`（1 443 行）、`i18n.zh-CN.json`（873 行 / 50 KB，27 个顶层键）。

单页无路由，三块区域：概览指标条 / 任务列表 / 任务详情。创建入口两条：

1. **对话面板**（`#conversation-form`）→ `POST /control/api/conversations/{id}/messages`
2. **高级表单**（`#task-create-form`）→ `POST /control/api/tasks`（两步：`preview` 再 `confirmed:true` 创建）

状态同步：**SSE 只当触发器用**。`onmessage = () => {…}` 忽略事件内容，300 ms 防抖后整块重拉 `GET /api/tasks/{id}` 再全量重渲染（`app.js:1868-1877`、`1893-1915`）；服务端 SSE 内部 1.5 s 轮询 SQLite，单连接上限 300 s（`workbench_server.py:51-52,1507-1521`）。

**前端处理了**：列表骨架屏、详情骨架屏、空列表、列表/详情错误块、`aria-live`（7 处）、`<noscript>`、`prefers-reduced-motion`。
**前端没处理**：SSE 断连（全站无 `onerror`）、静默刷新失败（`catch {}`）、会话空态、任务/会话删除、深链、错误级别的 `role="alert"`（0 处）。

### 4.3 前后端闭环的断点（FACT）

| 断点 | 证据 |
| --- | --- |
| 会话消息只有 `user` 角色，**系统从不写 assistant 消息** | 实跑：`GET .../messages` 返回 4 条全为 `user`（见 `P0_USER_JOURNEY_AUDIT.md` J1） |
| 澄清问题不进入会话记录 | 问题存在 `clarifications` 表，仅随 `POST .../messages` 响应返回；重新加载后不可见 |
| `POST .../messages` 不转发 `operation` | `workbench_server.py:470-476` 只传 `content/client_message_id/answer_to` |
| 后端实现了、前端从不调用 | `POST .../retry`、5 个 memory 端点、`session.cross_task_memory/manual_retry/automatic_recovery` |
| 会话列表加载失败静默 | `app.js:561-563` `catch (_) {}` |

---

## 5. Agent 内核的真实行为

### 5.1 产品执行路径上**没有模型**

`FACT`：
- `TaskService` 默认 `self.planner = planner or DeterministicPlanner()`（`service.py:361`）。
- 控制中心显式传 `DeterministicPlanner()`（`workbench_control.py:586,1157`）。
- `conversation_service.py` / `workbench_server.py` / `workbench_control.py` 中**没有任何** LLM/OpenAI 引用（grep 只有 `http.server` 与 URL 正则）。
- `llm_planner.py` 只在 CLI `--planner llm` 或 `ZEVORINTH_PLANNER=llm` 时启用（`cli.py:254-269`）。

**结论**：当前交付的产品是一个**确定性工作流引擎 + 聊天形状的输入框**，不是"模型理解目标 → 规划 → 执行"的 Agent。用户看到的"智能"来自表单字段与固定规则。

### 5.2 任务状态机

14 个状态（`models.py:14-28`），迁移表硬编码在 `state_machine.py:24-108`，由 `ensure_transition` 强制（`state_machine.py:111-113`），调用点在 `event_store.py` 的 4 处。
结构性约束：**`SUCCEEDED` 只能从 `VERIFYING` 进入**（`RUNNING → SUCCEEDED` 非法）→ 强制"先验证后成功"，这是本系统最值得保留的设计。

已知缺口：`record_event_and_update()`（`event_store.py:2307-2364`）**不调用** `ensure_transition`，只比对 `expected_status`。当前生产代码没有把 `"status"` 放进 `updates`，所以是"纪律"而非"机制"。

### 5.3 规划与澄清

- 确定性规划器按 `operation` 分派到 8 个 builder，每个产出**恰好 1 步**（`planner.py:424-470`）；多步只能由用户显式给 `execute_plan` + `steps` 数组（`planner.py:381-421`）。
- 缺字段即澄清，问题文本是 `f"请提供 {field}"`（`planner.py:486-488,592,630`）。必填字段表在 `planner.py:556-586`，**另有一份重复定义**在 `policy.py:54-115`（`resume` 用），两份漂移会产生不一致行为。
- LLM 规划器只能选 `write_text_artifact` / `run_tests` 两个操作，不能产出多步计划（`llm_planner.py:78-81`）；工作区/runner/镜像等可信字段由代码注入，模型输出若丢弃可信字段会被判 `trusted_parameter_loss`（`planning_validation.py:318-329`）。

**产品面最硬的一条**：`execute_plan` 不在 `SUPPORTED_TASK_OPERATIONS`（`capabilities.py:35-291` 无此键），`CONTROL_STARTABLE_OPERATIONS = SUPPORTED - {"run_tests"}`（`workbench_control.py:52-54`）。实跑：创建 `execute_plan` 任务后，`GET .../actions` **不提供** `start_queued_task`，只有 `revise/cancel/pause/note`；`POST` 启动返回 `task_start_not_allowed`。
→ **多步计划在产品里可创建、不可执行。** `run_tests` 同理（可创建、不可启动）。

### 5.4 执行

调用链（`service.py`）：`submit()`（431-443）= `enqueue()` + `execute_task()`；`execute_task`（1063）→ `_claim`（2654）→ `_run_claimed`（2723）→ `_run_with_renewal`（2798，起租约线程、同线程执行）→ `_plan_and_execute`（2899）→ `planner.plan_request`（2913）→ `policy.evaluate`（3069）→ `_execute_tool`（5137 起 if 链）→ `_verify_and_finish`（6883）→ `SUCCEEDED`（7086）。

**执行是同步的，阻塞请求线程**：`ThreadingHTTPServer` 提供并发连接，但没有任何后台执行队列；控制中心还把执行包在全局锁 `self._action_lock` 里（`workbench_control.py:201,1154`），执行期间其它控制动作排队。上限：租约 60 s、单任务 600 s（`service.py:323-324`）。

### 5.5 暂停 / 取消 / 恢复（能力边界）

| 动作 | 实际语义 | 证据 |
| --- | --- | --- |
| 暂停 | **只在多步计划的步边界生效**；单步任务无检查点 | `service.py:3278-3294,3439-3455`；`request_pause` 对 RUNNING 只写 `pause_requested_at`（849-865） |
| 取消（运行中） | 拒绝：有活跃租约时 `cancel_task` 抛 `PlanRevisionConflict`（`event_store.py:2163-2166`）；`request_cancel` 只置标志，检查点在 6 处（2726/2925/3267/5201/6885/7063） | 网页无 cancel 路由 |
| 工具级中断 | 只有 `run_tests` 能 kill 进程（`service.py:6346-6351` + `local_test_runner.py:444-447`）；写/编辑/shell **不可中断** | shell 单次 `subprocess.run(timeout)` |
| 恢复 | 有持久化证据 → 只重做验证；无证据 → **整步重跑**（`service.py:3163-3213`）。没有"步骤内续跑" | |
| 重试 | 手动触发，`MAX_RETRY_ATTEMPTS=3`，**创建新任务**；工具一旦 `TOOL_STARTED` 绝不重放（`service.py:3406-3416`） | `retry_policy.py:7,40,51-58` |

### 5.6 多步与部分成功

`MAX_PLAN_STEPS=8`、`MAX_STEP_ATTEMPTS=3`、`failure_policy ∈ {fail_fast, continue}`（`multi_step.py:16-29`）。步失败后 `fail_fast` 直接 `break`（`service.py:3456-3457`）；终态聚合支持 `PARTIAL`（`service.py:3893-3914`）。
实跑（引擎级）：3 步计划 → 在 s2 停在 `awaiting_approval` → 批准 → s3 失败 → `partial`，`step_results` 逐条给出 `succeeded/succeeded/failed`，错误码 `multi_step_partial`。
`PARTIAL` 是终态且**不可重试**（`retry_policy.py:147-153` 不含 partial）。

产物落在**子任务目录** `artifacts/<child_task_id>/`，父任务本身无产物；父任务 `step_results` 里有 `child_task_id`，但产品面没有从父任务跳到子产物的入口。

---

## 6. 工具、权限、验证

### 6.1 八个操作

| 操作 | 做什么 | 产品可启动 | 验证器 | 能否发现"成功但没产出" |
| --- | --- | --- | --- | --- |
| `write_text_artifact` | 写 `artifacts/<task>/<name>`，≤64 KiB，staging→fsync→hardlink，拒绝覆盖 | 是 | `artifact-readback-v1` 重读比字节+摘要 | **能** |
| `modify_text_artifact` | 基于已验证源工件派生新文件，记录 lineage | 是 | 同上 | **能** |
| `read_text_artifact` | 读已验证源工件到内存 | 是 | 重读比对 | **能** |
| `inspect_workspace` | 有界只读扫描 `workspaces/<name>` | 是 | 重扫逐字段比对 | **能** |
| `edit_text_file` | 单次精确替换 + sha256 前置 + 备份 + 原子 replace | 是 | 重解析 + 血缘核对 | **能** |
| `research_report` | 从本地源文件生成 Markdown 报告（要求精确引文） | 是 | 重建报告 + 重算摘要 | **能** |
| `controlled_shell` | 只读命令白名单、`shell=False`、1–30 s、64 KiB 上限 | 是 | **只查形状与命令串相等** | **不能**（空输出也通过） |
| `run_tests` | container（需 evidence）或 host（需一次性 grant） | **否**（可创建不可启动） | 重算 workspace diff、比对计数 | 能（diff 重算） |

### 6.2 权限

`policy.py` 是"逐操作白名单 + 参数形状 + 路径围栏"，两个总闸：操作必须在 `SUPPORTED_TASK_OPERATIONS`；**计划必须恰好 1 步**（`policy.py:137-148`）。
路径围栏分散在各工具：`local_test_runner.resolve_workspace`（299-318，拒符号链接与逃逸）、`workspace_edit._safe_regular_file`（323-349）、`artifact_tool`（单段文件名 + 目录必须是 artifacts 根的直接子目录，`policy.py:245-261`）。

`FACT`：**多步父任务不过 policy**（`service.py:3063-3069` 先分支到多步），策略只作用于每个子任务。由于子任务各自过 policy，这条在结果上是安全的，但它意味着"计划的整体形状"没有人审。

### 6.3 审批

用户能批准的只有多步计划里 `requires_approval` 的步骤（`approve_step`/`reject_step`）。绑定的是三元组：`pending_approval_step_id` + `expected_plan_version` + 租约 token；**没有 step digest**（`models.py:180-219` 无摘要字段）。
缺口（可复现的代码路径）：`approved_step_ids` **从不清空**（`service.py:935-945` 写、`3340` 读、`replan_pending_steps` 只清 `pending_approval_step_id`，`1026-1055`）。"批准 s1 → 暂停 → replan 用同一 step_id 换参数 → resume"在代码上不再要求审批。
另外：`approve_recovery_preview` 的 `approver` 只是自由字符串，无认证；且全仓只有测试调用它，**没有生产入口**。

### 6.4 隔离

`isolation.py` 是 macOS `sandbox-exec` 围栏，profile 为 `(deny default)(allow process*)(allow file-read*)(deny network*)`（`isolation.py:61-76`）——**读不受限**。而它自己的 `REQUIRED_BOUNDARIES` 要求 `read_outside_denied`（`42-47`），所以在当前 profile 下探针永远返回 `partial`，`require_verified` 会拒绝 → `run_protected` 实际上启动不了任何进程。且 `run_protected` 在 `src/` 内**无调用方**。

### 6.5 `safety_gate.py`

51 KB 的"离线评测 + 本地边界用例"工具，唯一生产调用方是 CLI `eval-safety-gate`（`cli.py:1258-1264`）。`service.py` **从不导入它** → 它不在运行时守卫路径上。把它当作"运行时安全门"是误读。

---

## 7. 记忆与上下文

| 能力 | 实际状态 |
| --- | --- |
| 会话历史 | 存 SQLite，上限 200 条（`event_store.py:126`），且取的是**最早** 200 条（`1237 ORDER BY turn_index ASC ... LIMIT ?`） |
| 历史是否进规划 | **否**。`conversation_service.py:308-320` 只把当前消息转成 `TaskRequest`；`planner` 只见 `goal/operation/parameters` |
| 摘要 | **无实现**。`context.py:248-249` 显式把调用方传的 `summary` 标为 `summary_authoritative=False` |
| 跨任务记忆 | `memory_service.py` 有 5 类型 × 3 作用域 + 软删 + supersede；**读只发生在 UI 查询接口**（`workbench_server.py:643,647`），`service/planner/multi_step/context` 中零引用 → **写-only 存储**，不影响任何决策 |
| 检索 | `recall` 只做 `content LIKE ?`（`memory_service.py:344-347`），无语义检索 |

---

## 8. 测试与质量保障（只算它真验了什么）

| 项 | 数值 | 说明 |
| --- | --- | --- |
| 测试文件 | 111 个 `test_*.py`（`tests/` 共 139 个 `.py`，84 924 行） | 全 `unittest`，0 个 pytest |
| 测试函数 | 1 687 个 | 静态计数 |
| HTTP 级测试 | 约 146 | 真起 `create_server` + `urlopen`（`test_workbench.py`、`test_workbench_control.py` 等） |
| 真实模型测试 | **0** | 全部 FakeClient / 假 `openai` 模块；live 用例只验证"未授权时不许发请求" |
| 容器测试 | 有，但默认被过滤/跳过 | `tests/task_control_center_evidence.py:160-163` 过滤 `container/docker/live/online` |
| 历史审计记录 | `905 passed / 0 failed`（`artifacts/audits/task-control-center-v1/stable-summary.json`）、`882 passed`（`phase-3.92`） | 均为当时的树；当前树 139 个文件全部晚于该审计，不能直接引用为现状 |
| 变异测试 | 235 killed / 0 survived（历史记录） | `tests/mutation_runner.py` |

`INFERENCE`：测试密度很高（84 924 行 / 源码约 30 000 行），但**验证的是确定性引擎的边界**，不是"模型能否完成任务"。这解释了为什么工程质量与产品体验会出现巨大落差。

---

## 9. 已验证 vs 未验证

### 已实测通过（本轮真实运行）

| 能力 | 证据 |
| --- | --- |
| 服务启动、页面、会话列表、任务列表、SSE 端点 | `GET /` 200、`/api/tasks` 200、`/control/api/session` 200 |
| 写文件任务端到端 | `note.txt` 42 B，`sha256 22eccb50…`，`verification.passed=true`，`artifact_observation.matches_registration=true` |
| 代码修改任务端到端 | `calc.py` 由 `a - b` 改为 `a + b`，`workspace_edit_verified`，备份 `5e204990…-calc.py.bak` |
| 策略拒绝（不落盘） | `text_match_count`、`source_file_changed` → `blocked`，事件止于 `policy_rejected` |
| 多步 + 审批 + 部分成功（引擎级） | `awaiting_approval` → 批准 → `partial`，`step_results` 三态 |
| 对话式完成（4 轮） | 3 次澄清后 `succeeded` |

### 未验证 / 无法验证

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 模型理解自然语言目标 | `NOT_IMPLEMENTED`（产品面） | 产品执行路径无模型 |
| 多步计划从产品启动 | `NOT_IMPLEMENTED` | 动作白名单不含 `start_queued_task` |
| `run_tests` 从产品启动 | `NOT_IMPLEMENTED` | 同上 |
| 运行中取消 | `NOT_IMPLEMENTED` | 无 HTTP 路由；引擎拒绝活跃租约 |
| 容器/宿主测试执行 | `NOT_TESTABLE` | 本机未配 docker / 未签发 host grant |
| 跨任务记忆影响行为 | `NOT_IMPLEMENTED` | 记忆不被读取 |
| 产物下载/版本历史 | `NOT_IMPLEMENTED` | 无下载端点、无版本列表 |
| 并发多任务执行 | `PARTIALLY_IMPLEMENTED` | 引擎支持多进程（有测试），产品内无调度器、控制面串行 |

---

## 10. 工程限制与结构性缺陷（汇总，编号供其它报告引用）

| 编号 | 缺陷 | 类型 |
| --- | --- | --- |
| R-01 | 产品执行路径无模型，目标理解靠表单字段 | 能力 |
| R-02 | `execute_plan` / `run_tests` 产品不可启动 | 能力 |
| R-03 | 执行同步阻塞请求线程 + 控制面全局锁 | 架构 |
| R-04 | 暂停只在多步步边界；单步不可暂停、运行中不可取消 | 能力 |
| R-05 | 会话无 assistant 消息；澄清问题不进会话记录 | 产品 |
| R-06 | 会话历史不进规划；无摘要；取最早 200 条 | 上下文 |
| R-07 | 记忆 write-only，不影响决策 | 能力 |
| R-08 | 审批无 step digest；`approved_step_ids` 不清空，replan 可绕过 | 安全 |
| R-09 | `controlled_shell` 验证不看输出内容 | 验证 |
| R-10 | 产物无版本列表/下载/血缘视图 | 交付 |
| R-11 | `isolation.py` 无调用方且 profile 允许全局读 | 安全（未启用） |
| R-12 | `safety_gate.py` 不在运行时路径 | 安全（误读风险） |
| R-13 | 必填字段表在 `planner.py` 与 `policy.py` 重复定义 | 维护 |
| R-14 | `record_event_and_update` 可绕过迁移表（纪律而非机制） | 一致性 |
| R-15 | 前端 SSE 无断连处理；静默刷新失败 | 体验 |
| R-16 | 无 health/metrics/version 端点 | 运维 |
| R-17 | 首跑只读模式 503，无引导 | 体验 |

> 编号说明：`R-18`（澄清不可用）、`R-19`（失败只能整步重跑）、`R-20`（错误文案直出）、
> `R-21`（无深链/删除/设置）、`R-22`（事件无分页）、`R-23`（operation 文案缺 3 个）在
> `P0_GAP_AND_RISK_REGISTER.md` 中补全；本表只列工程侧可直接观测的 17 项，避免编号冲突。

---

## 11. 本文件的证据限制

1. 未做任何并发压力测试、性能基准、故障注入（除引擎自带 test 场景外）。
2. 未验证容器路径与 host grant 路径（本机条件不满足）。
3. 未审 `.idea/`、未审 `.omx/` 内部实现（不属于产品）。
4. 前端结论来自源码 + 一次真实页面加载（浏览器可见 DOM 仅暴露输入框，截图受工具限制未取到）；未做逐像素视觉评审。
5. 本轮审查不改任何被评对象；`docs/product-research/p0-baseline/` 之外零写入。
