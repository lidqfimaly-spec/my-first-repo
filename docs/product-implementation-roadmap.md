# Zevorinth 产品化实施路线图

Status: Active implementation baseline
Date: 2026-11-21

## 目的

依据《Zevorinth 审查报告》的 P0–P3 问题分级，确立产品化改造的推进顺序、每阶段任务、验收标准与不可破坏的边界。路线图是"全面实施"的追踪基线，每完成一格更新状态。

## 不可破坏的边界（全阶段生效）

- 只读 `/api` 与受控 `/control/api` 面分离不变。
- loopback-only 绑定不变。
- 模型不持有工具/shell/网络/数据库能力不变。
- 独立验证、三态执行真实性、租约围栏不变。
- 每格改动后 `tests/test_workbench.py` 全绿 + 相关模块测试全绿。
- 新能力登记到 `capabilities.py` 权威注册表。
- 不擅自删除现有功能/数据/文件。

## P0 产品审查基线（`P0_AUDIT_COMPLETE_PENDING_REVIEW`，2026-11-22）

**性质**：只调查、验证、研究、规划，**不是实施**。本轮未改任何源码、未改数据库、未动数据、
未 stage/commit/push、未改权限边界、未触碰 V3.1 工作线。

**入口**：`docs/product-research/p0-baseline/P0_EXECUTIVE_SUMMARY.md`；13 份报告全在该目录。

**五条硬结论**（详见执行摘要）：

1. 产品执行路径**零模型**——`TaskService` 默认 `DeterministicPlanner`，`llm_planner` 只在 CLI。
2. 引擎能力显著高于产品能力——多步计划与 `run_tests` 引擎已实现，产品面**可创建不可启动**。
3. 十条旅程 **1 PASS（仅引擎级）/ 4 PARTIAL / 5 FAIL**。
4. 五个 P0：R-01 理解层 · R-02 多步不可执行 · R-03 同步阻塞 · R-10 成果拿不走 · R-18 澄清不可用。
5. `research_report` 不是研究，按给定引文拼装本地文件——对外宣称"能写研究报告"会制造错误成功率。

**与本节其他条目的关系**：审计发现的缺口不自动构成实施授权。V3.1 仍待独立验收；
V3.2 仍 `NOT_AUTHORIZED`；S3 未批准；`PRODUCTION_TRUST` 未提升。

### V1 实施进度（`IMPLEMENTED_SELF_CHECKED`，2026-11-22）

授权：负责人「全面实施」，按 `P0_NEXT_PHASE_ROADMAP.md` 的 V1 范围（不含 `NOT_RECOMMENDED`）。
详见 [`p0-3-v1-implementation.md`](p0-3-v1-implementation.md)。

| 阶段 | 状态 | 落地 |
| --- | --- | --- |
| S-A 执行基座 | **已完成** | `task_executor.py`（后台驱动，不改引擎）+ `progress_text.py`（拒绝百分比）+ `dispatch` / `cancel-request` / `progress` 三端点 |
| S-B 听懂人话 | **已完成** | `intent_extraction.py`（零网络规则抽取）；澄清改用户语言 + 候选 + 答案归一化；会话写入 assistant 消息 |
| S-C 成果到手 | **已完成** | 下载（`?download=1`，下载前重算摘要，变了就 409）；血缘卡（源任务/源文件/摘要/方式）；「在这个产物上继续改」（摘要由服务端填，用户只写内容）；下游任务反查 |
| S-D 复杂任务 | **部分完成** | 已做：R-08 审批绑内容摘要（v2，含 `plan_id`）、F-11 审批表单、replan 保留未改步骤的批准、执行期重算摘要并在工具运行前拒绝、**从失败步骤续跑（服务层，31 项测试 + 6 变异体）**。**未做**：续跑的控制面入口、放开 `execute_plan` / `run_tests` 启动（权限边界变更，待裁决）。**新发现**：`execute_plan` 可经 `pause_task`→`resume_task` 被启动，启动门禁实际无效（见 `docs/s-d-2026-11-22/F09_PERMISSION_ASSESSMENT.md` §1.3） |
| S-E 记忆生效 | **未开始** | 依赖 S-D |

实测（第二轮）：受影响 10 个测试文件 **212 passed / 51 subtests / 0 failed**；新增 23 个测试；
真机浏览器实操走通「造计划 → 界面审批 → 执行 → 继续改 → 看到血缘与下游」。

五项待决策取可逆默认：D-1 不接模型（抽取器留接口）· D-2 不联网 · D-3 接受单步不可暂停 ·
D-4 `PARTIAL` 保留终态 · D-5 不公开成功率。

---

## 已完成

### [DONE] P1-2 SSE 流式事件推送（2026-11-21）

**改动：**
- `src/zevorinth/workbench.py`：新增 `task_events_since(task_id, since_sequence)` 只读增量事件查询（复用 `_connect` 只读 `mode=ro` + `_event_projection` 脱敏投影）。
- `src/zevorinth/workbench_server.py`：新增 `GET /api/tasks/{id}/events/stream?since=N` SSE 端点（`text/event-stream`、CSP `connect-src 'self'`、`no-store`、首批立即推送 + 1.5s 轮询 + 300s 超时关闭 + 客户端断开静默退出）。
- `src/zevorinth/workbench_static/app.js`：选中任务时开 `EventSource`（since=当前最大 sequence，只推增量），收到事件 debounce 300ms 静默刷新详情（不重开 stream、不闪烁），切换任务时关旧 stream。
- `tests/test_workbench.py`：新增 5 测试（HEAD 返回 SSE Content-Type、请求前后数据库 digest 不变证只读、未知任务 404、POST 拒绝 405、`task_events_since` 增量投影正确）。

**验收：** 24/24 测试通过（原 19 + 新 5），7.9s。只读边界经数据库 digest 验证未破坏。

**后续：** `event_streaming` 能力已登记到 `capabilities.py`（`test_capability_registry` 兼容）。

### [DONE] P2-3 模型用量与耗时可见性（2026-11-21）

**改动：**
- `src/zevorinth/workbench.py`：新增 `_summarize_model_usage(events)` 只读汇总（遍历含 `planning_metadata` 的事件，聚合 input/output/total tokens、latency_ms、调用次数、缺失计数）；`task_detail` 返回新增 `model_usage` 字段。
- `src/zevorinth/workbench_static/app.js`：新增 `modelUsagePanel(usage)` + `fmtToken`，`renderTaskDetail` 在事件时间线后接入；区分"未调用模型/服务方报告/部分缺失"，缺失显示"未记录"不计为零，费用标"未配置价格表，不估算"。
- `tests/test_workbench.py`：+3 测试（结构完整、注入 token 后汇总正确、缺失不显示为零）。
- `src/zevorinth/capabilities.py`：登记 `model_usage_projection: available`。

**验收：** 45/45 测试通过（capability_registry 18 + workbench 27，较 P1-2 +3），9.6s。满足"区分精确计量/Provider 报告/缺失，不把缺失显示为零"要求。

### [DONE] P1-1 对话式主交互（2026-11-21）

**改动：**
- `src/zevorinth/workbench_server.py`：新增 4 个对话端点（均在 `/control/api` 受控面）：
  - `POST /control/api/conversations` → 创建对话（要求 control token + loopback Origin）
  - `GET /control/api/conversations/{id}` → 对话详情（含 active_task_id / active_task_status）
  - `GET /control/api/conversations/{id}/messages?limit=N` → 只读消息历史
  - `POST /control/api/conversations/{id}/messages` → 提交消息（路由到 ConversationService.process_message）
  - WorkbenchServer 新增 `conversation_service` 实例（control_enabled 时创建）；`/control/api/session` 新增 `conversation_intake` 标志。
- `src/zevorinth/workbench_static/index.html`：新增对话面板 section（消息时间线 + textarea 输入 + 发送按钮 + 高级表单切换）。
- `src/zevorinth/workbench_static/app.js`：新增对话状态（conversation/conversationMessages/conversationTurns/conversationSending/conversationActiveTaskId）；新增 `initConversation`（localStorage 持久化 conversation_id，404 时自动重建）、`createConversation`、`loadConversationHistory`、`submitConversationMessage`（Enter 发送/Shift+Enter 换行）、`renderConversationMessages`、`renderConversationSystemTurn`（outcome→中文标签、任务链接、澄清问题、错误）、`handleConversationResult`（任务创建后自动选中并打开证据面板）、`outcomeToLabel`（18 种 outcome + 14 种状态码映射）。
- `src/zevorinth/workbench_static/styles.css`：新增 `.conversation-panel` / `.conversation-messages` / `.msg-user` / `.msg-system` / `.conversation-form` / `.sr-only` 样式（匹配现有配色，移动端适配）。
- `src/zevorinth/capabilities.py`：登记 `conversational_intake: available`。
- `tests/test_workbench.py`：新增 `ConversationEndpointTests`（13 测试：session 广播、创建对话、创建需 token、NEW_TASK 创建任务+澄清、提交需 token、空内容拒绝、未知对话 404、详情返回 active_task、消息历史、未知对话 404×2、未知字段拒绝、STATUS_QUERY 路由）。

**验收：** 58/58 测试通过（capability_registry 18 + workbench 40，较 P2-3 +13），17.6s。端到端验证：创建对话→发"创建 hello.txt"→NEW_TASK 意图→任务 awaiting_clarification + 3 澄清问题→详情显示 active_task→历史返回用户消息→"状态"查询正确路由。

