# Zevorinth Phase 3.5：上下文连续性与安全计划修订

状态：**PASS WITH LIMITATIONS**

本阶段未调用真实模型 API、未读取 `.env`、未推送远程。

## 1. 目标与边界

Phase 3.5 在不改变任务执行内核的前提下增加：

- 可从 SQLite 重建的 `ConversationContext`；
- 原始消息与派生摘要的明确分离；
- 显式取消请求与取消命令；
- 无副作用任务的乐观版本化计划修订；
- 已执行任务的修订拒绝与人工确认边界。

不包含 LLM 意图分类、自动摘要压缩、多 Agent、已执行副作用的自动撤销。

## 2. ConversationContext

实现：`src/zevorinth/context.py`

上下文由以下持久化记录派生：

- conversation；
- messages；
- conversation_turns；
- tasks；
- plans 和 plan revision history；
- clarification requests/answers；
- artifacts、tool result 和 verification evidence；
- task error/status。

字段：

| 字段 | 来源/含义 |
| --- | --- |
| original_messages | 原始消息，完整保留 |
| current_task | 活动任务；无活动任务时取最后任务 |
| goal | 当前任务目标 |
| constraints | 含“必须/不要/只能/改为/文件名/CSV”等约束标记的用户原文 |
| confirmed_decisions | 已持久化的澄清答案 |
| completed_work | tool result、artifact、verification |
| pending_steps | 未执行计划步骤或缺失字段 |
| unresolved_questions | open clarification 和 task error |
| unconfirmed_assumptions | needs-attention/failed turn |
| sources | conversation/message/turn/task ID |
| summary | 调用方提供的派生文本 |
| summary_authoritative | 永远为 false |

版本：

```text
conv-<conversation.version>-plan-<task.plan_version>-claim-<generation>
```

上下文完全从数据库重建；进程内存不保存唯一副本。摘要遗漏不会删除原始消息或
从原文提取的约束。

CLI：

```text
zevorinth conversation context <conversation_id> [--summary TEXT] [--json]
```

## 3. 后续要求分类

现有确定性分类保留：

- `supplement`：记录补充候选，不自动覆盖计划；
- `modify_pending`：仅无副作用任务可提出修订；
- 已执行或运行中的材料性修改：要求新任务/确认。

新增：

- `cancel_request`：检测“取消/cancel”，返回
  `requires_cancel_confirmation`，不会自动取消任务。

“刚才的任务取消，改为分析另一份数据”现在先产生显式取消确认结果，而不是
覆盖旧任务。用户通过 `zevorinth cancel <task_id>` 明确取消，再创建新任务。

## 4. 取消协议

`TaskService.cancel()` -> `EventStore.cancel_task()`：

- 终态任务幂等返回；
- 有活动 Lease 时拒绝取消，避免越过 fencing；
- 无活动 Lease 的非终态任务转为 CANCELLED；
- 清除 owner/token/expiry；
- 写入 `task_cancelled` 事件；
- 不删除、不撤销已经存在的文件或 evidence。

CLI：

```text
zevorinth cancel <task_id> [--json]
```

## 5. 计划修订

Task JSON 追加：

- `plan_version`；
- `plan_revisions[]`。

每个 revision 记录：

- revision ID；
- from/to version；
- source ID；
- changed_at；
- confirmed；
- old/new request；
- old/new plan；
- changed step IDs。

修订规则：

1. 必须显式 `confirmed=True`。
2. 必须提供 `source_id`。
3. `expected_plan_version` 必须匹配。
4. Task 状态必须是 queued/awaiting/blocked/interrupted。
5. 必须没有 tool result、artifact 或真实执行模式。
6. Planner 结果必须是 planned 或 clarification。
7. 计划版本只允许 +1。
8. 重复 source ID 幂等返回当前任务。
9. 并发修订使用 SQLite 乐观版本检查，只允许一个 winner。
10. 事件写入失败时 Task JSON 和 plan_version 一起回滚。

CLI：

