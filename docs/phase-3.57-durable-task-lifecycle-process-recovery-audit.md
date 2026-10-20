# Zevorinth Phase 3.57：持久任务生命周期与进程级恢复审计

## 1. 结论与基线

**PASS。** 任务状态、事件、最终计划、验证结果和清理记录现在由只读生命周期审计器
重新计算一致性；独立进程竞争同一清理目标最多删除一次；测试完成后验证前 SIGKILL
可由新进程只读恢复，不重新执行测试。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `1d6c3546bfd79478559a2b6451b088319110ec1f` |
| 真实模型 API 请求 | **0**（本阶段） |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 任务生命周期审计器

新增只读审计：

```text
TaskService.audit_lifecycle(task_id)
```

它从 task、events、final execution plan、test evidence 和 cleanup records 重新计算：

- `succeeded` 是否具有通过的独立验证、成功工具结果和 `task_succeeded` 事件；
- `failed` / `cancelled` 是否具有匹配的终态事件；
- 最终执行计划版本、evidence_id、config_digest、command_digest 是否与证据一致；
- run_tests 成功是否具有退出码 0、`tests_passed` 和最终测试计划；
- 历史 run_tests 任务是否缺少最终执行计划；
- cleanup intent/terminal 是否成对，是否存在重复/孤立终态。

该审计器不修改任务、事件或证据。

## 3. 清理生命周期状态

清理操作由事件派生为：

| 状态 | 含义 | 重试 / 人工 |
| --- | --- | --- |
| `completed` | completed 事件存在，或 `already_cleaned` | 不重试 |
| `pending` | 只有 intent | 可重试 |
| `expired` | intent 超过 24 小时未终结 | 不自动重试，需人工 |
| `rejected` | 明确拒绝 | 按错误类型决定；多数需人工 |
| `unverified` | 结果不确定或事件未持久化 | 默认需人工 reconcile |

本阶段仍**不引入自动清理队列**；生命周期状态是只读审计和重试决策的基础。

## 4. 进程级任务恢复

worker 在真实测试命令完成后、`verification_started` 之后被 `SIGKILL`：

```text
TOOL_SUCCEEDED → VERIFICATION_STARTED → [SIGKILL] → 新进程 resume
```

新进程读取持久化 test evidence，只读验证并完成任务：

- 任务最终 `succeeded`；
- `tool_started` 事件仍为 1；
- 没有第二次测试执行；
- 没有第二个容器/测试进程。

## 5. 进程级清理竞争

两个独立 Python 进程同时清理同一 provisioning 文件：

- 一个 `completed`；
- 另一个返回 `already_cleaned` 或明确的 `cleanup_in_progress`；
- 实际删除最多一次；
- `cleanup_completed` 事件恰好一次。

在预检查发现目标已消失时，也会等待并检查另一请求的完成/intent 状态，不再直接
误报 `missing_target`。

## 6. 事务与历史任务

- event insert 失败时，任务状态更新随事务回滚；
- 没有最终执行计划的历史 run_tests 任务被审计器标记
  `missing_final_execution_plan`；
- 清理 API 继续返回 `no_final_execution_plan`；
- 不补造历史证据，不自动迁移。

## 7. 生命周期矩阵

完整矩阵：`artifacts/audits/phase-3.57/lifecycle-durability-matrix.json`。

| 类别 | 数量 |
| --- | --- |
| 总计 | 10 |
| 通过 | 10 |
| 真实进程竞争/SIGKILL | 4 |
| fixture / unit | 6 |
| 意外成功 | 0 |

Phase 3.52–3.56 真实模型超时、取消、验证器拒绝和启动失败基线保留，本阶段未重复
消耗模型额度。

## 8. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 生命周期/进程专项 | 10/10 |
| 系统 Python 全量 unittest | 625/625，exit 0 |
| `.venv` 全量 unittest | 625/625，exit 0 |
| Mutation runner | **73 killed / 0 survived / 0 invalid / 0 error** |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异：

- `task_lifecycle_verification_bypass` — killed；
- `cleanup_intent_expiry_ignored` — killed。

并发清理预检查/完成事件竞态修复后，进程竞争测试连续 10 次通过。

## 9. 未验证事项与残余风险

1. 本阶段没有新增真实模型调用；真实模型异常路径依赖 3.52–3.56 基线。
2. 进程级任务恢复测试使用 host backend，不是容器执行崩溃。
3. 清理仍是显式调用，没有自动队列或任务生命周期钩子。
4. 历史任务迁移未实现，继续 fail-closed。
5. cleanup TOCTOU 窗口仍未被证明彻底消除。
6. 进程竞争基于本机 SQLite 和 Python 进程，未覆盖所有 OS/文件系统组合。

## 10. 安全边界

- 真实模型 API 请求：0（本阶段）；
- 未读取 `.env` 或输出密钥；
- 未访问互联网、未安装依赖；
- 未启动或关闭 Docker Desktop；
- 未修改、暂存或跟踪 `.idea/`；
- 未执行 reset / clean / rebase / amend / 分支切换；
- 未放宽 policy、隔离证据或独立验证门禁；
- 未发生宿主机回退。

## 11. 产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.57/lifecycle-durability-matrix.json` | `a824a84d34fe27b37b72632a1d7cea97cb2ac2236a533d333a8cbc08b3fbfc71` |
| `artifacts/audits/phase-3.57/mutations.json` | `a2779147f243e9955908fd215d770664a1d337b4f14532bcece6655324494759` |
| `artifacts/audits/phase-3.57/stable-summary.json` | `3c7642a2b3bcbbeefbac6b6770482c315950e012b966e1b89b23aa7310c7c216` |

## 12. 阶段判定

**PASS**：持久任务生命周期审计、进程级清理竞争、SIGKILL 恢复、事件/状态事务性、
清理状态派生和历史任务 fail-closed 均有实际离线证据；全量回归和变异测试通过。
