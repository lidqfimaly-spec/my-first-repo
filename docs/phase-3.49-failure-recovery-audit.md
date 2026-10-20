# Zevorinth Phase 3.49：执行失败、取消与恢复可靠性审计

## 1. 结论与基线

**PASS。** 容器启动失败现在有明确错误码；测试退出码为 0 后，evidence 过期、撤销
或复核失败都会阻止任务成功；超时、取消、终止未知和恢复路径均保持失败关闭。故障
矩阵 15/15 通过，其中 4 项使用真实容器。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `cd7fd90a2e3718dee776ba05a0b0947272c06f87` |
| 真实模型 API 请求 | **0** |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 新增修复

### 2.1 容器启动失败

`_run_container_test()` 现在捕获 `Popen` 的 `OSError` 并返回：

```text
container_start_failed
```

最终执行计划已经持久化，失败工具记录为 `run_tests`，不会误报成功，也不会回退
宿主机。

### 2.2 执行后证据重验

`_verify_test_and_finish()` 在测试退出码为 0 后，仍会重新读取并验证 evidence：

- TTL 过期 → `evidence_expired`
- 已被撤销 → `evidence_invalidated`
- 其他复核失败 → `evidence_revalidation_failed`

即使 `tool_result.success=True`、测试退出码为 0，验证失败也不能进入 `succeeded`。

### 2.3 确定性故障注入点

新增：

```text
after_test_before_verification
```

测试可以在真实测试完成后、独立验证前撤销 evidence，验证执行后重验门禁。

## 3. 故障矩阵

完整矩阵：`artifacts/audits/phase-3.49/failure-matrix.json`。

| 场景 | 来源 | 结果 |
| --- | --- | --- |
| 最终计划持久化失败 | 模拟 provisioning/container | `execution_plan_persistence_failed`，容器未启动 |
| 容器启动失败 | 真实 evidence + 注入 Popen 失败 | `container_start_failed`，无容器 |
| 超时 | 真实容器 | `test_timeout`，终止确认 |
| 取消 | 真实容器 | `task_cancelled`，终止确认 |
| 终止无法确认 | 真实容器 | `container_termination_unverified`，不报成功 |
| 重复取消 | host backend | 取消状态幂等 |
| evidence 过期（退出码 0） | 模拟容器后验门禁 | `evidence_expired`，任务失败 |
| evidence 撤销（退出码 0） | 模拟容器后验门禁 | `evidence_invalidated`，任务失败 |
| evidence 撤销（真实容器） | 真实容器 + 真实撤销 | `evidence_invalidated`，任务失败 |
| 验证器拒绝非 0 退出 | 单元验证器 | `tests_failed` |
| 验证器拒绝截断输出 | 单元验证器 | `test_output_truncated` |
| 未知状态恢复 | 模拟 tool_started | `test_execution_unknown`，不重跑 |
| 验证阶段中断恢复 | 注入 store 故障 | 只读验证，不重跑 |
| 陈旧快照身份 | 单元验证器 | stale snapshot 被拒绝 |
| 容器任务中断恢复 | 模拟 tool_started | `test_execution_unknown`，不启动第二个容器 |

矩阵统计：

- 总计 15；
- 通过 15；
- 真实容器 4；
- 模拟 5；
- 单元/host 6；
- 意外成功 0。

**真实容器与模拟严格分开统计**：模拟容器场景不计入真实容器通过数。

## 4. 恢复语义

| 状态 | 行为 |
| --- | --- |
| 尚未开始 | 可按正常流程执行 |
| 已开始、结果未知 | `test_execution_unknown`，禁止重跑 |
| 已开始、结果已持久化但未验证 | 只读验证，不重新启动测试 |
| 验证失败 | 任务失败，不能进入 `succeeded` |
| 陈旧/不匹配快照 | fail-closed，拒绝验证通过 |

这会避免“结果未知被当作可安全重试”的误判。

## 5. Provisioning 文件与历史兼容

最终执行计划继续记录 `inside.txt` / `probe-written.txt` 的 ownership、retention 和
`deferred_to_controlled_cleanup`。本阶段没有删除文件，也没有绕过既有清理协议。

没有最终执行计划结构的历史任务会按 fail-closed 验证失败；本阶段没有实现迁移，
也没有放宽旧任务验证。

## 6. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 故障矩阵专项 | 15/15 |
| 相关故障/恢复专项 | 100/100 |
| 系统 Python 全量 unittest | 602/602，exit 0 |
| `.venv` 全量 unittest | 602/602，exit 0 |
| Mutation runner | **66 killed / 0 survived / 0 invalid / 0 error** |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异：

- `evidence_revalidation_ignored` — killed；
- `container_start_failure_misclassified` — killed。

## 7. 未验证事项与残余风险

1. 本阶段没有真实模型调用；故障注入均在离线路径完成。
2. evidence 过期场景使用模拟容器后验门禁；真实容器下验证的是 evidence 撤销。
3. provisioning 文件清理仍未执行，只记录了延迟清理策略。
4. 历史任务迁移未实现，旧结构继续 fail-closed。
5. 未覆盖所有宿主机/Docker 版本差异和所有异常退出组合。

## 8. 安全边界

- 真实模型 API 请求：0；
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
| `artifacts/audits/phase-3.49/failure-matrix.json` | `a18af8b53786397a9b54774fec0912493f761edbb24a495c534d456084dc4771` |
| `artifacts/audits/phase-3.49/mutations.json` | `cdaab504dcfe3744d156b999a4becf2a6ae27476d837f277193f174dfb04eb1c` |
| `artifacts/audits/phase-3.49/stable-summary.json` | `2ae1208cdd4e6b9134e9f97d36b686da3bbb349088f482a7aa326d7d97cace88` |

## 10. 阶段判定

**PASS**：容器启动失败、超时/取消、终止未知、evidence 过期/撤销、验证器拒绝和
中断恢复均有明确失败关闭行为；真实容器与模拟场景分开报告；全量回归和变异测试
通过。下一次真实模型调用仍需单独授权。
