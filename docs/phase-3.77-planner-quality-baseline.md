# Zevorinth Phase 3.77：LLM Planner 多样本质量基线

## 1. 结论

**PASS。** 本阶段建立了 planner-only 的多样本质量评测框架和固定数据集。它不启动工具、
不创建任务、不写 workspace、不访问网络，也不调用真实模型。

离线 scripted 结果全部通过只证明：

- 数据集内部一致；
- 独立评分器能识别正确和错误计划；
- capability registry、Policy 契约和报告结构可运行。

它不代表任何真实模型质量。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `9adec311b939dd6632107b2e15240e03e00f91e7` |
| 样本数 | 25 |
| 真实模型 / 网络 / 安装 | 0 / 0 / 0 |
| 工具执行 / 文件副作用 | 0 / 0 |

## 2. 数据集覆盖

`planner_quality_v1.json` 包含 25 个固定场景：

| 维度 | 覆盖 |
| --- | --- |
| 类别 | write、run_tests、clarification、unsupported、path safety、candidate contract、model failure、prompt injection |
| 难度 | easy / medium / hard |
| 风险 | low / medium / high |
| 操作 | `write_text_artifact`、`run_tests` |
| 失败 | refusal、invalid JSON、timeout、invalid target、unknown fields |
| 安全 | shell、浏览器、删除、路径逃逸、绝对路径、`.env`、prompt injection |

数据集包含版本和 SHA-256，报告可复现。

## 3. 独立评分

评分器不直接相信 Planner 的 metadata。它独立检查：

- classification 是否符合固定期望；
- planned operation 是否在 capability registry；
- write/run_tests 是否只生成一个合法步骤；
- artifact path、content、test target 和 trusted workspace 是否与固定参数精确一致，
  包括拒绝缺失值和额外值；
- clarification missing fields 是否与样本完全一致；
- unsupported / failed error code 是否符合期望；
- 本应澄清或不支持的任务是否错误生成了可执行计划；
- unsafe plan 是否被发射。

数据集加载也采用 fail-closed：顶层 JSON 不是对象、cases 不是列表、样本 ID 重复或
缺少必填维度时均直接拒绝，不会进入评分。

离线 scripted 客户端只提供固定候选，不产生真实模型质量结论。

## 4. 指标

当前离线自检：

| 指标 | 结果 |
| --- | --- |
| overall pass rate | 1.0 |
| classification accuracy | 1.0 |
| operation accuracy | 1.0 |
| parameter accuracy | 1.0 |
| clarification missing-field accuracy | 1.0 |
| unsafe plan rate | 0.0 |
| capability registry compliance | 1.0 |
| candidate validation pass rate | 15/22 = 0.6818181818 |
| real API calls | 0 |
| model calls | 25 scripted |

`candidate_validation_pass_rate` 小于 1 是预期的，因为数据集包含故意无效、越权和要求澄清的候选。
另有 3 个样本没有收到候选，因此不进入该比率的分母。

报告还按 difficulty 和 category 分组，并记录每个样本的期望/观察分类、操作、参数、
missing fields、错误码、plan summary 和失败原因。

同一数据集连续运行两次；除记录输出位置的 `report_path` 外，规范化 JSON 逐字节一致，
SHA-256 均为 `f9c5f6efebb8526a29222933f39d04b2b33ab50ef615e20c220f029067c6aad2`。

## 5. 离线与真实模型分离

新增 CLI：

```text
zevorinth eval-planner-quality --offline [--output PATH] [--json]
```

报告固定标记：

```text
mode=offline-scripted-planner-only
model_source=scripted
live_model_quality_measured=false
execution_side_effects_enabled=false
real_api_calls=0
```

本阶段没有 `--online` 选项。未来真实模型运行必须另行授权，并明确：

- provider/model；
- 数据集版本和 SHA-256；
- 最大样本数；
- timeout、max output tokens；
- 最大请求数和零重试；
- 结果路径不得覆盖既有报告；
- 真实结果与脚本自检分开存储。

## 6. 测试与变异

| 检查 | 结果 |
| --- | --- |
| Phase 3.77 planner-quality 矩阵 | 9/9 |
| 系统 Python 全量 unittest | 781/781，exit 0 |
| `.venv` 全量 unittest | 781/781，exit 0 |
| Mutation runner | **170 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 170/170 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异覆盖：classification 恒真、参数恒真、unsafe plan 隐藏、capability registry 绕过、
错误声明真实模型质量、错误声明有任务副作用，以及虚构 real API 调用。

审计产物：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.77/offline-quality-report.json` | `147acd9ba3886630e5ab48867250f4a4e119dfd0d573fe6ef8e9f4eca07adb70` |
| `artifacts/audits/phase-3.77/mutations.json` | `8b7579fca521b87f408eb2af25f842ec3690f9f03ab359a823d1bd76b699aad8` |

## 7. 限制

- scripted 数据不能衡量真实模型输出质量；
- 25 个固定场景不能代表所有用户任务；
- 数据集需要后续版本化扩充；
- 规划器只接受合成目标，未读取用户文件或凭据；
- 没有执行 workspace、容器或外部 API；
- 真实模型质量基线尚未测量。
- 全量测试期间存在既有 SQLite `ResourceWarning`；本阶段没有修改该生命周期代码。

## 8. 阶段判定

**PASS**：Planner 质量评测已经具备固定数据集、独立评分、可复现报告、双向反例测试和
离线/在线硬分离。下一阶段如进行真实模型质量测量，应单独授权并复用该契约。
