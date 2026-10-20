# Zevorinth Phase 3.4.2 崩溃一致性与可恢复执行审计

状态：**PASS WITH LIMITATIONS**

本报告只记录实际代码检查、隔离 SQLite 数据库、真实子进程中断和离线测试结果。
真实模型 API 请求为 0，未读取 `.env`，未推送远程。

## 1. 基线与范围

- 仓库：`/Users/lidequan/PythonProject/Zevorinth`
- 分支：`phase-2.6-response-hardening`
- 起始 HEAD：`dd99dd1f391e83b4b730dfae89a70e9ebe1e1f9a`
- 审计范围：ConversationService、EventStore、TaskService、状态机、
  Lease/Fencing、Policy、ArtifactTool、ArtifactVerifier、CLI 和 SQLite Schema v4。
- 本阶段未改变 Schema 版本，仍为 v4。

## 2. 实际调用链

```text
ConversationService.process_message()
  -> EventStore.append_user_message()
     -> conversation_turns + messages + conversation_events 同一事务
  -> IntentClassifier.classify()
  -> EventStore.update_conversation_turn()
  -> TaskService.submit() / TaskService.resume()
     -> EventStore.create_task()
        -> tasks + events + turn.task_id + task_linked 同一事务
     -> EventStore.claim_task()
     -> Candidate Validator / Planner
     -> ArtifactPolicy
     -> TOOL_STARTED 事务
     -> ArtifactWriter 文件副作用
     -> TOOL_SUCCEEDED 事务
     -> VERIFICATION_STARTED 事务
     -> ArtifactVerifier 独立读取
     -> VERIFICATION_COMPLETED 事务
     -> SUCCEEDED + TASK_SUCCEEDED 同一事务
```

澄清路径：

```text
clarification_requests + clarification_requested 同一事务
clarification_answers(pending_apply) + question.answered
  + clarification_answered 同一事务
TaskService.resume()
answer applied/rejected + clarification event 同一事务
```

## 3. 持久化边界

| 边界 | 事务内变更 | 事务外副作用 | 中断后的实际状态 |
| --- | --- | --- | --- |
| Message/Turn | 检查会话与幂等键，插入 Turn、Message、事件，增加 conversation version | 无 | 全部提交，或全部不可见 |
| Task 创建 | 插入 Task、task_created，更新 Turn.task_id，写 task_linked | 无 | 全部提交，或 Task/Turn 关联全部回滚 |
| ClarificationRequest | 插入全部问题与 clarification_requested 事件 | 无 | 要么问题和事件全部存在，要么全部回滚 |
| ClarificationAnswer | 插入 pending answer，标记 question answered，写 answered 事件 | 无 | pending answer 与问题状态同事务出现 |
| Task claim | Lease token、owner、generation、claim 事件同一事务 | 无 | 旧 token 无法提交受保护更新 |
| 工具执行 | TOOL_STARTED 已提交，文件写入在数据库事务外 | 创建任务目录和最终文件 | 可能文件存在但 TOOL_SUCCEEDED 尚未提交 |
| Verifier | 读取真实文件；完成后提交 verification event | 无写入 | 重启后可再次只读验证 |
| 最终成功 | Task 状态与 task_succeeded 事件同一事务 | 无 | 成功状态和事件一起可见或一起不可见 |

## 4. 不变量与证据

| 不变量 | 实际保证 | 主要测试 |
| --- | --- | --- |
| A 同一幂等请求只产生一个逻辑任务 | client message 唯一索引 + 单事务幂等读取 | `test_message_insert_is_atomic_and_replay_creates_one_task`、`test_concurrent_same_content_message_is_idempotent` |
| B Turn 不跨 Conversation 关联 Task | SQLite 复合外键 + create_task 校验 | `test_cross_conversation_turn_link_is_rejected_without_task` |
| C Answer 不跨会话或任务绑定 | question/task/conversation 校验 + 复合外键 | `test_question_must_match_active_conversation_and_task`、`test_answer_from_another_conversation_is_rejected` |
| D Task 创建失败不留半状态 | Task、Turn 关联和事件同事务 | `test_task_and_turn_link_roll_back_together_on_sigkill` |
| E 失败或阻断不报告成功 | 真实任务终态映射 | `test_process_cli_reports_blocked_task_status_consistently` |
| F 成功需要独立产物验证 | Verifier 读取文件并比较内容与证据 | `test_verifier_reads_independent_evidence` 等 |
| G 旧执行者不能提交过期状态 | lease token 复查 + generation | `test_stale_executor_cannot_commit_after_lease_takeover` |
| H 恢复不无条件重复工具 | 有 tool evidence 时只读重验证 | `test_sigkill_inside_final_success_transaction_rolls_back_together` |
| I Event 与 Task/Turn/Conversation 一致 | 复合外键与事件关联断言 | `test_clarification_events_carry_question_association` |
| J 数据库不确定时不伪报成功 | 最终状态与事件同事务，失败保持 VERIFYING/INTERRUPTED | `test_sigkill_inside_final_success_transaction_rolls_back_together` |
| K 成功任务不重新执行副作用 | 终态快速返回 + Task 唯一结果 | `test_committed_success_survives_caller_not_receiving_result` |
| L 失败事务不留下等价终态 | 事务回滚和事件/状态断言 | Task link 与最终事务 SIGKILL 测试 |
| M 区分可重试和必须先核实 | QUEUED 可恢复；有 artifact evidence 时只验证；有未记录 artifact 时 Policy 阻断 | SIGKILL 与 hardening 测试 |
| N 恢复不绕过 Policy/Verifier | 恢复统一调用 TaskService.resume | 上述恢复测试 |

