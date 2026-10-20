# Zevorinth Phase 3.4.4 Lease 接管协调审计

状态：**PASS WITH LIMITATIONS**

本阶段未调用真实模型 API、未读取 `.env`、未推送远程。

## 1. 基线

- 仓库：`/Users/lidequan/PythonProject/Zevorinth`
- 分支：`phase-2.6-response-hardening`
- 起始 HEAD：`03cd56192fe043a9409b898cdcf279195e4b0271`
- Schema：v4，无迁移。

## 2. 实际调用链

```text
TaskService._claim()
  -> if takeover of RUNNING/VERIFYING/INTERRUPTED:
       artifacts/.locks/<task_id>.lock  [flock]
  -> EventStore.claim_task()               [BEGIN IMMEDIATE]
  -> release coordination lock

TaskService._execute_tool()
  -> RealArtifactWriter.write()
     -> write and fsync .staging/<attempt>.part
     -> artifacts/.locks/<task_id>.lock    [same flock]
     -> EventStore.assert_task_authority()
     -> os.link(staging, final)
     -> release coordination lock
  -> TOOL_SUCCEEDED DB transaction
```

锁顺序固定为：

```text
filesystem publication lock -> SQLite transaction
```

没有数据库事务在持锁期间反向等待发布锁。

## 3. Lease 状态机

| 状态 | 触发 | 持久化行为 | 接管规则 |
| --- | --- | --- | --- |
| 未领取 | Task queued | 无 lease | 初始 queued claim 不等待发布锁 |
| 当前持有 | claim_task | token/generation/expiry | 有效 lease 拒绝其他 claim |
| 到期未接管 | 当前时间超过 expiry | 状态可从 RUNNING/VERIFYING 转 INTERRUPTED | 下一执行者先取得发布锁再 claim |
| 接管等待 | claim 在发布锁外等待 | 数据库不变 | 等待有界；超时 raise StoreBusyError |
| 新 generation | claim_task 成功 | generation + 1，token/attempt 更新 | 旧 token 不能再写受保护状态 |
| 释放 | release_claim | owner/token/expiry 清空 | 不增加 generation |
| 持有者崩溃 | 进程退出 | Lease 记录保留到过期 | OS 自动释放 flock；后续可接管 |

本阶段没有独立的 Lease 续租 API。长任务仍依赖初始 Lease 时长。

## 4. 文件发布状态机

| 状态 | 持久化/文件证据 | 失败规则 |
| --- | --- | --- |
| 无 staging | 无 | 可安全执行 |
| staging 写入中 | staging 文件可能不完整 | 不会发布 |
| staging fsync | attempt 专属 `.part` | 可进入发布协调 |
| 等待发布锁 | staging 完整，最终文件不存在 | 超时则不发布 |
| authority 检查 | token/attempt/generation/status/expiry | 不匹配抛 ClaimLost |
| authority 通过 | 仍持锁 | 允许一次不可覆盖 link |
| 最终 link | 最终文件存在，锁内 | 已存在则 artifact_exists |
| evidence 未持久化 | 文件存在，数据库无 tool evidence | 恢复时拒绝覆盖并 BLOCKED |
| 成功终态已提交 | Task SUCCEEDED 和 task_succeeded 同事务 | 重放不执行工具 |

## 5. 不变量

| 不变量 | 实现 | 测试 |
| --- | --- | --- |
| 旧 Lease 不能提交成功终态 | token 复查 + generation + SQLite fencing | authority/stale 测试 |
| staging 不互相冒充 | 文件名绑定 attempt UUID | staging SIGKILL 测试 |
| 旧执行者不覆盖最终产物 | 不可覆盖 hard link | overwrite/race 测试 |
| 未验证文件不算成功 | ArtifactVerifier 独立读取 | Verifier 测试 |
| evidence 可对账 | attempt + generation + hash + path | provenance 测试 |
| generation 增加不等于旧副作用停止 | 发布锁协调接管，但文件系统非原子 | authority-to-publish 测试 |
| 不确定时不盲目重试 | artifact_exists / lock timeout / ClaimLost | SIGKILL 与恢复测试 |
| 等待有界 | publish lock 和 claim 均使用 timeout | 两个 timeout 测试 |

## 6. 方案比较

| 方案 | 结论 |
| --- | --- |
| 接管和发布共用协调锁 | 采用；防止 generation 在发布窗口内被改写 |
| 发布窗口内续租 | 当前没有续租 API；不新增无边界续租 |
| attempt staging + 单发布者 | 已在 Phase 3.4.3 采用并保留 |
| 不确定时 fail closed | 保留 BLOCKED/FAILED/ClaimLost/StoreBusyError |

最终协议：

1. 初始 QUEUED claim 不创建文件锁。
2. RUNNING/VERIFYING/INTERRUPTED 的接管 claim 先取得 Task 发布锁。
3. 发布者同样在 authority 前取得该锁，并保持到最终 link 完成。
4. claim 等待和发布等待都有上限。
5. 旧 token 不能提交成功；不可覆盖规则不改变。

## 7. 锁范围与死锁

