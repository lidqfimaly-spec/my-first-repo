# Zevorinth Phase 3.41 修复后单次真实调用验证

## 1. 结论

**BLOCKED（候选契约未通过），不是安全失败。** 在 Phase 3.41 的 instructions 修复后，
唯一一次授权调用不再返回 `unsupported`，而是产生了一个结构化候选；该候选包含
契约之外的 `type` 字段，被本地校验器以 `unknown_candidate_fields` 拒绝。

没有重试、没有改写模型输出、没有 provisioning、没有启动容器、没有文件副作用。
由于原始候选未被持久化，**无法证明**模型这次具体选择了哪个 operation；不能把
“不再 unsupported”描述为“模型已能选择 `run_tests`”。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `3959bdce989e563e014cc13eef7a4aa7b4f5e175` |
| Provider / 模型 | DeepSeek / `deepseek-flash` |
| 授权请求上限 | **1** |
| 实际请求 | **1** |
| 自动重试 | **0** |
| 结果 | `planning_failed` / `unknown_candidate_fields` |

## 2. 执行设置

- 隔离临时根目录：`/var/folders/.../T/zv-phase342-bygx2d6s`
- 预置 workspace：`demo`，包含 `test_ok.py` 与 `inside.txt`
- 任务目标：`Run the unit test module test_ok from the demo workspace.`
- 可信任务参数：`workspace=demo`、`auto_provision_isolation=true`
- 候选 schema：DeepSeek 变体；模型只能提供 `test_target` 等受限字段
- 客户端 `max_retries=0`，`max_calls_per_plan=1`

## 3. 实际调用链

```text
TaskService.submit()
  → LLMPlanner.plan_request()
  → DeepSeek 调用 1 次（2.19 秒）
  → candidate_received=true
  → PlanningCandidateValidator.validate()
  → 拒绝：unknown_candidate_fields=["type"]
  → TaskStatus.PLANNING_FAILED
```

事件序列：`task_created → task_claimed → planning_started → planning_failed`。

## 4. 观察结果

| 观察项 | 实际值 |
| --- | --- |
| `model_call_attempted` | `true` |
| `candidate_received` | `true` |
| `candidate_validation` | `rejected` |
| 拒绝字段 | `type` |
| 本地错误码 | `unknown_candidate_fields` |
| 任务状态 | `planning_failed` |
| `plan_tool` | `null` |
| `tool_result` | `null` |
| `verification` | `null` |
| 证据文件 | 0 |
| 容器启动 | 0 |
| 运行中的 `zv-` 容器 | 0 |
| `test_ok.py` / `inside.txt` 哈希 | 调用前后完全一致 |

模型请求统计：输入 741 tokens，输出 171 tokens，总计 912 tokens；响应状态
`completed`，HTTP 层无错误。

## 5. 与 Phase 3.40 的差异

| 项目 | 3.40（修复前） | 本次（修复后） |
| --- | --- | --- |
| 模型分类 | `unsupported` | 产生结构化候选 |
| 失败阶段 | 模型行为门禁 | 本地候选契约 |
| 是否到达 provisioning | 否 | 否 |
| 是否启动容器 | 否 | 否 |
| 文件副作用 | 0 | 0 |

这说明 instructions 修复至少改变了模型的输出形态，但**没有**证明它选择了正确
operation，也没有证明执行链可用。

## 6. 安全边界

- 真实模型 API 请求：1（授权上限 1）；
- 自动重试：0；
- 未改写模型输出；
- 未回退宿主机执行；
- 未启动受保护进程；
- `protected_processes_started_without_verified_isolation = 0`；
- 未读取 `.env`；密钥仅取自进程环境，未打印或落盘；
- 未联网安装依赖、未拉取镜像、未启停 Docker Desktop；
- `.idea/` 未修改、未暂存。

## 7. 可诊断性缺口

本次失败暴露出一个真实缺口：`planning_failed` 事件只保存了未知字段名和 token
统计，没有保存候选的安全摘要（例如 operation、classification、字段名集合）。
因此无法在调用后确认模型是否选择了 `run_tests`，也无法离线复现该候选。

这不是授权绕过或执行风险，但会显著降低后续审计质量。

## 8. 未验证事项

1. 模型是否真的选择了 `run_tests`；
2. provisioning 与容器执行链在模型驱动下是否可用；
3. `TestResultVerifier` 是否能在真实模型任务中通过；
4. DeepSeek 是否稳定附加 `type` 字段，还是偶发行为；
5. 其他目标措辞下的模型行为。

## 9. 建议的下一步（离线优先）

1. 在不放宽安全门禁的前提下，为 `planning_failed` 增加**安全的候选形状摘要**：
   仅记录字段名、operation/classification 等非敏感值，不记录完整原始文本；
2. 评估是否对 DeepSeek 的 `type` 字段做显式、可测试的规范化，而不是放宽
   `unknown_candidate_fields` 的一般规则；
3. 完成离线测试后，再决定是否申请新的一次调用额度。

在完成上述离线工作前，不应再次消耗模型调用。

## 10. 产物

- 本地结果：`artifacts/audits/phase-3.42/pilot-result.json`
- SHA-256：`5587a4183f9aa26e473eb1181ac65a1d84599bc5fcb48b54cd920add234d97fd`
- `artifacts/` 被 `.gitignore` 排除，**仅本地存在，不随提交分发**。