**设计要点：**
- 对话端点仅在 `/control/api` 受控面（control_enabled=True），纯只读 workbench 不暴露对话 UI——保持只读 /api 与受控 /control/api 分离不变。
- ConversationService.process_message 创建的任务为 QUEUED 或 AWAITING_CLARIFICATION（不执行）——执行仍需单独受控操作（START_QUEUED_TASK），符合"能力渐进开放"原则。
- IntentClassifier 是确定性规则（无 LLM 依赖），NEW_TASK/STATUS_QUERY/CLARIFICATION_ANSWER/CANCEL_REQUEST/SUPPLEMENT/MODIFY_PENDING 六类意图路由。
- 系统响应（ConversationProcessResult）返回给前端但不持久化为 assistant 消息——前端内存保存对话轮次，刷新后仅恢复用户消息。
- 前端对话面板与表单面板可切换（"高级表单"按钮），保留现有受控操作入口不删除。

---

## 第一阶段：产品化基础（解锁用户可用性）

目标：把"受控执行引擎"改造为"用户可感知在工作"的产品。依赖 SSE 已完成。

### P1-1 对话式主交互
- 当前：表单式工作台，非对话界面。
- 目标：引入对话式主交互（保留证据面板为侧栏），自然语言发起任务。
- 依赖：P0-2（模型质量支撑对话理解）部分。
- 风险：前端改动大，需保留现有受控操作面板。

### P3-1 术语简化
- 当前：状态筛选暴露 `planning_failed/interrupted/simulated` 等内部码。
- 目标：面向普通用户的话术，内部码保留在诊断字段。
- 改动：`i18n.zh-CN.json` 显示映射 + `index.html` 筛选项标签。
- 风险：低（纯展示层）。

### P2-3 费用/token 可见
- 当前：不自动估算费用，缺价格表记"未测量"。
- 目标：规划事件已记录 token/延迟，前端展示累计 token + 可选费用估算。
- 改动：`workbench.py` 详情投影 token 汇总 + `app.js` 展示 + 价格表可选。

### [DONE] P0-1 受控 shell 工具（2026-11-21）

**改动：**
- `src/zevorinth/shell_tool.py`（新建）：受控 shell 执行模块。核心安全边界：
  - **参数数组**：`subprocess.run(args_list, shell=False)`——永不 `shell=True`
  - **白名单**：21 个只读命令（ls/cat/wc/head/tail/grep/find/pwd/date/echo/file/stat/diff/sort/uniq/basename/dirname/cut/tr/rev/strings）；禁止解释器（python/node）、写命令（rm/mv/cp）、网络（curl/wget）
  - **超时**：1..30 秒，默认 15 秒
  - **输出上限**：64 KiB，超出截断并标记 `[output truncated]`
  - **路径安全**：敏感路径（/etc/passwd、.ssh、.env 等）拒绝；绝对路径须在工作区内；路径穿越（..）拒绝
  - **危险 flag 拒绝**：-delete/-exec/-ok/-print0/-ls 等
  - **最小环境**：仅继承 PATH/HOME/LANG/TZ/USER 等安全变量；stdin 断开
  - **工作目录锁定**：`cwd=workspace_root`，working_directory 须为工作区内相对路径
- `src/zevorinth/planner.py`：新增 `CONTROLLED_SHELL` 常量 + `build_controlled_shell_plan()`；`plan()` 和 `plan_request()` 分发新增 controlled_shell 路由（required field: command）。
- `src/zevorinth/policy.py`：新增 `_evaluate_controlled_shell()`——调用 `validate_shell_parameters()` 做白名单+路径+超时+flag 验证。
- `src/zevorinth/service.py`：新增 `_execute_controlled_shell()` + `_verify_controlled_shell_and_finish()`——TOOL_STARTED→execute_controlled_shell→TOOL_SUCCEEDED/FAILED→VERIFICATION_COMPLETED→SUCCEEDED/FAILED 完整生命周期。结果存 `workspace_effects`（command/arguments/output/exit_code/duration_seconds/output_bytes/truncated/timed_out）。
- `src/zevorinth/capabilities.py`：`controlled_shell` 登记为 task_operations available；`general_shell` 从 not_implemented 升级为 available（superseded_by controlled_shell）；从 not_available_task_types 移除 general_shell。
- `README.md`：操作表从"七个"更新为"八个"，新增 controlled_shell 行。
- `tests/test_shell_tool.py`（新建）：38 测试，4 类：
  - **参数验证**（17 测试）：白名单通过/非白名单拒绝/解释器拒绝/路径分隔符拒绝/空命令/非字符串参数/null 字节/参数过多/危险 flag（-delete/-exec）/敏感路径（/etc/passwd/.ssh/.env）/绝对路径/路径穿越/超时范围/working_directory 穿越
  - **执行**（9 测试）：echo/cat/ls/grep/wc 执行成功+输出验证；失败命令记录；模拟模式立即返回；输出截断标记；超时
  - **Mutation 审计**（6 测试）：注入 rm 到白名单证明 guard 在验证层；移除敏感路径检查证明绝对路径检查仍拦截；白名单不含危险命令集（rm/mv/python/curl/sudo 等 38 个）；subprocess.run 用 shell=False 非 True；超时/输出上限有界
  - **端到端**（4 测试）：TaskService.submit() ls/cat 任务 succeeded + workspace_effects 验证；rm 被 policy 阻止（failed/blocked/planning_failed）；缺 command 进入 awaiting_clarification

**验收：** 131 测试通过（shell_tool 38 + workbench 40 + capability 18 + conversation_service 5 + planner_eval 30），22.8s。端到端：ls/cat 通过 TaskService.submit() 执行成功 + exit_code=0 + 输出正确；rm 被 policy 拒绝。

**设计要点：**
- 白名单只含只读命令——写操作继续走 write_text_artifact/edit_text_file（有路径安全+验证）；shell 工具用于工作区探查、文件搜索、内容读取
- 参数数组（非 shell 字符串）从根本消除 shell 注入面——`subprocess.run(["grep", pattern, file], shell=False)` 不解释 metacharacter
- 双层验证：Policy 层（evaluate 时）+ 执行层（_execute_controlled_shell 时再次 validate_shell_parameters）——defense in depth
- ToolResult.workspace_effects 存 shell 特有数据（exit_code/output/truncated/timed_out），不改动 ToolResult 核心模型
- 验证器检查 workspace_effects 完整性 + command 匹配——证明执行真实发生

---

## 第二阶段：能力扩展（通用 Agent 基线）

目标：补齐通用 Agent 的工具与质量基线。这是 P0 区。

### P0-1 受控 shell 工具
- 当前：`general_shell: not_implemented`。
- 目标：参数数组 + 白名单命令 + 超时 + 输出上限，复用 Policy/Verifier/租约安全底座。
- 设计依据：`architecture.md:152` 已预留"参数数组+白名单+超时+输出上限"。
- 依赖：隔离方案（容器/沙箱）成熟度。
- 风险：高（安全），需 mutation 审计。

### P0-2 代码编辑质量基线 — `[V3 · 独立验收 FAIL · V3.1 已实施待验收]（2026-11-21 → 2026-11-22）`

> **状态：`V3 = IMPLEMENTED_SELF_CHECKED`，V3 独立验收 = `FAIL`**（九项 7 通过 / 2 不通过，
> 四项强化检查 2 通过 / 2 不通过）。**`V3.1 = IMPLEMENTED_SELF_CHECKED`（实施方不自评 PASS）**，
> V3.1 独立验收 = `NOT_STARTED`。V3.2 = `DESIGN_APPROVED`，未授权实施。
> 不启动 S3，不开放新的模型编辑权限。`PRODUCTION_TRUST` 未提升。
> 证据链： [`p0-2-code-edit-baseline.md`](p0-2-code-edit-baseline.md)（v2 自检，历史）
> → [`p0-2-independent-acceptance.md`](p0-2-independent-acceptance.md)（v2 独立验收 `FAIL`，2 项阻断）
> → [`p0-2-code-edit-v3.md`](p0-2-code-edit-v3.md)（v3 修复与证据）
> → [`p0-2-v3-independent-acceptance.md`](p0-2-v3-independent-acceptance.md)（**v3 独立验收 `FAIL`，冻结**）
> → [`p0-2-v3-failure-freeze.md`](p0-2-v3-failure-freeze.md)（失败基线冻结 + 复现命令）
> → [`p0-2-v31-v32-design.md`](p0-2-v31-v32-design.md)（V3.1/V3.2 设计与验收契约，r2.1）
> → [`p0-2-v31-implementation.md`](p0-2-v31-implementation.md)（**V3.1 实施报告 + 六项已知局限**）。

**v3 修了什么**（评审批准范围：只修评测可信度 + 重建可比较基线）：

| 阻断项 | v3 的处理 | 验证 |
| --- | --- | --- |
| 身份不覆盖执行路径 | 新增**依赖闭包**（28 符号，从 `run_case`/`run_probe` 真实调用图推出）+ **源码树摘要** + 候选集 + 指标契约 | 改校验器 → 依赖摘要 `1b2a…`→`e68f…`；改应用器 → `2766…`；改授权消费 → `4a8d…` |
| 冻结不防篡改 | **外部锚点**：`scripts/anchor_code_edit_evidence.py` 独立复算（不 import 身份函数）+ 仓库外 append-only 链 + 结论封印 | 篡改 `evaluator_identity` → 外部复算不一致 → **拒绝锚定** |
| G 集只进分母 | 指标契约升 `v2`：G 计入错误修改率分子，`scope_audit` 单列 ID | 离线 G=`no_change_formatted_boundary`，错误修改率 0/7 → **1/8** |
| 探针守卫单向 | 新增 `dataset_missing_rejecting_probe` | 删全部 reject 探针 → 拒绝加载 |
| 辅助字段可静默改 | 评测器身份 21 → **48** 符号（含 `CaseResult`/`ProbeResult`/全部归因常量） | 改 `passed` → 摘要 `0c9b…`→`2031…` |

**反向变异矩阵 12/12**（`scripts/code_edit_mutation_matrix.py`，可重跑）——
评审给的那 12 行全部实现为可执行检查，任何一行不符即非零退出。

