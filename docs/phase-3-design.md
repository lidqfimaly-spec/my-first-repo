# Zevorinth Phase 3 设计：多轮会话与任务上下文连续性

状态：Phase 3.2 已提交（`9146ddea0defd0b3645c1974255bd27171ee596c`）；Phase 3.3 已实现 Schema v4 澄清绑定、确定性意图分类和多轮 `conversation process`，当前工作树尚未提交。真实 LLM 和上下文压缩仍未实现。早期章节保留设计草案，实际状态以第 16–20 节及 `docs/phase-3.3-design.md` 为准。

日期：2026-10-20（Asia/Shanghai）

## 1. 基线与审查范围

| 项目 | 实际值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| HEAD | `9146ddea0defd0b3645c1974255bd27171ee596c` |
| Phase 2.7 提交 | `test: seal phase 2.7 targeted DeepSeek retest` |
| 工作树 | 仅有既有 `.idea/` 未跟踪文件；本次未修改、删除或暂存 |
| 远程 | 未配置 |

已阅读的实现与测试包括：

- `src/zevorinth/models.py`
- `src/zevorinth/state_machine.py`
- `src/zevorinth/event_store.py`
- `src/zevorinth/service.py`
- `src/zevorinth/planner.py`
- `src/zevorinth/planning_validation.py`
- `src/zevorinth/llm_planner.py`
- `src/zevorinth/policy.py`
- `src/zevorinth/artifact_tool.py`
- `src/zevorinth/verifier.py`
- `src/zevorinth/cli.py`
- 现有验收、并发、恢复、EventStore、Planner 和 CLI 测试
- `docs/architecture.md` 及 Phase 1.5、Phase 2.7 文档

`docs/architecture.md` 含有早期 `state.json + events.jsonl` 设计历史；实际运行时代码已经使用 SQLite。本文以代码和 Phase 1.5 之后的文档为准，不把历史规划当作已实现能力。

## 2. 已确认的现状

### 2.1 任务状态与对象

`models.py` 当前有：

- `TaskStatus`：`queued`、`running`、`awaiting_clarification`、`blocked`、`verifying`、`succeeded`、`failed`、`planning_failed`、`interrupted`、`cancelled`、`simulated`。
- `ExecutionMode`：`not_executed`、`real`、`simulated`。
- `PlanningClassification`：`planned`、`clarification`、`unsupported`、`failed`。
- `Task` 保存 `task_id`、`request`、`status`、`plan`、`tool_result`、`artifact`、`verification`、`missing_fields`、`clarification_questions`、`attempt_id`、`claim_generation`、租约字段和规划元数据。

`state_machine.py` 的终态为 `succeeded`、`failed`、`planning_failed`、`cancelled`、`simulated`。终态没有出口：

- `awaiting_clarification` 只能回到 `queued` 或 `cancelled`；
- `blocked` 只能回到 `queued` 或 `cancelled`；
- `interrupted` 可以恢复为 `running`、`verifying`、`awaiting_clarification`、`blocked`、`failed`、`planning_failed` 或 `cancelled`；
- 终态不能重新执行。

当前 `Task` 同时承载“意图、计划、执行尝试、证据和结果”。`attempt_id` 与 `claim_generation` 是逻辑上的运行/尝试标识，但没有独立的 `TaskRun` 表。

### 2.2 SQLite 与事件

`EventStore` 当前 `SCHEMA_VERSION = 2`，数据库里有：

- `metadata`
- `tasks`：保存 `task_json`、状态和更新时间
- `events`：`task_id` 外键、全局 `AUTOINCREMENT sequence`、事件 payload

`transition()` 和 `record_event_and_update()` 在 `BEGIN IMMEDIATE` 中同时更新任务快照并插入事件；事件插入失败会回滚状态更新。`claim_task()` 也在同一事务中更新租约并追加 `task_claimed` / `claim_rejected` / `lease_expired` 等事件。

当前没有：

- Conversation、Message、Turn 表或字段；
- conversation 级事件流；
- 消息级幂等键；
- 会话级活动任务约束；
- schema v2 到 v3 的迁移框架；
- 上下文快照或压缩记录。

`initialize()` 每次启动都会写入 `schema_version=2`。因此新增 schema 时不能只追加表而不改版本迁移逻辑，否则会掩盖版本状态。

### 2.3 执行、恢复与副作用

`TaskService` 的实际路径：

1. `enqueue()` 创建新的 UUID 任务并写 `task_created` 事件。
2. `submit()` 等于 `enqueue()` 后立即 `execute_task()`。
3. `execute_task()` 对终态直接返回；否则通过 `EventStore.claim_task()` 取得租约。
4. `_plan_and_execute()` 记录 `planning_started`，调用 `Planner.plan_request()`。
5. 规划澄清进入 `awaiting_clarification`；不支持进入 `blocked`；规划错误进入 `planning_failed`。
6. 计划通过后进入 `running`，Policy 检查参数。
7. 通过后记录 `tool_started`，调用真实 `ArtifactWriter`，再写工具结果并进入 `verifying`。
8. `ArtifactVerifier` 独立读回文件，验证内容、大小、SHA-256 和工具证据；通过才进入 `succeeded`。

启动 `TaskService` 时会调用 `recover_expired_leases()`：

- 活跃租约不会被新实例抢占；
- 过期租约会使 `running`/`verifying` 任务进入 `interrupted`；
- 早于租约机制、但仍是 `running`/`verifying` 的任务也会被标记为 `interrupted`。

`_resume_interrupted()` 的安全规则：

- 已有真实 `tool_result` 和 artifact evidence：只重新验证，不重写；
- 没有持久化证据但已有 plan：重新经过 Policy 后才可能执行；
- 没有 plan：重新规划；
- 文件已存在但没有工具完成证据时，Policy 会以 `artifact_exists` 阻断，而不是猜测该文件是本次执行的产物。

真实写入使用同目录临时文件、`fsync`、硬链接和“不覆盖”语义；验证器与工具是两条独立路径。

### 2.4 当前澄清机制

