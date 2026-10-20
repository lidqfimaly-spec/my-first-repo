# Zevorinth Phase 3.4.5 Lease 续租审计

状态：**PASS WITH LIMITATIONS**

本阶段未调用真实模型 API、未读取 `.env`、未推送远程。

## 1. 基线

- 仓库：`/Users/lidequan/PythonProject/Zevorinth`
- 分支：`phase-2.6-response-hardening`
- 起始 HEAD：`ede3400138a353b20b2e968f5329cbdf79b5f43c`
- Schema：v4，无迁移。

## 2. 续租 API

新增 `EventStore.renew_lease()`：

```text
BEGIN IMMEDIATE
  load Task
  verify lease_token
  verify claim_generation
  verify status in QUEUED/RUNNING/VERIFYING
  verify current lease_expires_at > now
  verify new expiry is not earlier
  update lease_expires_at
  insert lease_renewed event
COMMIT
```

契约：

- 过期 Lease 不得被复活。
- 已被新 generation 替换的旧 token 不得续租。
- 取消、阻断或终态任务不得续租。
- generation 不因续租而增加。
- 续租只延长到期时间，不改变 attempt、owner 或计划。

## 3. 后台续租器

`LeaseRenewer`：

- 每隔 `interval_seconds` 尝试续租。
- 默认间隔为有效 Lease 时长的三分之一。
- 首次续租失败立即停止，不自动重试掩盖数据库故障。
- 达到 `max_execution_seconds` 后停止，错误码为
  `lease_renewal_limit_exceeded`。
- 实际 renewal 窗口至少覆盖一次有效 Lease：
  `max(configured_max_execution_seconds, effective_lease_seconds)`。
- 续租线程的 ClaimLost/数据库异常/意外异常都保存在线程安全状态中。
- 主执行流程通过 `ensure_healthy()` 观察失败。
- `stop()` 使用有界 join；线程为 daemon，不能永久阻止进程退出。

TaskService 默认：

- `lease_seconds=60`
- `max_execution_seconds=600`
- `lease_renewal_interval_seconds=20`

CLI 新增：

- `--max-execution-seconds`
- `--lease-renewal-interval-seconds`

## 4. 续租状态机

| 状态 | 触发 | 结果 |
| --- | --- | --- |
| 持有有效 Lease | renew | 更新 expiry，generation 不变 |
| Lease 已过期 | renew | ClaimLost，不复活 |
| 新 generation 已接管 | 旧 token renew | ClaimLost |
| 当前 token 正确、generation 错误 | renew | ClaimLost |
| Task cancelled/terminal | renew | ClaimLost |
| 续租数据库失败 | background worker | 记录失败并停止续租 |
| 达到最大执行时长 | worker/check | LeaseRenewalError |
| 执行结束 | stop | 停止后续续租 |

## 5. 锁顺序

保留 Phase 3.4.4 的顺序：

```text
filesystem publication lock -> SQLite lease transaction
```

续租只访问 SQLite，不获取文件发布锁，因此不会形成反向锁依赖。

并发裁决：

- 续租先提交：接管 claim 看到新 expiry，拒绝 active_lease。
- 接管先提交：旧 token/generation 续租得到 ClaimLost。
- 发布锁等待期间续租可以继续，避免 staging/锁等待耗尽 Lease。
- 所有 claim/publish 等待有界；续租总时长也有界。

## 6. 执行阶段检查

LeaseRenewer 覆盖 planning、staging、publish、Verifier：

1. planner 开始和返回后检查 heartbeat。
2. TOOL_STARTED 前检查 renewal 健康和 token/generation。
3. 发布锁内 authority 回调先检查 heartbeat。
4. writer 返回后、TOOL_SUCCEEDED 前再次检查。
5. Verifier 前和 Verifier 后检查。
6. VERIFICATION_COMPLETED 后、SUCCESS 前再次检查。
7. 模拟完成、终态和异常路径最终停止续租。

续租失败后：

