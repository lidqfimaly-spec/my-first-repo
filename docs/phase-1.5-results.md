# 第 1.5 阶段验收结果

日期：2026-10-19

## 1. Git 基线

- 根目录：`/Users/lidequan/PythonProject/Zevorinth`
- 初始分支：`main`
- Phase 1 基线提交：`752cae3 feat: establish verified artifact task loop`
- 基线文件数：33
- 基线测试：32 项，全部通过
- 加固分支：`phase-1.5-hardening`
- 未设置远程，未推送

## 2. 总体测试结果

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s tests -v
```

连续三次独立完整结果：

```text
Ran 46 tests in 1.085s
Ran 46 tests in 1.111s
Ran 46 tests in 1.038s
Ran 46 tests in 0.957s
OK
```

原有 A1–A6 测试全部保留并通过。

## 3. 执行租约与围栏

实现：

- Task 持久化 `execution_owner`；
- 每次认领生成新的 `lease_token`；
- 持久化 `lease_expires_at`；
- 每次认领增加 `claim_generation`；
- 关键状态和事件更新必须携带当前 token。

测试证明：

- 第一个执行者认领后，第二个执行者被拒绝并记录 `claim_rejected`；
- 新 TaskService 实例不会把活动租约任务误标为中断；
- 租约到期后可产生新 generation；
- 旧 token 提交状态会抛出 `ClaimLost`；
- 终态清除租约。

相关测试：

- `test_sqlite_claim_is_exclusive_and_fenced`
- `test_new_service_does_not_interrupt_an_active_lease`

## 4. 真实 SIGKILL

测试使用独立子进程，父进程通过 stdout 明确信号获知已到达：

```text
文件已写入
工具结果和产物证据已提交
任务已提交 verifying 检查点
Verifier 尚未执行
```

父进程随即发送 `SIGKILL`，并验证：

- 子进程返回码为 `-SIGKILL`；
- 数据库原始状态仍为 `verifying`；
- lease token 仍存在；
- 文件存在且包含预期内容。

租约过期后启动新运行时：

- 状态转为 `interrupted`；
- `resume` 只重新读取和验证既有文件；
- Writer 调用次数为 0；
- 最终进入 `succeeded`；
- 只有一个 `task_succeeded`。

文件被修改后使用同一流程：

- 不覆盖修改后的文件；
- 验证失败；
- 最终状态 `failed`；
- Writer 调用次数为 0。

测试不使用固定睡眠猜测执行进度；父进程用 `selectors` 等待明确信号，并设置超时清理。

这不是断电、磁盘故障或文件系统损坏测试。

## 5. C1：同任务并发执行

使用两个独立 Python 进程、同一 SQLite 数据库、同一 task ID。

第一个进程取得认领并在真实 Writer 中暂停。第二个进程随后尝试执行：

- 第二执行者没有写入文件；
- `claim_rejected` 被持久化；
- 第一执行者完成后 `task_succeeded` 只有一次；
- 文件内容只有一份正确结果；
- 最终 `claim_generation=1`，终态无活动租约。

测试：

- `test_c1_same_task_has_one_process_owner_and_one_side_effect`

## 6. C2：恢复与正常执行竞争

一个进程调用 `resume`，另一个进程调用 `execute_task`，两者针对同一中断任务。

- 只有一个进程取得租约；
- 竞争失败者不写文件；
- 恢复进程重新验证既有产物；
- 最终只有一个成功事件；
- 竞争失败不会覆盖已完成终态。

测试：

- `test_c2_recovery_and_normal_execution_contend_for_one_claim`

## 7. C3：不同任务并发

两个独立进程同时执行不同 task ID，共享数据库：

- 两个任务都成功；
- A/B 产物内容和 SHA-256 独立；
- 事件 `task_id` 无串线；
- 没有丢失更新；
- 一个任务不依赖另一个任务成功。

测试：

- `test_c3_different_tasks_run_concurrently_without_cross_linking`

## 8. SQLite 与异常恢复

已验证：

- 状态和事件同一事务提交；
- 事件插入异常会回滚状态更新；
- SQLite 锁超过 `busy_timeout` 后抛出 `StoreBusyError`；
- CLI 对存储/认领错误返回退出码 6；
- 非法终态回退被拒绝；
- 文件写入后工具证据入库失败，恢复只暴露 `artifact_exists` 阻断，不覆盖、不宣告成功；
- 产物删除后恢复失败；
- 产物修改后恢复失败；
- 终态重复 `execute`/`resume` 不追加事件；
- 相同请求重复 `run` 会创建不同任务，不静默合并。

## 9. 覆盖边界

已覆盖：

- 同机多进程；
- 本地 SQLite WAL；
- 同任务互斥；
- 不同任务并发；
- 真实 SIGKILL；
- 租约过期与 fencing；
- SQLite 锁超时；
- 状态/事件事务回滚。

未覆盖：

- 跨主机分布式锁；
- 网络文件系统；
- 断电和磁盘故障；
- 文件已写入但工具证据事务尚未提交时的真实 SIGKILL；
- 长任务租约心跳；
- 高并发压力或吞吐量基准。

## 10. 部署结论

支持同机多个进程共享一个本地 SQLite 数据库，并保证同一任务最多一个有效执行者。

不支持分布式执行。不得把 SQLite 租约外推为跨机器一致性、网络文件系统安全或高可用队列。

Phase 2 在此可靠性内核上增加了受限 LLM Planner；执行租约、fencing、并发和 SIGKILL 语义保持不变。

## 11. 隔离安装与 CLI 冒烟

在 `/tmp/zevorinth-phase1.5.xdT5hm/venv` 中执行：

```text
python3 -m venv ...
pip install --no-deps -e .
```

结果：

- 安装成功；
- 安装包版本 `0.2.0`；
- `zevorinth --version` 退出码 0；
- `zevorinth doctor --json` 报告 `hardening_phase=1.5`、SQLite execution leases 可用、分布式执行不可用；
- 安装后的入口真实完成任务 `740d2d9d-5cbd-4156-a4c0-10be244f7711`；
- 产物：`/tmp/zevorinth-phase1.5.xdT5hm/runtime/artifacts/740d2d9d-5cbd-4156-a4c0-10be244f7711/hardening.txt`；
- `wc -c`：24；
- 外部 `shasum`：`c53fd03b983d0fcd002559618e50026f2d94984f4e711dbe1c633e64e95cdd73`；
- `status` 和 `events` 查询退出码均为 0；
- 事件顺序 1–8，包含 `task_claimed`，任务终态清除执行者与租约。
