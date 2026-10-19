# 第 1 阶段最小可验证闭环实现说明

日期：2026-10-19

## 1. 实际范围

第 1 阶段实现了一个结构化文本产物任务：

```text
TaskRequest
  -> TaskService
  -> deterministic-v1 Planner
  -> ArtifactPolicy
  -> write_text_artifact
  -> ArtifactVerifier
  -> SQLite EventStore
  -> CLI/JSON 报告
```

不包含 LLM、自由文本规划、任意 shell、网络访问、多 Agent 或动态代码执行。

## 2. 数据契约

| 类型 | 关键字段 |
| --- | --- |
| `TaskRequest` | goal、operation、parameters |
| `Task` | task_id、status、execution_mode、request、plan、artifact、tool_result、verification、error、missing_fields、attempt_id、phase、时间 |
| `Plan` | plan_id、planner_name、deterministic、单步 PlanStep |
| `ToolResult` | attempt_id、tool_name、execution_mode、success、时间、artifact、error |
| `VerificationResult` | exists、path_safe、content_matches、evidence_matches、reason、artifact、checked_at |
| `Event` | event_id、task_id、event_type、occurred_at、payload、sequence |
| `EventStore` | 创建任务、原子状态转换、追加事件、查询任务和事件 |

任务状态和执行模式是两个独立字段。

## 3. 状态与不变式

合法转换集中在 `state_machine.py`。关键不变式：

- `succeeded` 只能由 `verifying` 且验证通过进入。
- 真实工具成功但验证失败时进入 `failed`。
- `blocked` 表示未调用工具，执行模式必须是 `not_executed`。
- `simulated` 表示模拟完成，执行模式为 `simulated`，不得携带真实产物。
- 终态不能被 `resume` 重启。
- 工具结果和验证结果分别保存，验证器不读取工具的 `success` 作为通过依据。

## 4. Planner 与 Policy

`deterministic-v1`：

- 只接受 `write_text_artifact`；
- 恰好生成一个步骤；
- 期望结果包含产物名、UTF-8 字节数和内容 SHA-256；
- 不调用模型。

Policy 在执行前检查：

- 必要字段；
- 支持的操作和工具；
- 单步有界计划；
- 安全单文件名；
- UTF-8 内容和 64 KiB 上限；
- 任务目录是否逃逸；
- 是否已有目标文件或符号链接。

## 5. 真实工具

`RealArtifactWriter` 的操作顺序：

1. 验证文件名与内容；
2. 创建或检查任务专属目录；
3. 以拒绝跟随符号链接的方式打开目录；
4. 在目录内创建临时文件；
5. 写入 UTF-8 字节并 `fsync`；
6. 使用硬链接以“不覆盖”语义提交目标文件；
7. 删除临时文件；
8. 重新打开目标文件，计算字节数和 SHA-256。

工具只报告从实际文件读回的证据。

`SimulatedArtifactWriter` 不打开文件、不创建目录、不生成 artifact evidence。

## 6. 独立 Verifier

`artifact-readback-v1`：

- 重新打开任务目录和目标文件；
- 拒绝非常规文件与符号链接；
- 读取完整 UTF-8 字节；
- 与请求内容比较；
- 重新计算 SHA-256 和字节数；
- 与工具证据比较；
- 明确返回 missing、read denied、content mismatch、evidence mismatch 或 verified。

模拟结果传入 Verifier 时固定失败为 `verification_requires_real_tool_execution`。

## 7. SQLite EventStore

数据库包含：

- `tasks`：当前任务快照，JSON 作为完整状态载荷；
- `events`：追加式有序事件；
- `metadata`：schema 版本。

每次状态转换在 `BEGIN IMMEDIATE` 事务中：

1. 重新读取数据库当前任务；
2. 校验合法转换；
3. 更新任务快照；
4. 插入事件；
5. 一次提交。

失败时回滚，不留下单独的状态变化或单独的事件。

## 8. 中断恢复

测试使用的是“确定性进程重启模拟”：

- 真实工具已写文件并保存证据；
- 任务已进入 `verifying`；
- 故障钩子在 Verifier 执行前抛出异常；
- 新 `TaskService` 实例在同一数据库上把 `verifying` 识别为 `interrupted`；
- `resume` 走重新验证路径，不调用写入工具；
- 修改过的文件不会被覆盖，而会明确失败。

测试还使用独立子进程：真实写入并提交 `verifying` 后直接 `os._exit(23)`，父进程由新运行时识别和恢复。

未执行真实 OS 强杀、断电或文件系统损坏测试，因此不能声称已经证明所有进程崩溃场景。

## 9. CLI

保留 `python -m zevorinth` 和安装后的 `zevorinth` 入口。

| 命令 | 作用 |
| --- | --- |
| `doctor` | 环境与能力自检 |
| `run` | 提交结构化任务 |
| `status` | 查询持久化任务 |
| `events` | 查询有序事件 |
| `resume` | 继续澄清、修正阻断或恢复中断任务 |

`--home` 指定运行根目录；数据和产物始终写入 `<home>/data`、`<home>/artifacts`。

## 10. 当前限制

- 只支持一种任务和一种工具；
- 无 LLM；
- Phase 1 原始实现为单进程；Phase 1.5 已增加同机 SQLite 执行租约，见 `phase-1.5-audit.md` 与 `phase-1.5-results.md`；
- 没有 OS 级沙箱；
- 没有真实崩溃恢复证明；
- 没有并发任务压力测试；
- 没有外部框架或数据库依赖。

> Phase 1.5 更新：已增加真实 SIGKILL 测试、同任务多进程竞争测试和 SQLite 锁失败测试。上面的“没有真实崩溃恢复证明”与“没有并发任务压力测试”仅描述 Phase 1 原始提交 `752cae3`，已被后续阶段覆盖。

> Phase 2 更新：新增可选 LLM Planner。模型只生成候选，最终仍通过本文件定义的 TaskService、Policy、Tool、Verifier 和 EventStore。详见 `phase-2-llm-planner.md`。
