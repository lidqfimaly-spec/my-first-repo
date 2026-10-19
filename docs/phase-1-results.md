# 第 1 阶段验收结果

日期：2026-10-19

## 总结

自动化测试共 32 项，全部通过。A1–A6 均有独立测试覆盖；A4 拆成三条、A5 拆成三条测试证明不同边界。

执行命令：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s tests -v
```

真实结果：

```text
Ran 32 tests in 0.193s
OK
```

## A1：真实执行成功

结果：通过。

证据：

- Planner 为 `deterministic-v1`，计划只有一步；
- Policy 允许；
- 工具创建真实文件；
- Verifier 独立读取并比较内容、字节数和 SHA-256；
- 最终状态 `succeeded`；
- 执行模式 `real`；
- 关闭并重新打开服务后仍可查询；
- 事件顺序严格递增，只有一个 `task_succeeded`。

测试：

- `test_a1_real_execution_success_is_independently_verified`

## A2：工具执行失败

结果：通过。

证据：

- 可控 Writer 注入 `injected_write_failure`；
- 任务进入 `failed`；
- 不生成验证完成事件；
- 不生成成功事件；
- 后续健康任务可正常成功。

测试：

- `test_a2_tool_failure_is_not_reported_as_success`

## A3：必要信息缺失

结果：通过。

证据：

- 缺少 `content` 时进入 `awaiting_clarification`；
- `missing_fields=["content"]`；
- 不调用工具，不创建任务产物目录；
- 通过 `resume` 补齐后真实执行并成功。

测试：

- `test_a3_missing_fields_require_clarification_then_resume`

## A4：安全策略阻断

结果：通过。

覆盖：

- `../outside.txt`；
- 绝对路径；
- 任务目录符号链接逃逸；
- `artifacts/` 根目录本身为符号链接；
- 已有文件覆盖；
- 空内容和超限内容由工具级测试覆盖。

证据：

- 状态为 `blocked`；
- 执行模式为 `not_executed`；
- 无 `tool_started` 事件；
- 允许根目录外没有文件副作用；
- 对阻断任务无修正的重复 `resume` 不追加事件；
- 提供安全新路径后可继续完成。

测试：

- `test_a4_policy_blocks_paths_before_tool_side_effects`
- `test_a4_symlink_escape_and_overwrite_are_blocked`
- `test_a4_symlinked_artifacts_root_is_blocked`

## A5：中断与恢复

结果：通过（确定性进程重启模拟）。

证据：

- 在真实写入后、Verifier 前触发故障；
- 故障前数据库状态为 `verifying`，文件真实存在；
- 新服务启动后将任务识别为 `interrupted`；
- 恢复服务使用“若被调用就失败”的 Writer，实际调用数为 0；
- 恢复通过重新读取与验证已有文件进入 `succeeded`；
- 只存在一个 `task_succeeded`；
- 再次 `resume` 不追加事件；
- 修改文件后恢复会进入 `failed`，保留被修改文件，不覆盖。
- 子进程完成真实写入与 `verifying` 检查点后使用 `os._exit(23)` 直接退出；父进程启动新运行时后识别 `interrupted`，重新验证并成功。

测试：

- `test_a5_interrupted_after_write_recovers_by_verifying_existing_file`
- `test_a5_modified_file_is_not_overwritten_during_recovery`
- `test_a5_new_process_recognizes_and_verifies_existing_file`

边界：已执行独立子进程的确定性异常退出，但没有执行真实 OS `kill -9`、断电或文件系统损坏测试。

## A6：真实、模拟、未执行

结果：通过。

| 场景 | 任务状态 | 执行模式 | 真实文件 | Verifier |
| --- | --- | --- | --- | --- |
| 真实 | `succeeded` | `real` | 是 | 通过 |
| 模拟 | `simulated` | `simulated` | 否 | 未执行 |
| 策略阻断 | `blocked` | `not_executed` | 否 | 未执行 |

测试：

- `test_a6_real_simulated_and_not_executed_are_distinct`

## 其他测试

- 状态机合法/非法转换：3 项；
- 路径、写入、覆盖、符号链接和模拟工具：6 项；
- Verifier 正常、缺失、内容不匹配、证据不匹配、模拟拒绝：5 项；
- CLI 自检、运行、查询、事件、模拟、澄清/阻断退出码、未知任务：6 项；
- EventStore 重启持久化、非法转换不追加事件：2 项。

## 实际安装和 CLI 冒烟

在隔离虚拟环境 `/tmp/zevorinth-phase1.4Ax2rK/venv` 中执行：

```text
pip install --no-deps -e .
```

结果：成功构建并安装 editable wheel。

安装后的入口实际完成一次真实任务：

- Task ID：`79eea6a4-1097-40fb-a42a-e7ec411ab30d`
- 产物：`/tmp/zevorinth-phase1.4Ax2rK/runtime/artifacts/79eea6a4-1097-40fb-a42a-e7ec411ab30d/proof.txt`
- `wc -c`：26
- `shasum -a 256`：`98dc2fe9222851d43618172f571c9f5e97fe4fb00c24b1cd9df6c03494b919e9`
- Verifier 报告同一字节数和 SHA-256；
- `status` 与 `events` 均从同一 SQLite 数据库成功查询。

最终复跑安装后的入口，再完成任务：

- Task ID：`33729bda-3d1f-46b6-a60a-c7d45dc4ee5f`
- 产物：`/tmp/zevorinth-phase1.4Ax2rK/runtime/artifacts/33729bda-3d1f-46b6-a60a-c7d45dc4ee5f/final.txt`
- `wc -c`：24
- `shasum -a 256`：`d355873498018fc790e082dea31a57087b630abba0582677cdb5000c6261c321`
- `status --json` 返回同一字节数、SHA-256、`succeeded` 和 `real`。

## Phase 1.5 后续状态

本文件保留为 Phase 1 基线 `752cae3` 的真实验收记录。Phase 1.5 在不删除 A1–A6 的前提下增加了执行租约、真实 SIGKILL、并发竞争和 SQLite 故障测试；最新结果见 `phase-1.5-results.md`。
