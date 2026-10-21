# Phase 3.89 — Exact Text Contract & Parameter Fidelity

## 结论

Phase 3.89 在离线范围内通过。

本阶段把 Planner 写文本分成三个明确模式，并在正式
`LLMPlanner.plan_request()` 路径中加入确定性门禁：

| 模式 | 判定 |
| --- | --- |
| `semantic` | 由候选内容表达任务语义，不施加系统级标点偏好 |
| `format_constrained` | 候选内容必须满足 trusted `content_format` |
| `exact_text` | 候选内容必须与 trusted `expected_text` 的 Unicode code point 序列完全一致 |

未声明 precision 元数据时使用 `semantic`。这是兼容性默认值，不会推断或改写普通
自然语言中的标点。`exact_text` 不执行 trim、Unicode normalization、换行调整或标点
修正。

## 契约

Active contract：`planner-exact-text-v1`

SHA-256：

`cf22a6adb91b03a03830c89c887a962e3d5835b7e7a2baa140e32eae15b8e8ec`

契约固定 `retroactively_applicable=false`，不回溯改变 Phase 3.88 的
applicability v1 分数：

- strict score 仍为 `15/19 = 78.95%`；
- `write_simple` 仍按旧契约记为 strict failure；
- 不会用 Phase 3.89 的语义默认值重写历史结果。

支持的格式为：

- `plain_text`：非空文本；
- `markdown`：第一个非空白 token 是 ATX heading；
- `json_object`：内容解析为 JSON object。

空文件内容仍由既有 write validator 在 text precision 之前拒绝。该阶段没有为精确
比较而放宽“非空内容”这一产品约束。

## 门禁顺序

正式 LLM Planner 路径：

1. 读取并校验 active exact-text contract；
2. 解析 trusted precision 元数据；
3. 契约错误在模型调用前 fail-closed；
4. Planner 返回原始候选；
5. 既有 Schema 与业务 validator；
6. text precision gate；
7. intent fidelity gate。

text precision 失败会返回：

- `text_precision_contract_error`：契约缺失、未知模式或必需值缺失；
- `text_precision_mismatch`：`exact_text` code point 不一致；
- `text_format_mismatch`：声明格式不满足。

该门禁只接受 TaskRequest 中的 trusted 元数据。模型不能通过候选字段自行声明
`precision_mode`、`expected_text` 或 `content_format`。

## 独立维度

核心回归使用一个意图忠实但精确内容错误的计划：

```text
expected_text = right
candidate content = wrong
```

结果为：

| 维度 | 结果 |
| --- | --- |
| Text precision | `exact_mismatch`, gate blocked |
| Intent fidelity | `planned_faithful`, fidelity_pass true |
| Final planning | `failed` |
| Task status | `planning_failed` |
| Plan created | false |
| Event chain | `task_created → task_claimed → planning_started → planning_failed` |
| `task_planned` | absent |
| `tool_started` | absent |
| Artifact created | false |

这证明 text precision 和 intent fidelity 不能被彼此的成功抵消。Schema、Policy、
授权、Execution Plan 校验和独立验证仍是后续独立防线；本阶段没有合并或替代这些检查。

## 回归矩阵

`planner_exact_text_v1.json` 包含 14 个固定离线案例：

| 类别 | 数量 | 结果 |
| --- | ---: | --- |
| exact text | 6 | 6/6 |
| format constrained | 4 | 4/4 |
| semantic | 2 | 2/2 |
| contract error | 2 | 2/2 |
| 合计 | 14 | 14/14 |

覆盖内容：

- 精确匹配；
- 保留原始中文句号；
- 增加中文句号导致 exact mismatch；
- leading/trailing whitespace mismatch；
- composed/decomposed Unicode mismatch；
- trailing newline mismatch；
- `write_simple` 的普通语义写作允许中文化句号；
- valid/invalid JSON object；
- valid/invalid Markdown heading；
- exact 缺少 `expected_text`；
- format 缺少 `content_format`。

