# Zevorinth Phase 3.3 设计：多轮对话理解与安全任务编排

状态：Phase 3.3 已实现确定性意图分类、Schema v4 澄清问题/答案绑定、多轮 `conversation process` 和保守的安全续接。补充/修改非结构化文本不会被自动写成任务参数；未接入真实 LLM。

基线：`9146ddea0defd0b3645c1974255bd27171ee596c`（Phase 3.2）

## 1. 目标

在已有 Conversation/Message/Turn 持久化和 TaskService 会话关联之上，增加：

1. 对用户输入的确定性语义分类；
2. 澄清热点的问题身份、答案来源和任务绑定；
3. 未执行计划的安全修订；
4. 已执行副作用后的新任务/人工确认边界；
5. 跨会话、并发和中断场景的可验证回归。

本阶段不把分类结果直接交给工具执行，也不允许 LLM 直接修改数据库或绕过 TaskService。

## 2. 3.3.1 意图分类协议

实现位置：`src/zevorinth/intent.py`、`src/zevorinth/models.py`。

分类结果 `IntentClassification` 包含：

- `kind`；
- `reason`；
- 目标 `task_id`；
- 可选 `question_id`；
- `requires_question_binding`；
- `requires_confirmation`。

分类类别：

| 类别 | 含义 |
| --- | --- |
| `new_task` | 创建独立新任务 |
| `supplement` | 给活动任务补充信息，不改执行计划 |
| `modify_pending` | 修改尚未执行的任务计划 |
| `clarification_answer` | 回答澄清问题 |
| `status_query` | 查询状态 |
| `ambiguous` | 不能安全分类，必须澄清或拒绝 |

确定性优先级：

1. 空消息：`ambiguous`；
2. 明确状态查询：`status_query`；
3. 带问题 ID 的答案：必须是当前 `awaiting_clarification` 任务，否则 `ambiguous`；
4. 没有活动任务：`new_task`；
5. 明确“新任务/重新开始”标记：`new_task`；
6. `awaiting_clarification`：
   - 修改标记：`modify_pending`，要求确认；
   - 其他文本：`clarification_answer`；没有 question ID 时要求绑定；
7. 终态任务：`new_task`；
8. 有修改标记：
   - 已有工具/产物副作用或处于 `running`/`verifying`：`new_task` 并要求确认；
   - `queued`/`blocked`/`interrupted` 且无副作用：`modify_pending`；
9. 其他活动任务文本：`supplement`。

分类器只读 Task 快照，不写数据库、不调用 Planner、不调用工具。

## 3. 安全任务状态矩阵

| 任务状态/证据 | 补充 | 修改计划 | 新目标 | 澄清答案 |
| --- | --- | --- | --- | --- |
| 无活动任务 | — | — | 新任务 | 拒绝/歧义 |
| `queued`，无副作用 | 同任务补充 | 可修改 pending | 新任务 | 不适用 |
| `blocked`/`interrupted`，无副作用 | 同任务补充 | 可修改 pending | 新任务 | 不适用 |
| `awaiting_clarification` | 绑定答案 | 修改需确认 | 新任务 | 同任务 |
| `running`/`verifying`，已有工具/产物 | 只记录上下文 | 新任务，要求确认 | 新任务 | 不适用 |
| 终态（succeeded/failed/planning_failed/cancelled/simulated） | 不修改 | 新任务 | 新任务 | 不适用 |

“已执行”不等于可撤销。新消息不得将旧任务改回成功、覆盖旧产物或重置事件。

## 4. 3.3.2 澄清问题绑定实现

现有 `conversation_turns` 和 `messages` 能保存问题文本/答案文本，但不能可靠表达：

- 一个问题的稳定 ID；
- 问题属于哪个任务、哪次 turn；
- 答案是否已应用；
- 同一问题的错误/冲突回答。

因此完成了**最小的追加式 Schema v4 迁移**：

- `clarification_requests`：question_id、task_id、conversation_id、turn_id、field_name、ordinal、question_text、status、created_at、answered_at、answer_message_id。
- `clarification_answers`：answer_id、question_id、message_id、normalized_value_json、validation_status、created_at。

约束：

- question 属于当前活动任务；
- 一个 question 同时最多一个 open 状态；
- 同一 question + message 不重复应用；
- 答案应用前任务必须未终态且无副作用；
- 应用通过 `TaskService.resume()`，不能直接改任务 JSON。

实际已实现上述澄清表和 v3→v4 迁移；问题、答案来源和应用状态可审计，重复答案由数据库唯一约束拒绝。

## 5. 3.3.3 安全续接与计划修订

规则：

- 只有无副作用的 `queued`/`blocked`/`interrupted` 或明确开放的澄清任务可以修订；
- `running`/`verifying` 出现材料性变化时创建新任务；
- 已存在工具结果或 artifact 时，不取消、不覆盖、不撤销旧任务；
- 原任务继续恢复/验证，新任务使用新的 task ID 和 artifact 目录；
- 终态任务不能被重新打开；
- 任何不确定的意图都进入 `ambiguous`，要求用户澄清；
- 不自动重试，不无限循环；续接仍受 TaskService 租约、Policy、Verifier 和预算约束。

当前实现将 `supplement` 和 `modify_pending` 记录为需要结构化更新/确认，不会从自然语言猜测并覆盖计划；真正的 plan supersede 和 task intent 版本表仍未实现。

## 6. 测试与验收标准

### 3.3.1 已覆盖

- 无活动任务 → 新任务；
- `queued` 任务补充 → supplement；
- `queued` 任务修改 → modify_pending；
- 明确 question ID 的答案 → clarification_answer；
- 未绑定答案 → clarification_answer + requires_question_binding；
- 已有副作用后修改 → new_task + requires_confirmation；
- 终态任务 → new_task；
- 状态查询；
- 空消息 → ambiguous；
- 错误任务状态的答案 → ambiguous。

### 3.3.2/3.3.3 已覆盖

- 澄清问题重启后仍可读取；
- 答案只绑定当前活动任务的开放问题；
- 错配/重复答案不写任务；
- v3→v4 迁移和迁移失败回滚；
- 跨会话答案拒绝；
- 并发答案只有一个有效结果；
- 旧任务执行/恢复回归。

## 7. 兼容性与边界

- Schema version 已升至 4，迁移为追加式；
- v3→v4 和 v2→v4 路径需通过迁移测试；
- 分类器不调用 LLM，结果必须经过本地校验；
- 任务状态机、租约/fencing、Policy、工具、Verifier 不因会话层改变；
- 真实模型 API 请求保持 0；
- 不实现自动摘要或上下文压缩。

## 8. Phase 3.3 实际实现

已实现：

- `clarification_requests`、`clarification_answers` 及其约束；
- `EventStore.create_clarification_requests()`、查询、记录答案、标记 applied/rejected；
- `ConversationService.process_message()`；
- CLI `conversation process`；
- `ConversationService.recover_task()` 和 CLI `conversation recover`；
- 新任务 → 生成问题 → question ID 回答 → TaskService.resume → 独立 Verifier 成功；
- 同一消息/问题重复答案和跨会话答案的拒绝；
- 并发答案只有一个有效结果；
- 状态查询不调用 Planner/工具；
- `supplement`/`modify_pending` 的保守安全出口，不会自动覆盖任务计划。

明确未实现：

- 自然语言自动提取结构化任务参数；
- 已执行任务的自动 supersede/新任务编排；
- LLM 意图分类、上下文压缩或自动任务续接；
- 真实模型 API。
