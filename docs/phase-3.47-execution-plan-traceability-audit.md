# Zevorinth Phase 3.47：执行计划可追溯性与恢复加固审计

## 1. 结论与基线

**PASS。** 最终执行计划现在会在容器启动前持久化，包含实际使用的 `evidence_id`、
`container_config_digest`、`command_digest` 和任务身份；持久化失败会阻断容器启动。
验证器会拒绝最终计划与测试证据不一致的任务。离线真实容器闭环、全量回归和变异测试
均通过。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `46ebb5c290cb42a602f4c95f925468d46ac519cd` |
| 真实模型 API 请求 | **0** |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 三个对象明确分离

| 对象 | 当前载体 | 语义 |
| --- | --- | --- |
| 模型候选 | 规划请求/诊断元数据 | 模型提出了什么操作 |
| 已批准计划 | `task.plan` | 系统批准的准备执行计划；模型不能提供可信字段 |
| 最终执行记录 | `execution_plan_snapshot` + `execution_plan_finalized` 事件 | 实际使用的证据、容器配置、命令摘要和任务身份 |

`task.plan` 不强行回写执行期注入的 `evidence_id`；最终执行记录是独立的权威快照。
这样不会把“计划批准”和“证据准备后实际执行”混为一个对象。

## 3. 最终执行计划

新事件：

```text
execution_plan_finalized
```

持久化字段包括：

- `kind=run_tests`
- `task_id`、`attempt_id`、`claim_generation`
- `plan_version`
- `tool_name`、`runner`、`target`、`workspace`
- `execution_backend`
- `evidence_id`
- `image`
- `config_digest`、`command_digest`
- `provisioning_artifacts`
- `prepared_at`

事件顺序：

```text
planning_completed → task_planned → execution_plan_finalized
→ tool_started → tool_succeeded → verification_started
→ verification_completed → task_succeeded
```

容器启动前执行以下检查：

1. 最终计划必须已持久化；
2. `task_id`、`attempt_id`、`claim_generation`、`plan_version` 必须匹配任务；
3. `evidence_id`、`config_digest`、`command_digest` 必须与执行器派生的身份一致；
4. 任何不一致都返回 `execution_plan_snapshot_mismatch` / `missing`，不启动容器。

持久化失败返回 `execution_plan_persistence_failed`；计划准备失败返回
`execution_plan_preparation_failed`。两者都不会启动容器。

## 4. 证据与验证器

`TestExecutionEvidence` 新增并持久化：

- `evidence_id`
- `container_name`
- `config_digest`
- `command_digest`

`TestResultVerifier` 现在把这些字段与执行参数比较；`_verify_test_and_finish()` 还会
比较最终执行计划与证据记录。任何不一致都会导致验证失败，不能进入 `succeeded`。

## 5. Provisioning 文件生命周期

最终执行计划记录：

| 文件 | 角色 | 所有权 | 保留 | 清理 |
| --- | --- | --- | --- | --- |
| `inside.txt` | 读探针哨兵 | workspace 输入或 provisioning | workspace baseline | 延迟到受控清理 |
| `probe-written.txt` | 写探针输出 | provisioning | workspace baseline | 延迟到受控清理 |

本阶段**没有删除**这些文件。它们的所有权、保留和清理策略已写入最终执行计划；后续
清理必须走现有受控协议，不能为了保持 diff 为零而直接删除。

## 6. 离线完整闭环

伪候选 + 真实 provisioning + 真实容器：

| 观察项 | 结果 |
| --- | --- |
| 真实模型 API 请求 | 0 |
| 最终计划 evidence_id | `auto-dd3246c6-9669-480b-a9b9-7fcbd7d390c0` |
| 最终计划 config_digest | `1a923d6c55fda3d54247dece36c1cca9b02b05d97d58bde64d2800d52a27e31e` |
| 最终计划 command_digest | `f53de1b7abdcd3d378eafa3f5a45cce37119df22ca69f312573ed061afc53024` |
| 计划/证据比对 | `None`（一致） |
| Provisioning 探针 | 7 |
| 容器 | `zv-dd3246c69669-85fdd1bd` |
| 容器退出码 | 0 |
| 测试退出码 | 0 |
| `TestResultVerifier` | `tests_passed` |
| 独立 evidence 复核 | `verified` |
| 任务内 workspace diff | 全零 |
| 宿主机回退 | 0 |

## 7. 失败与恢复覆盖

新增或锁定的分支：

| 分支 | 结果 |
| --- | --- |
| provisioning 失败 | `provisioning_failed`，容器不启动 |
| 最终计划准备失败 | `execution_plan_preparation_failed`，容器不启动 |
| 最终计划持久化失败 | `execution_plan_persistence_failed`，容器不启动 |
| 计划与证据不一致 | 验证失败，不能成功 |
| 工具名上下文缺失 | `unknown_tool`，不冒充写文件工具 |
| 容器超时/取消 | 既有真实容器测试通过，终止确认后才继续 |
| 证据过期/不匹配 | 既有证据生命周期测试通过，拒绝执行 |
| 测试未知状态恢复 | 既有测试确认不会盲目重跑 |
| 重复取消 | 既有测试确认幂等 |
| 并行容器名称冲突 | 测试改用 UUID 唯一名称，两套全量可并行 |

## 8. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 可信参数/执行契约专项 | 21/21 |
| 系统 Python 全量 unittest | 599/599，exit 0 |
| `.venv` 全量 unittest | 599/599，exit 0 |
| Mutation runner | **64 killed / 0 survived / 0 invalid / 0 error** |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异：

- `final_plan_evidence_id_ignored` — killed；
- `final_execution_plan_not_persisted` — killed。

既有 `run_tests_host_fallback_enabled` 已更新为复合变异（移除预检查并绕过容器分支），
仍然被杀死，证明没有静默宿主机回退。

## 9. 未验证事项与残余风险

1. 本阶段没有真实模型调用；最终计划修复的真实验证仍需一次新的授权调用。
2. 离线闭环使用伪候选，虽然执行器、evidence store 和容器都是真实的。
3. `type` 字段的实际值和语义仍未确定。
4. `probe-written.txt` 仍保留在 workspace baseline；清理策略已记录但尚未执行。
5. `execution_plan_snapshot` 是新的权威执行记录，但历史任务若没有该结构会验证失败；
   这是 fail-closed 行为，尚未提供迁移。

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
| `artifacts/audits/phase-3.47/offline-e2e.json` | `d3fed1dbbd29a7c7a641b5381f71b18c72082293b2e212120e0c23e9bc79dbbc` |
| `artifacts/audits/phase-3.47/mutations.json` | `8a2a288953a16b895aeab0d78245b8f0c74b23d0c53184432f69ee985a72b3c3` |
| `artifacts/audits/phase-3.47/stable-summary.json` | `a4d1adb95c2bae1cf68e0ca92789bde2d78445c743a08bcd7f860fd3fb35f169` |

## 12. 阶段判定

**PASS**：最终执行计划、证据身份、容器配置和命令摘要已形成可持久化的单一执行
记录；持久化失败阻断容器；计划/证据不一致无法成功；离线真实容器闭环和全量回归
均通过。下一次真实模型调用仍需单独授权，用于验证同一修复在真实模型路径下的行为。