## 5. 故障注入设施

`tests/crash_consistency_worker.py` 是测试专用子进程：

- 使用 stdout JSON 到达信号，父进程确认后再发送 SIGKILL。
- 不使用固定 `sleep()` 作为同步机制。
- 只在测试进程中通过 EventStore 子类或 TEMP trigger 注入中断。
- 普通 CLI/服务路径没有新增环境变量开关或 `os._exit()` 分支。
- 每次运行使用临时目录和独立 SQLite 数据库。

故障点：

1. `message_before_commit`
2. `turn_before_message_commit`
3. `message_after_commit`
4. `task_link_before_commit`
5. `task_after_commit`
6. `question_before_commit`
7. `answer_after_commit`
8. `answer_before_applied`
9. `success_before_commit`
10. `success_after_commit`
11. 原有 `after_write_before_verification`

## 6. 崩溃窗口结果

| 场景 | 中断后观察 | 恢复结果 |
| --- | --- | --- |
| Message 插入事务内 | Turn、Message、事件均为 0 | 相同 client ID 重放后 1/1/1，工具调用 1 次 |
| Turn 已插入、Message 未插入 | Turn、Message 均回滚为 0 | 重放正常完成 |
| Message 已提交、尚未处理 | Turn=received，Task=0 | 重放执行 1 个 Task，副作用 1 次 |
| Task 已插入、Turn 关联事务中 | Task=0，Turn.task_id=NULL | 重放创建 1 个 Task 和 1 个 task_linked |
| Task 已提交、执行前进程退出 | Task=queued，无 tool_started | conversation recover 执行 1 次，重复恢复不再执行 |
| 澄清问题插入事务中 | question=0，事件=0 | 重放重建 3 个唯一问题 |
| Answer 已提交为 pending | Task=awaiting，文件不存在 | 相同消息重放后完成，写入 1 次 |
| Task 已成功、Answer 尚未 applied | Answer=pending，文件已存在 | recover 只补记 applied，工具 0 次 |
| 最终 success 事务内 | Task=VERIFYING，无 task_succeeded | 重启后只读验证并成功，工具 0 次 |
| success 已提交、结果未返回 | Task=SUCCEEDED，文件和事件完整 | 重放不增加事件，工具 0 次 |

每次子进程测试都执行：

- `PRAGMA integrity_check`
- `PRAGMA foreign_key_check`
- Task/Turn/Message/Question/Answer 计数
- 关键事件类型与关联
- 产物内容
- 工具调用计数

全部通过。

## 7. 文件副作用与 Fencing

`RealArtifactWriter` 使用同目录临时文件、`fsync`、不可覆盖的硬链接和目录
`fsync`。数据库事务不覆盖文件写入：

```text
TOOL_STARTED commit
  -> write temp + fsync + link final + directory fsync
  -> TOOL_SUCCEEDED commit
```

因此进程可能在文件已存在、但工具结果未持久化时死亡。当前恢复策略：

- 有持久化 tool evidence：只读重新验证，不调用工具。
- 无持久化 evidence 且文件已存在：Policy 拒绝覆盖，任务进入 BLOCKED，要求人工处理。
- 无持久化 evidence 且文件不存在：允许按既有计划恢复。

Lease/Fencing 保护所有带 lease token 的数据库更新。文件工具本身不接收
lease token，因此不能宣称文件系统写入已被 fencing 原子保护。现有测试证明：
旧执行者完成文件写入并丢失 Lease 后，无法提交 TOOL_SUCCEEDED/Task 终态；
但操作系统级“检查 Lease 与写文件”之间仍存在竞争窗口。

## 8. 恢复决策矩阵

| Task 状态 | 自动恢复 | 必须验证产物 | 可再次调用工具 | 结果 |
| --- | --- | --- | --- | --- |
| queued | 是 | 否 | 可以，尚未产生副作用 | 正常执行 |
| running + 有效 Lease | 否 | 否 | 否 | 返回当前所有者 |
| running + Lease 过期 | 转为 interrupted 后恢复 | 视证据 | 仅无证据且目标不存在时 | 只验证或重新规划执行 |
| verifying | 过期后转 interrupted | 是 | 默认否 | 有 evidence 时只读验证 |
| interrupted | 是 | 有 evidence 时必须 | 仅安全场景 | 验证补记或执行 |
| failed / blocked / succeeded | 否 | 成功任务可只读核对 | 否 | 不重新执行 |
| awaiting clarification | pending answer 时恢复 | 最终执行仍需 | 仅在补全后可执行 | applied/rejected/pending |
| pending_apply answer | 是 | 最终执行仍需 | 由 TaskService 决定 | 对账 answer 与 Task 终态 |