Planner 可以返回 `clarification`。`TaskService` 会：

- 把任务转为 `awaiting_clarification`；
- 持久化 `missing_fields`、`clarification_questions`、规划元数据和当前 request；
- 释放租约，不调用文件工具。

CLI 的 `resume` 接受 `--goal`、`--operation`、`--artifact-path`、`--content`，并将其合并为 request patch。只有 `awaiting_clarification` 或 `blocked` 会应用 patch；如果必需字段仍不足，任务保持未执行状态并记录 `clarification_updated`。字段齐全后，任务先转回 `queued`，再重新规划。

当前限制：

- `clarification_questions` 是任务上的字符串列表，没有 question ID；
- 没有“答案对应哪个问题”的持久化记录；
- `clarification_updated` 事件只记录 `missing_fields`，不记录答案来源或用户消息；
- `resume` 的 patch 可以任意改变 `goal`、`operation` 或参数；
- 没有会话消息，因此无法从对话历史重建“用户当时到底回答了什么”；
- 终态任务上的 `resume` 直接返回，不会创建后续任务。

### 2.5 Planner、Policy、Verifier

当前 `Planner` 协议只有：

```python
plan_request(request: TaskRequest) -> PlanningOutcome
```

LLM Planner 只把 `request.goal` 作为一次 Responses API 请求的输入；没有多轮上下文。模型候选仍必须经过：

1. 响应规范化；
2. `PlanningCandidateValidator` 字段、分类、路径、内容大小和操作校验；
3. `TaskService` 状态转换；
4. `ArtifactPolicy`；
5. 租约/fencing；
6. 真实工具；
7. 独立 Verifier。

因此“会话记忆”不能成为绕过这些边界的第二套执行路径。

### 2.6 CLI 与幂等边界

当前 CLI 只有：

- `run`：提交并立即执行一个任务；
- `status`、`events`；
- `resume`：对已有任务补充字段或恢复。

没有 conversation、chat、message 或 turn 命令。

幂等行为是有限且局部正确的：

- 已成功/终态任务的 `execute_task()` 和 `resume()` 返回原任务，不追加事件；
- 同一任务由 SQLite 租约和 fencing 保护，C1/C2 测试覆盖；
- 重复 `submit()` 会创建不同 task ID，不是请求级幂等；
- 真实工具拒绝覆盖已有文件；
- 没有用户消息幂等键，因此网络重试或 CLI 重发可能创建重复任务。

## 3. 已确认的缺口与证据

| 缺口 | 证据 | 影响 |
| --- | --- | --- |
| 会话服务已实现，任务接入尚未实现 | `conversation_service.py` 已提供创建、追加、历史和同内容幂等；`TaskService`/CLI 仍未接入 conversation_id 和 turn 关联 | 多轮输入可以独立持久化，但还没有进入任务执行流程 |
| 任务会话关联已接通，后续意图字段未建立 | `TaskService.enqueue/submit` 已支持 conversation/turn；`origin_turn_id`、`parent_task_id` 尚未建立 | 基础会话任务可创建；后续 intent supersede 仍未实现 |
| 澄清答案没有结构化持久化 | `Task.clarification_questions` 只是字符串列表；`CLARIFICATION_UPDATED` 只有 `missing_fields` | 无法证明哪个答案对应哪个问题，也无法审计回填 |
| 没有意图变化判定 | `resume()` 对 `awaiting_clarification`/`blocked` 接受任意允许字段 patch | 用户改变目标时可能被当作普通补充，导致错误计划 |
| 没有上下文输入 Planner | `Planner.plan_request()` 只接收 `TaskRequest`；LLM 只发送 goal | 多轮要求无法进入规划，除非人工把全部内容拼进 goal |
| 重复消息尚无幂等返回 | `messages` 已有唯一索引，但 `append_user_message()` 目前对重复客户端 ID 抛 `MessageConflict` | 网络/CLI 重试还没有返回原记录的幂等语义 |
| 会话级活动任务边界尚未接入服务 | 数据库已有 `uq_tasks_one_active_per_conversation`，但 TaskService 不写会话 ID | 并发创建会话任务尚未经过服务级验证 |
| v2 到 v3 迁移已实现 | `EventStore.initialize()` 已有版本预检、原子迁移和回滚测试 | 迁移能力本身不再是缺口；后续变更仍需保持测试 |
| 没有上下文预算/压缩 | 现有 Planner 只限制 goal 16 KiB | 长对话可能超限、截断当前意图或泄露旧内容 |
| 会话服务级测试尚未完成 | 已有 schema/迁移测试和 EventStore 基础持久化测试；尚无 ConversationService、消息幂等和并发 turn 服务测试 | 服务层的原子性、顺序和并发行为仍未证明 |

## 4. 拟议数据模型

以下为 Phase 3.1 已实现的 schema v3；Phase 3.2 只逐步接入服务层，不回写或重写现有 `tasks` 和 `events` 数据。

### 4.1 实体关系

```text
Conversation 1 ── N Turn
Turn         1 ── N Message
Turn         0..1 Task
Task         1 ── N TaskIntent
Task         1 ── N Event（现有 task events）
Task         0..1 ArtifactEvidence
Task         1 ── N ClarificationRequest
ClarificationRequest 0..1 ClarificationAnswer
Conversation 1 ── N ConversationEvent
```

逻辑上的 `TaskRun` 暂不新增独立表：当前 `attempt_id` + `claim_generation` + 现有 task events 足以表达一次认领/尝试。若后续需要独立查询每次尝试的工具结果和验证结果，再增加 `task_runs`，不在 Phase 3 首个增量中做重复存储。

### 4.2 `conversations`

| 字段 | 类型 | 约束/含义 |
| --- | --- | --- |
| `conversation_id` | TEXT | UUID 主键 |
| `status` | TEXT | `active` / `archived`；首版不实现删除 |
| `active_task_id` | TEXT NULL | 设计草案字段；Phase 3.1 实际未建立 |
| `version` | INTEGER | 乐观并发版本，每次会话变更递增 |
| `created_at` | TEXT | UTC ISO-8601 |
| `updated_at` | TEXT | UTC ISO-8601 |

