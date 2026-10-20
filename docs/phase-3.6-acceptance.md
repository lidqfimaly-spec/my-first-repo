# Zevorinth Phase 3.6：协作取消与端到端验收

状态：**PASS WITH LIMITATIONS**

本阶段未调用真实模型 API、未读取 `.env`、未推送远程。

## 1. 基线

- 基线：`1dc4f26975fccdc9f55ec93c1df0d56452a3ea74`
- 分支：`phase-2.6-response-hardening`
- Schema：保持 v4，无迁移。

## 2. 取消状态

| 状态 | 持久化 | Owner/Lease |
| --- | --- | --- |
| 无取消 | cancel 字段为空 | 正常 |
| cancel_requested | 保存时间、来源、原因，写 cancel_requested 事件 | 不改变 |
| cancelled | TaskStatus.CANCELLED，清除 owner/token/expiry | 当前执行者处理 |
| completed | SUCCEEDED/FAILED 等终态 | 无 |

`request_cancel()` 幂等；重复请求不更新时间、来源或事件。

外部进程只能写取消请求，不能直接取消活动任务或修改所有权。

## 3. 检查点

当前 lease holder 在以下位置读取最新取消请求：

1. claim/resume 开始。
2. planning 返回后。
3. TOOL_STARTED 前。
4. 发布锁内、最终 link 前。
5. writer 返回后、保存 TOOL_SUCCEEDED 前。
6. Verifier 前。
7. VERIFICATION_COMPLETED 后、SUCCEEDED 前。

发布前取消不产生最终文件。

工具已完成后取消：

- 先持久化真实 tool result/evidence；
- Task 转 CANCELLED；
- 不执行 Verifier，不写 task_succeeded；
- 文件保留，明确记录 side effect 已发生，不宣称撤销。

## 4. 计划与证据隔离

TOOL_STARTED 时持久化：

- `execution_plan_version`
- `execution_plan_snapshot`

Verifier 前和成功提交前都再次比较当前 plan version/snapshot。若变化：

- 不允许 SUCCEEDED；
- 任务进入 FAILED `execution_plan_snapshot_changed`；
- 旧验证结果不能证明新计划成功。

计划修订本身仍拒绝任何已有 tool/artifact/真实执行模式的任务。

## 5. End-to-End Acceptance

运行器：`tests/e2e_acceptance_runner.py`

场景：

1. 目标到独立验证文件。
2. 追加约束并确认 revision。
3. 协作取消。
4. 工具后 SIGKILL 恢复。
5. 最终文件存在、evidence 缺失。
6. Verifier 否决。
7. 重复/过期 revision 与重复副作用。

机器可读报告：

```text
artifacts/audits/phase-3.6-acceptance.json
```

最近结果：

| 指标 | 值 |
| --- | --- |
| scenarios | 7 |
| success_rate | 1.0 |
| executable_success_rate | 1.0 |
| recovery_success_rate | 1.0 |
| duplicate_side_effects | 0 |
| cancellation_handled | true |
| evidence_complete_rate | 0.5714285714285714 |

evidence complete rate 包含故意无最终产物/证据的阻断与否决场景，不应解释为产品失败率。

## 6. 测试

| 项目 | 结果 |
| --- | --- |
| 系统完整 unittest | 216/216，exit 0 |
| 隔离环境完整 unittest | 216/216，exit 0 |
| context continuity | 10/10 |
| cooperative cancellation | 5/5 |
| context + cancellation 重复 | 20/20 轮 0 失败 |
| E2E acceptance | 7/7，连续 10 轮 0 失败 |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

## 7. 变异

本轮捕获：

- 移除 claim 检查点取消；
- 忽略发布前取消；
- 移除 execution plan snapshot 校验；
- 取消请求不幂等。

既有 context/revision 变异继续通过。

## 8. 限制

- 取消是协作式的；活动 Lease owner 不响应时不会强制抢占。
- 已发布文件不能撤销；取消只记录真实结果。
- 无活动 Lease 的 `cancel` 可立即取消；活动任务应先 `request-cancel`。
- E2E runner 使用确定性本地 Planner，不代表真实 LLM 通用 Agent 能力。
- coverage / mutation framework：NOT RUN，环境未安装。