## 9. 数据库与事件一致性

- Schema：v4，无迁移。
- 所有新增 SIGKILL 场景均通过 `integrity_check` 和 `foreign_key_check`。
- 正常成功事件顺序由既有 acceptance 测试验证：
  `task_created`、`task_claimed`、`planning_started`、`planning_completed`、
  `task_planned`、`tool_started`、`tool_succeeded`、`verification_started`、
  `verification_completed`、`task_succeeded`。
- 澄清 answered/applied/rejected 事件包含 question 所属的 `task_id` 和 `turn_id`。
- 最终事务中断时，任务状态更新与 `task_succeeded` 事件一起回滚。

## 10. 发现的缺陷与修复

### 10.1 queued Task 无法通过会话恢复入口继续执行

- 影响：Task 已提交但进程在调用工具前退出时，`conversation recover` 只返回 queued。
- 修复：无 pending answer 时，QUEUED/INTERRUPTED Task 进入 `TaskService.resume()`。
- 回归：`test_committed_queued_task_is_recovered_without_duplicate_side_effect`。

### 10.2 pending answer 重放被误判为 question_mismatch

- 影响：Answer 已安全持久化为 pending，但调用方重放时无法继续，只能另行恢复。
- 修复：先用当前 message ID 对账已有 answer；同 answer 可继续，其他消息仍拒绝。
- 回归：`test_pending_answer_replay_finishes_without_duplicate_side_effect`。

### 10.3 每次澄清回答重复创建剩余问题

- 影响：问题表和事件出现重复 field，可能使客户端绑定错误问题。
- 修复：`_create_questions()` 复用当前 missing field 的 open question，只创建缺失项。
- 回归：`test_answers_do_not_duplicate_remaining_open_questions`。

### 10.4 Verifier 异常会停留在 VERIFYING

- 影响：Verifier 意外抛出异常时，异常直接越过最终状态持久化，任务保留 Lease。
- 修复：捕获异常或无法解释的返回对象，生成类型级失败结果，随后写入
  verification event 和 FAILED 终态。
- 诊断只记录异常类型，不记录异常文本或产物内容。
- 回归：`test_verifier_exception_fails_closed_without_retrying_tool`、
  `test_verifier_invalid_result_fails_closed`。

## 11. 反例测试

每项均在 `/tmp` 隔离源码副本中注入，测试失败后丢弃副本。

| 注入错误 | 对应测试 | 结果 |
| --- | --- | --- |
| 移除 Message 应用层幂等分支 | 并发相同消息幂等测试 | 捕获，exit 1 |
| Task 插入后提前提交 | Task/Turn SIGKILL 原子性测试 | 捕获，`tasks=1` |
| 恢复忽略已持久化工具证据 | 最终事务恢复测试 | 捕获，Task 变为 BLOCKED |
| `_require_lease` 不检查 token | stale executor 测试 | 捕获，ClaimLost 未抛出 |
| Verifier 无条件 passed=True | 内容不匹配测试 | 捕获 |
| 终态守卫失效 | success 重放测试 | 捕获，事件数增加 |
| 澄清事件移除 task/turn 关联 | 事件关联测试 | 捕获 |
| 最终更新与事件之间提前提交 | 最终事务 SIGKILL 测试 | 捕获，状态已 SUCCEEDED |

8 项反例全部被现有测试捕获。

## 12. 测试执行

| 命令 | 结果 |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v` | 168/168，exit 0 |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` | 168/168，exit 0 |
| `python3 -m unittest -q tests.test_crash_consistency`，重复 10 轮 | 13/13，每轮 exit 0，0 失败 |
| `python3 -m unittest -q tests.test_concurrency`，重复 10 轮 | 3/3，每轮 exit 0，0 失败 |
| 并发初始化专项，重复 100 轮 | 100/100，0 失败 |
| 并发澄清答案专项，重复 20 轮 | 20/20，0 失败 |
| `python3 -m compileall -q src tests` | exit 0 |
| `git diff --check` | exit 0 |

## 13. 未解决风险与未执行项

- 文件工具在 Lease 检查之后、真正写入文件之前仍有检查/写入竞争窗口。
- 没有分布式 Lease、跨主机文件锁或共享文件系统语义。
- 没有自动网络文件系统电源故障、内核崩溃或磁盘损坏测试。
- 覆盖率：NOT RUN；当前环境未安装 `coverage`，未联网安装。
- 真实 LLM、在线评测和跨机故障：本阶段未执行。
- 未证明 SQLite 之外的外部系统具备事务语义。

## 14. API 与 Git

- 真实模型 API 请求：0。
- 读取 `.env`：否。
- 访问远程模型：否。
- 推送远程：否。
- 修改 `.idea/`：否。
- Schema 版本：v4。

最终提交 SHA、提交文件和完整测试计数由本阶段最终 Git 检查报告给出。