`active_task_id` 必须指向同一会话的任务；切换活动任务与任务状态变更必须在同一事务中完成，或通过可恢复的 pending 状态完成。

### 4.3 `messages`

| 字段 | 类型 | 约束/含义 |
| --- | --- | --- |
| `message_id` | TEXT | UUID 主键 |
| `conversation_id` | TEXT | 外键，不可为空 |
| `turn_id` | TEXT | 外键，不可为空 |
| `role` | TEXT | `user` / `assistant` |
| `kind` | TEXT | `user_input` / `clarification` / `status` 等受限枚举 |
| `content` | TEXT | 原文；按消息大小上限校验 |
| `content_sha256` | TEXT | 内容哈希，供审计 |
| `byte_count` | INTEGER | UTF-8 字节数 |
| `client_message_id` | TEXT NULL | 客户端幂等键 |
| `created_at` | TEXT | UTC ISO-8601 |
| `metadata_json` | TEXT | 白名单元数据，不放密钥 |

约束：

- `UNIQUE(conversation_id, client_message_id)`，且 `client_message_id` 非空时才参与幂等；
- 消息不可原地修改；纠错必须是新消息；
- 不把完整消息复制到事件 payload 或日志。

### 4.4 `conversation_turns`

| 字段 | 类型 | 约束/含义 |
| --- | --- | --- |
| `turn_id` | TEXT | UUID 主键 |
| `conversation_id` | TEXT | 外键 |
| `turn_index` | INTEGER | 会话内严格递增序号 |
| `user_message_id` | TEXT | 唯一，指向一条 user message |
| `assistant_message_id` | TEXT NULL | 对应助手消息 |
| `status` | TEXT | `received` / `classified` / `dispatched` / `awaiting_clarification` / `completed` / `failed` / `cancelled` / `needs_attention` |
| `intent_kind` | TEXT NULL | `new_task` / `clarification_answer` / `modify_pending` / `follow_up` / `status_query` / `duplicate` / `ambiguous` |
| `task_id` | TEXT NULL | 该 turn 关联的任务 |
| `created_at` / `updated_at` | TEXT | UTC ISO-8601 |

约束：

- `UNIQUE(conversation_id, turn_index)`；
- `UNIQUE(user_message_id)`；
- 一个 turn 最多关联一个根任务；
- 会话的 `turn_index` 分配必须在 `BEGIN IMMEDIATE` 中完成。

### 4.5 `clarification_requests` 与 `clarification_answers`

`clarification_requests`：

| 字段 | 含义 |
| --- | --- |
| `question_id` | UUID 主键 |
| `task_id` / `conversation_id` / `turn_id` | 关联 |
| `field_name` | `goal` / `operation` / `artifact_path` / `content` |
| `ordinal` | 同一任务内的问题序号 |
| `question_text` | 已通过本地长度限制的问题文本 |
| `status` | `open` / `answered` / `cancelled` / `stale` |
| `created_at` / `answered_at` | 时间 |
| `answer_message_id` | 当前有效答案消息，可空 |

`clarification_answers`：

| 字段 | 含义 |
| --- | --- |
| `answer_id` | UUID 主键 |
| `question_id` | 外键 |
| `message_id` | 用户答案消息 |
| `normalized_value_json` | 经过本地字段校验后的值 |
| `validation_status` | `accepted` / `rejected` / `pending_apply` / `applied` |
| `created_at` | 时间 |

约束：

- 一个 question 同时最多一个 `open` 状态；
- 同一 question + message 不重复插入；
- 原始答案保存在 `messages.content`，答案表不重复保存全文；
- 任务进入终态或已有工具副作用后，不允许把答案当作“撤销”或重新执行依据。

### 4.6 `task_intents`

| 字段 | 含义 |
| --- | --- |
| `intent_id` | UUID 主键 |
| `task_id` | 外键 |
| `version` | 从 1 递增 |
| `source_turn_id` | 产生或修改该意图的 turn |
| `request_json` | 通过本地结构校验的 `TaskRequest` 快照 |
| `request_hash` | 规范化请求哈希 |
| `change_kind` | `initial` / `clarification_answer` / `supplement` / `supersede` |
| `created_at` | 时间 |

`UNIQUE(task_id, version)`；intent 记录不可变。当前 `Task.request` 必须与最新 intent 快照一致；启动重建时不一致则进入 `needs_attention`，不猜测。

### 4.7 `conversation_events`

为避免把现有 task event 表改成可空 `task_id`，新增独立的追加式会话事件表：

| 字段 | 含义 |
| --- | --- |
| `sequence` | `INTEGER PRIMARY KEY AUTOINCREMENT` |
| `event_id` | UUID 唯一 |
| `conversation_id` / `turn_id` | 关联 |
| `task_id` | 可空，指向任务 |
| `event_type` | `conversation_created`、`message_recorded`、`turn_classified`、`task_linked`、`clarification_requested`、`clarification_answered`、`intent_superseded`、`conversation_needs_attention` 等 |
| `occurred_at` | UTC ISO-8601 |
| `payload_json` | 白名单元数据；不复制完整消息或模型原文 |

现有 task `events` 继续作为执行审计的主来源；conversation events 只记录会话层状态。查询时间线时按 `turn_index`、task event `sequence` 和 conversation event `sequence` 组合展示，不伪造一个跨表事务。

### 4.8 `tasks` 的追加字段

拟增加可空字段：

- `conversation_id TEXT NULL`
- `origin_turn_id TEXT NULL`
- `parent_task_id TEXT NULL`
- `supersedes_task_id TEXT NULL`

并在任务 JSON 中保持相同字段的可选序列化。旧任务缺失这些字段时默认 `None`，继续可读。

Phase 3.1 实际仅增加 `conversation_id`；`origin_turn_id`、`parent_task_id` 和 `supersedes_task_id` 尚未建立。`conversation_id` 已有指向 `conversations.conversation_id` 的外键；旧任务的该列为 `NULL`。

活动任务约束建议使用部分唯一索引：

