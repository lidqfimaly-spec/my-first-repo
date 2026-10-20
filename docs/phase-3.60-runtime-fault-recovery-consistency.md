# Zevorinth Phase 3.60：运行时故障与恢复一致性

## 1. 结论与基线

**PASS。** 真实容器测试完成、验证开始前进程被 `SIGKILL` 后，新进程只读恢复验证，
没有第二次测试或容器执行。`tool_succeeded` 事件写入失败不会产生成功状态，恢复时
被识别为未知执行并拒绝重跑。超时、取消和启动失败任务均通过生命周期审计。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `38176fdb630040b83152d97d591c8debeebdc4db` |
| 真实模型 API 请求 | **0**（本阶段） |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 真实容器进程中断恢复

worker 使用预先持久化的隔离 evidence 执行真实容器测试，在：

```text
TOOL_SUCCEEDED → VERIFICATION_STARTED
```

之后被 `SIGKILL`。新进程重新打开 SQLite，过期 claim，读取持久化 test evidence，
只读验证并完成任务：

- 任务最终 `succeeded`；
- `tool_started` 计数为 1；
- `tool_succeeded` 计数为 1；
- 没有第二次容器/测试执行；
- 没有重新 provisioning。

## 3. 持久化故障

注入 `tool_succeeded` event insert 失败：

- 执行线程异常退出；
- 任务未进入成功状态；
- 验证结果为空；
- 新进程恢复时返回 `BLOCKED / test_execution_unknown`；
- `tool_started` 仍为 1，没有盲目重跑；
- 没有 `task_succeeded`。

Event insert 失败与状态更新位于同一事务，状态不会被单独持久化。

## 4. 运行时状态审计

对真实容器超时、取消和启动失败任务调用 `audit_lifecycle()`：

| 场景 | 审计结果 |
| --- | --- |
| `test_timeout` | `ok=true` |
| `task_cancelled` | `ok=true` |
| `container_start_failed` | `ok=true`，无 test evidence |

这些运行时路径没有出现任务状态、事件或最终计划矛盾。

## 5. 恢复决策

- 执行任务的 `safe_to_retry` 始终为 `false`；
- 未知执行状态要求人工复核；
- evidence 读取失败降级为 `unverified`，不绕过验证；
- 清理仍遵守所有权、哈希和 pending intent；
- 没有宿主机回退。

完整矩阵：`artifacts/audits/phase-3.60/runtime-recovery-matrix.json`。

## 6. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 运行时恢复矩阵 | 8/8 |
| 系统 Python 全量 unittest | 637/637，exit 0 |
| `.venv` 全量 unittest | 637/637，exit 0 |
| Mutation runner | **73 killed / 0 survived / 0 invalid / 0 error** |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

## 7. 未验证事项与残余风险

1. 本阶段没有真实模型调用；真实模型异常路径使用既有 3.52–3.56 基线。
2. 进程恢复基于本机 SQLite 与 Docker；未覆盖所有 OS/文件系统组合。
3. 启动失败仍是注入故障，不是自然 Docker daemon/镜像异常。
4. 自动恢复、自动清理队列和历史迁移未实现。
5. cleanup TOCTOU 窗口仍未证明彻底消除。

## 8. 安全边界

- 真实模型 API 请求：0（本阶段）；
- 未读取 `.env` 或输出密钥；
- 未访问互联网、未安装依赖；
- 未启动或关闭 Docker Desktop；
- 未修改、暂存或跟踪 `.idea/`；
- 未执行 reset / clean / rebase / amend / 分支切换；
- 未放宽 policy、隔离证据或独立验证门禁；
- 未发生宿主机回退。

## 9. 产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.60/runtime-recovery-matrix.json` | `2d118a81e72b2510dc7b8a09b878da10b0993707beba7cd8b35177fc32399217` |
| `artifacts/audits/phase-3.60/mutations.json` | `09bf68029544c840579b80f39e676c947d526a12c6ae759cbf9778e19627915e` |
| `artifacts/audits/phase-3.60/stable-summary.json` | `331a3db746f7f268a29e5896bb67df0c07f802c1fbacdd96859b02ef3d6869e8` |

## 10. 阶段判定

**PASS**：真实容器中断可由新进程只读恢复；持久化故障不会伪造成功；运行时超时、
取消和启动失败与生命周期审计保持一致。
