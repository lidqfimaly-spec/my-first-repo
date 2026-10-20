# Zevorinth Phase 3.38：LLM Planner 的 run_tests 能力审计

## 1. 结论与基线

**BLOCKED（能力缺口，不是缺陷）**：`run_tests` 在策略、执行器、CLI 层都已注册，但**不在 LLM Planner 的候选 schema 与校验器中**——模型无法选择该工具。按任务书分类，属于 **B：已注册，但未进入模型候选**。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `3c35c42f2977c0a0312a8d8b2c32a61ab8df5209` |
| 分支 | `phase-2.6-response-hardening` |
| 真实模型 API 请求 | **0**（本阶段不发起） |

## 2. 工具候选 schema 的实际内容

`src/zevorinth/llm_planner.py:62` `CANDIDATE_JSON_SCHEMA`：

```python
"operation": {"anyOf": [{"type": "string", "enum": ["write_text_artifact"]}, {"type": "null"}]}
```

`DEEPSEEK_CANDIDATE_JSON_SCHEMA = deepcopy(CANDIDATE_JSON_SCHEMA)`（只放宽 `required`），因此两个 provider 都只允许 `write_text_artifact`。

`additionalProperties: false`，且字段只有 `classification` / `operation` / `artifact_path` / `content` / `missing_fields` / `clarification_questions` / `reason`。**没有** `target`、`workspace`、`evidence_id`、`image`、`execution_backend` 等字段。

## 3. 解析与规范化链

| 位置 | 行为 |
| --- | --- |
| `planning_validation.PlanningCandidateValidator.validate` | `operation="run_tests"` → `UNSUPPORTED` / `unsupported_operation`，`plan=None` |
| 同上 | 额外字段（如 `evidence_id`/`task_id`/`execution_id`）→ `FAILED` / `unknown_candidate_fields` |
| `llm_planner.LLMPlanner.plan_request` | 把校验结果映射为 `PlanningOutcome`；`unsupported` 时任务进入 BLOCKED |

实测（离线）：

```
baseline -> planned None
run_tests -> unsupported unsupported_operation
extra(evidence_id) -> failed unknown_candidate_fields
```

## 4. 已实现 vs 缺口

| 能力 | 状态 | 依据 |
| --- | --- | --- |
| `run_tests` 工具注册 | PASS | `policy.SUPPORTED_OPERATIONS`、`planner.build_test_plan`、`service._execute_test_tool`、CLI `test` |
| 确定性 Planner 支持 run_tests | PASS | `DeterministicPlanner.plan_request` 返回计划（测试 `test_deterministic_planner_supports_run_tests`） |
| **LLM 候选 schema 支持 run_tests** | **NOT IMPLEMENTED** | schema enum 仅 `write_text_artifact` |
| LLM 校验器接受 run_tests | **NOT IMPLEMENTED** | `unsupported_operation` |
| 模型自带 evidence/task 身份 | **FAIL（受控）** | 未知字段被拒 |
| 证据实时重验 / 容器门禁 | PASS（不依赖模型） | 3.27–3.37 |

## 5. 安全门禁检查

| 检查项 | 结论 |
| --- | --- |
| task_id / request_id 来源 | 由 `TaskService` 生成；模型无法提供（未知字段被拒） |
| evidence_id | 由调用方/provisioning 提供；模型不可见 |
| config_digest / command_digest | 执行器从**即将执行的命令**派生，不采信调用方 |
| required_artifacts / 超时 / 输出上限 | 经 policy 与 planner 透传；计划字段已逐项复制 |
| provisioning 触发 | 当前由调用方显式调用（`verify-isolation` / `provision_evidence`），**未由模型触发** |
| 证据异常时 | 进程启动前失败关闭（BLOCKED/FAILED），无宿主机回退 |
| 容器失败回退 host | 无（容器分支只失败，不切换） |
| 独立结果验证 | `TestResultVerifier` 强制，模型无法绕过 |

## 6. 新增离线测试

`tests/test_llm_run_tests_capability.py`（7 项，全部 mock/纯函数，无网络）：

1. 两个 schema 的 operation enum 都只有 `write_text_artifact`；
2. schema 不含任何测试执行字段；
3. schema 是封闭的（`additionalProperties: false`）；
4. 合法写入候选 → `planned`；
5. `run_tests` 候选 → `unsupported` / `unsupported_operation`，无 plan；
6. 模型自带 `evidence_id`/`task_id`/`execution_id` → `failed` / `unknown_candidate_fields`；
7. 确定性 Planner 仍支持 `run_tests`（证明缺口只在 LLM 侧）。

## 7. 回归

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest | 544/544，50.5s | 0 |
| 能力审计专项 | 7/7 | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

## 8. 下一步建议

要让模型选择 `run_tests`，需要**协议级**改动（不是小补丁）：

1. 扩展候选 schema：新增 `operation: run_tests` 与 `target`/`workspace` 字段；
2. 校验器接受并验证这些字段（复用 `validate_parameters`）；
3. 明确 `evidence_id` 仍不能由模型提供（由编排层 provisioning 后注入）；
4. 更新 `LLMPlanner.plan_request` 的映射与离线评测案例。

在此之前，**不适合**做"模型驱动容器执行"的真实调用验证——模型无法表达该意图。

## 9. 残余风险

- 若后续扩展 schema，需重新审视"模型可否影响 workspace 选择"（当前 workspace 是固定枚举更安全）。
- 本阶段仅静态/单元证据，未做真实模型调用（按任务书要求）。
- Docker 镜像 tag 曾出现一次 `inspect` 解析失败（`docker run` 正常），已通过重新 tag 修复；不影响本阶段结论。