```sql
CREATE UNIQUE INDEX uq_tasks_one_active_per_conversation
ON tasks(conversation_id)
WHERE conversation_id IS NOT NULL
  AND status IN (
    'queued', 'running', 'awaiting_clarification',
    'blocked', 'verifying', 'interrupted'
  );
```

该索引必须与 `conversations.active_task_id` 的应用层事务校验一起测试。旧任务 `conversation_id IS NULL` 不受影响。

### 4.9 暂不新增的表

- `task_runs`：首版用 `attempt_id`、`claim_generation` 和 task events 表达尝试；
- `context_snapshots`：首版确定性地从消息和任务重建上下文；只有引入摘要时才增加带 source hash 的快照表；
- 向量索引、分布式队列、外部会话服务：不在 Phase 3 首个增量中引入。

## 5. 会话与任务的关联规则

### 5.1 活动任务

每个会话最多有一个活动任务。活动状态为：

```text
queued
running
awaiting_clarification
blocked
verifying
interrupted
```

终态任务不参与活动任务指针。终态包括 `succeeded`、`failed`、`planning_failed`、`cancelled`、`simulated`。

### 5.2 一条新用户消息的判定顺序

按以下优先级判定，前一条命中后停止：

1. **幂等重复**：同一 `conversation_id + client_message_id` 已存在，返回原 turn/task，不创建新任务。
2. **明确答案**：消息绑定了 `question_id`，且该问题属于当前会话的活动任务、状态为 `open`，则归为 `clarification_answer`。
3. **没有活动任务**：创建新任务；若目标仍不足，让 Planner 产生澄清，而不是由会话层猜测。
4. **活动任务已终态**：默认创建新任务；若只是状态查询，则返回任务状态，不创建任务。
5. **活动任务未执行且没有工具副作用**：允许补充或修改；具体见 5.3。
6. **活动任务已进入工具执行/验证阶段**：只允许状态查询或新建独立任务；不得修改、取消或假装撤销已有副作用。
7. **无法判定**：进入会话层澄清，不创建任务、不调用 Planner、不调用工具。

### 5.3 补充、修改未执行计划和新建任务

“材料性变化”定义为以下任一字段规范化后不同：

```text
goal
operation
artifact_path
content
```

判定规则：

| 当前活动任务状态 | 消息内容 | 处置 |
| --- | --- | --- |
| `awaiting_clarification` | 回答开放问题，补齐字段 | 同一任务，经 `TaskService.resume()`，进入重新规划 |
| `awaiting_clarification` | 与开放问题无关的新目标 | 新任务；旧任务仅在无副作用时标记 `cancelled` 并记录 `superseded_by_task` |
| `blocked` | 修正同一被阻断请求的参数 | 同一任务 `resume()`，重新经过 Policy |
| `blocked` | 新目标或不相关操作 | 新任务；旧任务保持原阻断记录，不删除 |
| `queued` / `interrupted`，且没有 `tool_started` | 只补充不改变材料字段 | 记录消息/上下文；必要时同一任务重新规划 |
| `queued` / `interrupted`，且没有 `tool_started` | 改变材料字段 | 新任务，`parent_task_id`/`supersedes_task_id` 指向旧任务；旧任务在状态机允许时 `cancelled` |
| `running` / `verifying`，已有 `tool_started`、`tool_result` 或 artifact | 任何材料性变化 | 只创建新任务；旧任务继续恢复/验证，禁止撤销或覆盖已有产物 |
| 任意终态 | 新的创建/修改要求 | 新任务；旧任务和旧产物保持不可变 |
| 任意状态 | 单纯状态查询 | 返回状态，不创建任务 |
| 任意状态 | 无法安全分类 | 会话澄清；不猜测、不执行 |

“修改未执行计划”推荐通过**新任务 supersede 旧任务**，而不是在原任务上重写 plan：

- 当前状态机没有从 `running` 回到 `queued` 的合法转换；
- 旧 plan、事件和证据需要保留；
- 新 task ID 天然提供新的 artifact 目录，避免覆盖；
- 旧任务只有在没有 `tool_started`、`tool_result` 或 artifact 时才能安全取消；
- 任何已有副作用都保留原任务和原产物，不做“假撤销”。

如果未来希望复用同一 task ID 修改计划，需要先设计显式的 `replanning`/plan invalidation 状态和新的状态机测试；不能直接修改 `task_json` 或绕过现有转换表。

## 6. 澄清问题与答案的持久化/回填流程

### 6.1 规划产生澄清

1. `ConversationService.send()` 在事务中写入 user message 和 turn，分配 `turn_index`。
2. 通过 `TaskService.enqueue()` 创建任务；任务带 `conversation_id` 和 `origin_turn_id`。
3. Planner 返回 `clarification`，`TaskService` 继续使用现有路径进入 `awaiting_clarification`。
4. `ConversationService` 为每个允许的 `missing_field` 创建 `clarification_requests`，并写入一条 assistant 消息（问题文本）。
5. 记录 `clarification_requested` conversation event；任务侧已有 `planning_clarification` event。

### 6.2 用户回答

1. 新 user message 写入，带 `client_message_id` 和可选 `answer_to_question_ids`。
2. 在 `BEGIN IMMEDIATE` 中校验：
   - 会话一致；
   - 问题属于当前活动任务；
   - 问题仍为 `open`；
   - 任务尚未进入工具副作用阶段。
3. 将答案规范化成允许字段：
   - Phase 3 首个实现优先支持显式结构化答案（字段 + 值）；
   - 自由文本到结构化字段的自动解释不能未经测试就引入；无法确定时继续提问。
4. 写入 `clarification_answers`，状态为 `pending_apply`；同一 `question_id + message_id` 重复请求只返回已有记录。
5. 调用现有 `TaskService.resume(task_id, request_patch)`；不直接写任务状态。
6. `resume()` 成功后，将 answer 标记为 `applied`；如果任务仍缺字段，保持 open 或生成新的问题请求。
7. 如果 `resume()` 因冲突、任务已终态、已有工具副作用或数据库错误失败，answer 标为 `rejected` 或保留 `pending_apply` 并让会话进入 `needs_attention`；不得伪造任务成功。

