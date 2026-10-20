# Zevorinth Phase 3.72：任务状态、事件与审计证据语义一致性

## 1. 结论与范围

**PASS。** Phase 3.72 没有新增恢复 action、自动重试、自动清理、签名或外部服务。
本阶段验证并加固任务状态、事件、工具结果、验证结果和审计结论之间的语义一致性。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `038790dc7640c3a6ed30958ccd4be1e2ec46e271` |
| 新增 action | **0** |
| 真实恢复 / 清理 / 重试 | **0** |
| 真实模型 / 网络 / 安装 | 0 / 0 / 0 |

## 2. 新增生命周期约束

`audit_task_lifecycle()` 现在还检查：

- `task.tool_result` 必须有匹配 attempt ID 的 `tool_succeeded` 或
  `tool_failed` 事件；
- tool result 的成功标志必须与终态事件类型和 payload 一致；
- `task.verification` 必须有匹配 verifier、passed 和 reason 的
  `verification_completed` 事件；
- 无 `tool_result` 时不得存在工具终态事件；
- 无 `task.verification` 时不得存在验证完成事件；
- 重复 tool terminal 和重复 verification terminal 必须报告；
- `tool_started` 必须早于 tool terminal；
- `verification_started` 必须早于 `verification_completed`。

原有约束继续保留：终态状态与终态事件、成功与独立验证、`run_tests` 退出码 0、
最终执行计划和 evidence identity。

## 3. Preview 与任务审计一致

只要 lifecycle audit 返回 `ok=false`：

- recovery preview 必须生成 `manual_review`；
- `current_state=lifecycle_inconsistent`；
- assurance level 为 L0；
- 所有 issue code 进入 `unknown`；
- `execution_authorized=false`；
- `safe_to_retry=false`。

测试确认同一持久化任务的生命周期审计与 recovery preview 内嵌 audit 具有相同
issues 和 assurance level。

## 4. CLI 失败关闭

`status` 现在同时执行只读 lifecycle audit：

- JSON 输出包含 `lifecycle_audit`；
- human 输出显示 assurance、authenticity 和 security certification；
- audit 不一致时退出码为 5，即使持久化 task status 字段仍为 `succeeded`。

新增只读命令：

```text
zevorinth task-audit <task-id> [--checked-at ISO] [--json]
```

它不修改任务、事件或 workspace；`ok=false` 时返回退出码 5。

## 5. 工具成功与验证边界

工具结果为成功不能绕过独立验证：

- `succeeded` task 仍要求 verification passed；
- `run_tests` 仍要求证据、退出码、测试结论和最终计划一致；
- tool terminal 事件缺失或与持久化结果不匹配时，任务审计为 L0；
- 验证事件缺失或与持久化 verification 不匹配时，任务审计为 L0；
- CLI 不因 task status 为 `succeeded` 而返回成功退出码。

## 6. 状态矩阵

| 场景 | Audit | Preview | CLI exit |
| --- | --- | --- | --- |
| 正常完整事件链 | L1 / ok | 保持只读 action | 原任务退出码 |
| 缺 tool terminal | L0 / issue | manual_review | 5 |
| 缺 verification terminal | L0 / issue | manual_review | 5 |
| tool result 与事件成功标志冲突 | L0 / issue | manual_review | 5 |
| verification 字段与完成事件冲突 | L0 / issue | manual_review | 5 |
| 重复 tool/verification terminal | L0 / issue | manual_review | 5 |
| 持久化 succeeded 但 audit 失败 | L0 / issue | manual_review | 5 |

## 7. 测试与验证

| 检查 | 结果 |
| --- | --- |
| Phase 3.72 专项矩阵 | 7/7 |
| 系统 Python 全量 unittest | 742/742，exit 0 |
| `.venv` 全量 unittest | 742/742，exit 0 |
| Mutation runner | **141 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 141/141 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

全量套件还包含现有的事件写入失败、进程中断、证据缺失/损坏、历史任务、恢复边界和
清理故障测试。

## 8. 边界与限制

- L2/L3 仍未实现；
- A2 同 UID 全量重写和 A3 运行时/内核控制仍未防御；
- 未新增恢复 action；
- 未自动恢复、清理或重试；
- 未调用真实模型、未联网、未安装依赖、未读取 `.env`；
- `.idea/` 保持未跟踪且未修改；
- 当前仍只在 macOS/POSIX 环境下验证。

## 9. 阶段判定

**PASS**：工具结果、验证结果、终态事件、任务状态、生命周期审计、recovery preview
和 CLI 输出已在关键语义边界上保持一致；矛盾记录不会继续以成功退出码或无人复核
的恢复 preview 呈现。
