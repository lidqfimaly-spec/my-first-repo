# Zevorinth Phase 3.87：Planner Precision & Unsupported-Intent Handling

## 1. 结论

**PASS（离线精确性、Schema 与混合目标回归）。**

本阶段针对 Phase 3.86 暴露的真实失败，建立了独立 precision 回归集，并将 exact quoted
content 接入 active intent-fidelity 门禁。没有调用真实模型、联网或执行任务。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `de716b92c12f90985243bf99b134c82249303fb8` |
| 真实模型 / 网络 | 0 / 0 |
| Precision cases | 16 |
| Active intent contract | `planner-intent-fidelity-v3` |
| Active metric contract | `live-evaluation-metric-contract-v2` |

## 2. 精确参数契约

Intent Fidelity v3 新增：

```text
planned_quoted_content_must_match_exactly=true
```

如果 goal 明确给出带引号内容，例如：

```text
创建 exact.txt，内容严格为“Hello from Zevorinth”。
```

计划中的 content 必须逐字等于 `Hello from Zevorinth`。添加句号、空格或其他字符都会产生：

```text
status=lossy_plan
error=intent_fidelity_failed
plan=null
```

不会静默删除标点、修正文本或把 mismatch 升级为成功。

## 3. Schema 强制

Precision 回归覆盖：

- 缺少 `classification`；
- classification 类型错误；
- 使用 `type` 替代 `classification`；
- 非法 classification 枚举；
- 未知字段；
- operation 类型错误；
- 空 content；
- 缺失 content。

结果全部 fail-closed：

```text
schema_case_count=6
invalid_schema_plan_count=0
```

格式错误不会被宽松解析或自动修复为 executable plan。

## 4. 混合目标

覆盖：

- 安全 test target + shell 删除目标；
- 文件创建 + 未授权 shell 拆分；
- 正确 clarification；
- conditional fallback 安全分支；
- 文件名改变；
- 不支持操作替换。

结果：

```text
lossy_plan_bypass_count=0
unsupported_intent_plan_count=0
```

## 5. Precision 回归结果

| 指标 | 结果 |
| --- | --- |
| 固定案例 | 16 |
| 通过 | 16/16 |
| 无效 Schema 被接受为计划 | 0 |
| Lossy plan 绕过门禁 | 0 |
| 未授权拆分进入计划 | 0 |
| 执行尝试 | 0 |
| 真实 API calls | 0 |

Precision dataset SHA-256：

```text
bc9206acc2736ee63ad9dd8a31b088907e2ef28d144656e67a8882c843ad336c
```

## 6. 版本化与历史保护

历史契约保持原样：

- `planner-intent-fidelity-v1`；
- `planner-intent-fidelity-v2`；
- `live-evaluation-metric-contract-v1`；
- Phase 3.86 的 `14/19` 与原始 live report。

本阶段新增：

```text
planner-intent-fidelity-v3
sha256=83663c21266cecd729e9d3fc120d7c98d8c89eb69aa4c77b11617b69c456ae75

live-evaluation-metric-contract-v2
```

Metric v2 绑定 applicability v1 和 intent v3，原有指标分母与禁止 composite score 规则不变。

## 7. 执行路径不变量

Precision runner 只调用 `LLMPlanner.plan_request()`：

- 不创建 TaskService；
- 不提交任务；
- 不产生 tool event；
- 不启动容器；
- 不修改 workspace。

对于 schema error、lossy plan、拒绝与澄清，测试均要求 `plan_created=false` 或
`must_not_plan` 保持成立。

## 8. 验证结果

| 检查 | 结果 |
| --- | --- |
| Precision regression | 16/16 |
| Metric contract tests | 8/8 |
| Intent enforcement tests | 10/10 |
| 系统 Python 全量 unittest | 846/846，exit 0 |
| `.venv` 全量 unittest | 846/846，exit 0 |
| Mutation runner | **211 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 211/211 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

审计产物：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.87/precision-regression.json` | `e8dfb03f8f62f452632861f7c1238572e96a86502e746c76424aa0e9aaa4a80a` |
| `artifacts/audits/phase-3.87/offline-quality-report.json` | `de7d01de7605408684144c51b708379c6f9af0292d519e72fe001b221e0111f6` |
| `artifacts/audits/phase-3.87/mutations.json` | `bd79ba62119d06a2e3609e33a17fa26a86a3f1d0875a8bd23a0f849684a3b6a1` |

Precision dataset SHA-256：

```text
bc9206acc2736ee63ad9dd8a31b088907e2ef28d144656e67a8882c843ad336c
```

## 9. 限制

- 精确 quoted content 只覆盖 goal 中明确引用的文本，不推断未引用内容的字符级意图；
- 自然语言语义仍不是通用 entailment；
- Schema 拒绝依赖本地 validator 和 structured-output 合同；
- 未执行新的真实模型评测；
- Planner-only 结果不代表实际任务副作用成功率。

## 10. 阶段判定

**PASS：精确参数、Schema 失败与混合不支持目标已经形成独立回归和正式门禁。**

接下来可以冻结实现并选择一次新的真实模型评测，验证 quoted-content precision、
schema compliance 与 unsupported-intent handling 是否改善；不应继续无限增加同类离线阶段。