### 6.3 崩溃窗口

| 崩溃点 | 可能状态 | 恢复策略 |
| --- | --- | --- |
| 消息已写入，turn 尚未创建 | 残留 message | 以 message_id 幂等键检查；补齐 turn 或标记待处理，不新建第二个 message |
| turn 已创建，任务尚未创建 | `dispatched` 前的 turn | 用 turn 的 request 快照创建一次任务；幂等键防止重复 |
| 任务已创建，turn 关联尚未写 | 任务有 `origin_turn_id`，turn 无 task | 以 `origin_turn_id` 修复关联；无法唯一匹配则 `needs_attention` |
| 答案已写入，`resume` 前崩溃 | answer=`pending_apply` | 启动时重试一次安全应用；检查任务是否已有副作用 |
| `resume` 成功，answer 状态未更新 | 任务已变化，answer 仍 pending | 只更新 answer 状态，不再次执行任务 |
| 工具已写入，任务证据尚未提交 | 现有 task recovery 路径 | 遵循现有 `tool_result`/artifact evidence 规则；无证据时不覆盖，停在阻断/待确认 |

## 7. 上下文重建与压缩

### 7.1 `ConversationContext`

Planner 调用前由 `ConversationService` 组装只读上下文：

```text
conversation_id
current_turn_id
active_task:
  task_id, status, intent_version, request, missing_fields,
  clarification_questions, artifact_evidence, verification_summary
recent_turns:
  user/assistant messages, bounded by count/bytes
open_clarifications
context_budget
source_hashes
```

不把 ConversationContext 当作执行授权。它只是 Planner 的输入数据；Planner 的输出仍必须经过 Candidate Validator、TaskService、Policy、租约和 Verifier。

### 7.2 预算与优先级

建议首版预算（尚待实现和测量）：

- 单条消息最大 8 KiB UTF-8；
- 当前自然语言 goal 继续受现有 16 KiB 限制；
- 上下文总预算 16 KiB；
- 最多最近 8 个 turn；
- 当前活动任务 request、缺失字段和开放澄清问题优先保留；
- 超预算时先从最旧的已完成 turn 开始截断，并显式记录 `truncated_turns`、原始字节数和内容哈希；
- 当前用户消息或当前任务的关键字段不能被静默截断；超限应拒绝并请求更短输入。

### 7.3 重建与压缩

- 重启后从 `conversations`、`conversation_turns`、`messages`、`clarification_*` 和任务快照重建；不依赖进程内缓存。
- 首版只做确定性截断，不调用模型生成摘要，因此不需要 `context_snapshots`。
- 未来若增加摘要，必须：
  - 存 source message high-water mark 和 source hash；
  - 摘要只作为补充，不覆盖原始消息；
  - source hash 不匹配时丢弃摘要并从原文重建；
  - 摘要不能改变 intent、权限或任务状态。
- 旧对话内容不会因为新任务创建而删除；任务和产物也不随会话归档删除。

### 7.4 Planner 接口演进

拟议接口：

```python
plan_request(
    request: TaskRequest,
    *,
    context: ConversationContext | None = None,
) -> PlanningOutcome
```

`DeterministicPlanner` 可以忽略 `context`；`LLMPlanner` 将其作为 JSON 数据发送，不把历史消息拼进系统指令。现有无 context 调用保持兼容，默认 `None`。

## 8. 中断恢复、幂等与重复提交

### 8.1 复用现有任务恢复

会话层不复制任务恢复逻辑：

- 任务租约、fencing、过期检测、工具证据和 Verifier 仍由 `TaskService`/`EventStore` 负责；
- 会话恢复只负责修复消息/turn/任务关联，以及 pending answer 的应用状态；
- 绝不在会话层直接写 `tasks.status`、`tool_result` 或 `artifact`。

### 8.2 幂等键

- 用户消息：`UNIQUE(conversation_id, client_message_id)`；
- 澄清答案：`UNIQUE(question_id, message_id)`；
- turn：`UNIQUE(conversation_id, turn_index)`；
- task intent：`UNIQUE(task_id, version)`；
- 重复 `send` 返回已有 turn/task，不追加第二个 `task_created`；
- 重复 `resume` 终态任务保持现有 no-op 行为。

### 8.3 并发

- 同一会话的消息分配使用 `BEGIN IMMEDIATE`，并递增 `conversations.version`；
- 活动任务唯一性由 `conversations.active_task_id` 校验 + 部分唯一索引共同保证；
- 同一任务仍由 `claim_task` 的租约和 fencing 保证只有一个执行者；
- 不同会话使用不同 task ID/artifact 目录，不共享可变上下文；
- 进程内锁只能作为性能优化，不能作为跨进程互斥依据。

## 9. 数据迁移与向后兼容

### 9.1 v2 到 v3

迁移只做追加：

1. 创建新表（conversations、messages、conversation_turns、clarification_requests、clarification_answers、task_intents、conversation_events）。
2. 给 `tasks` 增加可空列（conversation_id、origin_turn_id、parent_task_id、supersedes_task_id）。
3. 创建索引，包括活动任务部分唯一索引。
4. 将 `metadata.schema_version` 从 `2` 更新为 `3`。
5. 不重写已有 `task_json` 或 event payload；旧任务的会话字段为 `NULL`。

迁移必须在 `BEGIN IMMEDIATE` 中执行；失败则回滚，旧数据库仍可被旧版本读取。不得在迁移中删除表、删列或修改已有事件。

### 9.2 版本检查

- 读到 `schema_version > 3`：fail closed，不尝试降级；
- 读到未知/缺失版本：按可识别的旧版本迁移，或停止并要求人工处理；
- 迁移成功后才写入 v3；
- 每次初始化重复执行应幂等；
- 旧代码写 v2 数据库后，新代码仍能迁移；新代码创建的 v3 数据库不支持旧代码降级。

### 9.3 兼容性