```text
zevorinth revise <task_id> \
  --expected-plan-version N \
  --source-id MESSAGE_OR_TURN_ID \
  --confirm \
  [--goal ... --operation ... --artifact-path ... --content ...]
```

修订不会执行任务，只保存新计划。

## 6. 已执行任务

- 任何工具、artifact 或执行模式证据都会阻止计划修订。
- 运行中或验证中任务不能原地修改。
- 已执行副作用不能通过新消息假装撤销。
- 取消不删除产物。
- 新约束影响已完成产物时必须创建新 Task 或人工处理。

## 7. 并发与恢复

- revision 不获取执行 Lease，也不启动任务执行预算。
- `apply_plan_revision()` 在 `BEGIN IMMEDIATE` 内复查 version 和 evidence。
- 两个并发修订基于同一版本时，只有一个提交成功。
- 进程重启后从 Task JSON 恢复 `plan_version` 与 revision history。
- 上下文重建不依赖进程内缓存。
- 数据库提交/事件写入失败不会留下部分计划更新。

## 8. 测试

`tests/test_context_continuity.py`：

- 三消息上下文与原始约束；
- summary 遗漏约束后仍从原文恢复；
- 重启后上下文一致；
- 已应用澄清决定出现在 confirmed decisions；
- 两次连续计划修订与历史持久化；
- 重复 source 幂等；
- stale plan version 冲突；
- 并发修订只有一个 winner；
- 事件写入失败回滚；
- 已执行任务拒绝修订；
- idle task 取消；
- active Lease 取消拒绝；
- 取消文本只产生确认请求，不自动取消。

现有 Lease、预算、发布、Verifier 测试继续运行。

实际结果：

| 项目 | 结果 |
| --- | --- |
| 系统完整 unittest | 216/216，exit 0 |
| 隔离环境完整 unittest | 216/216，exit 0 |
| context continuity 专项 | 10/10 |
| cooperative cancellation 专项 | 5/5 |
| 并发 revision race | 20/20 轮 0 失败 |
| context + cancellation | 20/20 轮 0 失败 |
| E2E acceptance | 7/7，连续 10 轮 0 失败 |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

隔离副本变异：

- 移除 expected plan version 检查：stale/并发测试捕获；
- 移除 source 幂等检查：重复消息测试捕获；
- 忽略 active Lease 强制取消：取消测试捕获；
- 只用摘要、丢弃原始消息约束：summary omission 测试捕获。

协作取消与证据隔离变异：

- 忽略 claim 检查点取消：claim cancellation 测试捕获；
- 忽略发布前取消：before-publish 测试捕获；
- 移除执行计划 snapshot 校验：snapshot mismatch 测试捕获；
- 取消请求不幂等：重复 request 测试捕获。

coverage / mutation framework：NOT RUN，环境未安装。

End-to-end runner：

```text
tests/e2e_acceptance_runner.py
artifacts/audits/phase-3.6-acceptance.json
success_rate=1.0
executable_success_rate=1.0
recovery_success_rate=1.0
duplicate_side_effects=0
cancellation_handled=true
evidence_complete_rate=0.5714285714285714
```

evidence complete rate 包含故意无最终 evidence 的阻断/否决场景，不代表产品失败率。

## 9. 未实现与风险

- 不自动把自由文本转换为完整任务参数；
- 不自动执行“补充/修改”候选，仍需结构化确认；
- 不实现摘要压缩和跨任务上下文摘要；
- Context 暴露最近 200 条原始消息；数据库仍保留更早消息，但当前快照有界；
- 已执行任务的替代仍由用户显式取消/新建；
- 澄清等待期间的执行预算仍按严格墙钟计算；
- 多 Agent 或 LLM 分类未接入。

## 10. 兼容性

- Schema 保持 v4，无迁移；
- Task 新字段均为末尾可选字段，旧位置参数保持稳定；
- 旧 JSON 缺失 plan_version/revision history 时默认为 0/空列表；
- 现有 Lease、发布锁、Policy、Tool 和 Verifier 未放宽。
