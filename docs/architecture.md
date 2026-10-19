# Zevorinth 初始架构与关键设计决策

## 1. 阶段边界

第 1 阶段只实现一个主 Agent、一个有界执行计划、一个真实受控工具、一个独立验证器、持久化任务状态与事件，以及对应的端到端测试。

明确不做：

- 多 Agent 集群、交接和投票；
- 自我修改核心代码；
- 无限自主循环；
- 任意 shell、网络浏览、凭据访问；
- 向量数据库、复杂记忆、消息队列、Web 前端；
- 依赖大型 Agent 框架来替代尚未定义的核心语义。

## 2. 最小端到端闭环

```text
用户任务
  -> TaskService 创建任务与初始事件
  -> ClarificationPolicy 判断字段是否充分
       -> 不足：进入 AWAITING_CLARIFICATION，停止
       -> 充分：继续
  -> Planner 生成有界 Plan（最多 1 个阶段、1 个工具步骤）
  -> Policy 检查工具、路径、覆盖与内容边界
       -> 拒绝：BLOCKED，记录原因，无工具副作用
       -> 允许：继续
  -> ToolExecutor 调用受控工具
       -> 异常/超时：FAILED，保存真实错误
       -> 返回：保存 ToolResult
  -> Verifier 独立读取结果并校验声明
       -> 不过：FAILED
       -> 通过：SUCCEEDED
  -> 原子保存 state.json，追加 events.jsonl
  -> Report 输出结果、执行证据、产物位置和限制
```

### 第 1 阶段的可验证任务

首个闭环接收一个结构化、字段明确的“创建文本产物”任务，例如：

```json
{
  "goal": "创建一份可验证的问候文本",
  "artifact_path": "hello.txt",
  "content": "Hello from Zevorinth\n"
}
```

这不是完整的自然语言 Agent，而是一个刻意收窄的真实执行切片。它先用确定性 planner 证明以下语义正确：

- 信息不足时确实不会执行工具；
- 危险路径确实会被阻止；
- 工具失败不会被报告为成功；
- 验证器和工具是两条路径；
- 中断后可识别，恢复不会重复写入；
- 真实执行、模拟执行和未执行可区分。

模型规划器将在该闭环稳定后接入同一个 `Plan` 协议，不改变任务、策略、工具、验证和存储语义。

## 3. 组件边界

| 组件 | 责任 | 不负责 |
| --- | --- | --- |
| CLI/API | 接收任务、展示报告 | 执行工具、决定成功 |
| TaskService | 创建任务、状态转换、恢复入口 | 生成模型内容 |
| ClarificationPolicy | 判断必需字段和歧义 | 猜测缺失值 |
| Planner | 生成有界、结构化的 `Plan` | 直接执行副作用 |
| Policy | 授权工具和参数、拒绝危险操作 | 验证产物正确性 |
| ToolExecutor | 调用已注册工具、处理异常 | 宣布任务完成 |
| Verifier | 独立核验结果和证据 | 修复失败 |
| EventStore | 原子状态与追加事件 | 隐藏或回填失败 |
| ArtifactStore | 管理允许目录内的产物 | 接受任意路径 |

## 4. 状态机

```text
RECEIVED
  -> AWAITING_CLARIFICATION
  -> PLANNED
  -> RUNNING
       -> VERIFYING -> SUCCEEDED
       -> FAILED
       -> INTERRUPTED -> (user-initiated RESUME) -> RUNNING
       -> BLOCKED
```

状态约束：

- `SUCCEEDED` 必须同时有真实工具结果和通过的 Verifier 证据。
- 工具异常只能进入 `FAILED`，不得进入 `SUCCEEDED`。
- `BLOCKED` 表示策略拒绝，不得产生任何工具副作用。
- `INTERRUPTED` 表示发现运行痕迹但未得到终态；它不是失败，也不是成功。
- 恢复必须创建新的尝试记录和 `attempt_id`，不能覆盖原事件。
- 第 1 阶段不做无限自动重试；恢复由用户显式触发。

## 5. 执行真实性模型

禁止使用模糊布尔值表示“是否执行”。采用显式枚举：

```text
ExecutionMode.REAL        真实调用工具或验算器
ExecutionMode.SIMULATED   测试替身执行，不得作为产物证据
ExecutionMode.NOT_RUN     没有执行
```

每个工具结果至少包含：

```text
execution_mode
tool_name
started_at / finished_at
success
structured_output
error_kind / error_message（失败时）
attempt_id
```