- 旧 `run/status/events/resume` 继续工作；
- 旧任务没有 conversation，`Task.from_dict` 使用默认 `None`；
- 新任务可以有 conversation，也可以保持无会话的旧 API 路径；
- 不能自动把旧任务猜测性地归入某个会话；需要显式 attach 或人工确认。

## 10. 安全边界与失败关闭

1. 会话消息是不可信数据；不得改变系统指令、工具列表、Provider、权限或状态机。
2. 会话层只能调用 `TaskService`/Planner/Policy/Verifier 的公开边界，不能直接执行工具或写任务终态。
3. 任何 context 截断、意图歧义、问题不匹配、任务已终态、已有副作用、数据库错误都 fail closed。
4. 不把“已有产物”当成“本次成功”；不自动删除、覆盖或撤销产物。
5. 不把模型摘要或意图分类当作执行授权；最终仍需本地 Candidate Validator 和 Policy。
6. 不在事件、日志或错误消息中复制完整消息、完整模型响应、认证头或密钥；只记录哈希、长度和白名单元数据。
7. 不读取 `.env` 或凭据；测试使用临时目录和临时数据库。
8. 不对会话/消息/产物提供隐式级联删除；归档只影响查询，不删除证据。

## 11. 分阶段实现与推荐提交边界

### Phase 3A：Schema 与迁移

- 增加 v3 表、`tasks` 可空列、索引；
- 原子 v2→v3 迁移和 idempotency 测试；
- 扩展 `Task.from_dict/to_dict`，默认向后兼容；
- 不接入 CLI，不改变 Planner。

推荐提交：`feat: add conversation schema and migration`

### Phase 3B：Conversation/Turn/Message 服务

- 新增 Conversation/Turn/Message 模型和存储方法；
- 新增 `ConversationService.create/send/show`；
- 只创建/关联任务，不改变澄清协议；
- 消息幂等、顺序和会话隔离测试。

推荐提交：`feat: add durable conversation turns`

### Phase 3C：澄清问题与答案回填

- 保存 question ID、答案消息、规范化值和应用状态；
- 通过 `TaskService.resume()` 回填；
- 处理 pending/apply/崩溃恢复；
- 保持无工具调用直到字段完整。

推荐提交：`feat: persist clarification answers`

### Phase 3D：意图变化与 supersede

- 实现显式意图判定和材料字段 diff；
- 未执行计划变更时创建新任务、取消旧任务或保留旧记录；
- 有副作用时只允许新任务；
- 终态 follow-up 创建新任务。

推荐提交：`feat: resolve conversation intent changes`

### Phase 3E：上下文重建与 Planner 接入

- 组装有预算的 `ConversationContext`；
- 扩展 Planner 接口并保持旧调用兼容；
- 只做确定性截断，不做模型摘要；
- prompt injection 仍作为数据处理。

推荐提交：`feat: add bounded conversation context`

### Phase 3F：恢复与并发加固

- 启动时修复 pending turn/answer 和任务关联；
- 增加跨进程并发、重复提交、SIGKILL 和数据库失败测试；
- 补充 CLI 状态查询和文档。

推荐提交：`test: harden conversation recovery and concurrency`

## 12. 测试与验收标准

### 12.1 Schema/迁移

- v2 数据库包含任务和事件时，迁移到 v3 后数据完全保留；
- 重复执行迁移幂等；
- 迁移失败不留下半迁移 schema；
- 旧任务仍可 `status`、`events`、`resume`；
- `schema_version > 3` fail closed。

### 12.2 消息、Turn 与幂等

- 两条消息顺序稳定，重开数据库后 `turn_index` 不变；
- 同一 `client_message_id` 重复发送返回同一 turn/task，不追加 `task_created`；
- 不同会话的相同消息互不干扰；
- 并发发送时 `turn_index` 唯一且连续；
- 任务创建失败时不会出现两个任务或静默丢失消息。

### 12.3 澄清回填

- 规划澄清后任务为 `awaiting_clarification`、工具调用为 0、无产物；
- 答案绑定 question ID 后，任务 request 只更新允许字段；
- 答案不完整时任务仍等待澄清，不规划、不执行；
- 重复答案幂等；
- 未知/过期 question、终态任务、已有工具副作用时拒绝应用；
- `resume` 后文件真实写入且独立 Verifier 通过；
- 数据库失败/进程中断时 answer 不伪报 applied。

### 12.4 意图变化

- 未执行任务补充材料字段时，按规则创建新任务或安全更新；
- 修改 plan 材料字段不被当作普通回答；
- 有 `tool_started` 或 artifact 时，旧任务不被取消、产物不被覆盖；
- 终态任务 follow-up 创建新任务，旧任务事件和产物不变；
- 模糊消息只产生澄清，工具调用为 0。

### 12.5 上下文

- 当前任务和开放问题在截断后仍存在；
- 超长历史只截断旧 turn，并记录截断元数据；
- 单条超限消息 fail closed；
- prompt injection 不能改变工具/权限；
- 重启后从数据库重建的上下文与原始消息哈希一致；
- 摘要机制未实现时，不得伪造摘要结果。

### 12.6 恢复与并发

- 真实 SIGKILL 后，会话消息、turn 和任务关联可诊断；
- 任务已有真实 artifact 时只重新验证，不重写；
- 无证据的现成文件不被自动覆盖；
- 同一任务执行者和恢复者竞争时只有一个有效 claim；
- 同会话两个并发新消息不会创建两个活动任务；
- 同会话并发答案与 resume 不会产生两次真实副作用；
- 不同会话并发任务不串线。

### 12.7 回归

- 现有 98 个离线测试全部继续通过；
- A1–A6、SIGKILL、并发/fencing、EventStore 事务和 Phase 2.7 两案例离线测试不删除；
- `compileall`、`git diff --check` 通过；
- 所有新增网络请求测试使用 Fake Client，真实 API 调用为 0。

## 13. 文件级改动清单（拟议）

### 新增