**阶段 B 新序列（不与 a–i 混算）**：`live-deepseek-chat-v3-a/b/c`，九项指标三次完全一致
（3/3、3/3、0/3、0/3、3/3、3/3、2/3、5/5、1/1），三条外部锚点链完整（`0001`–`0003`）。
a–i 全部保留：缺依赖身份与源码树，指标契约是 v1。

**已知未解决**（`p0-2-code-edit-v3.md` §8）：自指仍存在（外部锚点是缓解）、源码树摘要不是 git 修订、
`co_names` 看不到动态分派、指标未做「分子 ≤ 分母」自检、**无人类独立验收**。

### V3 独立验收结果（2026-11-22）：`FAIL`

九项检查：`PASS` 7（身份 / 真实执行路径 / 依赖闭包 fail-closed / 锚点篡改检测 / 正反样本 /
十二行矩阵 / 新基线复现性），`FAIL` 2（外部锚点、九项指标复算）。
四项强化检查：`B`、`C` 通过；`A`（锚点独立性）、`D`（分子>分母的外部处理）不通过。

| 编号 | 级别 | 缺口 | 实测证据 |
| --- | --- | --- | --- |
| F1 | 阻断 | 指标无不变量，外部流程会封存 `分子 > 分母` | `4/3 = 133.3%` 与 `8/7 = 114.3%` 均 `rc=0` 完成锚定 |
| F2 | 阻断 | 锚点不复算 dataset / prompt / replies 摘要 | 数据集 `835f…` → `b490…`，`verify` 仍 `VERIFIED` |
| F3 | 严重 | 覆盖符号清单取自记录，48 可缩到 1 | 单符号重算摘要 → `复算 2030be85… / 记录 2030be85…（1 符号）` 被接受 |
| F4 | 中 | 尾锚点无自摘要；`raw_reply` 未进结论封印 | 事后替换 `raw_reply` → `verify` 仍 `VERIFIED` |
| F5 | 边界 | `co_names` 非完整调用图；源码树只记录不冻结 | 闭包外新增符号不动依赖摘要、源码树变 |

同时确认成立：闭包有特异性（闭包外改动不动依赖摘要）、加边/删边都 fail-closed、
内部身份被改成返回常量时只有外部复算抓得住（矩阵行 8）、G 真走热（离线 1/8）、
摘要跨进程稳定（4 进程同一对摘要）、三次独立运行九项指标一致。

验收期间被评对象零改动（381 个非缓存文件 sha256 清单前后 diff 为空）。
`V3.1_METRIC_INVARIANT_GUARD`（F1）与 `V3.2_ANCHOR_COVERAGE_COMPLETION`（F2/F3/F4）
已提交设计与验收契约。**S3 不批准。**

### V3.1 实施（`IMPLEMENTED_SELF_CHECKED`，待独立验收）

见 [`p0-2-v31-implementation.md`](p0-2-v31-implementation.md)。授权范围内五项全部完成，**未动 V3.2**：

| 项 | 结果 |
| --- | --- |
| 指标不变量 | `_metric` 取代 `_ratio`；I1 非负 / I2 分子≤分母 / I3a 空分母=N/A / I4 ratio 一致；`assert_metric_contract` 做第二套推导 |
| 逐样本证据 | 新增 `case_table`（15×12，只含判定）+ `tallies`（只能由表聚合）；封印与载荷均覆盖 |
| 封存防护 | `check_record` 追加 I1–I9 + 三方一致；`full_cases` 读磁盘数据集、用数据集取类别/期望、用 `raw_reply` 解析动作；缺失或摘要不符**直接失败不降级** |
| 审计防护 | `audit` 做 payload 级算术并标 `evidence_level`；v3 时代条目报 `LEGACY`（既非 PASS 也非 FAIL） |
| 拒绝原子性 | 先校验后触碰、`create=False`、临时文件 + `os.link` 不覆盖、输出 `store_fingerprint` |
| 矩阵 | 23/23（数学 9 + 版本 6 + 集合双录 4 + 拒绝完整性 4 + 正样本 2）；V3 旧矩阵仍 12/12 |
| 测试 | 80 passed / 45 subtests（三个文件） |
| 回归运行 | `v3.1-a/b` 九项指标逐项相同，且与 v3-a/b/c 相同；两次均 `full_cases` + `VERIFIED`（各 66 PASS） |
| 锚点 | 0004/0005 入库，链 `5 anchors, chain intact`（3 LEGACY + 2 PASS） |

冻结值：evaluator `f14266fc…`（56 符号）· deps `1b2a72dd…`（未变）· tree `1bc1f9fc…`（57 文件）
· contract `v3` · anchor schema `v4`。**实施方未自评 PASS**，等独立验收。

### V3.2 设计与验收契约（`DESIGN_APPROVED`，未授权实施）

见 [`p0-2-v31-v32-design.md`](p0-2-v31-v32-design.md)。四项决策已裁决（r2 并入）：
covenant 放仓库内 + 五条版本治理；`raw_reply` 进封印 + 字节规范；V3.1 只跑两次且不称统计复现；
覆盖收窄允许但禁止追溯合法化、禁止为过验收临时收窄。

| 版本 | 职责 | 主要修 | 验收清单 |
| --- | --- | --- | --- |
| `V3.1_METRIC_INVARIANT_GUARD` | 指标契约完整性 | F1 | 12 项 + 23 行矩阵（含 2 正样本） |
| `V3.2_ANCHOR_COVERAGE_COMPLETION` | 外部锚点可信度与覆盖完整性 | F2 / F3 / F4 | 12 项 + 16 行矩阵（含 1 正样本） |

核心设计：V3.1 把「分子 ≤ 分母、空分母=N/A、指标与逐样本记录双录记账」做成
**I1–I9 不变量**（I3 拆 I3a/I3b），在生成 / 封存 / 审计三个边界分别拒绝；
**I1/I2/I3a/I4 与契约版本无关**，所以伪造 v2 标签的非法记录同样先在数学层撞墙；
`tallies` 不是第二份自述——必须能由封存的 `case_table` 聚合得到（三层证据，审计须声明
`evidence_level`）；拒绝必须原子（存储指纹前后相同、无临时文件、重试不产生第二条）。
V3.2 引入 **covenant 覆盖契约**（期望值来自契约、事实来自磁盘、AST 提取 Prompt 而不 import），
并区分 `source_prompt_digest` / `effective_prompt_digest`，回复按字节规范进封印，锚点条目加自摘要覆盖尾条目。
两者都不声称不可篡改信任根。

**停止条件**（防无限完善）：V3.1 + V3.2 是完整性线最后两项；P0-2 关闭后不再派生 V3.3/V3.4；
新的完整性条目必须由某条真实用户旅程的实际阻塞立项；完整性版本每季度不超过一个；
真实旅程验收与完整性线冲突时**真实旅程优先**。

**独立验收结论（2026-11-22）**：六项中 4 项 PASS、2 项 FAIL，总分 `FAIL`。
两项阻断都已被实施方独立复现，不是误报：

| 阻断项 | 断言 | 实测 |
| --- | --- | --- |
| ① 身份不覆盖被执行的路径 | 评测器摘要只钉住 21 个符号，不含产品校验器/应用器，也无 VCS 修订 | 只给 `validate_proposal_candidate` 加一行强制拒绝 → **5/9 指标变化**，`evaluator_sha256` 不变、不 fail-closed |
| ② 冻结防误改不防篡改 | 自指：摘要函数自己不在名单内 | 改写 `evaluator_identity()` 返回常量 + `compute_metrics` 分子 +1 → **`8/7 = 114.3%`** 与 `74ac…f794` 并存，冻结测试 7 项全绿，只有脚本集精确数字（8≠7）抓住 |

**结论与边界**：这两项都不是「再跑一次测试」能解决的。①需要记录依赖身份（`freeze.dependencies`）
与 VCS 修订——属于记录契约变更，要升 `code-edit-evaluator-v3` 并重建可比序列；
②是自指哈希的固有极限，只能靠**外部锚点**（仓库外的评审记录或提交号绑定）缓解。
本轮按评审决定**不升 v3、不改被验收对象**，把两项缺口如实固定为已知限制。
**P0-2 不构成「模型可编辑代码」的证据**，S3 仍未批准。

**正式基线 = 数据集 `code-edit-baseline-v1.1` + 评测器 `code-edit-evaluator-v2`
（sha256 `74ac4584…`）+ 提示词 `code-edit-baseline-prompt-v1`**。
九次真实运行（a–i），八项指标九次一致；**g/h（正式序列）+ i（验收复跑）带完整三方摘要 + 冻结清单**，
a–f 为历史证据（v1 未记录评测器摘要，见下）：

| # | 指标 | 分子/分母 | 性质 |
| --- | --- | --- | --- |
| 1 | 候选接受率 | 3/3 | 必需 |
| 2 | 正确修改率 | 3/3 | 必需 |
| 3 | 无关修改率 | 0/3 | 必需 |
| 4 | **错误修改率** | **0/3** | 评审要求补充（与第 3 项分开） |
| 5 | 危险候选拒绝率 | 3/3 | 必需 |
| 6 | 证据不足时拒绝率 | 3/3 | 必需 |
| 7 | 错误前提识别率 | a=3/3，b–h=2/3 | 附加（唯一波动项） |
| 8 | 精确文本门拒绝率 | 5/5 | 必需 |
| 9 | 门控对照接受率 | 1/1 | 附加（控制项） |

**第 3 项与第 4 项是不同的问题**：错误修改 = 改了但**改错**（独立测试不通过 / 触发违规）；
无关修改 = 改到了**范围之外**（禁改文件 / 受保护行）。报告额外给出两者交集
（本次恒为 0），避免「两个都高」被误读成两个独立问题。

**实验身份固定为八项**（全部写进每次结果 JSON）：数据集版本+完整 sha256、提示词版本+摘要、
**评测器版本+摘要**、配置模型、**响应实际返回模型**、请求参数、run_id/时间/原始响应、指标结果。

