# Zevorinth Phase 3.59：持久化故障注入与恢复决策验证

## 1. 结论与基线

**PASS。** 审计器现在能处理 event/state 矛盾、缺失/重复终态、孤立清理终态、非法
清理时间戳、evidence 失效/读取失败，以及阻塞或中断执行的人工恢复决策。审计仍为
只读，执行任务不会获得自动重试许可。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `1e2e2ea06eb3d934d2e277a70caf38bf289a08fd` |
| 真实模型 API 请求 | **0**（本阶段） |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 新增审计行为

### 阻塞/未知执行

以下状态强制：

```text
task_recovery.safe_to_retry = false
task_recovery.manual_intervention = true
task_recovery.recommendation = manual_review
```

- `BLOCKED`
- `AWAITING_CLARIFICATION`
- `INTERRUPTED`
- `RUNNING` / `VERIFYING`
- `test_execution_unknown`
- `container_termination_unverified`
- `isolation_unverified`

### Evidence 读取失败

审计器不再因 evidence store 的意外异常崩溃；读取失败降级为：

```text
evidence_state.state = unverified
```

成功任务若 evidence 不可验证，会产生
`succeeded_with_unverified_evidence`，不能报告为一致。

## 3. 故障/恢复矩阵

| 场景 | 结果 |
| --- | --- |
| 成功生命周期 | `ok=true` |
| 只读审计 | 任务、事件、workspace 文件均不变 |
| 缺失 `task_succeeded` | `succeeded_without_task_event` |
| 重复终态事件 | `duplicate_terminal_event` |
| cleanup completed 无 intent | `cleanup_terminal_without_intent` |
| cleanup intent 时间戳非法 | `invalid_cleanup_timestamp`，人工介入 |
| 成功 run_tests 的 evidence 失效 | `succeeded_with_unverified_evidence` |
| evidence 读取异常 | `evidence_state=unverified`，审计不崩溃 |
| 中断任务 | `safe_to_retry=false`，`manual_review` |
| 阻塞未知执行 | `safe_to_retry=false`，`manual_review` |
| event insert 失败 | 状态更新随事务回滚 |

完整矩阵：`artifacts/audits/phase-3.59/persistence-recovery-matrix.json`。

## 4. 恢复决策边界

| 情况 | 决策 |
| --- | --- |
| 成功/失败/取消终态 | 不自动重试 |
| 未知/中断/阻塞执行 | 人工复核，禁止盲目重跑 |
| evidence 不可验证 | 不成功，要求人工或重新验证 |
| cleanup pending | 可重试，但需另行授权 |
| cleanup expired | 不自动重试，人工介入 |
| cleanup unverified | 默认人工 reconcile |
| 历史任务无最终计划 | fail-closed，不补造记录 |

审计器返回 `scope=consistency_only` 和 `security_certification=false`，明确其不是
执行结果，也不是安全认证。

## 5. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 持久化/恢复矩阵 | 11/11 |
| 系统 Python 全量 unittest | 635/635，exit 0 |
| `.venv` 全量 unittest | 635/635，exit 0 |
| Mutation runner | **73 killed / 0 survived / 0 invalid / 0 error** |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

## 6. 未验证事项与残余风险

1. 本阶段没有真实模型调用；真实超时、取消、验证器拒绝、启动失败使用既有基线。
2. 审计器只验证已定义的不变量，不能证明底层数据库从未被篡改。
3. 自动恢复、自动清理队列和历史迁移仍未实现。
4. cleanup TOCTOU 窗口与跨 OS/文件系统行为仍未完整验证。
5. 执行任务恢复只做离线/进程级验证，不等于所有真实运行时故障已覆盖。

## 7. 安全边界

- 真实模型 API 请求：0（本阶段）；
- 未读取 `.env` 或输出密钥；
- 未访问互联网、未安装依赖；
- 未启动或关闭 Docker Desktop；
- 未修改、暂存或跟踪 `.idea/`；
- 未执行 reset / clean / rebase / amend / 分支切换；
- 未放宽 policy、隔离证据或独立验证门禁；
- 未发生宿主机回退。

## 8. 产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.59/persistence-recovery-matrix.json` | `6df5f37ded18410d904f7560b5eb13b8c5c214bcf5cc151f64756af9dc254b44` |
| `artifacts/audits/phase-3.59/mutations.json` | `cd16b6c958f441582d854217ed0d2b14be68c6686aad526c93ae648ec78f2b91` |
| `artifacts/audits/phase-3.59/stable-summary.json` | `3e9ea01e18674f1fd7ba892d3fb8be5c31917c8a16daeb53893942d8e056317a` |

## 9. 阶段判定

**PASS**：持久化故障能够被检出并保守分类，证据读取异常不会崩溃，未知/阻塞执行
不会获得自动重试许可，审计只读且全量/变异测试通过。
