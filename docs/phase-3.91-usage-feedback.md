# Phase 3.91 — Usage Feedback & Decision Record

## 结论

本次是短暂的本地使用验证，不是新的验收阶段。

工作台已经能有效承担只读观察入口：任务发现、Attention Inbox、详情、event timeline、
verification、artifact drift 和 recovery preview 都可以从统一界面理解。

使用过程中发现一个值得优先修复的语义问题：

> 尚未执行的 `run_tests` 澄清任务，被 Lifecycle Audit 标记为
> `missing_final_execution_plan` / `audit_ok=false`，尽管任务状态正确显示为
> `awaiting_clarification` 且工作台提示需要用户回答。

这并不影响任务执行安全，但会降低用户对 audit 结果的信任。

本记录不启动 Phase 3.92，也不修改执行、恢复或工作台控制能力。

## 使用范围

Baseline commit：`2a14e59`

使用环境：

- 本地 loopback 工作台；
- 独立临时 runtime root；
- 确定性 Planner；
- 实际临时 artifact 写入；
- 0 次真实模型请求；
- 0 次外部网络请求；
- 0 次真实用户 workspace 写入；
- 0 次容器执行。

创建并观察了 5 个任务：

| 场景 | 数量 |
| --- | ---: |
| succeeded | 2 |
| awaiting clarification | 1 |
| blocked | 1 |
| queued | 1 |

其中成功写入任务中的一个 artifact 被故意修改，用于观察 drift 展示。

## 正常路径

以下体验符合设计：

1. 不预先知道 task ID，也能从任务列表找到任务。
2. Attention Inbox 能定位 blocked 和 clarification 任务。
3. 点击 attention item 可进入正确任务详情。
4. 任务详情能关联目标、状态、phase、error、artifact、verification、events 和 recovery preview。
5. blocked 任务显示 `invalid_artifact_path` 和可读原因。
6. clarification 任务显示 `missing_fields=workspace` 与问题文本。
7. artifact drift 显示 registered/current SHA 不一致，并明确提示当前不能视为 verified。
8. artifact preview 加载前后，文件内容保持不变。
9. recovery preview 没有任何执行按钮。
10. 页面没有出现 POST、重试、恢复或删除入口。

## 摩擦点

### 1. 澄清任务的 Lifecycle Audit 误报失败

证据：

- task status：`awaiting_clarification`
- phase：`awaiting_clarification`
- execution mode：`not_executed`
- missing field：`workspace`
- 工作台主提示：`Needs your answer`
- Lifecycle Audit：`audit_ok=false`
- issue：`missing_final_execution_plan`
- recovery preview：`manual_review`，risk=`high`

影响：高，属于用户语义与审计语义冲突。

推断：`run_tests` 的 final execution plan 只在真正执行阶段产生。当前 auditor 对所有
`run_tests` 任务都要求 snapshot，没有区分“尚未执行、等待澄清”和“已经进入执行但缺少最终计划”。

建议：在进入后台 worker 或通用恢复前，先做一个很小的语义修复，把“尚未执行的前置状态”
与“执行阶段计划缺失”分开。该修复需要独立回归和变异测试，不应与恢复能力扩展捆绑。

### 2. 手动刷新导致视图落后

实测：

- 工作台打开时显示 4 个任务；
- 通过既有本地 Python 工作流创建第 5 个任务；
- 页面仍显示 4 个任务，新增 goal 不存在；
- 点击 Refresh 后显示 5 个任务，succeeded 数从 1 变为 2。

影响：中。当前读写工作流多为同步调用；如果以后出现后台任务或从其他进程提交，手动刷新会明显
降低可观察性。

建议：暂不立即加入自动刷新。先积累真实使用中等待任务完成或跨进程提交的频率；若频繁，
可评估显式 opt-in polling，而不是默认持续轮询。

### 3. 后续处理需要再次转回 CLI 并取得 task ID

实测：

- 任务列表行不显示 task ID；
- task detail 显示完整 task ID；
- clarification detail 没有 answer/resume/cancel 按钮；
- `Needs your answer` 说明只能通过既有 conversation 或 CLI 工作流处理。

影响：中。这是 v1 的有意安全取舍，但用户从“发现问题”到“处理问题”需要手动切换界面并
复制 ID。

建议：只有当真实使用中 attention 后续处理频繁时，再评估只读辅助能力，例如复制 task ID、
展示对应 CLI 命令，或安全 handoff 到现有操作入口。首版不应直接增加写操作按钮。

## 其他观察

- Event payload 摘要显示为 `dict(n)` / `list(n)`，足够支持 timeline 导航，但深入诊断仍需
  `events --json`。这是有意的有界/脱敏取舍，当前不算主要摩擦。
- 缺少自动刷新也是缺少“最后刷新时间”提示；用户能点击 Refresh，但无法从页面判断当前
  视图是否已落后。
- 任务列表只有 status filter。当前 5 个任务足够使用，尚无证据要求新增文本搜索、
  operation filter 或 conversation filter。

## 决策

| 决策 | 结果 |
| --- | --- |
| 立即启动新的宽泛 Phase | 否 |
| 立即扩展通用恢复 | 否 |
| 立即做真实模型评测 | 否 |
| 更新工作台为可写控制面 | 否 |
| 优先处理 clarification audit 语义 | 是，建议作为一次窄范围修复 |
| 继续收集真实日常使用样本 | 是 |

推荐顺序：

1. 修复“未执行澄清任务被标记 final execution plan missing”的审计适用性。
2. 保留工作台只读边界，继续使用短期真实任务样本。
3. 根据后续样本决定是否加入 opt-in refresh 或 task-ID handoff。
4. 只有观察到明确长任务需求后，才进入后台 worker、队列或 checkpoint。
5. 真实模型评测继续单独授权。

## 证据

本地反馈记录：

`artifacts/audits/phase-3.91/usage-feedback.json`

SHA-256：

`b39a90bad034b0b6aef5097f75bb2e69277407bcb89db8d9db4ba0bbe47bc194`

该记录包含本次 5 个任务的投影摘要、浏览器观察结果、artifact drift 证据和三项摩擦点。

## 限制

- 这是一次短时、单用户、临时目录使用验证，不是长期用户研究；
- 样本只有 5 个任务，不能据此排序长期产品需求；
- 没有调用真实模型，因此不包含真实 Planner 失败分布；
- 没有使用真实用户 workspace；
- 本次记录没有实施任何代码修复，也没有开启新阶段。
