# Zevorinth Phase 3.4.1 可靠性审计

状态：**PASS WITH LIMITATIONS**

本报告记录实际代码、SQLite 状态、测试和故障注入证据。未调用真实模型 API，未推送远程仓库。

## 1. 范围与实际 HEAD

- 仓库：`/Users/lidequan/PythonProject/Zevorinth`
- 分支：`phase-2.6-response-hardening`
- 起始 HEAD：`0e41fd105ec8c3236364c473d3548ffee37a9c09`
- 审计范围：ConversationService、EventStore、Schema v4、意图分类、TaskService、Policy、工具、ArtifactVerifier、CLI、测试和迁移。

## 2. 基线与调用链

实际链路：

```text
ConversationService.process_message()
  -> EventStore.append_user_message()
     -> conversations / conversation_turns / messages / conversation_events
  -> IntentClassifier.classify()
  -> EventStore.update_conversation_turn(intent_classified)
  -> new_task / clarification_answer / status_query / ambiguous
  -> EventStore.create_clarification_requests()
  -> EventStore.record_clarification_answer()
  -> TaskService.resume()
  -> TaskService._plan_with_current_status()
  -> PlanningCandidateValidator (where applicable)
  -> ArtifactPolicy
  -> EventStore TOOL_STARTED + ArtifactWriter
  -> EventStore TOOL_SUCCEEDED
  -> ArtifactVerifier
  -> EventStore VERIFICATION_COMPLETED / TASK_SUCCEEDED
```

任务领取和恢复继续使用：

- `EventStore.claim_task()`
- `Lease/Fencing`
- `TaskService._run_claimed()`
- `TaskService._resume_interrupted()`
- `TaskService._verify_and_finish()`

## 3. 状态与持久化边界

| 对象 | 状态/含义 | 持久化边界 | 重复行为 | 并发/恢复 |
| --- | --- | --- | --- | --- |
| Message | 不可变消息；`client_message_id` 幂等 | 与 turn 同事务 | 同内容返回原消息；不同内容冲突 | DB 唯一索引 |
| Turn | `received`、`awaiting_clarification`、`completed`、`needs_attention` 等 | intent 和最终状态更新事务 | 已处理 turn 返回已有结果 | conversation_turn 唯一序号 |
| Task | 现有状态机 | TaskService/EventStore 事务 | 终态不重开 | Lease/Fencing |
| ClarificationRequest | `open` → `answered`/`cancelled`/`superseded` | 问题创建和答案接收事务 | 非 open 问题拒绝再次回答 | question/task/ordinal 唯一约束 |
| ClarificationAnswer | `pending_apply` → `applied`/`rejected` | 答案记录事务；应用状态后续标记 | 同问题时答案唯一 | answer/question/message 唯一约束 |
| Artifact | 真实文件证据 | 工具后记录，Verifier 独立读取 | 不覆盖 | 恢复时重新验证 |
| Lease | 执行所有权与过期时间 | claim/expire/release 事务 | 活跃租约拒绝新执行者 | SQLite fencing token |

## 4. 发现与修复

### 4.1 CLI 终态退出码不一致（已修复）

问题：

- `conversation process` 的 `blocked`、`failed`、`planning_failed`、`simulated` 等 outcome 没有映射，默认落到退出码 5。
- 重复处理已经失败的 turn 时，服务曾返回 `already_processed`，CLI 可能返回 0，尽管任务实际为失败/阻断。

修复：

- `CONVERSATION_EXIT_CODES` 增加 blocked/failed/planning_failed/simulated/interrupted/cancelled。
- `_existing_task_result()` 对失败/阻断任务返回真实任务状态，而不是 `already_processed`。

回归：

- `test_process_cli_reports_blocked_task_status_consistently`

### 4.2 澄清事件缺少 task/turn 关联（已修复）

问题：

- `clarification_answered` 和 `clarification_applied/rejected` 事件此前没有携带问题和任务所属的 turn/task 关联。

修复：

- 答案记录事件写入 question 的 `turn_id`/`task_id`。
- 答案应用/拒绝事件同样写入关联。

回归：

- `test_clarification_events_carry_question_association` 直接断言
  `clarification_answered`、`clarification_applied` 和
  `clarification_rejected` 的 `turn_id`、`task_id` 与
  question/answer payload 关联。

### 4.3 早期 Turn 状态丢失窗口（已修复）

问题：

- 原实现可能在任务创建前把 turn 标记为已分类；进程在分类后、建任务前退出会丢失该消息。

修复：