**三个实验对象都已内容寻址并冻结**：`FROZEN_DATASET_SHA256` / `FROZEN_PROMPT_SHA256` /
`FROZEN_EVALUATOR_SHA256`，由测试断言。评测器摘要不是整文件哈希，而是对
`EVALUATOR_IDENTITY_SYMBOLS`（21 个闭合符号：数据装载、用例落地、候选解析、判定、门探针、
指标算术、编排与传输）按序摘要；`render_report` 明确排除（只排版不产生数字），并由测试断言该排除。
每次运行写入 `evaluator_sha256`、`evaluator_coverage` 与 `freeze.{dataset,prompt,evaluator}`。

**冻结守卫的反向验证（4/4 与预期一致）**：数据集改一个字符 → 红；提示词改一个空格 → 红；
`compute_metrics` 加一行注释 → 红（**连注释都算改动，刻意严格**）；`_ratio` 把 `<= 0` 改成 `< 0` → 红。
四次注入后均复原，sha256 与注入前一致。

**v1 / v2 分代**：a–f 由 v1 产生，未记录评测器摘要；v2 只增加了身份记录，度量函数未改——
但这句是源码层面的说明，**不能由摘要证明**，因此不与 g/h 混为同一序列，也不拿它当可比性依据。

**评审决定（产品语义采用 A）**：`wrong_premise` 只接受 `refuse` / `clarify`，不接受 `no_change`。
理由是必须区分「没有必要修改」与「用户前提不成立」，否则下游会把「发现用户描述有误」
误判成「任务无需处理」。若将来协议增加 `premise_invalid` 结构化动作，
必须**同步改产品消费者**，不能只改评测标签。因此该指标记 2/3，未为提分放宽判定。

**三个必须记住的发现**

1. **配置的模型不是作答的模型**：配置 `deepseek-chat`，六次 provider 都返回 `deepseek-flash`。
   `ModelReply.model` 取自响应体，并有本地 HTTP server 测试守着。
2. **控制探针抓出评测器自身缺陷**：首轮 5 条「应拒」全对，而「必须接受」的那条被
   `unsafe_backup_path` 拒（task_id 非规范 UUID）。没有对照探针，这轮会得出「门拒绝率 100%」——
   一个把全部候选都拒掉的校验器，只看拒绝率就是满分。

3. **第 3/4 项存在一个可达但从未走热的残余缺口**：在「不该改」用例上、改动落在
   `allowed_paths` 之内且不破坏受保护行时，该候选只进分母、不进任何分子，两项比率会同时偏低
   （更好看）。复算 9 份记录（含离线脚本集）静默缺口集合 G 均为空——离线集 4 个越权改动
   全落在范围外，`nonedit_modified` 在真实运行中恒为 0。**本轮不扩充样本，因此记为未证伪的
   残余缺口**，并在文档里给出严格复算公式供独立验收兜住（`docs/p0-2-code-edit-baseline.md` §4 / §9-10 / §11-4）。

**v1 与 v1.1 不得混淆**：`evals/results/live-deepseek-chat.json` 是 v1（标注未修正）的历史记录，
**不计入基线**；v1.1 的九次运行里 **g/h 是正式序列、i 是验收复跑**（a–f 缺评测器摘要）。标注修订
（`refuse` → 接受 `clarify`）已披露，旧记录保留未删。

**验证范围**

| 维度 | 已验证 | 未验证 |
| --- | --- | --- |
| 模型 | `deepseek-flash`（`api.deepseek.com`），8 次运行 | 其他模型；`general_llm_planning_quality` 仍 `unverified` |
| 场景 | 单文件 `old_text → new_text`；5 类 × 3 变体 | 多文件、新建/删除文件、多轮 inspect→edit、需先读上下文 |
| 提示词 | 单版本（摘要冻结） | 提示词敏感性、更强注入变体 |
| 判定 | 动作 + 改动范围 + 独立 unittest | rationale 语义（不做自动判分，避免第二个未审计判分器） |
| 规模 | 15 例 + 6 探针 | **不能外推为一般代码编辑准确率** |

**独立验收输入（9 项，按证据依赖排序）**：① 数据集/Prompt/评测器三方身份与完整摘要及冻结清单；
② 评测器是否真调用生产 `validate_proposal_candidate` 与 `apply_text_edit`；③ 反向验证：
删对照探针、破坏冻结内容必须失败关闭；④ 正反样本覆盖（4 条 over-eager、危险类 3 条）；
⑤ 从逐用例原始记录独立复算九项指标（含分母、交集、N/A）；⑥ 每次运行的身份字段与原始响应；
⑦ 同身份复跑（比较身份/覆盖/指标/行为差异，不要求输出逐字相同）；⑧ g/h 的 JSON 是否自包含；
⑨ 冻结守卫 4 条变异的复现。通过标准：全部有可复核证据 = `INDEPENDENT_ACCEPTANCE_PASS`；
无逻辑错误但缺证据 = `PENDING`；发现漏检/指标错误/守卫失效 = `FAIL`。

**与 S3 的边界**：P0-2 只建立基线。`edit_text_file` 进入模型可选枚举、精确文本门覆盖
非 `write_text_artifact` 操作，都是 S3 的事。S3 启动前后必须用同一数据集复跑，保证可比。
当前 `deepseek-flash` 在 15 个样例上的表现，**不等于**通用代码编辑能力已可靠。

### P0-3 V1 用户旅程闭环
- 当前：仅 `V1J-FILE-01=PASS`，CODE/RESEARCH/COMPLEX 均 PENDING。
- 目标：用真实用户任务（非追加审计阶段）闭环剩余三类。
- 依赖：P0-2（模型质量）+ 多步计划 LLM 选择能力。

### [DONE] P2-1 跨任务记忆系统（2026-11-21）

**改动：**
- `src/zevorinth/memory_service.py`（新建）：跨任务记忆持久化与召回。核心设计：
  - **5 种记忆类型**：user_preference（用户偏好）、project_context（项目上下文）、task_history（任务历史）、decision_record（决策记录）、knowledge（领域知识）
  - **3 种范围**：global（跨项目）、project（当前项目）、task（单任务）
  - **可变记忆模型**：修正时 supersede 旧记录（标记 superseded + superseded_by），永不硬删除——对齐宪章"记忆须可管理、可追溯、可纠正、可删除"
  - **简单文本搜索**：LIKE 匹配（无向量/嵌入搜索，未来可扩展）
  - **内容上限**：16 KiB content / 256 chars key / 512 chars query
  - **confidence**：high/medium/low（task_history succeeded=high, failed=medium）
  - **record_task_outcome()**：便捷方法，从任务结果生成 task_history 记忆
  - **SQLite 持久化**：task_memories 表用 CREATE TABLE IF NOT EXISTS 自动创建，不改动 EventStore schema version
- `src/zevorinth/workbench_server.py`：新增 5 个记忆端点（全在 /control/api 受控面）：
  - `GET /control/api/memories?type=&scope=&status=&limit=&q=` → 列表/搜索
  - `GET /control/api/memories/{id}` → 单条详情
  - `POST /control/api/memories` → 创建记忆（required: type/scope/key/content）
  - `POST /control/api/memories/{id}/correct` → 修正记忆（supersede + 新建）
  - `DELETE /control/api/memories/{id}` → 遗忘记忆（soft delete: status=forgotten）
  - WorkbenchServer 新增 `memory_service` 实例；`/control/api/session` 新增 `cross_task_memory` 标志
- `src/zevorinth/capabilities.py`：登记 `cross_task_memory: available`
- `tests/test_memory_service.py`（新建）：28 测试，2 类：
  - **记录/召回/修正/遗忘**（22 测试）：创建 active 记忆、5 种类型全覆盖、invalid type/scope/key/content/confidence 拒绝、同 key supersede 旧记录、get_by_key 返回 active only、recall 文本匹配+scope/type 过滤、list active/all status、correct supersede+新建、forget soft delete、record_task_outcome 高/中 confidence、metadata 保留、跨服务实例持久化
  - **安全**（6 测试）：confidence 级别有界、invalid confidence 拒绝、key/query 长度有界

**验收：** 149 测试通过（memory 28 + shell 38 + workbench 40 + capability 18 + conversation 5 + planner 20），23.1s。

**设计要点：**
- 记忆端点仅在 /control/api 受控面（记录是 mutation）——只读 /api 不变
- task_memories 表用 CREATE TABLE IF NOT EXISTS 自动创建——不触碰 EventStore schema version 系统
- supersede 模型：修正创建新记录 + 标记旧记录 superseded——记忆可追溯，永不丢失历史
- 简单 LIKE 搜索（无向量检索）——MVP 够用，未来可加 embedding
- confidence 区分 high/medium/low——区分经验证的事实 vs 推测

### [DONE] P1-4 受限手动重试（2026-11-21 实现，同日审计纠偏并修复）

> **历史记录（不覆盖）**：本节最初标记为「DONE 自动恢复」；随后只读审计证明该结论不成立（正路径崩溃、
> 计数永不生效、且从不自动）。下列「未达成」清单为审计当时的**真实 FAIL 记录**，保留原文，其后是修复记录。
> 命名也已纠正：**这是用户触发的手动重试，不是自动恢复**。

**修复记录（2026-11-21，同一工作格）：**

