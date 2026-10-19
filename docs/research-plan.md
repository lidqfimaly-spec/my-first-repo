# 全球 Agent 技术对标研究计划 v1

## 1. 目的与边界

研究只服务于已经识别出的工程决策，不以“读完所有文档”为目标。第 0/1 阶段的优先决策是：

1. 主 Agent 循环和工具协议的边界；
2. 计划是否需要模型，模型输出如何受限；
3. 持久化、检查点、恢复和人工介入语义；
4. 追踪、错误分类和评测证据；
5. 何时值得引入外部框架，何时标准库实现更可靠。

边界：

- 官方文档、官方仓库和官方发布优先；二手文章只能用于发现线索。
- 不复制整套框架，不以安装 SDK 代替架构决策。
- 不在第 0 阶段安装这些框架。
- 每个候选必须映射到具体问题、真实任务测试和回滚方式。
- 不将文档中的能力描述直接当作 Zevorinth 已具备能力。

## 2. 证据等级

| 等级 | 含义 |
| --- | --- |
| A | 本机或新项目中的可重复实测 |
| B | 官方文档、官方发布记录或官方仓库源码 |
| C | 推测、二手介绍或未复现结论 |

C 级不得用于支持上线决策。

## 3. 候选对标

版本和许可证于 2026-10-19 从官方 GitHub API/页面核对；版本数字是当日“最新 release”快照，不是永久结论。

| 候选 | 官方来源 | 当日版本/状态 | 许可证 | 重点机制 | 对 Zevorinth 的潜在收益 | 复杂度与风险 |
| --- | --- | --- | --- | --- | --- | --- |
| OpenAI Agents SDK | https://openai.github.io/openai-agents-python/ ；https://github.com/openai/openai-agents-python | `v0.24.0`（2026-10-15 发布） | MIT | Agent loop、工具调用、handoff、guardrail、tracing、eval | 对照最小 loop、工具 schema、护栏和追踪字段 | SDK 抽象可能遮蔽持久化/权限语义；trace 默认上报边界需核查 |
| Anthropic Agent Engineering | https://www.anthropic.com/engineering/building-effective-agents | 官方工程文章，非版本化软件 | 页面内容受站点条款约束；不据此复制代码 | workflow 与 autonomous agent 的边界、简单模式优先、评测 | 防止在没有必要时引入自治循环和多 Agent | 文章不能替代本地实测；模型行为不可直接外推 |
| LangGraph | https://langchain-ai.github.io/langgraph/concepts/persistence/ ；https://github.com/langchain-ai/langgraph | `1.2.16`（2026-10-16 发布） | MIT | graph、checkpoint、持久化、恢复、human-in-the-loop、state | 对照任务状态、检查点、恢复和人工批准语义 | 引入 graph/checkpoint 生态与依赖；单机最小闭环可能过重 |
| Microsoft Agent Framework | https://learn.microsoft.com/en-us/agent-framework/ ；https://github.com/microsoft/agent-framework | `python-1.22.0`（2026-10-15 发布） | MIT | workflow orchestration、session state、可观测性、Python/.NET | 对照工作流、会话状态和 telemetry 的接口设计 | 框架仍在快速演进；多语言和生态复杂度较高 |

## 4. 工程问题、机制与验证

| 编号 | 工程问题 | 候选机制 | 最小验证 | 停止条件 |
| --- | --- | --- | --- | --- |
| R1 | 一个 Agent 循环何时结束？ | 最大步数、工具错误、无进展检测、显式终态 | 构造成功、工具失败、重复调用三次、上下文截断四类测试 | 能用明确状态解释每种结束，且不伪造成功 |
| R2 | 模型如何调用工具而不越权？ | JSON schema、工具白名单、前置策略、后置验证 | 注入路径逃逸、未知工具、缺参数和失败结果 | 危险调用在执行前被阻止，并留下可机械检查的事件 |
| R3 | handoff/多 Agent 什么时候值得？ | 单 Agent、workflow、handoff、supervisor | 先在单 Agent 真实任务上测失败；只有边界明确时做对照实验 | 单 Agent 无法满足已测需求时才进入候选设计 |
| R4 | 如何持久化和恢复？ | event log、state snapshot、checkpoint、幂等步骤 | `RUNNING` 中断、写入前后中断、重复恢复 | 中断可识别，恢复不重复副作用，证据不足不猜成功 |
| R5 | 人工介入点放哪里？ | 工具前批准、计划批准、验证后批准 | 操作者拒绝、超时、批准后重试 | 拒绝不执行、超时不自动放行、批准可审计 |
| R6 | guardrail 何时执行？ | 输入检查、工具输入检查、工具输出检查、最终验证 | 分别注入违规输入、危险参数、错误产物 | 每层失败都有测试，且失败类别可区分 |
| R7 | 如何追踪而不泄露？ | trace/span、事件、脱敏、采样 | 本地 trace 落盘、错误注入、秘密哨兵字符串 | 证据足以复现步骤，日志不出现凭据值 |
| R8 | 如何做可重复评测？ | deterministic fixtures、simulator、real smoke、regression tasks | 真实/模拟/未执行三态矩阵 | 评测能区分模型、执行器和测试装置故障 |
| R9 | 何时引入框架？ | 现有协议适配、SDK、自研最小核心 | 用同一任务分别评估自研和框架路径；不先安装 | 框架只解决已测瓶颈，且迁移/回滚成本明确 |

## 5. 研究执行顺序

### 第一批：只影响第 1 阶段

1. 从 Anthropic 官方文章提取“先 workflow、后 agent”的判据，形成是否引入模型循环的决策门槛。
2. 从 OpenAI Agents SDK 官方文档核对最小 Agent loop、tool call、guardrail 和 trace 的字段语义。
3. 把结论映射为 Zevorinth 的 `Plan`、`ToolRequest`、`Event` 和 `VerificationResult` 数据契约。
4. 不安装 SDK；只做官方源码/文档阅读和数据契约对照。

### 第二批：影响恢复设计

1. 对照 LangGraph 的 checkpoint、state 和 resume 语义。
2. 对照 Microsoft Agent Framework 的 session state 与可观测性。
3. 写出“文件状态机 vs checkpoint 库”的决策记录。
4. 用一个强制中断任务验证本项目的恢复，不用框架结果代替。

### 第三批：仅在真实瓶颈出现后

1. 多 Agent 或 handoff 真实失败样本；
2. 多任务并发、跨进程恢复或长期运行；
3. 需要标准化 trace 导出或外部评测；
4. 需要接入已有 MCP/ACP 生态。

## 6. 每个候选的评估产出

每个候选最终必须形成一页记录：

- 精确版本、发布日期、许可证和官方链接；
- 解决的 Zevorinth 工程问题；
- 采用的机制与不采用的机制；
- 依赖增加、运行成本、故障模式；
- 最小验证命令和真实输出；
- 回滚到自研实现的路径；
- 最终判定：采纳、局部借鉴、暂缓或拒绝。

## 7. 停止条件

满足以下条件即停止一个研究主题：

1. 对当前阻塞决策已有足够官方证据；
2. 已写出可验证的本地实验；
3. 已明确采纳/拒绝所需的触发条件；
4. 继续阅读只会增加背景信息，不改变当前实现。

若连续两轮研究没有产生可执行决策、测试或风险澄清，则暂停该主题，转向最小闭环实现。