- 未开始的工具不会被调用。
- staging 中已写入但未发布的内容不会发布。
- 已发布但未持久化 evidence 的文件会在恢复时触发不可覆盖检查。
- 已通过 Verifier 但 heartbeat 失效时不能提交 SUCCEEDED。

## 7. 恢复矩阵

| 状态 | 动作 |
| --- | --- |
| Lease 仍有效 | 允许正常执行/续租 |
| 过期但未接管 | 不续租；可被新 claim 接管 |
| 已被新 generation 接管 | 旧执行者 ClaimLost |
| 续租结果未知 | fail closed；不得假定 Lease 有效 |
| staging 存在、最终文件不存在 | 可重新执行 |
| 最终文件存在、evidence 不存在 | 不覆盖，BLOCKED 或 FAILED |
| evidence 存在、文件缺失 | 只读验证失败 |
| hash 不一致 | FAILED |
| 归属无法确认 | 不覆盖，人工确认 |
| 发布完成、终态未提交 | 有 evidence 时只读验证；无 evidence 时 fail closed |
| 续租失败但进程仍存活 | 在下一个安全边界停止，不发布/不提交成功 |

## 8. 测试

新增 `tests/test_lease_renewal.py`，9 项：

- 正常续租并阻止 takeover。
- 错误 token、错误 generation、过期、接管后旧 token。
- cancelled 任务不能续租。
- 最大执行时长有界。
- 续租后台异常可观察。
- 续租数据库失败时工具不启动。
- staging 后阻塞的长工具调用保持 Lease 并成功。
- 等待发布锁期间 renewal 持续。
- terminal 任务不能续租。

确定性同步使用 Event、线程/进程、stdout checkpoint 和 SQLite 状态，不以固定 sleep 作为唯一同步。

## 9. 真实 SIGKILL

沿用并通过：

- tool write 后、Verifier 前 SIGKILL。
- 发布锁持有期间 SIGKILL。
- publish 后、evidence 前 SIGKILL。
- evidence 后、最终成功事务前 SIGKILL。

进程死亡会终止 renewal 线程；OS 释放 flock；后续接管依赖持久化残留状态判断。

## 10. 变异

本阶段 9 项适用变异均被捕获：

1. 移除 renewal token 检查。
2. 移除 generation 检查。
3. 允许过期 Lease 续租。
4. 忽略续租失败。
5. 允许旧 generation 通过 authority。
6. 取消后继续续租。
7. 续租无限延长。
8. 移除发布前 authority。
9. 忽略 evidence provenance。

锁顺序由 `[entered, db_claim, exited]` 顺序断言保护；没有执行完整代码重排变异。

## 11. 测试结果

| 项目 | 结果 |
| --- | --- |
| 系统 Python 完整 unittest | 192/192，exit 0 |
| 隔离环境完整 unittest | 192/192，exit 0 |
| lease renewal 专项 | 10/10 |
| side-effect fencing 专项 | 9/9 |
| crash consistency 专项 | 16/16 |
| concurrency 专项 | 3/3 |
| 并发初始化 | 100/100 |
| 并发澄清答案 | 20/20 |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

coverage / mutation framework：NOT RUN，环境未安装且未联网安装。

## 12. 剩余限制

- SQLite Lease 与文件发布不能共同原子提交。
- 续租线程停止时有界 join；若其正卡在 SQLite busy timeout 中，线程可能在
  stop 返回后短暂存活，但下一次续租仍受 token/generation/expiry 约束。
- 续租与 takeover 由 SQLite 串行裁决，但文件发布仍存在最终发布窗口。
- 残留 staging 文件没有自动清理。
- 非协作进程可绕过锁和 writer。
- `flock` 仅适用于单机本地协作文件系统；多机和 NFS 未验证。
- 没有跨主机 Lease 服务。

## 13. API 与 Git

- 真实模型 API 请求：0。
- 读取 `.env`：否。
- 访问远程模型：否。
- 推送远程：否。
- `.idea/`：六个既有文件保持原样。

最终提交 SHA 和工作树状态由最终报告给出。
