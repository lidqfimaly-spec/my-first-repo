# Zevorinth Phase 3.73：事件序列完整性与审计盲区

## 1. 结论与范围

**PASS。** Phase 3.73 没有新增 action、自动重试、自动清理、签名或外部审计服务。
本阶段只扩展只读生命周期审计，回答“现存事件彼此一致时，历史是否仍然完整，因果是否
可绑定到当前 attempt”。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `def796c18d4a50d14ecd0c1c7de998153bbbaaf5` |
| 新增 action | **0** |
| 真实恢复 / 清理 / 重试 | **0** |
| 真实模型 / 网络 / 安装 | 0 / 0 / 0 |

## 2. 必需事件矩阵

成功真实任务要求：

```text
task_created
→ planning_started
→ planning_completed
→ task_planned
→ [execution_plan_finalized for run_tests]
→ tool_started
→ tool_succeeded
→ verification_started
→ verification_completed
→ task_succeeded
```

模拟任务要求到 `simulation_completed`；失败和取消任务至少要求
`task_created + task_failed/task_cancelled`。

当前 schema 成功任务缺少必需事件时：

- `event_completeness.state=incomplete`；
- issue 为 `missing_required_lifecycle_event`；
- assurance 为 L0；
- CLI 返回非零退出码。

## 3. Attempt 因果绑定

验证事件现在为未来记录持久化稳定的 `attempt_id`：

- `verification_started`；
- `verification_completed`；
- generic write 和 run_tests 路径；
- persisted evidence resume 路径。

审计检查：

- `tool_started` 必须包含当前 `tool_result.attempt_id`；
- verification 事件若包含 attempt ID，必须等于当前 attempt；
- 旧 attempt 的事件不能被新 attempt 采用；
- attempt 完全缺失时标记 `unknown_historical`，而不是断言篡改；
- 明确存在其他 attempt ID 时标记 `mismatch`。

## 4. 完整性与因果状态

| 字段 | 值 | 含义 |
| --- | --- | --- |
| completeness | `complete` | 当前 schema 必需事件齐全 |
| completeness | `minimal_complete` | 失败/取消路径的最小必需链齐全 |
| completeness | `incomplete` | 当前记录确认缺少必需事件 |
| completeness | `unknown_historical` | 缺项且无 attempt 绑定，无法区分旧版本与损坏 |
| completeness | `open` | 非终态任务 |
| causality | `verified` | tool 和 verification 均绑定当前 attempt |
| causality | `tool_verified` | tool 绑定当前 attempt；无需验证或尚未进入验证 |
| causality | `unknown_historical` | 缺少 attempt 绑定 |
| causality | `mismatch` | 明确发现旧 attempt 或冲突 ID |

`unknown_historical` 仍然 fail-closed：`ok=false`、assurance L0、要求人工复核。

## 5. 因果顺序

审计会拒绝：

- `task_succeeded` 早于 `verification_completed`；
- `verification_completed` 早于 `verification_started`；
- tool terminal 早于 `tool_started`；
- `tool_result` 没有匹配尝试的事件；
- verification 结果没有匹配完成事件；
- duplicate tool/verification terminal。

## 6. CLI 可见性

`task-audit` JSON 包含完整：

```text
event_completeness.state
event_completeness.required_event_types
event_completeness.missing_event_types
event_completeness.causality
```

human 输出包含 `Event completeness: <state> causality=<state>`。
`status` 的 lifecycle 摘要也显示 completeness。

## 7. 测试与变异

| 检查 | 结果 |
| --- | --- |
| Phase 3.73 专项矩阵 | 9/9 |
| 系统 Python 全量 unittest | 751/751，exit 0 |
| `.venv` 全量 unittest | 751/751，exit 0 |
| Mutation runner | **148 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 148/148 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

## 8. 审计盲区

即使所有现存事件一致，仍然不能证明：

- 某条历史事件一定真实发生过；
- 旧 attempt 和所有摘要没有被同权限主体一起重写；
- 文件系统、运行时或内核没有篡改事件顺序；
- `unknown_historical` 的具体原因；
- 未持久化的历史行为。

因此：

- `complete + verified` 只表示当前记录覆盖完整且内部因果一致；
- 它不升级为 L2/L3；
- 它不构成来源认证；
- 无法证明的历史保持 L0 和人工复核。

## 9. 边界与限制

- 不新增 action 或恢复入口；
- 不自动重跑未知副作用任务；
- 不联网、不安装依赖、不读取 `.env`；
- 不修改 `.idea/`；
- 当前只在 macOS/POSIX 环境下验证；
- 事件序列仍是权威顺序，其历史真实性依赖 L1 的内部一致性边界。

## 10. 阶段判定

**PASS**：任务终态所需事件、工具/验证因果关系、当前 attempt 绑定和历史缺项均获得
明确、保守、可解释的判定；无法证明的历史状态不会被误报为完整或可信成功。