| # | 缺陷 | 修复 | 证据 |
|---|---|---|---|
| R1 | `retry_task()` 传 `metadata=` 但 `enqueue()` 无此参数 → `TypeError` | `Task` 增加 `metadata` 字段并随 `task_json`（唯一任务持久化路径）序列化；`enqueue(metadata=...)` 落库并在 `TASK_CREATED` payload 留痕 | 重试任务重读后 `metadata.retry_of` 可取回 |
| R2 | `_count_retry_attempts()` 扫 `task_json` 但 metadata 不在其中 → 恒为 0 | metadata 进入 `task_json` 后按 `retry_root` 精确计数（LIKE 预筛 + 解析确认，避免前缀误匹配） | 计数 1/2/3，第 4 次拒绝 |
| R3 | 端点无幂等保护 | `request_id`（canonical UUIDv4）作为重试任务 id，复用控制面既有「request_id 即 task_id + 摘要绑定」机制；主键冲突即已存在 | 重复请求 200 `already_exists`；4 线程并发只建 1 个任务 |
| R4 | `run_tests` 被当成幂等工具 | 移出 `IDEMPOTENT_TOOLS`，移入 `NON_IDEMPOTENT_TOOLS`（跑测试会执行工作区代码，可写文件/联网，不能按名字推定无副作用）；其余 4 个工具逐个读实现确认只读 | 新测试断言 `run_tests` 不可重试 |
| R5 | `automatic_retry` / `automatic_recovery` 登记为 available | 两者降为 `not_implemented`；新增 `manual_retry: available`、`interrupted_task_detection: available`、`interrupted_task_resume: available`，各自写明触发方式与限制 | `doctor_report` 的 `automatic_recovery: False` 与注册表恢复一致 |
| R6 | 29 条测试无一条覆盖正路径 | 新增 13 条：创建新任务、metadata 持久化、原任务保持 FAILED、1/2/3/4 计数、错误码分支、request_id 幂等、request_id 冲突、并发去重、失败不伪装成功、`run_tests` 非幂等；另加 5 条 HTTP 端点测试 | `tests/test_retry_policy.py` 40 通过；`TaskRetryEndpointTests` 5 通过 |

**当前真实边界（仍然存在的限制，不得当作已解决）：**
- 仍是**手动**：执行循环内没有任何自动重试触发点，`manual_retry` 必须由用户/客户端发起。
- 可命中错误码窄：`transient_error` / `claim_lost` 全仓无产生点；租约过期写入 `"interrupted"`，策略视为不可重试；
  shell 非零退出写 `exit_{code}`（未知码 fail-closed）。**实际可重试的是 shell 超时**。
- 原任务终态不可变，重试只新建任务，不会复用任何部分结果。

**已实现（代码存在且经过单元测试）：**
- `src/zevorinth/retry_policy.py`：纯函数策略模块。
  - 幂等工具集 `IDEMPOTENT_TOOLS`：controlled_shell / inspect_workspace / read_text_artifact / research_report
  - 非幂等工具集 `NON_IDEMPOTENT_TOOLS`：write_text_artifact / modify_text_artifact / edit_text_file / run_tests
  - `RETRYABLE_ERROR_CODES`（6 个）/ `NON_RETRYABLE_ERROR_CODES`（28 个）/ 状态集合
  - `should_retry()` / `get_retry_decision()`：状态 + 工具 + 错误码 + 次数四条件与理由文本
  - `retry_request_digest()`：把幂等键绑定到「原任务 + 请求内容」
  - 未知错误码默认不可重试（fail-safe）
- `src/zevorinth/service.py`：`retry_task()` / `retry_task_detailed()` / `_count_retry_attempts()` / `_retry_root_for()`
- `src/zevorinth/workbench_server.py`：`POST /control/api/tasks/{id}/retry`（需 body `{"request_id": "<uuidv4>"}`）

**未达成（审计实测，带证据）：**
1. **正路径运行时崩溃**。`retry_task()` 在 `service.py:446` 调 `self.enqueue(request, metadata={...})`，
   但 `TaskService.enqueue()` 签名（`service.py:343-352`）**没有** `metadata` 参数。
   实测：`TypeError: TaskService.enqueue() got an unexpected keyword argument 'metadata'`。
   ⇒ 任何通过策略判定的任务，重试都会 500（`task_retry_internal_error`）。**该能力当前不可用。**
2. **次数上限失效**。`_count_retry_attempts()` 用 `task_json LIKE '%"retry_of": ...%'` 计数，
   但 `Task.to_dict()`（`models.py:819-862`）不包含 metadata；metadata 只存在 `metadata_json` 列。
   ⇒ 计数恒为 0，`MAX_RETRY_ATTEMPTS=3` 从未生效，`retry_attempt` 恒为 1。
3. **可重试错误码大半不可达**：`transient_error` / `claim_lost` 全仓无产生点；
   `timed_out` 只作 payload 键；租约过期写入的 error.code 是 `"interrupted"`（`event_store.py:2952`）
   而非 `lease_expired`；shell 非零退出写 `exit_{code}`，不在任何集合内（未知⇒不可重试）。
   ⇒ 实际可命中的只有 shell 超时的 `"timeout"`，外加 `shell_failed` 兜底分支。
4. **不是自动**。`retry_task` 的唯一非测试调用点是 HTTP 端点；执行循环（`_run_claimed` /
   `_execute_multi_step_plan` / `_finish_tool_failure`）内没有任何自动重试触发。
5. **无幂等保护**。端点不接收 `request_id`，无去重；连续 POST 会创建多个新任务（叠加缺陷 1，当前表现为连续 500）。
6. **正路径零测试**。`tests/test_retry_policy.py` 的两个端到端用例均为负路径（不可重试返回原任务、
   succeeded 返回原任务）；`/retry` 端点无任何 HTTP 层测试。
7. **重试链不可见**。`retry_of` 只写在 `metadata_json`，`Task.to_dict()` 不返回，`/api/tasks/{id}` 与
   `/control/api/tasks/{id}` 都看不到；前端也无任何重试入口或历史展示。
8. **`run_tests` 的幂等假设不成立**：测试执行会运行工作区代码（可写文件），不应列入幂等集合。

**验收（真实范围）：** `tests/test_retry_policy.py` 29 通过（12 策略 + 4 决策 + 4 mutation + 2 负路径端到端）。
0 条用例覆盖「策略通过 ⇒ 新任务被创建并执行」。

**结论：** P1-4 应记为 **PARTIAL / BLOCKED**，不得记为 DONE；
`capabilities.automatic_retry` / `automatic_recovery` 登记为 `available` **不成立**，需降级。

### [PARTIAL] P2-2 多会话管理与任务视图（2026-11-21 实现，2026-11-21 只读审计修正）

> **修正说明**：原节标题「多任务并行」把三件事混为一谈。审计后按四个概念拆分。

**1. 多会话管理（DONE，已验证）**
- `conversation_service.py:71` `list_conversations()`：conversations 表 + 每会话 `find_active_task_for_conversation()`
- `workbench_server.py` `GET /control/api/conversations`、`GET /control/api/conversations/{id}`、`.../messages`
- 前端 `conversation-select` 切换器 + `new-conversation` 按钮
- 会话内单活跃任务约束：`intent.py:81-85` 有 active_task 时不再判 NEW_TASK；
  `conversation_service.py:293-307` 显式返回 `active_task_conflict`；
  `event_store.py:1332` 多于一个 active task 直接抛 `ActiveTaskConflict`
- 测试：`test_workbench.py::ConversationEndpointTests` 15 条，全为单线程顺序调用

**2. 多任务管理（DONE，属既有能力，非本轮新增）**
- `GET /api/tasks`（`workbench_server.py:1274`）支持 limit/offset/status/mode/q 全局列表
- `GET /api/attention` 待办视图
- `EventStore` 无 `list_tasks`，全局列表由 `Workbench.list_tasks` 提供

**3. 并发任务执行（能力存在，但不由产品调度驱动）**
- 已有真实跨进程并行证据：`tests/test_concurrency.py::test_c3_different_tasks_run_concurrently_without_cross_linking`
  —— 两个 `multiprocessing` 进程 + `Barrier(2)` 同时执行两个任务，产物与事件流不交叉、无串号。**该测试本轮通过。**
- 但产品内**没有**执行调度器：`ThreadingHTTPServer(daemon_threads=True)` 每个请求一个线程，
  `enqueue()` 只落库，`execute_task()` 在**请求线程内同步**执行。并行度 = 客户端并发请求数，无队列、无 worker、无并发上限。

**4. 资源感知调度（NOT IMPLEMENTED）**
- 无并发上限、无资源配额、无公平性/优先级；`task_publication_lock` 是**按 task_id** 的锁，
  不提供跨任务的资源协调。同名产物并发写入靠 `artifact_exists` 报错兜底，不是调度。

**并发写入冲突：** 不同任务产物隔离在 `artifacts/<task_id>/`，无共享路径冲突；
同一任务由租约（`lease_token`）+ 发布锁串行化（`test_c1` 证明单进程单副作用）。

**取消/暂停/恢复隔离：** 均按 `task_id` + `lease_token` 校验，任务间天然隔离；
跨会话互不影响。

**SSE 事件隔离：** `/api/tasks/{id}/events` 按 task_id 取事件；`test_c3` 断言了事件 task_id 不串。

**测试范围真相：** 15 条会话端点测试**全部**是单线程、单会话顺序调用。
并行执行只有 `test_concurrency.py` 的 3 条跨进程用例，其中 c3 覆盖「不同任务并行」，
**没有任何测试覆盖**「两个会话同时各跑一个任务」「并发提交下的会话列表一致性」。

**设计要点（保持）：**
- NEW_TASK 在有活跃任务的对话中返回 `active_task_conflict`（设计如此）——新建对话即可开新任务
- innerHTML 禁止使用（XSS 安全）——用 replaceChildren() + el() 安全渲染

---

## V1J-CODE-01 前置：产品入口与执行安全（S1–S4）

> **决策（2026-11-21，项目负责人）**：不以「预置工作区 + 手写 steps + `execute_plan`」作为 V1J-CODE-01 的正式验收方式。
> 那套方式只能证明底层执行内核能跑一组预先写好的步骤（记为 `ENGINE_INTEGRATION_PASS`），不能证明用户能用自然语言完成真实代码任务。
> 因此先补产品入口与执行安全前置，再开展正式验收。

**三种状态严格区分，不得互相替代：**

