# Zevorinth Phase 3.4.6 执行预算与产物恢复审计

状态：**PASS WITH LIMITATIONS**

本阶段未调用真实模型 API、未读取 `.env`、未推送远程。

## 1. 基线

- 仓库：`/Users/lidequan/PythonProject/Zevorinth`
- 分支：`phase-2.6-response-hardening`
- 起始 HEAD：`1ab0ac45a4916878bef83749bc836e692ca323ea`
- Schema：v4，无迁移。

## 2. 执行预算契约

现在严格区分：

| 概念 | 语义 |
| --- | --- |
| execution budget | 首次成功 claim 设置的持久化总执行截止时间 |
| Lease expiry | 当前执行者 authority 的有效期 |
| renewal interval | 后台 Lease 续租调度间隔 |
| publish lock timeout | 等待 Task 文件发布锁的上限 |
| SQLite busy timeout | 数据库锁等待上限 |
| tool duration | 由外部工具行为决定，不单独伪造超时 |
| Verifier duration | 包含在总执行预算内 |

首次 claim 在 SQLite 事务内保存：

- `execution_started_at`
- `execution_deadline_at = now + max_execution_seconds`

后续续租只更新 `lease_expires_at`，不改变执行截止时间。

执行预算从首次 claim 起按墙钟时间计算，包括：

- planning
- staging
- 发布锁等待
- 文件发布
- Verifier
- 恢复前的中断时间

等待用户澄清的时间也计入该严格总预算。这是当前的明确契约；若未来需要暂停
预算，必须新增持久化的“已消耗执行时间”机制。

`max_execution_seconds` 现在是严格上限，不再与 Planner 要求的 Lease 时长取
最大值。Planner 所需 Lease 可以更长，但任务总预算不会因此被扩大。

## 3. 预算恢复

- 新任务第一次 claim 时写入 deadline。
- 同一 Task 后续 claim 复用已持久化 deadline。
- 新 TaskService 的 `max_execution_seconds` 不会覆盖旧 Task 的 deadline。
- deadline 已过时，`claim_task()` 在同一事务中将非终态 Task 标记为 FAILED，
  错误码 `execution_budget_exceeded`，并清除 Lease owner/token/expiry。
- 超时任务不会启动 Planner 后续工具，也不会发布新产物。
- 如果副作用已开始，进程只会在安全边界检查 heartbeat；已发布文件保留供
  对账，不会被当作成功。

状态机增加 `QUEUED/AWAITING_CLARIFICATION/BLOCKED -> FAILED`，仅用于预算
到期等不可继续状态。

## 4. staging 状态

| 状态 | 条件 | 清理 |
| --- | --- | --- |
| 当前活动 attempt | 文件名等于 Task.attempt_id 且 Lease 有效 | 保留 |
| 已知旧 attempt | attempt UUID 出现在 tool_started 事件 | 可在 Task 锁内删除 |
| 已发布 attempt 残留 | 已有最终文件和 evidence，文件名对应旧 attempt | 可作为已知旧 attempt 删除 |
| unknown attempt | 无法在持久化 tool_started 中找到 | 保留并报告 |
| unsafe name | 非 canonical UUID | 保留并报告 |
| 非普通文件/符号链接 | 不是安全 regular file | 保留并报告 |
| 超大 staging | 超过 64 KiB 上限 | 报错保留 |

清理不依据文件年龄或单一文件名。

## 5. 清理协议

`TaskService.cleanup_staging()`：

1. 枚举 `artifacts/<task_uuid>/.staging`。
2. 对每个 Task 获取 Task publication lock。
3. 在锁内读取 Task 和事件链。
4. 从 `tool_started` payload 构建已知 attempt 集合。
5. 当前 attempt 且 Lease 有效时保留。
6. 未知 attempt 保留。
7. 已知非活动 attempt：
   - 使用 `O_NOFOLLOW` 打开；
   - 验证 regular file；
   - 限制 64 KiB；
   - 计算 SHA-256 和字节数；
   - unlink；
   - 追加 `staging_cleaned` 事件。
8. 清理失败只影响当前文件/Task，不回滚其他 Task。

清理是 idempotent 的；重复执行不会再产生删除事件。

CLI：

```text
zevorinth cleanup-staging [--json]
```

## 6. 发布后恢复对账