验证结果单独记录：

```text
verifier_name
passed
expected
observed
evidence
```

测试报告必须明确真假执行，不能只断言“函数被调用”。

## 6. 受控工具 v1

首个真实工具：`write_text_artifact`

允许：

- 仅写入任务专属产物目录；
- 仅允许普通文件名和受限子目录；
- 内容必须为 UTF-8，大小有上限；
- 目标文件默认必须不存在；
- 写入使用临时文件加原子替换，但“不覆盖”策略在替换前已检查。

拒绝：

- 绝对路径、`..`、空路径、控制字符；
- 解析后逃出产物根目录的路径；
- 符号链接目标或符号链接父目录；
- 已存在文件；
- 超限内容；
- 通过文件名伪装成工具配置、密钥或系统文件。

不提供 shell 字符串接口。未来若加入命令工具，只接受参数数组、明确白名单、超时和输出上限。

## 7. 持久化布局

```text
data/tasks/<task-id>/
  state.json
  events.jsonl

artifacts/<task-id>/
  <verified artifact>
```

`state.json` 保存当前快照，通过“同目录临时文件 + fsync + os.replace”原子更新。`events.jsonl` 只追加，不原地修改。写入 `state.json` 前先追加事件，恢复时以最后可解析事件和快照交叉检查。

第 1 阶段使用单进程锁避免并发写同一任务。SQLite、分布式锁和跨进程 worker 暂不引入。

## 8. 恢复设计

启动或查询任务时执行：

1. 若要恢复的任务仍是 `RUNNING`，检查最后事件与计划步骤。
2. 将陈旧运行标记为 `INTERRUPTED`，记录检测时间和依据。
3. 检查计划步骤是否已有成功的幂等结果。
4. 若产物尚未写入，可在新 `attempt_id` 下重新执行。
5. 若产物已存在但缺少验证证据，只运行验证，不重复写文件。
6. 若证据不足以判定，停在 `INTERRUPTED` 并报告未知，不猜测成功。

“识别中断”与“自动恢复”是两个能力。第 1 阶段必须先证明前者，再提供显式恢复命令。

## 9. 安全与凭据

- 不读取 `.env` 来推断能力。
- 检查凭据时只报告环境变量是否存在，不打印值。
- 事件和错误消息经过统一脱敏。
- 所有路径先解析再与允许根目录比较。
- 任何策略拒绝都是一等结果，必须可见。
- 测试中使用临时目录，不触碰真实用户项目。

## 10. 关键设计决策

### ADR-001：标准库优先

决定：第 0/1 阶段不引入 Agent 框架或大型 Web 框架。

原因：当前尚未证明需要框架提供的持久化、调度和可观测能力；先建立最小语义，后续再按具体缺口选型。

### ADR-002：文件状态机先于数据库

决定：`state.json + events.jsonl` 起步。

原因：数据量小、可直接检查、易于构造中断测试。只有出现多任务并发、复杂查询或跨进程需求时才评估 SQLite。

### ADR-003：主 Agent、确定性边界

决定：模型只生成受模式约束的计划和候选参数；策略、执行和验证由确定性代码控制。

原因：模型判断不能作为“执行成功”的证据。

### ADR-004：旧代码延迟复用

决定：在许可证与具体消费点核实前，不复制旧项目代码。

原因：三个旧项目根目录均未发现许可证文件；历史上有效不等于当前可安全复用。

## 11. 后续演进顺序

1. 完成结构化任务的最小闭环和六个验收场景。
2. 引入真实模型规划器，但仍只生成同一 `Plan` 协议。
3. 增加第二个受控工具，并证明工具选择、拒绝与验证失败可区分。
4. 再评估旧项目中有明确验证证据的组件。
5. 只有真实单 Agent 瓶颈出现后才评估多 Agent。

## 12. 第 1 阶段实现更新（2026-10-19）

本节记录实际实现，并明确替代 Phase 0 中尚未落地的部分设计。前面的阶段规划仍保留为设计历史，不代表全部已经实现。

### 12.1 持久化决策修订：SQLite 替代双文件

Phase 0 曾计划 `state.json + events.jsonl`。第 1 阶段改为标准库 SQLite，原因如下：