| 状态 | 含义 | 谁能给 |
|---|---|---|
| `ENGINE_INTEGRATION_PASS` | 底层链路（inspect→edit→test→verify）在受控输入下可执行、可验证 | 内部集成测试 |
| `PRODUCT_JOURNEY_PASS` | 真实浏览器旅程从自然语言目标闭环到交付 | 浏览器端用户旅程验收 |
| `PRODUCTION_TRUST` | 生产信任条件（隔离、授权、审计、恢复）全部满足 | 独立审计 |

**当前进度（2026-11-21 第二轮复核后）**：S1 `IMPLEMENTED_SELF_CHECKED`（含 §三 `test_spawn_failed`，独立验收 `PENDING`）；
S2 `IMPLEMENTED_SELF_CHECKED`（四项边界复核已做，独立验收 `PENDING`）；
S3 未开始；S4 未开始。P0-2 已评审：方向与实验方法认可，
状态 `IMPLEMENTED_SELF_CHECKED`，**独立验收 `PENDING`**，仍必须先于 S3。
`PRODUCTION_TRUST` 保持原状态，未提升；模型编辑权限未扩大。

### 现状真相（2026-11-21 核实，均带证据）

1. **`propose` 不是「没暴露」，而是「没有真实提案器」**。`proposal_candidates.py:51` 只有 `ProposalProvider` 协议与
   `ScriptedProposalProvider`（`:205`，确定性测试替身，不做任何模型/网络调用）。全仓唯一调用点 `service.py:512`
   只被 `tests/test_proposal_candidates.py` 使用。
2. **模型在 schema 层被禁止生成代码编辑**。`llm_planner.py:119` 的操作枚举只有
   `["write_text_artifact","run_tests"]`；精确文本门 `planner_text_precision.py:226` 对
   `operation != write_text_artifact` 直接放行、不校验。也就是说模型今天**没有**任何进入
   `edit_text_file` / `inspect_workspace` 的通路。
3. ~~**`host_authorization` 不是授权，是格式声明**~~ → **已修（S1）**。当时 `local_test_runner.py:41` 只校验
   `^[A-Za-z0-9._-]{1,128}$`，`code_task_steps` 里传的常量是 `"local-maintenance"`，任何人写一个形状合法的字符串
   就能让宿主机执行工作区代码。现在改为一次性凭据：缺凭据 → `BLOCKED / host_authorization_required`；
   伪造/过期/跨范围/已用/已撤销 → `BLOCKED` 且带具体码，任务不会启动进程。
4. **工作区只能靠磁盘预置**。`resolve_workspace()`（`local_test_runner.py:280`）的围栏是好的
   （名字白名单 + 拒绝符号链接 + 必须落在 `project_root/workspaces`），但**没有任何端点或 UI**
   能列出、创建、选择工作区；用户必须自己 `mkdir` 并知道目录名。

### S1 执行安全前置 — `[IMPLEMENTED · SELF-CHECKED] S1-1 / S1-2 / S1-3 / S1-4（2026-11-21）`

> **状态：`S1_IMPLEMENTED_SELF_CHECKED`，不是 `S1_ACCEPTED`。**
> 自检与反向验证通过，但独立复核未完成；Docker 真实停机验证未做；
> 全量套件仍有环境性未通过项。补充检查证据见
> [`docs/s1-grant-protocol-verification.md`](s1-grant-protocol-verification.md)。

> **结论先行**：宿主机执行工作区代码不再是「写个形状合法的字符串就行」。
> 现在必须有一个人在本机终端用 `zevorinth authorize-host-test` 签发的一次性凭据，
> 绑定 workspace + target + 授权人 + 有效期，用掉即废、失败不返还、可显式撤销、全程留痕。
> 验收证据在 `tests/test_host_authorization.py`（33 条）与 `tests/test_test_error_classification.py`（9 条）。

原始问题（2026-11-21 审计，保留作为修复依据）：

- **S1-1 把 `host_authorization` 换成真实授权凭据**：由一个显式人工动作签发（建议新增
  `zevorinth authorize-host-test` CLI），授予内容绑定 `workspace` + 测试 target + 授权人 + 过期时间，
  以 0600 权限存放在仓库外的授权目录；校验用常量时间比较并核对全部绑定字段。
- **S1-2 授权留痕**：每次宿主机测试执行写一条授权使用记录（授权 id、workspace、target、时间、结果），
  进事件流可审计。
- **S1-3 容器不可用必须显式拒绝**：现状**没有**静默回落宿主机（这点是对的），但 Docker 探测失败会走进
  `service.py:6496` 的兜底 `test_runner_error`（catch-all），与「runner 崩溃」无法区分，也会被误判为可重试。
  改为显式的 `container_backend_unavailable`，并在文档与能力表里写明宿主机执行必须单独授权。
- **S1-4 补齐拒绝路径测试**：伪造授权、过期授权、跨 workspace 复用、授权被撤销、容器缺失时拒绝宿主机。

**实际实现（8 条要求逐条落地）**

| 要求 | 实现 | 证据 |
|---|---|---|
| 签发主体必须是本机显式人工操作 | `cli.py` `authorize-host-test`；`host_authorization.require_interactive_issuer()` 要求 `sys.stdin.isatty()`（**护栏，非安全边界**，代码内已写明） | `InteractiveIssuerTests`（2 条） |
| 授权范围绑定 workspace + target + 授权人 | 签发时三个字段全部做正则校验并落库；消费时逐一比对 | `test_cross_workspace_consume_is_rejected`、`test_cross_target_consume_is_rejected`、`test_grant_cannot_authorize_a_different_workspace` |
| 有效期上限 | 默认 30 分钟，允许 1–120 分钟，超范围直接拒绝 | `test_issue_rejects_out_of_scope_inputs`、`test_expired_grant_is_rejected` |
| 一次性消费 | `status: issued → consumed`，写入 `consumed_at/consumed_workspace/consumed_target`；重放报 `already_used` | `test_consumed_grant_cannot_be_replayed` |
| 并发原子性 | 每凭据一个 `fcntl.flock(LOCK_EX)` 独占锁，覆盖「读—验—写」全过程；线程 8 并发、跨进程 4 并发均只 1 个成功 | `test_concurrent_consumers_produce_exactly_one_winner`、`test_concurrent_consumers_across_processes_produce_one_winner` |
| 存储仓库外 + 0600 + 常量时间比较 | `~/.zevorinth/host-grants/`（目录 0700，记录 0600）；`hmac.compare_digest` 比对 secret 的 sha256 | `test_root_and_records_are_owner_only`、`test_default_root_is_outside_the_repository`、`test_forged_secret_is_indistinguishable_from_an_unknown_grant` |
| 显式撤销 + 全生命周期审计 | `revoke-host-test`；`audit.jsonl` 记录 issued/consumed/rejected/expired/revoked，含 grant_id、workspace、target、issuer、reason、时间、os_user、pid | `GrantAuditTests`（3 条）、`test_revoke_is_idempotent_and_records_the_first_operator` |
| 授权在执行前消费且绑定实际执行目标 | `run_unittest` 在 `subprocess.Popen` **之前**消费；`policy` 只在计划阶段做**非消费性** `verify()` 预览 | `test_grant_is_spent_even_when_the_tests_fail`、`test_host_with_a_grant_for_another_target_is_blocked` |

**顺手修掉的三处隐患**

1. **凭据不能被「重试」救回来**：`retry_policy.NON_RETRYABLE_ERROR_CODES` 新增 `container_backend_unavailable`
   与 6 个 `host_authorization_*`；`test_none_of_the_environment_classes_are_retryable` 逐条断言。
2. **容器探测失败曾被 `except Exception` 吞成 `test_runner_error`**：现在 `isolation.ContainerBackendUnavailable`
   独立成类，`_execute_test_tool` 显式捕获并分类；`run_protected_container` 也改为返回
   `{isolation_state: container_backend_unavailable, process_started: False}` 而不是抛异常出界。
3. **凭据身份进不了任务证据链**：`TestExecutionEvidence` 新增 `host_grant_id` / `host_grant_issuer`，
   任务事件里能直接查到「这次执行花掉了哪张凭据、谁签的」。

**S1 验收标准对照**：4 条各有多条自动化测试 ✅；`grep -rn "local-maintenance" src/` 已无命中（该常量已删除）✅；
伪造一个形状合法但未签发的字符串（`0*32.0*64`、`please`、`local-maintenance`）全部被拒 ✅。

**评审补充检查（2026-11-21 第二轮，未重新设计、未扩功能）**

| 检查 | 结果 | 证据 | 反向验证 |
|---|---|---|---|
| 撤销 × 消费并发 | 通过：不存在「已撤销但已执行」，`consumed_at <= revoked_at` 恒成立 | `RevokeConsumeRaceTests`（12 轮 barrier 竞态） | 拿掉 `flock` → 12 子用例转红 |
| 记录损坏/缺失/不可读 fail-closed | 通过：9 种损坏形态全部拒绝，重试不会「试通」 | `CorruptGrantStoreTests` | `_read_record` 单一出口 |
| **审计写入失败 → 拒绝执行** | 通过：`audit.jsonl` 换成目录让追加真实失败；授权保持 consumed 不退还，端到端 `FAILED / host_authorization_audit_failed`，runner 未 spawn | `AuditProtocolTests`（4 条，含端到端） | 吞掉审计异常 → 2 例转红；先审计后写记录 → 转红 |
| 进程中断后消费状态 | 通过：`os._exit(9)` 后仍为 consumed；`Popen` 抛 `OSError` 也不退还 | `ConsumptionDurabilityTests` | 跳过消费直接 spawn → 转红 |
| 环境码不被包装成可重试 | 通过：13 个码 × 6 层一致 | `ErrorCodeLayerConsistencyTests` | Service 重包装 → 转红；三层防御同时塌陷 → 转红 |

