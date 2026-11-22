# P0 · Agent 能力矩阵

`[P0_AGENT_CAPABILITY_MATRIX · 2026-11-22]`

> 九层能力模型，逐层给：实现状态 / 成熟度 / 模块 / 真实调用路径 / 端到端验证 / 缺口。
> 成熟度定义：L0 不存在 · L1 概念或设计 · L2 基础实现 · L3 局部真实验证 · L4 端到端验证 · L5 稳定可恢复的产品级。
> **成熟度只由证据决定，不由模块数量决定。**

---

## 0. 一页总表

| # | 能力 | 状态 | 成熟度 | 一句话 |
| --- | --- | --- | --- | --- |
| A1 | 目标理解 | `PARTIALLY_IMPLEMENTED` | **L1** | 目标文本无人读；理解 = 字段是否齐全 |
| A2 | 澄清与信息收集 | `IMPLEMENTED_PARTIALLY_VERIFIED` | **L2** | 会问，但问内部字段名，答案不校验 |
| A3 | 规划与任务分解 | `PARTIALLY_IMPLEMENTED` | **L2**（引擎 L3） | 单步确定性映射；多步只能用户给全量 steps |
| A4 | 工具选择与授权 | `IMPLEMENTED_VERIFIED`（策略）/ `PARTIALLY_IMPLEMENTED`（审批） | **L2** | 工具由 operation 决定，不是"选"；审批无 digest |
| A5 | 执行与状态管理 | `IMPLEMENTED_VERIFIED` | **L3** | 状态机与事件扎实；同步阻塞、无调度器 |
| A6 | 观察与结果验证 | `IMPLEMENTED_VERIFIED` | **L4**（7/8 工具） | 独立回读验证是本系统最强项 |
| A7 | 异常恢复与重规划 | `PARTIALLY_IMPLEMENTED` | **L2** | 整步重跑；无步骤内检查点；无自动重试 |
| A8 | 成果交付 | `PARTIALLY_IMPLEMENTED` | **L2** | 有摘要和预览；无下载/版本/血缘视图 |
| A9 | 记忆与持续改进 | `PARTIALLY_IMPLEMENTED`（存储）/ `NOT_IMPLEMENTED`（生效） | **L1** | 记忆 write-only；无自改进回路 |

**没有一项达到 L5。** 全系统最高 L4，出现在"验证"这一层——这与"工程质量高、产品能力低"的落差完全一致。

---

## A1 · 目标理解

| 字段 | 内容 |
| --- | --- |
| 定义 | 从自然语言里识别真实意图、约束、成功标准、歧义点 |
| 用户价值 | 用户说人话就能开始干活 |
| 状态 | `PARTIALLY_IMPLEMENTED`（只有规则路由） |
| 模块 | `intent.py`（意图路由）、`planner.py:472-521`（字段完备性） |
| 真实调用路径 | `conversation_service.process_message` → `classify_intent` → `TaskRequest(goal=content, operation=None)` |
| 数据结构 | `TaskRequest{goal, operation, parameters}` |
| 测试 | 意图/规划相关单测有；无自然语言泛化测试 |
| 端到端 | **失败**：J1 全流程 |
| 模型依赖 | 产品路径**无模型**；`llm_planner` 仅 CLI 且限 2 操作 |
| 安全约束 | 无（模型不参与，无注入面） |
| 当前限制 | `_STATUS_QUERIES` 是 5 个精确字符串（`intent.py:15-16`）；`goal` 只被存下来，从不解析 |
| 主要缺口 | 无理解层；无实体抽取（`note.txt` 就在文本里却没被读） |
| 建议 | 引入受约束的理解层（模型只输出结构化候选，字段与枚举由代码校验），并保留确定性兜底 |

**证据**：实跑 `把这段内容保存成 note.txt：…` → 要求 `operation/artifact_path/content`；`你好` → 同样三问。

---

## A2 · 澄清与信息收集

| 字段 | 内容 |
| --- | --- |
| 定义 | 识别信息缺失、提出用户能回答的问题、记住已答内容 |
| 状态 | `IMPLEMENTED_PARTIALLY_VERIFIED` |
| 模块 | `planner.py:473-490`、`conversation_service._patch_for_question`、`service.py:2938-2963` |
| 真实调用路径 | 缺字段 → `PLANNING_CLARIFICATION` → `AWAITING_CLARIFICATION` → 逐字段 patch → 重规划 |
| 数据结构 | `clarifications` 表（question_id/field_name/status） |
| 测试 | 有（澄清补全、幂等） |
| 端到端 | 4 轮完成（J1） |
| 当前限制 | 问题文案 = `请提供 {field}`；答案原样写入，无枚举/格式校验（实测把 `写文件` 写进 `operation`） |
| 主要缺口 | 不提"为什么需要"、不给候选值、不做类型校验；问题不进会话记录，刷新即消失 |
| 建议 | 问题模板化（每个字段有用户语言版本 + 候选值 + 示例），答案走同一套校验；问题写进会话消息流 |

---

## A3 · 规划与任务分解