- 任务状态与事件必须在同一事务中提交，避免“状态已变但事件没写”或相反；
- 事件顺序由自增 `sequence` 保证；
- 重启后可直接查询历史任务；
- SQLite 不增加数据库服务或第三方依赖；
- 单进程阶段仍可使用 `BEGIN IMMEDIATE` 和 WAL 降低部分写入风险。

因此 ADR-002 被本决策替代。数据库位置为 `<runtime-root>/data/zevorinth.sqlite3`。

### 12.2 实际状态与执行模式

任务状态：

```text
queued
running
awaiting_clarification
blocked
verifying
succeeded
failed
interrupted
cancelled
simulated
```

执行模式：

```text
not_executed
real
simulated
```

`simulated` 是独立的可信终态：它表示模拟流程完成，但明确没有真实产物。它不等同于 `succeeded`。

`state_machine.py` 是合法转换的唯一声明处。EventStore 的每个转换都在数据库事务内重新读取当前状态、验证转换、更新快照并追加事件。

### 12.3 实际模块调用链

```text
CLI
  -> TaskService
       -> DeterministicPlanner
       -> ArtifactPolicy
       -> RealArtifactWriter / SimulatedArtifactWriter
       -> ArtifactVerifier
       -> EventStore(SQLite)
```

| 模块 | 实际职责 |
| --- | --- |
| `models.py` | Task、Plan、ToolResult、VerificationResult、Event 等数据契约 |
| `state_machine.py` | 合法状态转换和终态定义 |
| `event_store.py` | 原子任务快照、追加事件、顺序与重启查询 |
| `planner.py` | 确定性单步计划，不接受任意自然语言 |
| `policy.py` | 澄清字段、操作白名单、文件名、大小、路径和覆盖检查 |
| `artifact_tool.py` | 真实原子写入与显式模拟写入 |
| `verifier.py` | 独立读回、内容/SHA/工具证据核对 |
| `service.py` | 闭环编排、失败路径和恢复协议 |
| `cli.py` | 提交、查询、事件、恢复和自检 |

### 12.4 真实写入的安全属性

`write_text_artifact`：

- 只接受单个文件名，不接受绝对路径或目录层级；
- 任务目录必须位于 `<runtime-root>/artifacts/`；
- 在打开任务目录时使用不跟随符号链接的目录描述符；
- 文件读取使用 `O_NOFOLLOW`，并要求普通文件；
- 先写同目录临时文件并 `fsync`，再使用不覆盖的硬链接提交；
- 目标已存在时返回 `artifact_exists`，不覆盖；
- 工具证据由写后重新读取实际文件计算。

这降低了路径替换和写入中断导致“静默成功”的风险，但不等同于操作系统级沙箱。

### 12.5 恢复协议

服务启动时扫描遗留的 `running`、`verifying` 任务，将其转为 `interrupted`，并记录前一状态和阶段。

恢复规则：

1. 已有真实工具结果和产物证据：先独立重新验证，不调用写入工具。
2. 无已落盘产物证据：在相同计划和安全策略下创建新 `attempt_id`，重新执行。
3. 已有文件但内容不一致、证据不一致或路径不安全：进入显式失败或阻断，不覆盖。
4. 已成功或已模拟的任务再次 `resume`：直接返回，不追加重复成功事件。

当前采用单进程模型。启动第二个服务实例会把数据库中的进行中任务视为重启遗留，因此本阶段不支持并发运行同一运行根目录。

### 12.6 已知边界

- 已测试确定性中断注入和重新实例化，未执行真实操作系统强杀恢复测试。
- SQLite 保证本进程可见事务的原子提交，但不是完整防篡改存储。
- 硬链接提交依赖支持该操作的文件系统。
- 路径安全针对产物目录和文件名，不是完整 OS 沙箱。
- 模型规划器和多工具选择仍未实现。

## 13. 第 1.5 阶段：执行租约、围栏与崩溃恢复

### 13.1 新增执行权协议

每次运行既有任务都必须在 SQLite 事务中取得执行权：

```text
owner_id
lease_token
lease_expires_at
claim_generation
attempt_id
```

认领规则：

1. 在 `BEGIN IMMEDIATE` 中读取最新 Task。
2. 终态或不可运行状态拒绝认领。
3. 其他执行者持有未过期 lease 时拒绝，并记录 `claim_rejected`。
4. lease 已过期时先记录 `lease_expired`；`running/verifying` 转为 `interrupted`。
5. 授予新 token，增加 `claim_generation`，记录 `task_claimed`。

