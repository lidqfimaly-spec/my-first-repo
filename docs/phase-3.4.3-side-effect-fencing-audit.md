# Zevorinth Phase 3.4.3 文件副作用 Fencing 审计

状态：**PASS WITH LIMITATIONS**

本阶段没有调用真实模型 API，没有读取 `.env`，没有推送远程。

## 1. 基线与范围

- 仓库：`/Users/lidequan/PythonProject/Zevorinth`
- 分支：`phase-2.6-response-hardening`
- 起始 HEAD：`b2958c440ca0d00d3ebe77d96f053fdefcb4d267`
- Schema：保持 v4，无迁移。
- 范围：TaskService、EventStore Lease/Fencing、RealArtifactWriter、
  ArtifactVerifier、ArtifactEvidence、文件发布测试和崩溃恢复测试。

## 2. 实际调用链

```text
TaskService.execute_task/resume
  -> EventStore.claim_task BEGIN IMMEDIATE
     -> lease_token / attempt_id / claim_generation
  -> TaskService._plan_with_current_status
  -> ArtifactPolicy
  -> TOOL_STARTED transaction
  -> RealArtifactWriter.write
     -> create task/.staging
     -> write unique <attempt_id>.part
     -> fsync staging file
     -> acquire task/.publish.lock with flock
     -> EventStore.assert_task_authority
        -> token + attempt + generation + RUNNING + active expiry
     -> os.link(staging, final)
     -> fsync task directory
     -> read final bytes and construct evidence
  -> TOOL_SUCCEEDED transaction with old lease token
  -> ArtifactVerifier independent read
  -> VERIFICATION_COMPLETED
  -> final SUCCEEDED transition
```

关键位置：

- `TaskService._claim()`：Lease 产生和 generation 增加。
- `EventStore.assert_task_authority()`：发布前的权限复查。
- `RealArtifactWriter.write()`：暂存、发布锁、不可覆盖发布和 evidence。
- `TaskService._execute_tool()`：向文件工具传递目录、尝试和授权回调。
- `TaskService._verify_and_finish()`：传递当前 generation 上限并独立验证。
- `ArtifactVerifier.verify()`：验证内容、哈希、attempt 和 generation provenance。

## 3. 竞态证明

### 场景 A：旧执行者在文件副作用前暂停

1. A 领取 generation 1。
2. A 将完整内容写入 `.staging/<attempt-a>.part`。
3. A 在获得发布锁和 authority 检查前暂停。
4. 测试使 A Lease 过期，并创建 generation 2。
5. B 恢复任务，通过 authority 检查并发布最终文件。
6. A 继续；获取发布锁后 authority 发现 token/generation 已变化。
7. A 抛出 `ClaimLost`，不能发布，也不能提交数据库终态。

测试：`test_stale_attempt_cannot_overwrite_newer_publication`

### 场景 B：旧执行者在 authority 通过后暂停

1. A 获得 generation 1，写入 staging 并通过 authority。
2. A 持有 `.publish.lock`，在最终 link 前暂停。
3. 测试使 Lease 过期，B 获得 generation 2。
4. B 写入自己的 staging，随后等待同一发布锁。
5. A 继续并发布最终文件，但因 token 已变化，数据库提交失败。
6. B 获得锁后看到最终文件已存在，`artifact_exists`，任务进入 FAILED。

测试：`test_authority_to_publish_window_fails_closed_without_overwrite`

该场景证明：authority 检查与文件发布不是一个跨 SQLite/文件系统的原子事务。
A 若在 Lease 有效时通过检查并先获得发布锁，仍可能成为第一个发布者。协议不允许
B 覆盖 A 的文件，也不允许 A 提交成功，但会要求人工处理该冲突。

### 场景 C：不同 Task 使用相同相对路径

最终文件位于 `artifacts/<task_id>/` 下。两个 Task 即使都请求
`fenced.txt`，也分别写入不同 Task 目录；evidence 中的 `attempt_id` 和
`claim_generation` 分别对应各自 Task。

测试：`test_same_relative_path_in_different_tasks_is_isolated`

### 场景 D：真实 SIGKILL

- staging 写入后、发布前：最终文件不存在，Task 仍为 RUNNING；重启可重新执行，
  旧 `.part` 被忽略。
- 最终文件发布后、evidence 持久化前：最终文件存在但数据库无 tool evidence；
  重启后 Policy 拒绝覆盖并进入 BLOCKED。

测试：

- `test_sigkill_after_staging_leaves_no_published_artifact`
- `test_sigkill_after_publish_blocks_without_overwrite`

## 4. 方案比较

| 方案 | 优点 | 限制 | 结论 |
| --- | --- | --- | --- |
| 副作用前重新查 Lease | 实现简单，能拒绝已明确的过期权限 | 检查与写入仍有 TOCTOU | 作为协议的一部分，不单独使用 |
| 尝试专属 staging | partial 文件不会成为最终产物；文件名绑定 attempt | 会产生陈旧 staging；不能替代最终发布协调 | 采用 |
| Task 目录发布锁 + 不可覆盖 link | 串行化发布；旧执行者不能覆盖已发布结果 | 文件锁与 SQLite 非原子；锁等待需上限 | 采用 |
| 严格串行或 fail-closed | 无法确认所有权时不接受成功 | 不能让旧执行者绝不产生第一份文件 | 作为失败策略采用 |

