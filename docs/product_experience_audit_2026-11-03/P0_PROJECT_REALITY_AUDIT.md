# P0 工程真实现状审查

> 审查日期：2026-11-03（Asia/Shanghai）  
> 审查对象：`/Users/lidequan/PythonProject/Zevorinth`  
> 产品阶段（registry）：`3.92` / `single_host_local_alpha`  
> 标签约定：**[事实]** 可复核证据；**[推断]** 基于事实的合理解释；**[建议]** 下一阶段行动；**[无法确认]** 本轮未验证。  
> 约束：未改业务代码；旧仓与 Origin 云端另线仅作背景，不纳入本仓能力断言。


## 1. 审查范围与方法

**[事实]** 本轮检查路径与范围：

| 维度 | 路径 / 对象 |
| --- | --- |
| 根目录 | `/Users/lidequan/PythonProject/Zevorinth` |
| 包代码 | `src/zevorinth/`（约 45 个 `.py`） |
| 前端静态 | `src/zevorinth/workbench_static/`（`index.html` / `app.js` / `styles.css` / `i18n.zh-CN.json`） |
| 测试 | `tests/`（94 个 `test_*.py`） |
| 权威能力表 | `src/zevorinth/capabilities.py` |
| 文档对照 | `README.md`、`DESIGN.md`、`docs/phase-*`（只作对照，不当实现） |
| 运行时实测 | `/tmp/zevorinth_audit_2026-11-03` 独立 home；CLI + loopback workbench/control-center |
| Git 近况 | `git log --oneline -25`（至 `65981ef feat: deliver task control center v1`） |

**[事实]** 未改业务代码；仅新建本审查目录下 markdown。

**[事实]** 代表性安全测试本轮通过：`tests.test_capability_registry`（18）、`tests.test_state_machine`（3）、`tests.test_clarification_workflow`（9）、`tests.test_cli.CliTests.test_missing_information_and_policy_block_have_distinct_exit_codes`、`tests.test_workbench.WorkbenchHTTPTests.test_mutation_methods_and_unknown_routes_are_rejected`（合计 32 OK）。未跑全量 121 测试文件 / container / live。

## 2. 目录与模块结构

**[事实]** 根结构精简：`src/`、`tests/`、`docs/`、`artifacts/`、`data/`、`.venv/`、`pyproject.toml`、`README.md`、`DESIGN.md`。包入口 `zevorinth` → `cli:main`；Python `>=3.11`；默认零硬依赖，LLM 可选 `openai>=3.35.1,<4`。

**[事实]** 核心模块（按职责）：

| 模块 | 职责（据源码） |
| --- | --- |
| `capabilities.py` | 机器可读能力注册表（product_stage `3.92`） |
| `service.py` | `TaskService` 任务生命周期、执行、修订、取消、恢复入口 |
| `cli.py` | 全部操作员 CLI |
| `planner.py` / `llm_planner.py` | 确定性 / LLM 规划（仅规划） |
| `policy.py` | 执行前授权；**单步计划** |
| `artifact_tool.py` / `local_test_runner.py` | 两类工具 |
| `verifier.py` | 独立验证 |
| `event_store.py` | SQLite 事件与任务持久化 |
| `conversation_service.py` | 会话消息、上下文、process |
| `state_machine.py` | 状态转移表 |
| `workbench.py` / `workbench_server.py` / `workbench_control.py` | 只读工作台 + 控制中心 |
| `safety_gate.py` / `controlled_recovery.py` / `isolation.py` 等 | 安全门、恢复、隔离 |

**[事实]** 源码中 **无** `memory` / `knowledge` / embedding / vector 模块文件。

## 3. 前端技术与页面结构

**[事实]** 无前端框架；vanilla HTML/CSS/JS + 简中 i18n。`DESIGN.md` 明确：loopback 只读工作台；可选 control center v1。

**[事实]** UI 结构：总览指标、Attention、任务列表/筛选、任务详情（状态/计划/事件/验证/产物预览）、控制中心模式下的创建面板与确认动作。

**[事实]** 实测：`zevorinth workbench --port 8765` → `GET /` 200；`GET /api/tasks` 返回 `read_only:true` 与任务投影；`POST /api/tasks` → 405 / `read_only_workbench`。`control-center --port 8766` session 返回 `contract_version=task-control-v1`、actions 白名单；无 Origin 的 curl POST 被 `invalid_origin` 拒绝（浏览器同源保护生效）。

## 4. 后端架构与 API

**[事实]** 无独立多用户 HTTP API 服务；形态为：

1. **CLI**（主操作面）
2. **Python `TaskService` / `ConversationService`**
3. **loopback HTTP**（只读 API + 可选 control POST，带 process token + Origin）

**[事实]** CLI 子命令：`doctor, conversation, eval-*, workbench, control-center, run, test, cleanup, verify-isolation, status, task-audit, recover-prepublish, recovery-audit, events, cleanup-staging, cancel, request-cancel, revise, resume`。

**[事实]** 无 `pause` / `suspend` 子命令（`--help` 与源码确认）。

## 5. Agent 核心机制