所有执行中的状态更新和事件写入都必须携带当前 lease token。token 不匹配时抛出 `ClaimLost`，旧执行者不能覆盖新执行者的状态。

### 13.2 正常执行时间线

```text
queued
  -> task_claimed（持久化执行者与租约）
  -> task_planned (running)
  -> tool_started
  -> 文件写入
  -> tool_succeeded + 工具证据
  -> verification_started
  -> verification_completed
  -> succeeded / failed
  -> 清除租约
```

如果策略阻断、模拟结束或工具失败，终态转换同样在事务中清除租约。

### 13.3 恢复协议

- 新实例不会中断活动租约。
- 租约到期后，`running/verifying` 才转为 `interrupted`。
- 有持久化工具证据时，只重新验证产物。
- 没有工具证据但目标文件已存在时，Policy 返回 `artifact_exists`，不覆盖。
- 有证据但文件缺失、修改或证据不一致时进入显式失败。
- 终态任务再次 `execute`/`resume` 直接返回原结果，不重复成功。

### 13.4 SQLite 一致性

- 任务快照和事件在同一事务中提交。
- 状态转换前重新读取数据库，避免使用进程内旧快照决定转换。
- `busy_timeout` 有界；超时转换为 `StoreBusyError`，CLI 退出码为 6。
- 事件插入异常会回滚整个事务。
- lease token 围栏保护关键写回。

### 13.5 多实例结论

当前支持：

- 同一主机、多个 TaskService 实例；
- 同一本地文件系统上的多个 Python 进程共享同一 SQLite 数据库；
- 同一任务单执行者；
- 不同任务并发执行。

当前不支持：

- 跨主机分布式锁；
- 网络文件系统上的可靠租约语义；
- 长任务自动租约续期；
- 分布式文件系统围栏或其他外部存储事务。

因此不得把当前实现描述为分布式执行系统。

### 13.6 真实 SIGKILL 覆盖点

测试子进程在以下时点被父进程发送 `SIGKILL`：

- 真实文件已经写入；
- 工具结果与产物证据已经持久化；
- 任务已提交 `verifying` 检查点；
- 独立 Verifier 尚未执行。

父进程等待明确的进程输出，不使用固定睡眠猜测时点；超时会清理子进程。测试确认返回码是 `-SIGKILL`。租约到期后新运行时恢复并验证文件；文件被修改时恢复失败且不覆盖。

## 14. 第 2 阶段：受限 LLM Planner

### 14.1 Planner 边界

Planner 只负责把目标转换成统一 `PlanningOutcome`，不能拥有执行能力。

```text
TaskService（持有租约）
  -> Planner.plan_request(TaskRequest)
  -> PlanningOutcome
       planned / clarification / unsupported / failed
  -> 本地状态转换
  -> Policy
  -> 既有 Tool / Verifier / EventStore
```

保留 `deterministic-v1`。LLM Planner 返回候选后，由独立 `PlanningCandidateValidator` 转换为现有 `TaskRequest` 和单步 `Plan`。

### 14.2 LLM 调用约束

- 使用 OpenAI 官方 Python SDK 的 Responses API。
- 当前验证的 SDK 版本：`3.35.1`。
- 客户端设置 `max_retries=0`。
- 每次规划最多一次 API 调用。
- `store=False`。
- 有界 `timeout`、`max_output_tokens` 和输入大小。
- strict JSON Schema 约束候选字段。
- 不提供 tools、shell、code interpreter、MCP 或文件访问。

官方文档域在实施时返回 HTTP 403；API 形状通过隔离环境中的官方 SDK 签名和源码核对。该证据不替代未来在线行为验证。

### 14.3 统一规划结果

| 分类 | 状态 | 执行模式 |
| --- | --- | --- |
| planned | 继续 `running`，但还要过 Policy | 尚未执行时为 `not_executed` |
| clarification | `awaiting_clarification` | `not_executed` |
| unsupported | `blocked` | `not_executed` |
| failed | `planning_failed` | `not_executed` |

模型失败不会回退到确定性 Planner。不同 Planner 可通过 CLI 显式选择。

### 14.4 本地双重校验

第一层是 Responses API 的 strict JSON Schema，第二层是本地业务校验：

- 候选必须是对象且字段集合完全一致；
- 操作必须在白名单；
- 文件名必须通过现有安全校验；
- 内容必须非空且有界；
- 澄清字段和问题数量有界；
- 未知字段、shell、Python、SQL、绝对路径和路径穿越被拒绝；
- 候选大小有上限。

