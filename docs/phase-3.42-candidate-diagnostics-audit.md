# Zevorinth Phase 3.42：LLM 候选诊断与供应商字段兼容性

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 已确认 `type` 是模型候选 JSON 的顶层字段，而不是 SDK
响应包装字段；但其值和语义没有持久化，仍无法确定。基于证据不足，本阶段**没有**
移除或规范化 `type`，保留 `unknown_candidate_fields` 的 fail-closed 行为，只增加了
安全、受限的候选形状诊断。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `87799e98ccdb4c73bcf39c421a9cb61d523c433c` |
| 起始工作区 | 仅 `?? .idea/` |
| 真实模型 API 请求 | **0** |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见阶段最终报告。

## 2. `type` 来源调查

### 2.1 代码路径

`src/zevorinth/response_normalization.py` 的 `normalize_response_object()`：

- 从 `response.output` 中提取 `message` 的 `output_text`；
- 仅把 `output_item_types`、`content_part_types` 作为响应诊断保存；
- 对文本执行 `json.loads(text)`，结果直接赋给 `ModelCallResult.candidate`；
- **没有**把 API 包装层的 `type` 合并进候选对象。

`src/zevorinth/planning_validation.py` 的 `PlanningCandidateValidator.validate()`：

- 对 `candidate` 计算 `set(candidate) - CANDIDATE_FIELDS`；
- 3.41 后的真实调用记录为 `candidate_unknown_fields: ["type"]`。

因此可以确定：`type` 是模型输出 JSON 的**顶层候选键**，不是 SDK 响应包装层的
元数据。

### 2.2 证据等级

| 问题 | 结论 | 证据等级 |
| --- | --- | --- |
| `type` 是否来自候选 JSON | 是，顶层键 | 代码路径可证 |
| 是否来自 API 包装层 | 否 | 代码路径可证 |
| `type` 的实际值 | 未保存，无法确认 | 未确定 |
| `type` 的语义 | 未确定；可能是类型判别符，也可能是模型自造字段 | 未确定 |
| 是否 DeepSeek 特有 | 无跨供应商证据，不能断言 | 未确定 |
| 仅靠本地记录能否判断 | 不能判断值和语义，只能判断来源层级 | 已确认 |

3.42 的真实调用元数据只有 `candidate_unknown_fields=["type"]`、token 数和响应状态；
原始 `output_text` / 候选对象没有被持久化。因此不能把“模型可能想返回 plan 类型”
写成事实。

## 3. 是否规范化 `type`

**本阶段不实施规范化。** 原因：

1. 没有证据表明 `type` 是供应商适配层的已知元数据；
2. 删除未知字段会放宽通用封闭契约，可能掩盖真正的协议错误；
3. 仅凭一次调用无法区分“DeepSeek 兼容字段”和“模型自造业务字段”；
4. 任务书明确要求来源不明时保持拒绝行为。

因此 `unknown_candidate_fields` 仍是失败路径，候选不会被自动改写为
`write_text_artifact` 或 `run_tests`。

## 4. 安全诊断摘要

在 `PlanningCandidateValidator` 的失败路径中增加以下元数据：

| 字段 | 内容 | 限制 |
| --- | --- | --- |
| `candidate_fields` | 候选顶层字段名 | 最多 20 个；不匹配安全模式的名字替换为 `<redacted>` |
| `candidate_field_count` | 顶层字段数量 | 整数；生成失败时为 -1 |
| `candidate_classification` | `plan` / `clarification` / `unsupported` / `invalid` | 只记录安全枚举或固定哨兵 |
| `candidate_operation` | `write_text_artifact` / `run_tests` / `invalid` | 只记录安全枚举或固定哨兵；缺失或非字符串时省略 |

约束：

- 不保存完整候选文本、任意字段值、文件内容或凭据；
- 不把模型内容当作日志格式字符串；
- 字段名排序并限长，异常时降级为 `["<unavailable>"]`；
- 摘要只附加在失败/拒绝路径；成功的 `write_text_artifact` 与 `run_tests` 元数据
  保持不变；
- 摘要生成失败不会改变校验结论，候选仍按原规则拒绝。

## 5. 离线测试

新增 `tests/test_candidate_diagnostics.py`，共 12 项：

- 未知 `type` 被拒绝，同时记录 `candidate_fields`、`candidate_operation`；
- 未知字段值、`content` 值不会进入诊断；
- 合法写文件候选行为与成功元数据不变；
- 合法 `run_tests` 候选行为不变；
- 缺少 `operation`、非法 `operation` 类型、非对象 JSON 均确定性拒绝；
- 字段名数量受限且不安全名称被替换；
- 伪造 `evidence_id` 仍被拒绝；
- `unsupported` 不会被转换；
- `planning_failed` 事件载荷包含安全形状摘要。

执行结果：

| 检查 | 结果 |
| --- | --- |
| 新增诊断专项 | 12/12 通过 |
| Planner / run_tests / 证据 / 执行器 / 验证器专项 | 118/118 通过 |
| 系统 Python 全量 unittest（单独运行） | 571/571 通过，48.9s，exit 0 |
| `.venv` 全量 unittest（单独运行） | 571/571 通过，48.9s，exit 0 |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

### 并行全量运行的已知抖动

两套全量测试**同时**运行时，各出现 1 个相同失败：

`test_container_timeout_cancel.ContainerTimeoutCancelTests.test_cancel_during_container_run_stops_it`

失败断言检查全局 `docker ps --filter name=zv-`，会把另一套测试正在运行的容器误判为
残留。该测试单独运行时通过（1/1，1.36s），两套全量测试单独运行时也各自通过。

这属于测试隔离限制，不是产品执行路径回归；按要求**没有修改断言**。

## 6. 变异测试

现有 mutation runner：60 个案例，单独运行。

| killed | survived | invalid | error | 状态 |
| --- | --- | --- | --- | --- |
| 60 | 0 | 0 | 0 | PASS |

## 7. 未验证事项与残余风险

1. `type` 的实际值和语义仍未确定；
2. 没有跨供应商样本，不能判断 `type` 是否 DeepSeek 特有；
3. 诊断摘要只保留字段名与安全枚举，仍不足以复现完整候选；
4. 并行全量测试受全局 Docker 名称过滤影响，存在测试级抖动；
5. 未验证下一次真实调用是否会返回不带 `type` 的候选；
6. 未触达 provisioning、容器执行或 `TestResultVerifier`（本阶段无真实调用）。

## 8. 安全边界

- 真实模型 API 请求：0；
- 未读取 `.env` 或输出密钥；
- 未访问互联网、未安装依赖；
- 未启动或关闭 Docker Desktop；
- 未修改、暂存或跟踪 `.idea/`；
- 未执行 reset / clean / rebase / amend / 分支切换；
- 未放宽 policy、隔离证据或独立验证门禁。

## 9. 审计产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.42/stable-summary.json` | `b580673423bbe1278253cc282ac06b3abf80e1c768a2ea3a7ec5ff73cf60c37b` |
| `artifacts/audits/phase-3.42/mutations.json` | `f4777c7dde4ecc0a9a1887df480b17a216ce576d6c56092863969ff3679c2335` |

## 10. 阶段判定

**PASS WITH LIMITATIONS**：诊断能力已完成并通过离线验证，`type` 的来源层级已确认，
但其值和语义仍未确定，因此没有实施规范化。下一次真实调用前，应先决定如何安全地
保留足够的候选结构元数据；即使下一次返回合法候选，也必须分别验证 provisioning、
容器执行和独立验证，不能把规划成功当作整条闭环成功。