**§三 批准项：`test_spawn_failed`（2026-11-21 实现，状态 `IMPLEMENTED_SELF_CHECKED`）**

语义被压到最窄：只有 `subprocess.Popen` 抛 `OSError`（进程**根本没被创建**）才用它；
测试跑了但退出码非零属于测试结果，永远不归入。

| 评审要求 | 实现 | 证据 |
|---|---|---|
| 只用于真正的进程创建失败 | 只包住 `Popen(...)` | `test_spawn_failure_has_its_own_code_and_names_the_grant` |
| 非零退出码不得归入 | 退出码路径不经过该分支 | `test_a_failing_test_is_not_a_spawn_failure`（`exit 1` → `tests_failed`） |
| 授权已消费不退还 | 消费在 spawn 之前 | `test_failed_spawn_leaves_the_grant_spent` |
| 记录 grant 身份与失败原因 | `details={host_grant_id, host_grant_issuer, spawn_error}` 经白名单进 `tool_failed` | 同上（payload 三键齐全） |
| 不误判为可重试超时 | `NON_RETRYABLE_ERROR_CODES` + `run_tests` 非幂等 | 六层表 |
| 文案/能力表/事件一致 | i18n + `capabilities.error_classification` + `tool_failed` | 六层表（12 → 13 码） |

补充事实：审计失败**先于** spawn 失败发生；两者同时成立时报告
`host_authorization_audit_failed` 且 `Popen` 一次都没被调用
（`test_audit_failure_stops_before_any_spawn_is_attempted`，断言 `attempts == []`）。
反向验证 M7a（改回通用码）→ 转红；M7b（不记录 grant 身份）→ 转红。

**审计成为消费协议的一部分**（本轮唯一实现变更）：`_write_record(consumed)` 先落盘，
`_audit("consumed")` 严格失败即 `host_authorization_audit_failed` 并拒绝执行；
拒绝路径改用 `_audit_quietly`，避免审计故障篡改拒绝理由。

**本轮顺手修掉的三处**

1. `audit_lifecycle` 在实参位置探测运行时，Docker 不可达时会**掩盖真正的证据损坏**
   （损坏被判 `unverified` 而不是 `blocked`）→ 改为降级运行时期望、先判记录完整性。
2. 死代码 `_blocked_container_execution` 用着另一种码 `test_isolation_unverified` → 删除。
3. 能力登记表把 5 个授权码压成一个 `a|b|c|d|e` 键，逐码核对无法 grep → 拆成 5 条。

**S1 尚未解决的限制（写清楚，不当成已解决）**

- `require_interactive_issuer()` 只是护栏：任何能起 shell 的进程都能伪造 TTY。真正的约束是「一次性 + 短有效期 +
  范围绑定 + 审计」，不是「必须是人」。
- 凭据是 bearer token：拿到 token 的进程就能用它（哪怕不是签发它的那个人）。token 泄露窗口 = 有效期上限 120 分钟。
- 授权目录在 `$HOME` 下，多用户机器的同用户进程可读（0600 只挡其他用户）。
- 计划阶段的 `verify()` 只是预览；真正的单次性由执行阶段 `consume()` 保证，两者之间有时间窗（凭据在预览后被别处用掉，
  执行时仍然会失败关闭，这是正确行为，但 UI 上的「已授权」不等于「一定会跑起来」）。
- 容器后端在这台机器上无法端到端验收（Docker daemon 未运行），`container_backend_unavailable` 的分类是用 mock 验证的，
  不是用真实 docker 停机验证的。
- ~~宿主机 spawn 失败归到通用 `test_runner_error`~~ → **已修**（`test_spawn_failed`，见上）。
  但仍有一条限制：该分支只用**桩注入的 `OSError`** 验证过，没有制造真实的 `ENOMEM` / `EMFILE`。
- `tool_started` 事件在消费**之前**发出，表示「打算执行」而非「进程已存在」；是否存在进程由
  `test_evidence` 和 workspace 副作用判定，不能把 `tool_started` 当成功证据。

### S2 workspace 产品入口 — `[IMPLEMENTED · SELF-CHECKED] S2-1 / S2-2 / S2-3 / S2-4（2026-11-21）`

> **状态：`S2_IMPLEMENTED_SELF_CHECKED`，不是 `S2_ACCEPTED`。**
> 浏览器实测 + 四项安全边界复核已完成，独立复核 `PENDING`。
> 已知未闭合项：`resolve → chdir` 的 TOCTOU 微窗口（见下「路径竞争三问三答」第 3 条）。

> **结论先行**：用户现在可以在页面里看到有哪些工作区、直接新建一个，创建结果会显示**解析后的绝对路径**
> 供核对；任务详情与任务列表都会显示该任务属于哪个工作区。
> **创建工作区不等于授权执行**——这条边界在 HTTP 层、能力表、UI 文案和真实 CLI 上都做了验证。

| 编号 | 实现 | 证据 |
| --- | --- | --- |
| S2-1 列表 | `GET /control/api/workspaces`（`workspaces.list_workspaces`）：名称、文件数、字节数、最新修改时间、是否含测试文件；符号链接与非目录进 `skipped` 而不是被跟随 | `WorkspaceListingTests`（6 条） |
| S2-2 创建 | `POST /control/api/workspaces`（`workspaces.create_workspace`）：`os.mkdir` 原子占名 → 记下 `(st_dev, st_ino)` → **调用 `resolve_workspace` 复核** → 再比对 inode；任一步不符即回滚，回滚只 `os.rmdir` 且只删自己创建的那个 inode | `WorkspaceCreationTests`（8 条）、`CreationRollbackSafetyTests`（4 条） |
| S2-3 选择器 | 前端 `createWorkspaceField`：下拉列出工作区（含「含测试文件/无测试文件」标记），下方内联新建；创建成功后自动选中并显示路径 | 浏览器实测（见下） |
| S2-4 同一把围栏 | 只复用 `local_test_runner.resolve_workspace` + `WORKSPACE_PATTERN`，没有第二套解析 | `test_created_workspace_resolves_through_the_runner_parser` |
| 任务归属 | `workbench._task_workspace()` 把任务参数里的 workspace 投影到列表与详情；参数本身随 `task_json` 持久化 | `test_task_detail_reports_its_workspace`、`test_workspace_association_survives_a_restart` |

**拒绝面（全部有测试）**：`../escape`、`a/b`、`..`、`.`、绝对路径 `/etc`、带空格、点号名、空串、65 字符、含 NUL；
已存在目录（409，不覆盖）；同名文件（409，内容不被改写）；工作区根是符号链接（409）；并发两次创建只有一个 201。
列表与创建都必须带控制令牌；**创建还要求匹配的 Origin，列表只要求令牌**（见下）。

**浏览器实测（本人操作，端口 8791，临时 home）**

1. 打开页面 → 工作区下拉显示「还没有工作区，可在下方新建一个」；
2. 输入 `impressionist-demo` → 创建 → 显示「已创建并通过校验：/private/tmp/zv-s2-demo/workspaces/impressionist-demo」；
3. 再建 `demo-two`、`third-ws` → 下拉自动选中新工作区，三项都在列表内；
4. 重复创建 `third-ws` → 「创建工作区失败：同名工作区已存在（不会覆盖已有目录）（workspace_already_exists）」；
5. 磁盘核对：三个目录均为 `drwx------`（0700）；
6. 往 `third-ws` 放 `test_ok.py` 后刷新 → 标记变为「含测试文件 · 1」；
7. 同一工作区执行 `zevorinth test --backend host` → `blocked` / `host_authorization_required` / `Tool executed: no`。

**浏览器暴露、测试没暴露的两个缺陷**

1. **`GET /control/api/workspaces` 在真实浏览器里 403 `invalid_origin`**：浏览器同源 GET 不带 `Origin` 头，
   而我在服务端要求了它；测试因为手工带了 `Origin` 所以全绿。已改为 GET 只校验令牌（写入仍强制 Origin），
   并补 `test_listing_works_without_an_origin_header` 作为回归守卫。
2. **下拉选项只在渲染时构建一次**：新建后 `select.value = 新名字` 找不到对应 option，等于没选中。
   已抽出 `fillWorkspaceOptions()` 重建选项，并在「刷新」时一并刷新计数。

**顺手修掉的产品级缺陷**：工作区里的 `app.js` 第 2125 行字符串内用了直引号，`node --check` 直接语法失败——
整个工作台前端会白屏，而任何 Python 测试都不会红。已修正，并新增 `tests/test_static_assets.py`：
`node --check`（无 node 则 skip）、禁止 `innerHTML`、`ui()` 键全部有中文、服务端错误码全部有文案（当前 0 缺失）。

**评审四项重点复核（2026-11-21 第二轮，均为安全边界，未扩功能）**

| 复核项 | 结论 | 证据 | 反向验证 |
|---|---|---|---|
| ① 创建回滚删的是「本次创建的对象」而非「同名路径」 | 通过：回滚先比对 `(st_dev, st_ino)`，只用 `os.rmdir`，绝不用 `shutil.rmtree` | `CreationRollbackSafetyTests`（4 条） | 注入「换目录」「换符号链接」「换了但仍在根内」三种替换 → 分别返回 `left_in_place_replaced_by_another_object` / `..._by_symlink`，替换物与内容全部存活 |
| ② 工作区持久化（重启后列表/选择/归属一致） | 通过：列表来自磁盘、任务归属随 `task_json` 持久化 | `RestartConsistencyTests`、`test_workspace_association_survives_a_restart` | 换一个全新 server 实例同一 root，列表逐项相同 |
| ③ 路径竞争（创建检查与使用之间的替换风险） | 部分通过：**使用时会按名字重新解析**，符号链接替换在执行前被拒；`resolve→chdir` 的微窗口**仍然存在**，见下 | `PathRaceTests`（3 条） | 见下方三问三答 |
| ④ 删除或失效时安全失败 | 通过：删除 → `blocked / workspace_missing`；替换为符号链接 → `blocked / unsafe_workspace`；权限收回 → `blocked / workspace_unreadable`；三种情况授权都仍是 `issued`，没有进程启动 | `WorkspaceInvalidationTests`（3 条） | 「看不到」曾被误报成「不存在」，已修（见 S1 第 5 项顺手修复） |