| 字段 | 内容 |
| --- | --- |
| 定义 | 把目标拆成有依赖的可执行步骤 |
| 状态 | `PARTIALLY_IMPLEMENTED`（产品）/ `IMPLEMENTED_VERIFIED`（引擎多步） |
| 模块 | `planner.py`（8 个 builder，各 1 步）、`multi_step.py`（≤8 步、依赖校验）、`llm_planner.py` |
| 真实调用路径 | `service._plan_and_execute` → `planner.plan_request` → `Plan(steps)` |
| 数据结构 | `Plan{steps[], failure_policy}`、`PlanStep{step_id,tool_name,parameters,depends_on,requires_approval,max_attempts}` |
| 测试 | 多步用例充分（含 fail_fast/continue、依赖未满足跳过） |
| 端到端 | 引擎：`awaiting_approval → approve → partial`（J2 引擎段） |
| 当前限制 | ① 产品不可启动 `execute_plan`；② LLM 不能产多步；③ 步骤由用户手写 JSON |
| 主要缺口 | 没有"目标 → 步骤"的生成能力（无论规则还是模型） |
| 建议 | 先做"模板化计划"（常见 5 类任务的可参数化模板），再做模型生成计划 + 强校验 |

---

## A4 · 工具选择与授权

| 字段 | 内容 |
| --- | --- |
| 定义 | 选对工具、判风险、该审批就审批、拦住越权 |
| 状态 | 策略 `IMPLEMENTED_VERIFIED`；审批 `PARTIALLY_IMPLEMENTED` |
| 模块 | `policy.py`（8 操作白名单 + 形状 + 路径围栏）、`host_authorization.py`（一次性 grant）、`workbench_control.py`（approve/reject） |
| 真实调用路径 | `policy.evaluate` → 允许则 `_execute_tool`，否则 `policy_rejected` + `blocked` |
| 测试 | policy/围栏/grant 用例充分 |
| 端到端 | 两个拒绝用例实测（`text_match_count`、`source_file_changed`），文件未动 |
| 当前限制 | **工具不是"选"出来的**：operation 决定 tool，模型无选择权（LLM 也只 2 个操作）；多步父任务不过 policy |
| 主要缺口 | 审批无 step digest；`approved_step_ids` 不清空 → replan 可绕过（`service.py:1026-1055` vs `3340`）；host grant 无产品入口 |
| 建议 | 审批绑定"步骤内容摘要 + 参数"；replan 必须清空对应批准；把 grant 的申请/查看搬进产品 |

---

## A5 · 执行与状态管理

| 字段 | 内容 |
| --- | --- |
| 定义 | 真执行、真状态、可追踪、可幂等、可中断 |
| 状态 | `IMPLEMENTED_VERIFIED` |
| 模块 | `service.py`（7180 行）、`event_store.py`、`state_machine.py`、`lease_renewal.py` |
| 真实调用路径 | `submit → enqueue → execute_task → _claim → _run_claimed → _run_with_renewal → _plan_and_execute → _execute_tool → _verify_and_finish` |
| 数据结构 | `Task`（14 状态）+ `task_events`（44 种事件）+ 租约（60 s）+ 执行预算（600 s） |
| 测试 | 含 SIGKILL、崩溃一致性、租约过期 |
| 端到端 | 写文件、改代码均 `succeeded`；事件序列完整 |
| 当前限制 | **同步阻塞请求线程**；控制面全局锁；无任务队列/调度器；无并发上限 |
| 主要缺口 | 异步执行 + 队列是暂停/恢复/取消/多任务/长任务共同的前置 |
| 建议 | 引入单进程执行器线程 + 任务队列（SQLite 已够），把 HTTP 变成"提交即返回" |

---

## A6 · 观察与结果验证

| 字段 | 内容 |
| --- | --- |
| 定义 | 不信工具自述，独立检查产物 |
| 状态 | `IMPLEMENTED_VERIFIED` |
| 模块 | `verifier.py`（`artifact-readback-v1` / `local-test-result`）、各工具 `verify_*` |
| 真实调用路径 | `_verify_and_finish`（6883）→ `verifier.verify`（6958-6969）→ 通过 `SUCCEEDED`（7086）/ 失败 `_fail`（7103） |
| 数据结构 | `verification{passed, reason, content_matches, evidence_matches, path_safe}` |
| 测试 | 充分（含 verifier 异常、证据伪造） |
| 端到端 | `artifact_observation.matches_registration=true`；`workspace_edit_verified` |
| 当前限制 | `controlled_shell` 只查形状（空输出也 PASS）；`run_tests` 以 evidence 里的 `exit_code` 为准（验的是证据自洽，不是测试语义） |
| 主要缺口 | 验证结论不进用户语言；"验证通过"没有可读的"验了什么" |
| 建议 | 保留机制，增加"验证摘要"（人类可读一句话 + 可展开的证据链） |

**这是全系统最值得作为产品卖点的一层**：`SUCCEEDED` 只能从 `VERIFYING` 进入，机制上排除了"工具说成功就算成功"。

---

## A7 · 异常恢复与重规划

