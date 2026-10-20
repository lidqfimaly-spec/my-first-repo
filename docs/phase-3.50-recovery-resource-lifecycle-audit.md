# Zevorinth Phase 3.50：恢复边界与资源生命周期审计

## 1. 结论与基线

**PASS。** 容器终止不确定、恢复幂等、执行后证据失效、provisioning 文件清理和历史任务
兼容均已纳入离线验证。真实 provisioning 生成的 `probe-written.txt` 已通过受控
quarantine + unlink 清理，`inside.txt` 因所有权不明确被保留。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `5f16252bda5e5ad595f548cccf78f53e03e05db3` |
| 真实模型 API 请求 | **0** |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 恢复边界

### 终止状态

- 容器终止无法确认 → `container_termination_unverified`；
- 任务保持失败，不报成功；
- 再次 `resume` 返回同一失败任务，不启动第二个容器；
- `tool_started` 事件计数保持不变。

### 恢复幂等

| 状态 | 行为 |
| --- | --- |
| 未开始 | 允许正常执行 |
| 已开始但结果未知 | `test_execution_unknown`，禁止重跑 |
| 已开始且结果已持久化 | 只读验证，不重新启动测试 |
| 验证阶段中断 | 只读恢复验证 |
| 陈旧快照身份 | fail-closed 拒绝 |
| 重复取消 | 取消状态幂等 |

## 3. 执行后证据一致性

新增执行后 evidence 重验：

- 测试退出码为 0、`tool_result.success=True` 后，仍重新读取 evidence；
- 过期 / 撤销 / 复核失败分别进入：
  - `evidence_expired`
  - `evidence_invalidated`
  - `evidence_revalidation_failed`
- 真实容器场景已验证：测试成功后撤销 evidence，任务仍失败；
- 最终执行计划与 evidence 的 `evidence_id/config_digest/command_digest` 不一致时，
  不能进入 `succeeded`。

## 4. Provisioning 文件清理契约

新增显式 API：

```text
cleanup_provisioning_artifact(task_id, relative_path, enabled, authorized)
reconcile_provisioning_cleanup(task_id, operation_id)
```

清理条件：

1. 任务存在最终执行计划；
2. 任务身份、attempt、generation 与最终计划一致；
3. 路径必须来自最终计划 `provisioning_artifacts`；
4. 仅允许 `ownership="provisioning"`；
5. 必须标记 `cleanup="deferred_to_controlled_cleanup"`；
6. 显式 `enabled=True` 且 `authorized=True`；
7. 任务无活动 lease；
8. 当前文件是普通文件、非符号链接；
9. 当前 SHA-256 与 provisioning 记录一致；
10. cleanup intent 必须先持久化，再进入 quarantine；
11. 复用现有 `execute_cleanup` 的 quarantine、重新复核和 unlink；
12. 重复请求返回 `already_cleaned`，不重复副作用。

`inside.txt` 的 ownership 是 `workspace_input_or_provisioning`，因来源不明确被拒绝
清理并保留。只有 `probe-written.txt`（ownership=`provisioning`）在授权后允许清理。

## 5. 真实 provisioning 清理验证

伪候选 + 真实 provisioning + 真实容器后执行：

| 观察项 | 结果 |
| --- | --- |
| 任务测试状态 | `succeeded` |
| 清理前文件 | `inside.txt`、`probe-written.txt`、`test_ok.py` |
| cleanup intent | 已持久化 |
| 清理结果 | `completed`，removed=true |
| quarantine 路径 | `.quarantine/<operation>/probe-written.txt` |
| 恢复核对 | `reconciled_completed` |
| 重复清理 | `already_cleaned`，removed=false |
| `inside.txt` | 保留 |
| 任务状态 | 清理后仍为 `succeeded` |
| 事件 | `cleanup_intent` → `cleanup_completed` → `cleanup_rejected`（重复请求） |

这证明清理不会修改原任务最终状态，也不会把重复请求报告为新的删除。

## 6. 历史任务策略

没有 `execution_plan_snapshot` 的历史任务：

- 不补造最终执行计划；
- 不自动迁移；
- 清理请求返回 `no_final_execution_plan`；
- 继续 fail-closed。

后续如需迁移，应单独设计只读审计与显式迁移流程。

## 7. 恢复/清理矩阵

完整矩阵：`artifacts/audits/phase-3.50/recovery-cleanup-matrix.json`。

| 类别 | 数量 |
| --- | --- |
| 总计 | 19 |
| 通过 | 19 |
| 真实容器 | 3（终止未知、真实证据撤销、真实 provisioning 清理） |
| 真实进程 | 1（并行 marker 隔离） |
| fixture / simulated / unit | 15 |
| 意外成功 | 0 |

## 8. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 恢复/清理矩阵 | 19/19 |
| 系统 Python 全量 unittest | 610/610，exit 0 |
| `.venv` 全量 unittest | 610/610，exit 0 |
| Mutation runner | **69 killed / 0 survived / 0 invalid / 0 error** |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增清理变异全部被杀死：

- `provisioning_cleanup_ownership_bypassed`
- `provisioning_cleanup_hash_ignored`
- `provisioning_cleanup_intent_ignored`

## 9. 未验证事项与残余风险

1. 本阶段没有真实模型调用；清理 e2e 使用伪候选，但 provisioning、容器和清理都是真实的。
2. 清理 API 是显式调用，不会自动在任务结束时清理；这避免隐式副作用，但也需要编排层决定调用时机。
3. `inside.txt` 仍保留，所有权不明确；当前策略是拒绝清理。
4. 现有 cleanup 原语的 TOCTOU 已被缩小但没有被证明完全消除。
5. 历史任务迁移未实现，继续 fail-closed。
6. 未覆盖所有宿主机/Docker 版本和所有中断时点。

## 10. 安全边界

- 真实模型 API 请求：0；
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
| `artifacts/audits/phase-3.50/cleanup-e2e.json` | `3e705ab2288ace2f7fb6f4a2b9208d2dcf3042e0cc3916a9779d4c7e7bbdafb2` |
| `artifacts/audits/phase-3.50/recovery-cleanup-matrix.json` | `deecc7ee2c65088fa9c9665e1d6d197aba46dc57c6be4f1abe5fba8cfb98dd05` |
| `artifacts/audits/phase-3.50/mutations.json` | `f3acce71003e83efa3b358e9155e60cb600d985f3253bb47e7e23a58112ec52e` |
| `artifacts/audits/phase-3.50/stable-summary.json` | `7dcc5a23dfb8ae96bd43d373966d9edd7a0a14fde08a3c96d0ce66816eb0d377` |

## 12. 阶段判定

**PASS**：恢复边界、证据失效、清理意图门禁、quarantine 清理、幂等性和历史任务
fail-closed 均有实际离线测试；真实 provisioning 生成的探针文件已完成受控清理。
下一次真实模型调用仍需单独授权。
