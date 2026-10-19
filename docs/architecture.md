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