- 锁键：Task UUID。
- 锁文件：`artifacts/.locks/<task_id>.lock`。
- 不同 Task 使用不同锁，可并行。
- 同 Task 的接管和发布串行化。
- 发布者先锁文件后读 SQLite；接管者先锁文件后开始 SQLite claim。
- 没有 DB transaction 在持有期间反向申请发布锁。
- 进程被 SIGKILL 后，内核释放 flock；文件本身保留，不阻止后续打开。
- lock timeout 抛出明确错误，不执行未经授权的发布。

## 8. 竞态与 SIGKILL 结果

### 接管与发布同时发生

- A 在 authority 通过后持锁暂停。
- B 申请接管，在取得发布锁前等待。
- 测试确认 `newer_entered` 尚未发生，最终文件不存在。
- A 释放后，接管由同一锁顺序裁决。

测试：`test_authority_to_publish_window_fails_closed_without_overwrite`

### 旧执行者在新结果之前发布

A 在 staging 后暂停；B 先取得锁、claim generation 2 并发布。
A 恢复后 authority 失败，不能覆盖 B。最终 inode 保持 B 的文件。

测试：`test_stale_attempt_cannot_overwrite_newer_publication`

### authority 后的锁持有

测试从父进程使用同一锁键取得锁，确认 timeout；证明 writer 在
authority 与最终 link 之间没有提前释放。

测试：`test_writer_holds_lock_through_authority_and_publish`

### 持锁进程 SIGKILL

在 `after_publish_authority` checkpoint 杀死子进程：

- 最终文件不存在；
- staging 文件残留；
- OS 释放 flock；
- 新 generation 可以取得锁并完成发布；
- 工具调用次数为 1。

测试：`test_sigkill_while_holding_publish_lock_releases_coordination`

### 发布后、evidence 前 SIGKILL

最终文件存在但数据库无 tool evidence。恢复 Policy 拒绝覆盖，Task BLOCKED。

测试：`test_sigkill_after_publish_blocks_without_overwrite`

## 9. 恢复矩阵

| 数据库/文件状态 | 自动恢复 | 工具重试 | 终态 |
| --- | --- | --- | --- |
| queued，无文件 | 是 | 可执行 | 正常执行 |
| interrupted，无 evidence，无文件 | 是 | 可执行 | 重新执行 |
| interrupted，有完整 evidence | 是 | 禁止 | 只读验证 |
| running/verifying，有效 Lease | 否 | 否 | 等待现有 owner |
| Lease 过期，发布锁可用 | 接管 | 视 evidence | 验证或执行 |
| 发布锁超时 | 否 | 否 | StoreBusyError |
| 最终文件存在，无 evidence | 否 | 禁止 | BLOCKED |
| evidence 存在，文件缺失 | 否 | 禁止覆盖 | FAILED |
| evidence attempt/generation 不符 | 否 | 禁止 | 验证失败 |
| 成功终态已提交 | 不执行 | 禁止 | SUCCEEDED |

## 10. 变异结果

| 变异 | 捕获测试 | 结果 |
| --- | --- | --- |
| 移除接管发布锁 | takeover coordination 测试 | 捕获 |
| authority 后提前解锁 | writer holds lock 测试 | 捕获 |
| 移除 Lease 有效期检查 | expired same-token 测试 | 捕获 |
| 忽略续租失败 | N/A：当前无续租 API | 不适用 |
| 允许旧 generation 发布 | authority provenance 测试 | 捕获 |
| 不确定时自动重写 | publish SIGKILL 测试 | 捕获 |
| 忽略 evidence provenance | provenance 测试 | 捕获 |
| 删除锁等待超时 | bounded lock 测试 | 捕获 |
| 改变锁顺序 | DB claim 在 lock 内的顺序断言 | 代码级保护；未做完整重写变异 |

7 项适用变异全部捕获。续租变异不适用；锁顺序通过
`["entered", "db_claim", "exited"]` 断言保护，但没有执行完整的代码重排变异。

## 11. 测试结果

| 项目 | 结果 |
| --- | --- |
| 系统 Python 完整 unittest | 182/182，exit 0 |
| 隔离环境完整 unittest | 182/182，exit 0 |
| side-effect fencing 专项 | 8/8，连续 10 轮 0 失败 |
| crash consistency 专项 | 16/16，连续 10 轮 0 失败 |
| concurrency 专项 | 3/3，连续 10 轮 0 失败 |
| 并发初始化 | 100/100 |
| 并发澄清答案 | 20/20 |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

coverage / mutation framework：NOT RUN，环境未安装且未联网安装。

## 12. 未解决风险

- SQLite 与文件系统仍不能共同原子提交。
- 发布锁释放到 TOOL_SUCCEEDED 提交之间仍有短窗口；接管可能赢得 DB 竞态，
  旧发布者随后 ClaimLost，文件保持不可覆盖。
- 没有 Lease 续租；长时间 I/O 只能依赖初始 Lease 和发布锁等待。
- 残留 staging 文件没有自动清理。
- 非协作进程可以绕过 `.locks` 和 writer。
- 跨主机、共享文件系统、NFS flock 语义未验证。

## 13. API 与 Git

- 真实模型 API 请求：0。
- 读取 `.env`：否。
- 访问远程模型：否。
- 推送远程：否。
- `.idea/`：六个既有文件保持原样。

最终提交 SHA 和工作树状态由最终报告给出。