**[事实]** 执行内核本地完成认领、状态机、Policy、工具、Verifier、事件；LLM **不得**直接碰 SQLite/工具/状态（README + `doctor` Notice + 实测 LLM 无 Key → `planning_failed` / exit 7 / `not_executed`）。

**[事实]** Policy：`Phase 1 permits exactly one plan step`（`policy.py`）；任务操作仅 `write_text_artifact` 与条件 `run_tests`（registry）。

**[事实]** 状态机终端：`succeeded/failed/planning_failed/cancelled/simulated`；允许路径含 clarification / blocked / interrupted 等（`state_machine.py`）。

## 6. 对话与上下文

**[事实]** `conversation create/send/history/context/process/recover` 存在；process 可绑定结构化参数创建任务。实测 create → process（带 `--artifact-path`/`--task-content`）→ 任务 `succeeded` 且 `conversation_id` 关联。

**[事实]** process 后 `history` 仅见用户消息一条；未见助手侧解释消息自动入史（本轮观测）。

**[推断]** 对话层更偏「任务绑定与幂等消息」，而非竞品式多轮聊天 UX。

## 7. 任务规划与状态

**[事实]** 确定性 planner：缺参 → `awaiting_clarification`；完整结构化 → 单步计划。LLM planner：条件可用，需 provider/model/key。

**[事实]** `revise`：未执行任务、版本校验、`--confirm`；实测 queued 任务 content 从 `old`→`new-v2`，`plan_version` 0→1，status 仍 `queued`。

**[事实]** `cancel`：无活跃 lease 可取消；`request-cancel`：协作取消请求。无暂停。

## 8. 工具注册、调用与执行

**[事实]** 工具面极窄：写文本产物（≤64KiB、不覆盖同任务路径）+ 有界 run_tests（容器/隔离证据；host 需显式授权）。registry：`general_shell`/`browser` = `not_implemented`。

## 9. 文件与成果

**[事实]** 成功任务写入 `artifacts/<task_id>/`，独立 SHA-256 读回验证；`task-audit` 给出 L1 assurance（`internal_consistency_only`，禁止宣称认证/自动恢复）。

**[事实]** 「覆盖」是任务目录内拒绝；不同 task_id 可同名文件（实测第二次 `hello.txt` 新任务成功）。

## 10. 记忆与知识

**[事实]** 无长期记忆 / RAG / 知识库模块；会话有 `context` 投影字段，但 `summary_authoritative=false`（空会话实测）。

**[事实]** registry：`l2_l3_trust=not_implemented`；assurance 明确 `evidence_authenticated=false`。

## 11. 权限、安全与人工审批

**[事实]** Policy + clarification + control-center `confirmed:true` + host_authorization token + live eval `--authorize-live` default-deny + loopback Origin/token。

**[事实]** 无通用「工具授权 UI」；授权语义分散在 CLI 标志、Policy 码与控制中心确认框。

## 12. 日志、追踪与可观测

**[事实]** 有序事件流（实测成功任务含 created→claimed→planning_*→tool_*→verification_*→succeeded）；`events`/`task-audit`/`recovery-audit`/`doctor`。

**[事实]** workbench Attention Inbox 投影失败/阻塞任务。

## 13. 测试、评估与质量

**[事实]** 大量离线契约/生命周期/控制中心测试；planner quality offline + 授权 live baseline 协议。registry 自报 live strict 基线历史分数（文档级记录在 README；能力表 limitations 同步）。

**[事实]** `doctor` 硬编码 `phase: "3.75"`，而 `CAPABILITY_REGISTRY["product_stage"]="3.92"`、README 写 3.92——**版本表述漂移**。

## 14. 异常、恢复与生命周期

**[事实]** 有：clarification/resume、cancel/request-cancel、lease 恢复、prepublish 有界恢复、controlled marker（internal）、cleanup 显式调用。

**[事实]** registry 明确 `automatic_retry/recovery/cleanup`、`general_task_recovery` = `not_implemented`。

## 15. 维度结论速览

| 维度 | 状态标签 | 证据摘要 |
| --- | --- | --- |
| 目录/模块 | 已实现且已验证 | 目录列表 + import doctor |
| 前端 workbench | 已实现且已验证（只读） | HTTP 实测 + 测试 |
| 控制中心 v1 | 已实现但 UI 变更面未充分本轮浏览器点击验证 | session API + 单测存在；curl 无 Origin 被拒 |
| Agent 双工具内核 | 已实现且已验证 | run/status/events/audit |
| 多步通用 Agent | 尚未实现 | policy 单步 + registry |
| 对话 UX | 部分实现 | conversation CLI 可跑；历史助手消息弱 |
| 记忆知识 | 尚未实现 | 无模块 |
| 暂停 | 尚未实现 | 无命令 |
| 版本标识一致性 | 存在缺陷 | doctor 3.75 vs registry/README 3.92 |

## 16. 未完成 / 无法确认

- **[无法确认]** 全量 unittest、container live `run_tests`、真实 LLM 规划质量（本机无 Key）。
- **[无法确认]** 控制中心浏览器端完整点击流（本轮 API/源码/单测为主）。
- **[无法确认]** 跨 OS 语义（registry=`unverified`）。