- `src/zevorinth/conversation_service.py`
- `tests/test_conversation_store.py`
- `tests/test_conversation_service.py`
- `tests/test_conversation_recovery.py`
- `tests/test_conversation_migration.py`
- `docs/phase-3-implementation.md`（实现后）
- `docs/phase-3-results.md`（验证后）

### 修改

- `src/zevorinth/models.py`：Conversation/Message/Turn/Clarification/Intent 模型和 Task 关联字段
- `src/zevorinth/event_store.py`：schema v3、迁移、会话表/事务方法、活动任务索引
- `src/zevorinth/service.py`：任务接收 conversation/turn 元数据；保持执行和恢复语义
- `src/zevorinth/planner.py`：可选上下文参数，默认兼容旧调用
- `src/zevorinth/llm_planner.py`：将上下文作为有界 JSON 数据发送；不增加工具
- `src/zevorinth/cli.py`：conversation create/send/answer/show；保留旧命令
- `README.md`：多轮用法、限制和隐私边界
- `docs/architecture.md`：补充实际 v3 数据模型与执行边界
- `docs/execution-log.md`
- `docs/file-manifest.md`

不建议在第一个增量中引入：

- 外部消息队列；
- Redis/分布式锁；
- 向量数据库；
- 自动摘要模型调用；
- 删除级联；
- 多 Agent 会话编排。

## 14. 待验证事项与未决问题

以下内容不能由当前代码确认，必须通过实现和测试验证：

1. 用户期望的会话 CLI/API 形态（create/send/answer/show 的具体命令）尚未定义。
2. 自由文本答案如何可靠映射到 `goal`/`operation`/`artifact_path`/`content`，尚未设计出确定性方案；首版应优先要求显式字段，未知时继续澄清。
3. 是否需要独立的 `task_runs` 表；当前 `attempt_id`/`claim_generation` 是否足以支持未来审计，需在实现并发测试后确认。
4. 消息保留期限、用户删除/归档语义和敏感内容策略尚未定义。
5. 上下文预算 8 KiB/16 KiB/8 turns 是拟议值，尚未经过真实模型和离线评测验证。
6. SQLite 仍是单机共享数据库语义；没有多实例、跨机器或网络文件系统验证。
7. 意图分类若未来使用 LLM，需要额外的受限分类契约和离线夹具；本阶段不调用真实 API。
8. `tasks` 增加列和 `Task.to_dict()` 字段会影响外部消费者，需确认是否存在未知消费者；当前仓库内测试可验证兼容性，外部 API 兼容性待确认。
9. 旧任务不会被自动归入会话；是否需要显式 attach 命令尚未决定。
10. `docs/architecture.md` 的早期设计段落与实际实现存在历史差异；本次不修改该文档，后续应单独整理，避免读者把 `state.json` 当成现状。

## 15. 本次只读审查结论

- 现有任务内核具备可靠的状态转换、SQLite 事务、租约/fencing、真实写入、独立验证和有限恢复能力；
- 现有澄清仅支持“任务字段 patch + 重新执行”，不支持会话级问答存档或答案溯源；
- 现有系统没有 Conversation/Message/Turn 持久化，也没有会话上下文进入 Planner；
- 直接复用现有 `TaskService` 作为执行边界是可行的，但必须先建立会话/消息的原子存储和幂等协议；
- 任何多轮记忆方案都不得绕过 Policy、租约、工具权限或 Verifier；
- 在实现和测试完成前，不能宣称 Phase 3 的多轮会话能力已经可用。

## 16. Phase 3.1 实际实现记录

本节记录 2026-10-20 Phase 3.1 的最小落地范围，避免把设计草案误读为已实现服务。

### 16.1 实际 schema v3

已建立：

- `conversations`
- `conversation_turns`
- `messages`
- `conversation_events`
- `tasks.conversation_id` 可空列

尚未建立：

- `task_intents`
- `clarification_requests`
- `clarification_answers`
- `context_snapshots`
- 独立 `task_runs`

因此当前只有会话能力的数据库基础设施，没有会话 CLI、消息写入服务、澄清回填或意图分类。

### 16.2 版本来源与迁移

- 版本来源仅为现有 `metadata.schema_version`；`PRAGMA user_version` 保持未使用（值为 0）。
- 初始化先做只读版本预检，再进入 `BEGIN IMMEDIATE`。
- v2 → v3 在一个短 SQLite 事务中完成：创建 `conversations`；重建 `tasks` 以加入 `conversation_id` 外键；复制旧任务；创建其余会话表、索引；执行 `PRAGMA foreign_key_check`；最后写入 v3。
- `tasks` 重建只进行一次受控的表替换：创建 `tasks_v3`、复制数据、删除旧表、重命名新表，并恢复旧任务上已有的索引和触发器。旧任务没有会话关联，复制时 `conversation_id` 为 `NULL`。
- `tasks` 重建期间在事务外临时关闭 `PRAGMA foreign_keys`，提交前执行 `PRAGMA foreign_key_check`，随后恢复 `PRAGMA foreign_keys=ON`。这是有明确边界的迁移操作，不改变普通任务连接的 ON 配置。
- 迁移失败会回滚 schema、索引、任务和事件；测试验证了版本、表、列和数据均保持 v2 状态。
- 读到高于 v3 的版本时，在 schema 写入前抛出 `SchemaVersionError`。
- 没有自动把旧任务关联到会话；旧任务 `conversation_id` 为 `NULL`。
- WAL 配置对并发初始化做有界重试，避免 `PRAGMA journal_mode` 在进入迁移事务前产生假 busy。

迁移失败时，事务内的 schema/data 会回滚，但连接初始化阶段已经设置的 WAL 模式属于事务外的持久数据库设置，可能仍然保持为 `wal`；测试明确断言了这一现实，不把 WAL 变化描述为事务回滚的一部分。

### 16.3 当前约束

