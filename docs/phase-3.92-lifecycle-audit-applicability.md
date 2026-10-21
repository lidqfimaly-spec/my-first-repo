# Phase 3.92 — Lifecycle Audit Stage Applicability

## 结论

Phase 3.92 定向维护通过。

本阶段只修正 Lifecycle Audit 对 `run_tests` final execution plan 的阶段适用性，并同步修正
合法 clarification 状态在 recovery preview 中的风险表达。没有新增 action、恢复执行路径、
工作台写操作或新的持久化状态。

Auditor semantic version 从 `lifecycle-audit-v1` 升级为：

```text
lifecycle-audit-v2
```

## 问题

旧规则：

```text
if run_tests and execution_plan_snapshot is None:
    missing_final_execution_plan
```

该规则没有区分：

- 尚未进入执行的 `queued` / `awaiting_clarification` / 执行前 `blocked`；
- 已经进入执行，按契约必须拥有 final execution plan 的任务。

结果是合法 clarification 任务得到 `audit_ok=false`，并进一步在 recovery preview 中显示
high-risk `manual_review`。

## 修复后的适用条件

只有在以下任一条件成立时，缺失 `execution_plan_snapshot` 才产生
`missing_final_execution_plan`：

- task status 为 `verifying` 或 `succeeded`；
- 存在 `execution_plan_finalized` 事件；
- 存在 `tool_started` 事件；
- tool result 已持久化 test evidence。

这体现了实际执行顺序：

```text
planning
  -> execution-plan preparation
  -> execution_plan_finalized + snapshot persistence
  -> tool_started
  -> tool result / verification
```

执行前状态不会仅因尚未生成 final execution plan 被判失败。

## Clarification 语义

`awaiting_clarification` 的新结果：

| 维度 | 结果 |
| --- | --- |
| Task status | `awaiting_clarification` |
| Audit version | `lifecycle-audit-v2` |
| `audit_ok` | true |
| Audit issues | empty |
| Manual intervention | true |
| Preview action | `none` |
| Preview current state | `awaiting_clarification` |
| Preview risk | low |
| Requires approval | false |

`manual_intervention=true` 保留，因为用户仍需要回答澄清问题。它不再被表达为
high-risk recovery。`audit_ok=true` 的含义保持为：

> 当前生命周期阶段适用的审计要求均已满足。

它不是“任务已经执行成功”，也不是“未来不需要用户输入”。

## 反向保护

修复没有放宽执行阶段要求。

专项故障场景：

```text
task status = verifying
event = execution_plan_finalized
snapshot = missing
```

结果：

```text
audit_ok=false
issue=missing_final_execution_plan
```

因此，执行已经开始但缺少必要 final plan 的任务仍然 fail-closed。

## 状态矩阵

| 场景 | Final plan 要求 | 结果 |
| --- | --- | --- |
| queued `run_tests` | 否 | audit ok |
| awaiting clarification | 否 | audit ok，preview low-risk none |
| blocked before execution | 否 | audit ok；recovery 仍按 blocked 状态要求人工查看 |
| verifying with plan-finalized event and no snapshot | 是 | audit failed |
| succeeded `run_tests` | 是 | 继续要求 plan、test evidence 和 verification |
| failed before execution | 依据现有证据，不机械要求 plan | 现有 failed 语义不变 |

## Workbench

工作台无需修改代码。它继续只投影新的 audit 与 preview 结果。

浏览器实测：

- clarification detail 显示 `Audit L1: open`；
- 不再显示 `Audit failed closed: missing_final_execution_plan`；
- recovery preview 显示 `none / LOW RISK`；
- 操作说明为通过现有 conversation 或 CLI 提供澄清；
- 未出现 resume、retry 或 recovery 执行按钮。

## 自动化验证

| 项目 | 结果 |
| --- | --- |
| 系统 Python unittest | 882/882 |
| `.venv` unittest | 882/882 |
| Mutation testing | 235 killed / 0 survived / 0 invalid / 0 error |
| Execution unverified | 0 |
| Compileall | PASS |
| `git diff --check` | PASS |

新增变异：

- clarification 重新要求 final execution plan；
- 执行阶段缺失计划被放行；
- clarification preview 被提升为高风险；
- audit semantic version 回退。

## Phase 3.91 完整性

| 文件 | SHA-256 | 状态 |
| --- | --- | --- |
| `artifacts/audits/phase-3.91/stable-summary.json` | `0360e53253b82ae4a60979b9c4e71e612c54967cb7b01f5425c49fcb0879b89d` | match |
| `artifacts/audits/phase-3.91/workbench-matrix.json` | `86514ff0ec5963f72f153ac2549110e1c112b2a94d6f795767dfd0bed3bfa65e` | match |

Phase 3.91 baseline commit：`f3564ea`。

## 隔离

- 真实模型请求：0；
- 外部网络请求：0；
- 依赖安装：0；
- 真实 workspace 写入：0；
- 容器执行：0；
- 工作台 HTTP 写操作：0。

## 本地产物

| 文件 | SHA-256 |
| --- | --- |
| `lifecycle-applicability.json` | `1f9e4777b4adcc6cfb80370e03539166af150f938ba8fc9f2cc8edb346468927` |
| `phase-3.91-integrity.json` | `cbc4403fe40ecee9918ec380f05ba02b04be9f2996c383fc255e3893e21b4b77` |
| `mutations.json` | `82a5d4e777a70047e9d828c0d9370a165187044040a59b8a857af26e4ccd71d5` |

## 限制

- 修复只处理 final execution plan 的阶段适用性；
- recovery preview 仍会为 running、verifying、interrupted、blocked 等状态保留独立人工风险；
- `audit_ok=true` 仍不表示来源认证、执行成功或未来无需用户输入；
- 本阶段没有扩展后台 worker、队列、polling 或通用恢复；
- 没有真实模型质量证据。