Schema 通过不等于执行授权。后续仍需现有 Policy、租约和 Verifier。

### 14.5 持久化与隐私

Task 新增：

- `planner_mode`；
- `planning_metadata`；
- `clarification_questions`。

规划事件记录分类、模型名称、耗时、Token、响应 ID、输入哈希和错误类别，不默认保存完整模型响应或 Prompt。任务快照保存执行所需的最终结构化请求。

### 14.6 恢复

- 已有计划时，恢复继续使用持久化计划，不再次调用模型。
- 中断在规划阶段且没有计划时，恢复会重新取得租约并重新规划。
- 规划失败是终态，不自动重试或切换 Planner。
- 既有 SIGKILL、并发、fencing 和真实工具语义保持。

### 14.7 评测

固定案例位于 `src/zevorinth/evals/planner_cases.json`，至少 20 个，当前 28 个。

离线测试：

- 使用脚本化 Client；
- 不需要 API Key；
- 不发起网络请求；
- 只评估本地契约、分类、安全失败和端到端执行语义；
- 不代表真实模型质量。

在线模式：

- 必须显式 `eval-planner --online`；
- 需要 API Key 和模型配置；
- 每个案例使用隔离临时数据库与产物目录；
- 记录真实延迟、Token 和失败；
- 默认最多 5 个案例，可显式调整；
- 不自动运行于普通测试或 CI。

### 14.8 第 2.5 阶段指标与在线试点修订

Phase 2 的两个指标口径已修正：

- 旧 `contract_validity_rate` 只从最终分类推断契约有效，现改为基于规划阶段显式记录的 `candidate_received` 和 `candidate_validation`；
- 旧 `invalid_plan_rate` 实际是固定数据集中预期不可执行案例的占比，现改名为 `expected_non_executable_case_share`，旧值保留在 `legacy_metrics`。

新增分层指标：

- `unsafe_side_effect_block_rate`：没有工具副作用；
- `unsafe_classification_accuracy`：不安全请求分类正确；
- `planned_case_end_to_end_success_rate`：仅预期可执行案例；
- `model_call_attempts`、`real_api_calls`、`scripted_client_calls` 分离；
- `clarification_rate`、`unsupported_or_rejected_rate`、`planning_failure_rate` 分离。

在线选择改为版本化 `phase-2.5-v1` 五案例清单，必须显式 `--pilot phase-2.5` 或最多 5 个 `--case-id`。在线执行必须提供持久化输出路径，普通测试和预检不产生网络请求或结果文件。

SDK 兼容性 fail-closed：最低版本为 `3.35.1`。只检测模块存在不足以允许在线调用。

### 14.9 DeepSeek Provider 适配

Provider 必须通过 `ZEVORINTH_LLM_PROVIDER` 显式选择。

| Provider | 密钥变量 | Base URL |
| --- | --- | --- |
| OpenAI | `OPENAI_API_KEY` | SDK 默认 |
| DeepSeek | `DEEPSEEK_API_KEY` | `https://api.deepseek.com` |

DeepSeek 官方 Responses API 支持 OpenAI Python SDK、`text.format.json_schema` 的 `name/schema`、`max_output_tokens` 和 `instructions`。`store` 不受支持，因此 DeepSeek 请求不发送该参数。DeepSeek 文档没有声明 `strict` 字段，因此该 Provider 的 format 不发送 `strict`；本地 Candidate Validator 仍然是最终业务边界。

Provider 与密钥变量一一绑定。DeepSeek 模式不会读取 OpenAI Key，OpenAI 模式不会读取 DeepSeek Key，也不会自动回退或切换服务商。

### 14.10 响应规范化与分类级候选契约

SDK 响应先经过 `response_normalization.py`，再进入规划语义判断。规范化层只向内部结果提供：

- 白名单响应状态和 HTTP 状态；
- incomplete reason；
- output item/content part 类型；
- 文本存在性、长度；
- JSON 错误类别；
- Token、延迟和响应 ID。

它不保留完整模型文本、拒答文本、用户目标或候选字段值。

Candidate Validator 现在按分类检查字段：

- planned：完整文件参数；
- clarification：澄清问题与缺失字段；
- unsupported：不支持原因。

未知字段仍被拒绝；缺少与本分类无关的字段不再导致错误。DeepSeek 的 JSON Schema 只共同要求 `classification`，具体必需字段由本地验证器强制。