- 分支完成前保持 `received`；最终任务/答案状态成功写入后再提交 turn 终态。
- 已存在任务的 turn 通过 `_existing_task_result()` 恢复问题、pending answer 和终态。

### 4.4 pending answer 恢复（已实现）

- `ConversationService.recover_task()` 与 `conversation recover` 处理 pending answers。
- 任务成功 → answer `applied`；
- 失败/阻断 → answer `rejected`；
- 等待更多澄清 → 保持 pending；
- running/verifying/interrupted → 不盲目重放，留待安全检查。

回归：

- `test_recovery_marks_pending_answer_applied_after_task_success`

## 5. 故障注入与并发结果

已实际覆盖：

- 并发初始化：100 次，0 失败。
- 并发澄清答案：20 次，0 失败。
- v2→v4、v3→v4 迁移；迁移失败回滚。
- 既有 SIGKILL 工具写入后/Verifier 前恢复、修改文件不复用、旧任务恢复。
- 数据库写入后异常、删除 artifact 后恢复失败。
- 跨会话答案拒绝。
- pending answer 成功后的状态对账。

未覆盖或仅部分覆盖：

- 每个 Message/Turn/Task 创建窗口的独立 SIGKILL 子进程测试。
- Planner 执行中途强制终止的专门子进程测试。
- 数据库最终事务提交瞬间的进程强制终止。
- 多租户/多机分布式恢复。
- 完整变异测试尚未自动化。
- 覆盖率报告未生成（未安装额外覆盖率工具）。

这些缺口不改变当前已通过路径的安全结论，但不能称为“所有崩溃时序均已证明”。

### 5.1 隔离副本反例测试

在 `/tmp` 的隔离源码副本中逐一重新引入已修复缺陷，运行对应回归后
丢弃副本；正式工作树未被修改。

| 注入的错误 | 对应测试 | 实际结果 |
| --- | --- | --- |
| 重复处理失败任务时重新返回 `already_processed` | `test_process_cli_reports_blocked_task_status_consistently` | 测试失败（`0 != 4`），缺陷被捕获 |
| `clarification_answered` 不再写入 question 的 task/turn 关联 | `test_clarification_events_carry_question_association` | applied 与 rejected 两个分支均失败，缺陷被捕获 |

两项反例退出码均为 1，说明新增断言能够捕获对应错误实现。

## 6. Policy、Verifier 与工具

已有负面测试覆盖：

- 无效 Candidate/JSON、未知字段、非法路径、绝对路径、覆盖目标；
- Policy 拒绝无工具副作用；
- 工具失败不成功；
- 工具成功但 Verifier 检出缺失/内容错误/证据不一致；
- 模拟执行不产生真实 artifact；
- 恢复时拒绝覆盖已有文件。

Verifier 读取真实文件并比对内容、字节数和 SHA-256，不信任工具 `success=True`。

## 7. 测试结果

```text
系统 Python 完整 unittest: 154/154 通过，exit 0
隔离环境完整 unittest: 154/154 通过，exit 0
并发初始化: 100/100 通过，0 失败
并发澄清答案: 20/20 通过，0 失败
```

专项覆盖：

```text
Intent classifier: 8/8
Clarification workflow: 8/8
Conversation CLI: 6/6
Conversation schema: 18/18
```

## 8. 编译与差异

```text
python3 -m compileall -q src tests: exit 0
git diff --check: exit 0
```

## 9. 数据库与迁移

- Schema version：4。
- v3→v4：追加澄清表、索引和版本更新；测试覆盖成功与失败回滚。
- v2→v4：链式迁移；旧任务/事件保留。
- 高版本 schema 在 schema 写入前拒绝。
- 损坏/不完整 schema fail closed，不自动重建。

## 10. 未证明的行为与风险

- 自由文本到完整任务参数的通用解析未实现。
- `supplement`/`modify_pending` 只提供保守的安全结果，不自动改写计划。
- 计划 supersede/task intent 版本仍未实现。
- 没有真实 LLM 或在线评测。
- 没有分布式 Lease/跨机文件系统语义。
- 未覆盖所有子进程崩溃时点；现有 SIGKILL 和恢复测试主要覆盖任务工具/Verifier 窗口。

## 11. API 与 Git

- 真实模型 API 请求：0。
- 未读取 `.env` 或凭证。
- 未推送远程。
- `.idea/` 六个既有未跟踪文件保持原样。

本审计报告随本阶段提交封存；完整测试结果和提交 SHA 以最终 Git 状态为准。