专项 runner 没有访问网络或真实模型：

- model call attempts：12（全部为脚本客户端）；
- real API calls：0；
- task submissions：0；
- tool executions：0；
- container executions：0；
- workspace side effects：0；
- plan produced by mismatch/error cases：0。

核心任务链的隔离验证使用临时 TaskService；它只产生 planning rejection，不启动工具。

## 自动化验证

| 项目 | 结果 |
| --- | --- |
| 系统 Python unittest | 859/859 |
| `.venv` unittest | 859/859 |
| Mutation testing | 219 killed / 0 survived / 0 invalid / 0 error |
| Execution unverified | 0 |
| Compileall | PASS |
| `git diff --check` | PASS |

变异覆盖包括：

- contract version drift；
- 允许 text normalization；
- exact whitespace 比较被 trim；
- semantic 模式错误拒绝标点；
- exact mismatch 被接受；
- 无效 format 被接受；
- malformed exact contract 继续进入模型；
- runner 错误宣称发生过 normalization；
- 既有 intent fidelity checker fail-open。

## 历史完整性

以下 Phase 3.88 原始证据和冻结契约全部保持字节级一致：

| 文件 | SHA-256 | 状态 |
| --- | --- | --- |
| `artifacts/evaluations/phase-3.88-quality-live.json` | `6a5c9b2d349c09a807877434988cedc89f169da83caa247d5852aa7d430ac028` | match |
| `artifacts/audits/phase-3.88/stable-summary.json` | `9e4465e32a39104e04f33e92020b4de0ce14d4b8b72ab0d218e2e0aa4c944cd2` | match |
| `src/zevorinth/evals/planner_intent_fidelity_v3.json` | `83663c21266cecd729e9d3fc120d7c98d8c89eb69aa4c77b11617b69c456ae75` | match |
| `src/zevorinth/evals/live_evaluation_metric_contract_v2.json` | `1ec71b8ed77e7559e23f88e0ef9902ddc2c838b88ca2c3f0bde819ffe59325f2` | match |
| `src/zevorinth/evals/planner_precision_v1.json` | `bc9206acc2736ee63ad9dd8a31b088907e2ef28d144656e67a8882c843ad336c` | match |

没有发起第五次 live run，也没有修改 Phase 3.88 报告、正式分数或 SHA-256。

## 本地产物

以下证据位于 `artifacts/audits/phase-3.89/`：

| 文件 | SHA-256 |
| --- | --- |
| `exact-text-regression.json` | `86d65622d86cf9ce44aeac8459e0b98dc5efe956475d0b02a787db3c65a91333` |
| `offline-quality-report.json` | `fd3525b57aabd2efa520c8388bbe1e26e3c9022aabf408e212210b73f94d3310` |
| `core-regression.json` | `cbfc10bde9dabee299390a43d420884bb43dfa54ac261b7c4fb8b0d8ad2f65e2` |
| `phase-3.88-integrity.json` | `15521b58d5cd4d01cdd57ff298ac3292fa3d29e622d81fcbf6a6d91e6a92c6ef` |
| `exact-text-contract.json` | `b4d0292c7bd32605cac6155f59ed0b3a616e290b746a09fadf77e6c15ab75bb0` |
| `mutations.json` | `9c2854a32d6c8aab7e27c373d94c0366bff261569451660778e5f9ee72b1689b` |

这些文件由仓库的 `.gitignore` 作为本地运行产物排除，不随源码提交。

## 限制

- precision mode 只来自 trusted TaskRequest 元数据；不从自然语言关键词推断精确模式；
- 显式引用的 quoted content 仍由独立的 intent fidelity v3 检查；
- exact text 检查不证明目标来源身份或 trusted context 未被篡改；
- 本阶段只适用于 planned `write_text_artifact`；
- 本阶段不新增执行权限、工具、恢复路径、后台重试或 cleanup；
- 没有测量真实模型在新契约下的表现；
- 14 个 case 是合成离线契约，不等于真实用户任务分布。