- `messages(conversation_id, client_message_id)` 对非空客户端幂等键唯一；
- `conversation_turns(conversation_id, turn_index)` 唯一；
- 消息的 `(turn_id, conversation_id)` 必须匹配同一 turn；
- turn/event 的任务关联必须匹配同一会话的 task；
- `tasks.conversation_id` 的非空值必须引用真实 `conversations`；`NULL` 仍允许用于旧任务；
- 同一会话最多一个活动任务，由部分唯一索引约束；
- 所有会话表外键在当前 `EventStore` 连接上启用 `PRAGMA foreign_keys=ON`。
- v3 校验不是按名称猜测：它比较 `sqlite_master` 中实际表/索引 SQL，覆盖列、外键、UNIQUE、CHECK 和部分索引谓词。规范化只处理字面量外部的空白和大小写，并保留单引号字符串内容，避免把不同大小写的 CHECK 值误判为相同。

### 16.4 Phase 3.1 测试

新增 `tests/test_conversation_schema.py`，当前共 16 个测试，覆盖空白数据库、v2 迁移、旧任务读取与执行、重复初始化、迁移失败回滚与 WAL 行为、高版本拒绝、并发初始化、并发 v2 迁移、消息/turn 唯一键和外键、任务活动唯一约束、任务会话外键、旧索引/触发器保留、伪造 v3 拒绝和 `Task` 位置参数兼容性。

## 17. Phase 3.2 第一小阶段实际实现

本节记录第一小阶段。当时只实现领域模型和 EventStore 持久化；随后第 18 节增加了 ConversationService 和消息幂等。TaskService 关联和上下文压缩仍未实现。

已新增：

- `Conversation`、`Message`、`ConversationTurn` 数据类及基础枚举；
- `EventStore.create_conversation()`；
- `EventStore.get_conversation()`；
- `EventStore.append_user_message()`；
- `EventStore.list_conversation_messages()`；
- 会话创建、消息写入和 `message_recorded` conversation event 的事务写入。

实际行为：

- `create_conversation()` 创建 active 会话并写入 `conversation_created` 事件；
- `append_user_message()` 在 `BEGIN IMMEDIATE` 内验证会话、分配 turn 序号、写 turn、写 message、写 conversation event，并更新会话时间和版本；
- 历史按 `turn_index`、`created_at`、`message_id` 排序，默认 50 条，最大 200 条；
- 重复 `client_message_id` 当前抛出 `MessageConflict`，尚未实现“相同内容返回原记录”的幂等返回；
- 任务与 turn 的关联、TaskService 的 `conversation_id` 参数和 ConversationService 仍未实现；
- 未改变 Schema version 3。

新增测试 `tests/test_conversation_store.py` 覆盖会话重开、消息/turn/event 原子写入、历史排序和上限、重复客户端消息拒绝、无效会话拒绝和注入事件写入失败回滚。

## 18. Phase 3.2 第二小阶段实际实现

本轮在已有 EventStore 持久化之上增加服务层和消息幂等，不接入 TaskService。

已新增：

- `src/zevorinth/conversation_service.py`
- `ConversationService.create_conversation()`
- `ConversationService.get_conversation()`
- `ConversationService.append_user_message()`
- `ConversationService.history()`

幂等行为：

- `append_user_message()` 在 `BEGIN IMMEDIATE` 内按 `(conversation_id, client_message_id)` 查询已有消息；
- 相同 key 且内容、哈希和字节数一致：返回已有 `ConversationTurn` 和 `Message`，不新增 turn；
- 相同 key 但内容不同：抛出 `MessageConflict`，不覆盖原记录；
- 不同 key：正常创建新 turn 和 message；
- 数据库 `uq_messages_client_id` 仍是最终唯一约束。

并发测试覆盖：

- 两个进程同时提交相同客户端 ID 和相同内容，只产生一条消息和一个 turn；
- 两个进程使用不同客户端 ID，turn 序号唯一、消息不丢失；
- 两个进程使用相同客户端 ID 和不同内容，一个成功、一个 `MessageConflict`，只保留原记录。

本小节记录服务层完成时的状态；TaskService 会话关联随后在第 19 节实现。尚未实现意图分类、上下文压缩和自动任务续接，也没有任何真实模型 API 调用。

本轮没有改变 Schema version 3。

## 19. Phase 3.2 任务会话关联实际实现

已新增：

- `TaskService.enqueue(request, conversation_id=None, turn_id=None)`；
- `TaskService.submit(request, conversation_id=None, turn_id=None)`；
- `EventStore.create_task(task, event, turn_id=None)`。

事务行为：

- 在 `BEGIN IMMEDIATE` 内验证 conversation 存在；
- 验证 turn 存在、属于同一 conversation 且尚未关联任务；
- 检查同一 conversation 是否已有活动任务；
- 插入 task 和 `task_created` 事件；
- 更新 `conversation_turns.task_id`；
- 写入 `task_linked` conversation event；
- 任一步失败整体回滚。

兼容性：

- 不传 conversation_id/turn_id 的旧调用仍创建 `conversation_id=NULL` 的任务；
- 非法 conversation、跨会话 turn、已关联 turn 和活动任务冲突都有明确错误；
- 没有改变任务状态机、租约/fencing、Policy、工具、Verifier 或恢复规则。

新增 `tests/test_task_conversation.py` 覆盖会话任务创建、旧路径、活动任务冲突、跨会话 turn 拒绝、合法关联后真实执行，以及 turn 关联失败后的任务回滚。

## 20. Phase 3.2 基础会话 CLI 实际实现

`cli.py` 新增：

```text
zevorinth conversation create
zevorinth conversation send
zevorinth conversation history
```

- `create` 支持可选的 `--conversation-id` 和 JSON `--metadata`；
- `send` 要求 `--client-message-id`，在写事务内执行相同内容幂等或内容冲突判断；
- `history` 支持 `--limit`，受 EventStore 的 200 条上限约束；
- `run` 支持可选的 `--conversation-id` 和 `--turn-id`，把任务创建与 turn 关联交给 TaskService；
- 三个命令都支持 `--json`；
- 会话不存在和幂等冲突返回非零退出码并给出明确错误。

会话命令本身只访问会话持久化层；`run` 的会话参数只负责任务关联，仍使用原有 TaskService、Planner、Policy、工具和 Verifier。没有调用 LLM，也没有自动多轮意图分类。