| 状态 | 决策 |
| --- | --- |
| staging 存在，最终文件不存在 | 可重新执行 |
| 最终文件存在，evidence 不存在 | 不覆盖；Policy 阻断 |
| evidence 存在，Task 未成功 | 只读重新验证 |
| Task 已成功，调用方未收到 | 幂等返回，不重跑 |
| 文件哈希/字节数不符 | FAILED |
| evidence attempt/generation 不符 | FAILED |
| 新 generation 接管后旧执行者仍存活 | 旧 token 不能发布/提交 |
| 发布后 Task 被取消 | 保持 CANCELLED，不转 SUCCEEDED |
| 发布后执行预算耗尽 | FAILED `execution_budget_exceeded` |
| 数据库提交结果未知 | 重读并 fail closed |
| 另一 Task 使用同相对路径 | Task 目录隔离 |

不依据单一文件存在或单一 `tool_succeeded` 事件判定成功。

## 7. 确定性测试

新增 `tests/test_execution_budget.py`：

- claim 持久化 deadline。
- 续租不重置 deadline。
- 新服务重启不重置 deadline。
- Planner 等待预算耗尽时工具调用为 0。
- deadline 到期后 claim 返回 FAILED。

新增 `tests/test_staging_cleanup.py`：

- 活动 attempt 保留。
- 已知旧 attempt 删除。
- unknown attempt 保留。
- 最终发布文件不受影响。
- 删除写入审计事件。
- 重复清理幂等。
- 找不到 Task 的 staging 保留。

新增崩溃测试：

- 发布后、evidence 前 SIGKILL；预算仍有效时 BLOCKED。
- 发布后预算耗尽时 FAILED，不重写。
- 发布后取消保持 CANCELLED。

测试同步使用可控时钟、Event、multiprocessing、stdout checkpoint 和锁状态，
不以固定 sleep 作为唯一同步方式。

## 8. SIGKILL 边界

现有及新增测试覆盖：

- 工具写文件后、Verifier 前。
- 持有发布锁期间。
- 最终文件发布后、evidence 前。
- evidence 后、最终状态前。
- staging 文件存在但最终文件未发布。
- 发布后预算过期或取消。

进程死亡后：

- renewal 线程随进程终止；
- flock 由 OS 释放；
- staging 由后续显式 cleanup 对账；
- 最终文件不能被覆盖。

## 9. 变异测试

本阶段运行并记录：

| 变异 | 捕获测试 | 结果 |
| --- | --- | --- |
| 重启时重置 deadline | restart budget 测试 | 捕获 |
| 同时移除 claim 与启动前预算检查 | expired-budget 测试 | 捕获 |
| 忽略续租/预算失败健康检查 | planner budget timeout 测试 | 捕获 |
| 忽略活动 attempt 清理 staging | active-attempt cleanup 测试 | 捕获 |
| 忽略 unknown attempt 保护 | unknown-attempt cleanup 测试 | 捕获 |
| 使用其他 attempt evidence | provenance 测试 | 捕获 |
| hash 不一致仍成功 | Verifier 测试 | 捕获 |
| 不确定时重写 | publish-after-SIGKILL 测试 | 捕获 |
| 忽略取消/超时后的文件 | cancel/budget-after-publish 测试 | 捕获 |

名称/年龄安全的变异通过显式状态条件保护；未建立自动 mutation framework。

## 10. 回归结果

| 项目 | 结果 |
| --- | --- |
| 系统 Python 完整 unittest | 201/201，exit 0 |
| 隔离环境完整 unittest | 201/201，exit 0 |
| execution budget 专项 | 4/4，连续 20 轮 0 失败 |
| staging cleanup 专项 | 3/3，连续 20 轮 0 失败 |
| lease renewal 专项 | 10/10 |
| side-effect fencing 专项 | 9/9 |
| crash consistency 专项 | 18/18 |
| concurrency 专项 | 3/3 |
| 并发初始化 | 100/100 |
| 并发澄清答案 | 20/20 |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

coverage / mutation framework：NOT RUN，环境未安装且未联网安装。

## 11. 未解决风险

- 执行预算是墙钟严格上限，等待用户澄清也消耗预算。
- SQLite 与文件系统不能共同原子提交。
- staging cleanup 与活跃 writer 的协作受 writer 协议约束；非协作进程仍可竞争。
- 清理后到审计事件写入前存在极短窗口，SIGKILL 可导致文件已删而事件缺失；
  重跑幂等，但该次清理事件无法补写。
- 没有自动清理所有空 `.staging` 目录。
- 非协作进程可绕过锁与清理协议。
- 多机、共享文件系统和网络文件系统未验证。

## 12. API 与 Git

- 真实模型 API 请求：0。
- 读取 `.env`：否。
- 访问远程模型：否。
- 推送远程：否。
- `.idea/`：六个既有文件保持原样。

最终提交 SHA 和工作树状态由最终报告给出。