| 字段 | 内容 |
| --- | --- |
| 定义 | 失败能识别、能继续、能改计划 |
| 状态 | `PARTIALLY_IMPLEMENTED` |
| 模块 | `retry_policy.py`、`controlled_recovery.py`、`recovery_preview.py`、`service._resume_interrupted` |
| 真实调用路径 | 租约过期 → `INTERRUPTED` → `resume` → 有证据只重验 / 无证据整步重跑 |
| 数据结构 | `retry_request_digest`、`IDEMPOTENT_TOOLS`、`StepResult` |
| 测试 | 有（崩溃、租约、重试幂等） |
| 端到端 | 未跑（无真实崩溃注入）；代码路径清晰 |
| 当前限制 | 无步骤内检查点；无自动重试调度器；`PARTIAL` 不可续做；`approve_recovery_preview` 无生产入口 |
| 主要缺口 | "接着干"这件事在产品上不存在，只有"再干一遍" |
| 建议 | 先做"从失败步骤续跑"（多步场景已具备数据基础），再做自动重试策略 |

---

## A8 · 成果交付

| 字段 | 内容 |
| --- | --- |
| 定义 | 成果有身份、可定位、可验证、可继续修改、可重新交付 |
| 状态 | `PARTIALLY_IMPLEMENTED` |
| 模块 | `artifact_tool.py`、`workbench.artifact_preview`、`service._execute_read_tool/_derive_modify_content` |
| 真实调用路径 | 写 → `artifacts/<task_id>/<name>` + staging/fsync/hardlink；读 → `GET /api/tasks/{id}/artifact` |
| 数据结构 | `ArtifactEvidence{path,byte_count,sha256,attempt_id,claim_generation}` |
| 测试 | 充分 |
| 端到端 | `note.txt` 42 B / `22eccb50…` 可读可验 |
| 当前限制 | 预览 ≤64 KiB；无下载；无版本列表；无血缘视图；"基于此继续"要手填 `expected_sha256` |
| 主要缺口 | 交付是"磁盘上的一个文件"，不是"用户手里的成果" |
| 建议 | 加下载、版本列表（同 name 多次写 = 多版本）、血缘卡片、一键"在此之上继续" |

---

## A9 · 记忆与持续改进

| 字段 | 内容 |
| --- | --- |
| 定义 | 记住用户偏好/项目知识，跨任务复用，并能受控变强 |
| 状态 | 存储 `PARTIALLY_IMPLEMENTED`；生效 `NOT_IMPLEMENTED` |
| 模块 | `memory_service.py`、`context.py`、`evaluation.py` / `safety_gate.py` / `code_edit_baseline.py` |
| 真实调用路径 | 记忆只在 `workbench_server` 的 UI 接口被读写；`service/planner/multi_step/context` **零引用** |
| 数据结构 | `memories{type,scope,key,content,confidence,status}`，5 类型 × 3 作用域，软删 + supersede |
| 测试 | 有（CRUD、supersede、forget） |
| 端到端 | **未生效**：记忆不改变任何规划或执行 |
| 模型依赖 | 无 |
| 安全约束 | 内容 16 KiB 上限、类型/作用域枚举 |
| 当前限制 | 检索是 `LIKE`；无引用/来源；无"这条记忆从哪来" |
| 主要缺口 | 记忆没有进入决策回路，等于一套独立数据库 |
| 建议 | 先做"项目级上下文注入"（进 prompt/进 plan 输入），再做偏好记忆；没有决策回路前不要扩记忆功能 |

---

## 11. 能力依赖（关键瓶颈）

```
A1 目标理解 ──┬─→ A3 规划 ──┬─→ A4 工具授权 ──→ A5 执行 ──→ A6 验证 ──→ A8 交付
              │             │                              │
              └─→ A2 澄清 ──┘                              └─→ A7 恢复 ──→ A8
                                                                    │
A5 异步执行（当前缺失）═══════════════╗                              │
   → 阻塞 A4 审批体验、A7 暂停/恢复、A8 长任务交付、多任务管理        │
A9 记忆 ── 依赖 A1/A3 有"决策回路"才可能生效 ────────────────────────┘
```

- **首要瓶颈 = A1**。没有理解层，后面所有层的输入都是"用户手填的结构化参数"。
- **第二瓶颈 = A5 的异步化**。暂停、取消、恢复、多任务、"边跑边看"全都挂在它下面。
- **最成熟的 A6 应当被"用起来"**：它是唯一可以对外讲"我们不假装成功"的机制。

详细依赖图见 `P0_AGENT_CAPABILITY_DEPENDENCY_MAP.md`。

---

## 12. 与风险登记的映射

| 能力缺口 | 对应风险编号 |
| --- | --- |
| A1 L1 | R-01 / G-01 |
| A2 L2 问内部字段 | R-18 / G-02 |
| A3 多步不可启动 | R-02 / G-03 |
| A4 审批可绕过 | R-08 / G-07 |
| A5 同步阻塞 | R-03 / G-04 |
| A7 只能整步重跑 | R-19 / G-05 |
| A8 无下载无版本 | R-10 / G-06 |
| A9 记忆不生效 | R-07 / G-08 |

编号定义见 `P0_GAP_AND_RISK_REGISTER.md`。