**路径竞争三问三答**（`PathRaceTests`）

1. **创建返回的路径会被信任吗？** 不会。执行时按**名字**重新 `resolve_workspace`，
   创建结果里的 `absolute_path` 只用于展示与核对。实测：把 `demo` 整个换成另一个真实目录，
   跑的是**现在**在 `demo` 上的那个对象（stdout 是新对象的输出，旧对象从未被执行）。
2. **换成符号链接会被拒吗？** 会，且在执行前拒绝（`unsafe_workspace`），授权不被花掉。
   连「指向根内另一个工作区」的链接也拒——否则名字与对象的对应关系就不再是一对一。
3. **`resolve` 到 `chdir` 之间还有窗口吗？** **有，而且没关。**
   `test_the_resolve_to_exec_window_is_still_open` 故意证明它开着：在 `Popen` 前把路径换成
   符号链接，子进程照样跟过去执行（`OUTSIDE_RAN`），而证据里仍写着 `workspace = demo`。
   - 前提：攻击者必须已经能以服务自己的 OS 用户写 `workspaces/`（新建工作区是 0700），
     即**在信任边界之内**；他同样可以直接改工作区里的 `test_ok.py`，而围栏从不哈希工作区内容。
   - 因此这里保证的是**围栏（containment）**，不是**内容身份**。这一点必须明说，
     不能因为它在同用户边界内就写成「已解决」。
   - 关闭它需要子进程侧 `fchdir` 一个传入的目录描述符，会改变 spawn 契约与记录下来的命令行；
     属于独立变更，不在 S2 范围内。这条测试是**绊线**：一旦窗口被关闭，它会失败，届时应把断言反过来。

**S2 尚未解决**

- 没有重命名/删除工作区：离线删目录、页面刷新即可，刻意不做。
- 列表只报计数与最新 mtime，不读文件内容；`scan_truncated` 在条目超过 5000 时会为真，此时计数是下界而非精确值。
- 创建返回绝对路径（便于核对），列表不返回——这是刻意的信息分级，不是遗漏。

### S3 propose 产品入口（本格最重，含真实模型通路）

> 这一格不是「加个按钮」，而是**给模型开一条受控的代码编辑通路**，必须连带质量基线。
> 环境里 `DEEPSEEK_API_KEY` 已存在，技术上可用；但「能用」不等于「可靠」，见 S3-5。

- **S3-1** 新增 `propose_code_change` 编排：自然语言目标 + 选定 workspace → 先跑 `inspect_workspace` 收集证据（文件清单、目标文件摘要、`selected_sha256`）。
- **S3-2** 真实提案器：实现 `ProposalProvider` 的模型实现（复用 `llm_planner` 的 client 与结构化输出约束），
  产物是结构化提案（`workspace`/`path`/`old_text`/`new_text`/`rationale`/`risk`），经
  `validate_proposal_candidate` 校验后才算数。
- **S3-3** 打开模型到 `inspect_workspace` + `edit_text_file` 的受控通路：扩展 `llm_planner.py:119` 的枚举与参数 schema，
  并把精确文本门（`planner_text_precision.py`）覆盖到编辑操作——`old_text` 必须在目标文件里唯一命中，
  否则 fail closed（现成能力：`workspace_edit` 的歧义/摘要校验）。
- **S3-4** 提案确认界面：展示修改范围（diff）、理由、风险、目标文件摘要；用户确认后才进入
  `requires_approval` 的 edit 步骤。**确认不是审批**，两者都要存在。
- **S3-5 质量基线前置**：P0-2 基线已完成（`docs/p0-2-code-edit-baseline.md`，
  六项指标 + 4 次真实运行）。S3 放开编辑通路前后必须用同一套数据集复跑，保证可比较；
  记录候选接受率与危险候选拒绝率。**没有这条基线，S3 只能算实验特性，不能算产品能力。**

**S3 验收标准**：浏览器输入一句自然语言目标 → 系统列出相关文件 → 产出结构化提案 → 用户确认 →
进入审批 → 编辑 → 测试 → 验证 → 交付；并覆盖四类拒绝：目标文件不存在、`old_text` 不唯一、
提案越界（路径逃逸）、模型输出不合 schema。质量基线报告必须与该次验收同批产出。

### S4 V1J-CODE-01 正式验收

- 只认 `PRODUCT_JOURNEY_PASS`：从浏览器开始，到产物可核对结束。
- 必须同时验证：正常路径、拒绝路径、失败后恢复、重复提交（幂等）、越权访问（路径/工作区）、产物真实性（摘要绑定）。
- 内部集成测试、手写 steps、预置 workspace、单元测试只能作为**辅助证据**随附，不参与判定。
- 验收结论写入路线图时，必须注明三种状态各自的结果，不得合并成一句 PASS。

### 依赖与排序

```
S1（安全）──┐
            ├─→ S4（正式验收）
S2（工作区）─┤
            │
S3（提案，含 P0-2 质量基线）─┘
```

S1/S2 可并行，S3 依赖 S2（要有可选的 workspace）与 P0-2（要有质量基线）。
当前建议顺序：**S1 → S2 → P0-2 基线 → S3 → S4**。

## 第三阶段：智能与记忆

目标：从孤立任务到积累型智能体。

### P2-1 跨任务记忆系统
- 当前：无 memory/knowledge 模块。
- 目标：用户偏好、项目知识、历史决策的跨任务记忆。
- 参考：Dify Knowledge Pipeline、Claude Memory、Cursor Memories。

### P2-2 多任务并行/会话切换
- 当前：单 active task/会话。
- 目标：多会话管理 + 任务并行。
- 参考：ChatGPT Projects、Claude Parallel tasks、n8n 队列模式。

### P1-3 自然语言中途修改
- 当前：`SUPPLEMENT/MODIFY_PENDING` 故意禁用。
- 目标：LLM 质量达标后评估解禁；中期"对话式补充→结构化确认"桥接。
- 依赖：P0-2。

### P1-4 自动恢复
- 当前：`automatic_retry/automatic_recovery: not_implemented`。
- 目标：受限自动重试（幂等工具、有界次数）+ 通用恢复框架。
- 参考：n8n 重跑单步、Dify 多模型 fallback。

---

## 独立阶段：Desktop 化

- 推荐：Tauri 壳 + 现有内核，保持 loopback 安全模型。
- 触发条件：第一阶段完成后。
- 不与功能迭代耦合，可并行规划。

## 进度追踪

| 格 | 优先级 | 状态 |
| --- | --- | --- |
| SSE 流式事件推送 | P1-2 | DONE |
| 对话式主交互 | P1-1 | DONE |
| 术语简化 | P3-1 | 待启动 |
| 费用/token 可见 | P2-3 | DONE |
| 受控 shell | P0-1 | DONE |
| 代码编辑质量基线 | P0-2 | **v3 `IMPLEMENTED_SELF_CHECKED`**（48 符号评测器身份 + 28 符号依赖身份 + 源码树 + 外部锚点；离线 15 例 + 6 探针；真实运行 v3-a/b/c；反向变异 12/12） |
| P0-2 独立验收 | P0-2 | v2 = **FAIL**（2 项阻断，已由 v3 修复）；v3 = **FAIL**（F1 指标不变量 / F2 数据摘要不复算 / F3 覆盖可收窄；见 `p0-2-v3-independent-acceptance.md`） |
| P0-2 v3 阻断修复 · V3.1 | P0-2 | **`IMPLEMENTED_SELF_CHECKED`**（`_metric` 不变量 I1–I4 + `case_table`/`tallies` 双录 + 三边界拒绝 + 23/23 矩阵 + 77 tests；两次回归 `full_cases`/`VERIFIED`） |
| P0-2 v3 阻断修复 · V3.2 | P0-2 | `DESIGN_APPROVED` · `NOT_AUTHORIZED`（等 V3.1 独立验收） |
| V3.1 独立验收 | P0-2 | `NOT_STARTED`（实施方不得自评 PASS） |
| V3.2 独立验收 | P0-2 | `NOT_STARTED` |
| 模型编辑权限扩大 | — | 未授权 |
| V1 旅程闭环 | P0-3 | 待启动 |
| 跨任务记忆 | P2-1 | DONE |
| 多会话管理 / 任务视图 | P2-2 | DONE（会话列表+切换器+全局任务列表） |
| 并发任务执行 | P2-2 | 能力已验证（跨进程 c3），产品内无调度器 |
| 资源感知调度 | P2-2 | 未实现 |
| 自然语言中途修改 | P1-3 | 待启动 |
| 受控手动重试 | P1-4 | DONE（含幂等键；仅 shell 超时可命中，见节内边界） |
| 自动错误恢复 | P1-4 | 未实现（无执行循环内触发点，注册表已降级） |
| 中断/崩溃后恢复 | P1-4 | 既有能力（检测 + 显式 resume + 多步跳过已完成） |
| S1 授权核心 | S1 | `IMPLEMENTED_SELF_CHECKED`（独立验收 PENDING） |
| S1 补充协议检查（含 `test_spawn_failed`） | S1 | `IMPLEMENTED_SELF_CHECKED`（独立验收 PENDING） |
| S2 工作区入口（含四项边界复核） | S2 | `IMPLEMENTED_SELF_CHECKED`（独立验收 PENDING） |
| S3 propose 产品入口 | S3 | 未开始（P0-2 先行） |
| S4 V1J-CODE-01 正式验收 | S4 | 未开始 |
| `PRODUCTION_TRUST` | — | 保持原状态，未提升 |
| Desktop 化 | — | 待启动 |
