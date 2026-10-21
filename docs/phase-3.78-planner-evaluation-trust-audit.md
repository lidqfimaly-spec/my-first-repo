# Zevorinth Phase 3.78：Planner 评测可信度与真实基线协议

## 1. 结论

**PASS（离线评测可信度审查）。**

本阶段没有调用真实模型，也没有新增任务执行能力。它修正了 Phase 3.77 中一个容易
误导的指标，建立了可复核的样本覆盖审计、评分独立性审计和真实模型授权协议。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `373f263f0ed5a33174cf5acf464e583026edf83e` |
| 数据集 | `planner-quality-v2`，25 个固定场景 |
| 真实模型 / 网络 / 安装 | 0 / 0 / 0 |
| 任务提交 / 工具 / 容器 | 0 / 0 / 0 |
| 真实模型质量 | 未测量 |

## 2. 修正后的安全指标

Phase 3.77 的 `unsafe_plan_rate` 同时混合了任务风险和执行计划接受情况，解释力不足。
Phase 3.78 使用数据集顶层的 `dangerous_candidate_ids` 固定 6 个危险候选案例，并分别
记录：

| 指标 | 结果 | 含义 |
| --- | --- | --- |
| dangerous candidate cases | 6 | 固定标注的危险候选场景 |
| proposed | 6/6 = 1.0 | scripted 原始候选要求生成计划 |
| identified | 6/6 = 1.0 | 解析与本地校验没有把危险候选接受为 planned |
| accepted | 0/6 = 0.0 | 没有危险候选在规划层被接受 |

这里的 `accepted=0` 只表示**规划器没有接受危险计划**，不表示已经执行，也不证明真实
模型不会提出危险计划。scripted 数据本身故意提出了全部 6 个危险候选，因此这些数字
验证的是评分器和本地拒绝边界，不是模型安全质量。

其他指标保持独立：

| 指标 | 结果 |
| --- | --- |
| overall contract pass rate | 1.0 |
| planned contract accuracy | 1.0 |
| clarification decision accuracy | 1.0 |
| clarification fields accuracy | 1.0 |
| refusal decision accuracy | 1.0 |
| candidate contract decision accuracy | 1.0 |
| candidate validation pass rate | 15/22 = 0.6818181818 |
| nonconforming executable plan rate | 0.0 |

`candidate validation pass rate` 不是任务成功率。数据集故意包含 7 个无效候选，另有
3 个样本没有收到候选，因此其分母是 22，而不是 25。

## 3. 样本代表性审计

报告新增 `coverage_audit`，实际覆盖为：

| 维度 | 覆盖 |
| --- | --- |
| 样本来源 | synthetic fixed contract |
| 数量 | 25 |
| 类别 | 8 |
| 难度 | easy 6 / medium 11 / hard 8 |
| 风险 | low 11 / medium 3 / high 11 |
| expected classification | clarification 5 / failed 7 / planned 6 / unsupported 7 |
| 操作 | `write_text_artifact`、`run_tests` |
| unsafe task cases | 10 |
| dangerous candidate cases | 6 |

当前没有未覆盖的已支持任务操作，但“覆盖所有当前操作”不等于“代表真实用户任务总体”。
报告明确标记：

```text
sample_origin=synthetic_fixed_contract
real_user_task_population_sampled=false
```

已知缺口包括：没有真实模型样本、没有真实用户任务分布、没有执行结果验证、没有重复
采样方差或置信区间，也没有 provider、延迟和成本变化。

## 4. 评分独立性

`scoring_audit` 记录：

```text
expected_values_source=versioned_fixed_dataset
planner_metadata_used_for_plan_pass_fail=false
planner_reported_pass_used=false
raw_candidate_observed_before_local_validation=true
dangerous_candidate_label_source=fixed_dataset_annotation
candidate_validation_reported_separately=true
task_execution_used=false
```

关键边界：

- 期望分类、操作、参数、错误码和危险候选标注来自固定数据集；
- Planner 自报 metadata 只用于候选接收、校验状态、Token 和延迟等诊断；
- 计划通过与否由独立字段比较决定；
- 危险候选标签不根据当前 Planner 输出反推；
- 评分器只调用 Planner，不提交任务，不运行工具或容器；
- 数据集拒绝重复 JSON 键、未知危险候选 ID、重复案例 ID 和无效顶层结构。

## 5. 真实模型基线协议

离线报告现在携带 `planner-live-baseline-protocol-v1`。它是协议清单，不是在线执行器：

```text
protocol_status=authorization_required
live_runner_implemented=false
live_requests_enabled=false
current_real_api_calls=0
```

硬限制：

- 最多 25 个案例、25 次请求；
- 0 次重试；
- 每例 timeout 30 秒；
- 每例最多 1500 output tokens；
- task submissions、tool executions、container executions 均为 0；
- 输出路径必须不存在，且与离线报告分开；
- 必须显式提供 provider、精确模型、案例集合、预算和停止条件。

调用前还必须确认外部 API 成本；报告中不得包含凭据。任何需要重试、任务提交、工具执行、
覆盖旧证据或扩大授权范围的情况都必须停止。

## 6. 验证结果

| 检查 | 结果 |
| --- | --- |
| Planner quality tests | 13/13 |
| Capability registry tests | 9/9 |
| 定向评测相关测试 | 52/52 |
| 系统 Python 全量 unittest | 786/786，exit 0 |
| `.venv` 全量 unittest | 786/786，exit 0 |
| Mutation runner | **174 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 174/174 in-process，0 unverified |
| 离线报告规范化重复 | 逐字节一致 |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

数据集 SHA-256：

```text
1b26439ac9340d06250ed06b2900dec65ed0065f1a5004f6350aadacef8a71e0
```

规范化离线报告 SHA-256：

```text
6156fba16ab56e2d2ec664c0d514ba08f706edfcd74a851dcd2f547899cb8e4b
```

## 7. 限制

- 本阶段只证明离线评测器的解释力和边界，不测量真实 LLM 质量；
- 6 个危险候选是固定合成样本，不代表真实模型的危险候选提出率；
- 25 个样本没有统计显著性，也没有重复采样不确定性；
- `live_baseline_protocol` 是尚未启用的协议，不是已实现或已批准的在线评测；
- L2/L3 来源认证、跨 OS 语义和同权限攻击防护仍未建立；
- 全量测试仍有既有 SQLite `ResourceWarning`，本阶段未扩大范围修改。

## 8. 阶段判定

**PASS（限于离线可信度审查）。**

Phase 3.78 已把容易混淆的指标拆开，并完成了真实模型调用前的协议约束。当前仍保持
零真实模型调用；下一步只有在用户单独批准 provider、模型、预算、样本和输出范围后，
才能实现并执行真实 Planner 基线。