最终选择：**staging + 每 Task `flock` + SQLite authority recheck +
不可覆盖 hard link + evidence provenance + fail-closed**。

## 5. 新旧保证

旧协议：

- 文件在最终路径的同目录临时文件中写入。
- final link 前没有 Lease 或 generation 复查。
- 旧执行者可以在 Lease 失效后发布最终文件。
- evidence 不包含 attempt 或 generation。

新协议保证：

1. 未完整写入且未 fsync 的 staging 文件不会发布为最终产物。
2. 经过 authority 检查确认 token/attempt/generation 不匹配时，执行者不能发布。
3. 已存在的最终文件不会被覆盖。
4. 发布操作在同一 Task 内由本地文件锁串行化。
5. 旧执行者不能覆盖新执行者已经发布的结果。
6. 旧执行者在发布后无法用旧 token 提交 TOOL_SUCCEEDED 或 Task 终态。
7. Verifier 要求 evidence 的 attempt 与持久化 tool result 一致，
   且 claim generation 为正且不超过当前 Task generation。
8. 相同相对路径在不同 Task 中保持隔离。

仍不保证：

- authority 检查与 `os.link` 无法与 SQLite 组成单一原子事务。
- 在检查仍有效时已获得发布锁的旧执行者，可能在 Lease 随后过期后成为第一个
  发布者；新执行者会 fail closed，但不会自动清理该文件。
- 非协作进程如果不遵守 `.publish.lock` 和固定发布流程，仍可直接操作文件系统。
- 网络文件系统或跨主机环境的 `flock` 语义未验证。

## 6. 恢复与失败规则

| 情况 | 行为 |
| --- | --- |
| staging 后进程死亡 | 最终文件不存在；可重新执行；陈旧 `.part` 被忽略 |
| 发布后、TOOL_SUCCEEDED 前死亡 | 最终文件存在但无 evidence；Policy 拒绝覆盖，任务 BLOCKED |
| authority 已失效 | 抛出 `ClaimLost`；不产生成功事件 |
| 发布锁超时 | `publish_lock_timeout`；不发布最终文件 |
| 最终文件已存在 | `artifact_exists`；不覆盖 |
| evidence attempt 不符 | Verifier `tool_evidence_mismatch` |
| evidence generation 过高 | Verifier `tool_evidence_mismatch` |
| 旧 evidence 恢复 | 允许只读验证，但 generation 不得高于当前 Task generation |

## 7. 测试结果

| 测试 | 结果 |
| --- | --- |
| 系统 Python 完整 unittest | 178/178，exit 0，5.539 秒 |
| 隔离环境完整 unittest | 178/178，exit 0，5.565 秒 |
| 文件副作用 fencing 专项 | 6/6，连续 10 轮 0 失败 |
| 崩溃一致性专项 | 15/15，连续 10 轮 0 失败 |
| 并发任务专项 | 3/3，连续 10 轮 0 失败 |
| 并发初始化 | 100/100，0 失败 |
| 并发澄清答案 | 20/20，0 失败 |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

coverage / mutation framework：当前环境未安装，**NOT RUN**；未联网安装。

## 8. 隔离副本变异

| 注入错误 | 对应测试 | 结果 |
| --- | --- | --- |
| 移除 token/attempt/generation 检查 | authority token 测试 | 捕获，exit 1 |
| 移除 Lease 到期检查 | expired same-token 测试 | 捕获，exit 1 |
| 取消发布前 authority 回调 | authorized writer 测试 | 捕获，exit 1 |
| 允许覆盖已发布文件 | writer overwrite 测试 | 捕获，exit 1 |
| 忽略 evidence attempt | provenance 测试 | 捕获，exit 1 |
| 将错误内容判定为完整内容 | content mismatch 测试 | 捕获，exit 1 |
| 恢复忽略持久化工具 evidence | 最终事务恢复测试 | 捕获，exit 1 |

7/7 变异被捕获。

## 9. 数据与兼容性

- Schema 不变。
- `ArtifactEvidence` 增加可选 `attempt_id`、`claim_generation`。
- 旧 JSON 缺少这两个字段时仍可反序列化；生产恢复要求新 provenance，缺失时
  fail closed。
- Task、Event、Conversation 和 Clarification 结构未改变。

## 10. 未完成事项

- 没有实现自动清理陈旧 staging 文件；它们被忽略，但会占用磁盘。
- 没有建立跨主机发布锁或网络文件系统语义。
- 没有验证恶意非协作进程直接绕过 writer 的威胁。
- 没有自动 mutation framework 和覆盖率报告。
- 未调用真实 LLM，未运行在线评测。

## 11. 安全与 Git

- 真实模型 API 请求：0。
- 读取 `.env`：否。
- 访问远程模型：否。
- 推送远程：否。
- `.idea/`：六个既有未跟踪文件保持原样。

最终提交 SHA、文件清单和工作树状态由本阶段最终 Git 检查报告给出。
