# Zevorinth Phase 3.44：单次真实模型端到端验证审计

## 1. 结论

**BLOCKED。** 唯一一次授权调用产出了合法的 `run_tests` 候选，目标为 `test_ok`，
本地校验通过；但计划层把可信的 `auto_provision_isolation=true` 丢成了 `false`，
执行器因此跳过了 provisioning，并在 evidence 门禁处失败。

没有重试、没有启动容器、没有 provisioning、没有文件副作用、没有宿主机回退。
候选协议问题本次得到正面证据；执行链问题仍未验证。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `a1f1c65d4a3fb67586bd6c258e4ba1c07fcfa441` |
| Provider / 模型 | DeepSeek / `deepseek-flash` |
| 授权请求上限 | **1** |
| 实际请求 | **1** |
| 自动重试 | **0** |
| 结果 | `failed` / `isolation_unverified` / `evidence_id_missing` |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 离线前置结果

| 检查 | 结果 |
| --- | --- |
| 系统 Python 全量 unittest | 578/578，exit 0 |
| `.venv` 全量 unittest | 578/578，exit 0 |
| Planner / run_tests / 证据 / 执行器专项 | 136/136，exit 0 |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |
| `DEEPSEEK_API_KEY` 存在性 | true（未打印内容） |
| 本地 `python:3.13-alpine` | 存在 |

## 3. 唯一一次真实调用

固定目标：

```text
Run the unit test module test_ok from the demo workspace.
```

固定任务参数：

```json
{
  "workspace": "demo",
  "auto_provision_isolation": true
}
```

调用统计：

- 模型调用 1 次，延迟 1.68s；
- 输入 741 tokens，输出 108 tokens，总计 849 tokens；
- `candidate_received=true`；
- `candidate_validation=passed`；
- `plan_tool=run_tests`；
- `test_target=test_ok`；
- 没有重试，没有改写模型输出。

这是首次在真实调用中确认：模型可以产出通过当前严格校验的 `run_tests` 候选。
但样本只有一次，不能据此宣称模型稳定。

## 4. 执行失败与根因

### 4.1 实际状态

| 观察项 | 实际值 |
| --- | --- |
| 任务状态 | `failed` |
| 任务阶段 | `tool_failed` |
| 错误码 | `isolation_unverified` |
| 错误信息 | `evidence_id_missing` |
| plan tool | `run_tests` |
| plan `auto_provision_isolation` | **false** |
| request `auto_provision_isolation` | **true** |
| plan `evidence_id` | null |
| plan `image` | null |
| provisioning | 未执行 |
| 容器 | 未启动 |
| `TestResultVerifier` | 未触达 |
| 文件副作用 | 0 |

事件序列：

```text
task_created → task_claimed → planning_started → planning_completed
→ task_planned → tool_started → tool_failed → task_failed
```

### 4.2 根因

任务持久化记录显示：

```text
request.parameters.auto_provision_isolation = true
plan.steps[0].parameters.auto_provision_isolation = false
```

`src/zevorinth/planning_validation.py` 的 `_plan_test()` 调用
`validate_parameters()` 后，把返回的 `normalized` 传给 `build_test_plan()`。
`src/zevorinth/local_test_runner.py` 的 `validate_parameters()` 返回字典只包含：

```text
runner, target, workspace, timeout_seconds, max_output_bytes,
execution_backend, allowed_side_effects, protected_paths
```

它丢掉了 `auto_provision_isolation`，也丢掉了其他可信测试字段（`evidence_id`、
`image`、`config_digest`、`command_digest`、`host_authorization`、
`required_artifacts`、`disk_limit_bytes`、`workspace_budget_bytes`）。

因此：

1. `build_test_plan()` 生成 `auto_provision_isolation=false` 的计划；
2. `_execute_test_tool()` 的 provisioning 条件不成立，跳过 provisioning；
3. `_run_container_test()` 读到 `evidence_id=null`，按 fail-closed 返回
   `isolation_unverified / evidence_id_missing`；
4. 容器没有启动，执行链在 evidence 门禁处结束。

这是 LLM 计划路径的参数透传缺陷，不是模型候选错误，也不是隔离门禁失效。

## 5. 附带的审计完整性问题

失败结果的 `tool_result.tool_name` 记录为 `write_text_artifact`，而实际计划是
`run_tests`。

原因：`TaskService._finish_tool_failure()` 在未传入 `ToolResult` 时使用
`self.writer.tool_name`，默认即写文件工具名。`_execute_test_tool()` 的失败路径
没有传入带 `run_tests` 的 `ToolResult`。

这没有造成执行或安全后果，但会让失败审计看起来像写文件任务，必须在下一次端到端
验证前修正。

## 6. 安全边界

- 真实模型 API 请求：1（授权上限 1）；
- 自动重试：0；
- 未改写模型输出；
- 未回退宿主机；
- 未启动受保护进程；
- `protected_processes_started_without_verified_isolation = 0`；
- 未创建 evidence 文件；
- workspace 文件哈希调用前后完全一致；
- 未读取 `.env`；密钥仅取自进程环境，未打印或落盘；
- 未联网安装依赖、未拉取镜像、未启停 Docker Desktop；
- `.idea/` 未修改、未暂存。

## 7. 已证明与未证明

### 已证明（本次单样本）

- 模型能产出合法的 `run_tests` 候选；
- 目标 `test_ok` 与预批准目标一致；
- 本地严格校验通过；
- 失败时没有宿主机回退；
- evidence 门禁在缺失 `evidence_id` 时 fail-closed。

### 未证明

- provisioning 是否能成功执行七项边界探针；
- evidence 是否能正确持久化并被执行器实时复核；
- 容器是否实际启动；
- 测试退出码是否为 0；
- `TestResultVerifier` 是否能通过；
- 完整链路是否无重复副作用。

## 8. 下一次离线修复建议

在申请新的真实调用前，先做离线修复与回归：

1. 让 `validate_parameters()` 保留并校验可信测试字段，或在 `_plan_test()` 中
   显式合并可信字段；至少包括：
   - `auto_provision_isolation`
   - `evidence_id`
   - `image`
   - `config_digest`
   - `command_digest`
   - `host_authorization`
   - `required_artifacts`
   - `disk_limit_bytes`
   - `workspace_budget_bytes`
2. 增加回归测试：LLM `run_tests` 候选 + `auto_provision_isolation=true` 的计划
   必须保留 true；
3. 修正 `_execute_test_tool()` 失败路径，使 `tool_result.tool_name` 为
   `run_tests`；
4. 离线验证 provisioning、容器执行和 `TestResultVerifier`；
5. 完成后再申请**新的**单次真实调用授权。

不得通过删除 `auto_provision_isolation` 检查、自动注入 evidence_id 或放宽
`isolation_unverified` 来绕过本问题。

## 9. 产物

- 本地结果：`artifacts/audits/phase-3.44/pilot-result.json`
- SHA-256：`90a86696d7efc9271a04ba8e4bb5173410e003628ccdfd5d887ebb3b8f3cc659`
- 本地稳定摘要：`artifacts/audits/phase-3.44/stable-summary.json`
- SHA-256：`f6a157d0041b34293481c72c331c45ef0150f4d7473829f5b530ae6ae39831a3`

`artifacts/` 被 `.gitignore` 排除，**仅本地存在，不随提交分发**。
