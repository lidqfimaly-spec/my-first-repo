# P0-2 代码编辑质量基线：独立验收报告

- 验收对象：`src/zevorinth/code_edit_baseline.py`（`code-edit-evaluator-v2`）、数据集 `code-edit-baseline-v1.1`、提示词 `code-edit-baseline-prompt-v1`
- 验收日期：2026-11-22
- 验收方式：只读复算 + `/tmp` 副本变异。**未修改仓库任何项目文件**（两个被 `.gitignore` 忽略的 `.DS_Store` 被系统刷新了 mtime，见 §1 末段）、未执行 git、未联网、未调用 `--live`。
- 结论：**`INDEPENDENT_ACCEPTANCE_FAIL`**（六项中 2 项 FAIL，见 §0 与 §9）

> 本报告只写我自己复算出来的数字。凡与实施方报告不一致处，在 §8 单列。
> 所有复算/变异脚本在 `/tmp/zv-review/`，重跑命令见 §11。

---

## 0. 判定总表

| # | 检查项 | 判定 | 一句话依据 |
| --- | --- | --- | --- |
| 1 | 实验身份完整性 | **FAIL** | g/h/i 在 §2.1 罗列的 11 项中有 9 项齐全，但记录未钉住被实际执行的产品校验器/应用器，也无 VCS 修订；实测改 `proposal_candidates.py` 使 **5/9 项指标变化而 `evaluator_sha256` 纹丝不动**（§2.3） |
| 2 | 真实执行路径 | **PASS** | 候选确实经 `validate_proposal_candidate`（L705）与 `apply_text_edit`（L721）落地；全模块仅 3 处写文件，均为夹具构造，无第二条候选落盘路径（§3） |
| 3 | 评测器冻结 | **FAIL** | (a) 身份可精确复算 PASS；(b) 九项指标口径闭合、辅助字段不闭合；(c) 31 个符号级变异无一改动九项指标，但自指实验 D2 一击即穿；(d) **改写 `evaluator_identity` 后全套 33 项测试通过**，且 8/7=114.3% 的指标与摘要 `74ac…` 并存（§4） |
| 4 | 正反样本覆盖 | **PASS** | 5 类 × 3 变体 = 15 格全满；3 例“应改”/12 例“不应改”；5 类各含 1 例对抗；删除唯一条 `accept` 对照探针后 `load_dataset` 必报 `dataset_missing_control_probe`（§5） |
| 5 | 九项指标独立复算 | **PASS** | 10 份记录逐项复算全部 MATCH；`G = {modified ∧ expected_kind≠edit ∧ violations=∅}` 在 10 份记录中恒为空，严格值与报告值一致；但 G **可被 3 行候选直接走热**（§6） |
| 6 | 追溯与同条件复跑 | **PASS** | g/h/i 身份字段、九项指标、逐用例动作/判定/门探针完全一致；差异仅 run_id、时间、耗时、延迟、`notes`（§7） |

**总分：`INDEPENDENT_ACCEPTANCE_FAIL`。** 阻止 PASS 的项：#1、#3。

---

## 1. 只读声明与验收方法

**只读证据**：验收开始（00:15）后仓库内新增/改动文件数为 0。

```bash
cd /Users/lidequan/PythonProject/Zevorinth
find . -newermt "2026-11-22 00:15" -not -path "./.git/*" -not -path "./.venv/*" -type f
# -> ./.DS_Store
#    ./evals/.DS_Store
#    ./docs/p0-2-independent-acceptance.md   （本报告，唯一允许的写入）
```

如实记录一处**非我主动写入**的副作用：两个 `.DS_Store`（macOS Finder 元数据，已被 `.gitignore` 第 1 行忽略）在 00:22 被系统刷新了 mtime。我没有对它们执行任何写操作，但它们确实被触碰；除这三个文件外，全仓库没有任何项目文件（`*.py` / `*.json` / `*.md`）发生变化。若按“仓库内任何文件都不得变化”的字面口径，这属于一次环境噪声导致的轻微越界，特此声明，不隐瞒。

**方法**：

1. 身份复算脚本 `/tmp/zv-review/recompute_identity.py` **不 import 被验收模块**，直接用 `ast.parse` 切片 + `ast.literal_eval` 取源码，自己实现摘要约定。
2. 指标复算脚本 `/tmp/zv-review/recompute_metrics.py` **不 import** `compute_metrics` 或任何指标函数，只读记录里的逐样本字段。
3. 所有变异在 `/tmp/zv-review/{pristine,work/M*,d1,d2,x1}` 的 `src/`+`scripts/` 副本上做，跑完即弃。
4. 测试套件只在副本里跑（7 个测试类、33 个用例、36 个子测试），避免在仓库写入 `.pytest_cache`。

**副本基线**（`pristine`，`--offline`）：9 项指标 7/7、3/3、4/7、0/7、2/3、2/3、2/3、5/5、1/1，`failures={model_over_eager_modification: 4}`，摘要 `74ac4584…`；测试 33 passed / 36 subtests passed。

---

## 2. 第 1 项：实验身份完整性 — **FAIL**

### 2.1 正式记录 g / h / i（三份逐字段核对）

| 身份项 | g | h | i-acceptance | 证据字段 |
| --- | --- | --- | --- | --- |
| 数据集版本 + 完整摘要 | 有 | 有 | 有 | `dataset.version=code-edit-baseline-v1.1`，`dataset.sha256=835f2bb0…193e`，`freeze.dataset` |
| 提示词版本 + 摘要 | 有 | 有 | 有 | `prompt_version=code-edit-baseline-prompt-v1`，`prompt_sha256=e32a89bc…1897`，`freeze.prompt` |
| 评测器版本 + 摘要 | 有 | 有 | 有 | `evaluator_version=code-edit-evaluator-v2`，`evaluator_sha256=74ac4584…f794`，`evaluator_coverage` 21 项，`freeze.evaluator.covered_bytes=40008` |
| 配置模型 | 有 | 有 | 有 | `producer.configured_model=deepseek-chat` |
| 响应实际返回模型 | 有 | 有 | 有 | `producer.returned_models=["deepseek-flash"]`，逐例 `reply_model` |
| 请求参数 | 有 | 有 | 有 | `temperature=0.0`、`max_tokens=1200`、`json_mode=true`、`base_url_host=api.deepseek.com`；**缺** `timeout_seconds`、重试策略 |
| run_id / 时间 | 有 | 有 | 有 | 32 位 hex + `started_at` + `duration_seconds` |
| 原始响应 | 部分 | 部分 | 部分 | 逐例 `raw_reply` 保存了模型输出正文；**provider 报文未保存**：`ModelReply.raw`（`id`/`model`/`finish_reason`）与 `usage` 在 `run_baseline` 里被丢弃，10 份记录全文 grep `finish_reason|usage` 命中 0 |
| 指标结果 | 有 | 有 | 有 | 9 项 `numerator/denominator/ratio` |
| **被执行的校验器/应用器身份** | **无** | **无** | **无** | 记录里没有任何 `proposal_candidates` / `workspace_edit` 的版本或摘要 |
| **VCS 修订** | **无** | **无** | **无** | 无 commit / tree hash 字段 |

结论：**11 项里 8 项完整、1 项（原始响应）只有正文、2 项（被执行的校验器/应用器身份、VCS 修订）完全缺失**；因此更强的要求「别人能判定在评什么」不成立——真正决定“接受/拒绝/改动”的产品组件完全在身份之外（§2.3 有实测）。

### 2.2 早期记录的差异

| 记录 | run_id | prompt_sha256 | evaluator_version | evaluator_sha256 / coverage / freeze | overlap | 指标字段数 |
| --- | --- | --- | --- | --- | --- | --- |
| a | 无 | 无 | 无 | 无 | 无 | 7（无 `incorrect`、无 `wrong_premise`） |
| b | 无 | 无 | 无 | 无 | 无 | 7 |
| c | 无 | 无 | 无 | 无 | 无 | 8（无 `incorrect`） |
| d | 无 | 无 | 无 | 无 | 无 | 8 |
| e | 有 | 有 | `code-edit-evaluator-v1` | 无 | 有 | 9 |
| f | 有 | 有 | `code-edit-evaluator-v1` | 无 | 有 | 9 |
| g / h / i | 有 | 有 | `…-v2` | 有 | 有 | 9 |
| `live-deepseek-chat.json` | 无 | 无 | 无 | 无 | 无 | 7（数据集为 v1，`039a1e01…`） |

a–d 连 `run_id` 都没有，只能靠 `started_at` 与 `notes` 区分；a/b 记录里根本没有 `wrong_premise_recognition` 字段，a–d 没有 `incorrect_modification`——实施方报告表里的 a–d 数值是从 `cases` 反推的（我的复算与之一致，见 §6），但**记录本身不携带这些指标**。

### 2.3 决定性缺口：身份未覆盖“被执行的路径”

在 `/tmp/zv-review/x1` 中，只给产品校验器 `validate_proposal_candidate` 加一行强制拒绝（`src/zevorinth/proposal_candidates.py`，不改评测器任何字节），`--offline` 结果：

| 指标 | 基线 | x1 变异 | 变化 |
| --- | --- | --- | --- |
| candidate_acceptance | 7/7 | **0/7** | 是 |
| correct_modification | 3/3 | **0/3** | 是 |
| unrelated_modification | 4/7 | **0/0 → N/A** | 是 |
| incorrect_modification | 0/7 | **0/0 → N/A** | 是 |
| gate_control_acceptance | 1/1 | **0/1** | 是 |
| dangerous / insufficient / wrong_premise / precision_gate | 2/3、2/3、2/3、5/5 | 不变 | 否 |
| `evaluator_sha256` | `74ac…f794` | **`74ac…f794`** | 否 |

即：**9 项指标里 5 项变化，实验身份一分未动**，且不会 fail-closed（`failures` 里出现 `gate_false_reject: 1`，但那是事后归因，不是身份）。这不是对抗场景才有的问题：产品侧一次正常重构（收紧/放宽校验器）就会造成同样的“同身份、不同数字”。没有 VCS 修订字段，第三方也无法判定两次运行是否跑在同一份产品代码上。

**判定：FAIL。** 缺的是：产品校验器/应用器的版本+摘要、VCS 修订、provider 报文（至少 `id`/`model`/`finish_reason`/`usage`）。

---

## 3. 第 2 项：真实执行路径 — **PASS**

**导入**（`code_edit_baseline.py:41-52`）：`from .proposal_candidates import (PROPOSAL_SCHEMA_VERSION, ProposalCandidateError, ProposalSessionIssuer, proposal_context_sha256, validate_proposal_candidate)`；`from .workspace_edit import (EDIT_AUTHORIZATION_TOKEN, WorkspaceEditError, apply_text_edit)`。是产品模块，不是替代实现。

**调用点与上下文**：

| 位置 | 行号 | 上下文 |
| --- | --- | --- |
| `run_case` → 校验 | 704-713（调用在 705） | 先用 `_candidate_body` 把模型回复转成产品的闭集 body，再由 `ProposalSessionIssuer` 签发一次性会话，`validate_proposal_candidate(project_root, envelope, expected_kind="code", provider=..., session=..., issuer=..., context=...)` |
| `run_case` → 落地 | 720-731（调用在 721） | 只把 `validated.body["edit"]` 加 `authorization` 后交给 `apply_text_edit(project_root, artifacts_root, str(uuid.uuid4()), ...)`；`result.accepted = True` 在调用返回之后（L738） |
| `run_probe` → 校验 | 901-910（调用在 902） | 门探针走同一条校验器 |
| `run_probe` → 落地 | 918-927（调用在 919） | 同上 |

**绕过检查**：全模块写文件只有三处——`materialize`（L439，写数据集夹具）、`run_probe`（L870，写探针夹具；L878，`mutate_before_apply` 模拟“候选生成后文件被别人改动”）。没有任何一处把候选内容直接写盘；被拒绝的候选路径只调用 `_score(..., applied=None)`（L675/681/690/718/736），不产生副作用。

**必须说清的一点**：`allowed_paths` 在 `src/zevorinth/` 内除评测器外 **grep 命中 0 次**——产品的 `apply_text_edit` 不做范围校验，越界判定是评测器事后在 `_score`（L760-763）用 `changed_outside_scope` 完成的。因此指标里的“accepted”含义是“产品校验器+应用器接受了”，**不等于“产品会授权这次修改”**。这是设计边界，不是造假：离线集里 `dangerous_weaken_tests_normal` 确实改写了 `test_calc.py` 并被记为越界（4/7 无关修改率由此而来）。

**判定：PASS**（附带上述语义边界）。

---

## 4. 第 3 项：评测器冻结 — **FAIL**

### 4.1 (a) 独立复算身份 — **PASS**

方法（`/tmp/zv-review/recompute_identity.py`，不 import 被验收模块）：

1. 读 `src/zevorinth/code_edit_baseline.py` 原文，`ast.parse` 枚举模块级 `FunctionDef/ClassDef/Assign`。
2. 函数/类：取 `[首个 decorator 或 def 行 … end_lineno]` 的原始行（保留行尾换行）——即 `inspect.getsource` 的等价物，但由我自己切片。
3. 常量：从源码里 `ast.literal_eval` 取值后 `json.dumps(value, sort_keys=True, ensure_ascii=False)`（这是被验收代码对 `Mapping` 的约定，我从 `_symbol_source` 读出后自己实现，未调用它）。
4. 摘要：`sha256( name + "\x00" + source_utf8 + "\x00" ... )`，顺序取记录里的 `evaluator_coverage`。

结果（g/h/i 三份相同）：

```
record evaluator_sha  : 74ac4584257199967a5c7a0fc13a34ebdeb540de0e193595a94a77433d4ef794
recomputed sha256     : 74ac4584257199967a5c7a0fc13a34ebdeb540de0e193595a94a77433d4ef794   MATCH
record covered_bytes  : 40008
recomputed bytes      : 40008   MATCH
21/21 symbols extracted (19 functions/classes + METRIC_LABELS + METRIC_DEFINITIONS)
```

逐符号字节数（可用于核对 `covered_bytes` 的加总）：

`load_dataset 2000`、`_validate_cases 6536`、`_validate_probes 1947`、`materialize 527`、`file_digests 324`、`snapshot 360`、`build_messages 541`、`parse_candidate 1198`、`_candidate_body 906`、`run_case 3704`、`_score 2935`、`_run_unittest 818`、`run_probe 2711`、`_ratio 301`、`compute_metrics 4404`、`_category_summary 302`、`run_baseline 4987`、`_guarded_probe 513`、`ChatCompletionsClient 3592`、`METRIC_LABELS 481`、`METRIC_DEFINITIONS 921`（合计 40008）。

另一条旁证：模块文件 mtime 为 `2026-11-21 23:58:53`，早于 g 的 `23:58:58`、h 的 `23:59:14`、i 的 `00:14:40`，且当前文件能复现该摘要——说明记录对应的就是当前源码（至少这 21 个符号）。

### 4.2 (b) 覆盖闭合性 — 九项指标闭合，辅助字段不闭合

静态统计：模块级符号 **59** 个，摘要覆盖 **21** 个（40008 / 58845 字节 = **68.0%**），未覆盖 **38** 个；从 `compute_metrics` / `run_baseline` / `run_case` / `load_dataset` 出发可达的未覆盖符号 **32** 个。

其中直接从 `compute_metrics` 可达的 5 个：`CATEGORIES`、`CaseResult`、`GATE_FALSE_ALLOW`、`GATE_FALSE_REJECT`、`ProbeResult`。
只在 `run_case` 路径上的：`MODEL_OVER_EAGER`、`MODEL_UNDER_EAGER`、`MODEL_BAD_PATCH`、`MODEL_WRONG_RESULT`、`MODEL_MISCLASSIFIED_RESPONSE`、`ENVIRONMENT_ERROR`、`BaselineError`、`ModelReply`、`_CandidateProvider`。
只在 `run_baseline` 上的：`HARNESS_ERROR`、`EVALUATOR_VERSION`、`EVALUATOR_IDENTITY_SYMBOLS`、`CandidateClient`、`Dataset`、`_host_only`、`SYSTEM_PROMPT`、`freeze_manifest`、`evaluator_identity`、`_symbol_source`、`_identity_digest`。
只在 `load_dataset` 上的：`DATASET_PATH`、`PROMPT_VERSION`、`SUPPORTED_SCHEMA`、`VARIANTS`、`EXPECTED_KINDS`、`ACCEPTABLE_ACTIONS`、`VERIFICATION_KINDS`。

结论分两层：

- **九项指标口径闭合**：31 个符号级变异（M1–M31，含已覆盖符号与未覆盖符号）**没有一个改动九项指标中的任意一项**（§4.3 表）；`CaseResult`/`ProbeResult` 的字段由已覆盖的 `_score`/`run_probe` 赋值，`CATEGORIES` 改动会 fail-closed。
- **辅助上报字段不闭合**：`CaseResult.passed`（M1）可把 `per_category` 的通过数从 11/15 改成 15/15；`ProbeResult.correct`（M2）可让一个 `gate_false_allow` 从 `failures` 里彻底消失；`MODEL_*`/`GATE_*` 常量（M3-M9、M26-M27）可改归因键名。**这些都不改九项指标，但都改报告第 2、3 节呈现的数字**，且摘要不动。

### 4.3 (c) 变异实证

判据（按任务定义）：**指标变了而摘要没变 = 真实漏洞**；fail-closed、或对指标无数值影响、或被另一条守卫接住 = 可接受残余。

每次变异都是一处最小替换，然后跑 `--offline`（脚本 `/tmp/zv-review/mutations.py`，结果 `/tmp/zv-review/mutation_results.json`）。基线摘要 `74ac4584…`。

| # | 符号 | 变异 | 九项指标 | 摘要变化 | 其他可观测变化 |
| --- | --- | --- | --- | --- | --- |
| M1 | `CaseResult` | `passed` 恒真 | 不变 | 否 | `per_category` 通过数 11/15 → 15/15 |
| M2 | `ProbeResult` | `correct` 恒真 | 不变 | 否 | 离线无门错误→看不出；合成错误探针下 `failures` 由 `{gate_false_allow:1}` 变 `{}` |
| M3 | `GATE_FALSE_ALLOW` | 键改名 | 不变 | 否 | 同上场景下键名变 `gate_false_allow_RENAMED` |
| M4 | `GATE_FALSE_REJECT` | 键改名 | 不变 | 否 | 离线未触发 |
| M5 | `MODEL_OVER_EAGER` | 键改名 | 不变 | 否 | `failures` 键名变 |
| M6 | `MODEL_BAD_PATCH` | 键改名 | 不变 | 否 | 离线未触发 |
| M7 | `MODEL_MISCLASSIFIED_RESPONSE` | 键改名 | 不变 | 否 | 离线未触发 |
| M8 | `ENVIRONMENT_ERROR` | 键改名 | 不变 | 否 | 离线未触发 |
| M9 | `HARNESS_ERROR` | 键改名 | 不变 | 否 | 离线未触发 |
| M10 | `CATEGORIES` | 删/改一个必选类别名 | **fail-closed** | — | `BaselineError: fix_guard_normal: unknown category` |
| M11 | `EVALUATOR_VERSION` | 改 `v2`→`v9` | 不变 | 否 | 记录写 `v9` 但摘要仍是 `74ac…`（版本号与摘要脱钩） |
| M12 | `EVALUATOR_IDENTITY_SYMBOLS` | 名单加 `_pct` | 不变 | **是**（`30e82bc7…`） | — |
| M13 | `SYSTEM_PROMPT` | 末尾加一个空格 | 不变 | 否 | `prompt_sha256` 变（`7cde3d99…`），被提示词冻结兜住 |
| M14 | `_host_only` | 恒返回 `None` | 不变 | 否 | 离线无 client，本就不记录 host |
| M15 | `_CandidateProvider` | name 改 v1→v2 | 不变 | 否 | — |
| M16 | `Dataset` | 字段加默认值 | 不变 | 否 | — |
| M17 | `ModelReply` | 加一个未用字段 | 不变 | 否 | — |
| M18 | `BaselineError` | 异常消息加 `!` | 不变 | 否 | — |
| M19 | `VERIFICATION_KINDS` | 词表加一项 | 不变 | 否 | — |
| M20 | `ACCEPTABLE_ACTIONS` | 词表加一项 | 不变 | 否 | — |
| M21 | `DATASET_PATH` | 指向不存在文件 | **fail-closed** | — | `dataset could not be read: FileNotFoundError` |
| M22 | `PROMPT_VERSION` | 改字符串 | 不变 | 否 | 数据集自带 `prompt_version`，仅作 fallback |
| M23 | `SUPPORTED_SCHEMA` | 改字符串 | **fail-closed** | — | `dataset schema is not the supported one` |
| M24 | `VARIANTS` | 词表加一项 | **fail-closed** | — | `correct_fix is missing variants: extra` |
| M25 | `EXPECTED_KINDS` | 词表加一项 | 不变 | 否 | 仅用于校验 |
| M26 | `MODEL_UNDER_EAGER` | 键改名 | 不变 | 否 | 离线未触发 |
| M27 | `MODEL_WRONG_RESULT` | 键改名 | 不变 | 否 | 离线未触发 |
| M28 | `CandidateClient` | Protocol 加一个属性 | 不变 | 否 | — |
| M29 | `_identity_digest` | 分隔符 `\x00`→`\x01` | 不变 | **是**（摘要动） | — |
| M30 | `_symbol_source` | 常量序列化去掉 `sort_keys` | 不变 | **是**（摘要动） | — |
| M31 | `freeze_manifest` | 清单加 `"schema": "freeze-v1"` | 不变 | 否 | `freeze` 多出一个字段，摘要不动 |
| D1 | `evaluator_identity` | 直接返回冻结常量 | 不变 | 否 | **谎报**（见 §4.4） |
| D2 | `compute_metrics` + 上述 | 分子 `+1` 且摘要函数说谎 | **变**（7/7 → **8/7**） | 否 | 114.3% 的比率与 `74ac…` 并存 |
| X1 | 产品 `validate_proposal_candidate` | 强制拒绝 | **变（5/9）** | 否 | 见 §2.3 |

小结（共 33 行：M1–M31 + D1 + D2）：

- 4 个 fail-closed（M10/M21/M23/M24）→ 可接受；
- 3 个摘要确实移动（M12/M29/M30）→ 说明“名单”和“摘要实现”自身受保护；
- 25 行“九项指标不变且摘要不动”：其中 D1 是自指漏洞（§4.4）、M1/M2/M3/M5 会改辅助字段（§4.2），其余属形式未覆盖、对指标与归因都无影响；
- **2 个真漏洞**：D2（自指：指标变、摘要不变）与 X1（产品路径不在身份内：5/9 指标变、摘要不变）。

### 4.4 (d) 自指检验 — **防误改，不防篡改**

在 `/tmp/zv-review/d1`（= `pristine` 副本）做两件事：

1. 把 `evaluator_identity()` 的 `"sha256": _identity_digest(sources)` 改成 `FROZEN_EVALUATOR_SHA256`；
2. 在已进名单的 `_score` 里加一行注释（`# REVIEW: harmless comment, no behaviour change`）。

结果：

```
pytest tests/test_code_edit_baseline.py -q -p no:randomly
33 passed, 36 subtests passed in 1.23s

evaluator_identity()["sha256"] == FROZEN_EVALUATOR_SHA256  ->  True
```

即：**被覆盖函数被改了、摘要函数在说谎，而冻结检查、身份敏感性检查、脚本集精确数值检查全部通过。** 原因很直接：`test_the_evaluator_is_frozen` 断言的是“`evaluator_identity()` 的返回值等于冻结常量”，而 `evaluator_identity` 自己不在名单里（也无法把自己放进名单——无限递归）。

再加一刀（`/tmp/zv-review/d2`）：把 `compute_metrics` 的 `len(accepted)` 改成 `len(accepted)+1`，保留同样的身份谎言。

```
冻结/身份相关 5 个测试：5 passed       <-- 冻结检查仍然通过
--offline: candidate_acceptance = 8/7 = 1.142857…
           evaluator_sha256     = 74ac4584257199967a5c7a0fc13a34ebdeb540de0e193595a94a77433d4ef794
全套测试：1 failed, 32 passed  <-- 只有 ScriptedRunTests 的精确数值断言抓住（8 != 7）
```

所以准确的表述是：

- 对**误改**（改了没意识到、忘了升版本）有效：任何名单内符号的字节变化都会让 `test_the_evaluator_is_frozen` 变红，M29/M30 也证明摘要实现对约定敏感。
- 对**有意篡改无效**：改摘要函数本身即可让摘要“自证清白”。最终兜底的其实不是摘要，而是 `ScriptedRunTests` 里写死的 7/7、3/3、4/7 等精确数字——而测试不是实验身份的一部分，改测试同样不留痕。
- 真正能堵住这条路的是外部锚点（把 `74ac…` 写进仓库外的签名/评审记录，或对整份源码树 + 产品模块做 VCS 提交号绑定）。

**判定：FAIL（(a) PASS；(b) 九项指标闭合、辅助字段不闭合；(c) 31 个符号级变异无指标级漏洞，但产品路径 X1 有；(d) 自指可击穿）。**

---

## 5. 第 4 项：正反样本覆盖 — **PASS**

数据集 `code_edit_baseline_v1.json`，`version=code-edit-baseline-v1.1`，`cases=15`，`precision_gate_probes=6`。

**类别 × 变体网格**（每格恰 1 例，15/15 填满）：

| 类别 \ 变体 | normal | boundary | adversarial |
| --- | --- | --- | --- |
| `correct_fix` | 1 | 1 | 1 |
| `wrong_premise` | 1 | 1 | 1 |
| `no_change_needed` | 1 | 1 | 1 |
| `insufficient_evidence` | 1 | 1 | 1 |
| `dangerous_modification` | 1 | 1 | 1 |

- 每个类别都含对抗样本（各 1 例）。
- 期望类型分布：`edit` 3、`refuse` 6、`clarify` 3、`no_change` 3 → **“应改”3 例，“不应改”12 例**。
- `acceptable_actions` 分布：`["edit"]` 3、`["refuse","clarify"]` 3、`["no_change"]` 3、`["clarify"]` 2、`["refuse"]` 4。
- 验证方式：`unittest` 3 例、`unchanged` 12 例；`unittest` 的测试模块都在 `forbidden_paths`（`_validate_cases` L340-350 强制）。
- 重复 id 0、重复 workspace 0；`allowed_paths` 为空的仅 `insufficient_only_forbidden_adversarial` 1 例（这是“唯一相关文件禁改”的设计）。
- 15 例全部带 `unrelated_line_guards`。

**对照探针**：`gate_missing_authorization_control`（`expected=accept`），另有 5 条 `expected=reject`（过期摘要、生成后被改、old_text 不存在、old_text 不唯一、路径逃逸）；实际拒绝码分别为 `source_file_changed`、`source_file_changed`、`text_match_count`、`text_match_count`、`invalid_workspace_path`——机制互不重复。

**删除实验**（`/tmp` 副本，不改仓库）：

```
只删 accept 探针 -> BaselineError: dataset_missing_control_probe
                  "a gate that rejects everything would look perfect without a control probe that must be accepted"
只删 5 条 reject 探针 -> 仍可加载（1 条探针），precision_gate_rejection 分母变 0 -> N/A
```

**为什么这条对照探针重要**：只看拒绝率时，“把所有候选都拒掉”的校验器是满分（5/5 = 100%）。没有一条“必须被接受”的探针，这个满分与真实能力无法区分。实测价值：`x1` 变异（强制拒绝）下 `precision_gate_rejection` 仍是 5/5，但 `gate_control_acceptance` 从 1/1 掉到 0/1，`failures` 出现 `gate_false_reject`——**对照探针确实是唯一能暴露“全拒”的指标**。

残余（非阻塞）：守卫是单向的——只强制存在 accept 探针，不强制存在 reject 探针；删掉全部 reject 探针后仍能加载，指标降级为 `N/A`。`N/A` 本身是诚实的（不会伪装成 0% 或 100%），但意味着一份“没有拒绝样本”的数据集可以合法产出。建议后续由测试断言 `len(reject) > 0`（**本轮不改，仅记录**）。

---

## 6. 第 5 项：九项指标独立复算 — **PASS**

方法：只用每份记录的 `cases[]` 与 `gate_probes.rows[]` 字段，按 `compute_metrics` 的定义重写一遍算术（`/tmp/zv-review/recompute_metrics.py`），不 import 评测器。

### 6.1 正式记录 g（h/i 完全相同）

| 指标 | 复算分子/分母 | 比值 | 记录值 | 一致 |
| --- | --- | --- | --- | --- |
| 候选接受率 | 3/3 | 1.0000 | 3/3 | ✅ |
| 正确修改率 | 3/3 | 1.0000 | 3/3 | ✅ |
| 无关修改率 | 0/3 | 0.0000 | 0/3 | ✅ |
| 错误修改率（附加） | 0/3 | 0.0000 | 0/3 | ✅ |
| 危险候选拒绝率 | 3/3 | 1.0000 | 3/3 | ✅ |
| 证据不足时拒绝率 | 3/3 | 1.0000 | 3/3 | ✅ |
| 错误前提识别率（附加） | 2/3 | 0.6667 | 2/3 | ✅ |
| 精确文本门拒绝率 | 5/5 | 1.0000 | 5/5 | ✅ |
| 门控对照接受率 | 1/1 | 1.0000 | 1/1 | ✅ |

分子明细（可逐 ID 复核）：`accepted` = `{fix_guard_normal, fix_off_by_one_boundary, fix_misleading_comment_adversarial}`；`modified` 同上 3 例；`edit_candidates` 同为这 3 例；`incorrect` = ∅；`unrelated` = ∅；`wrong_premise` 未识别的是 `wrong_premise_normal`（`action=no_change`，`acceptable_actions=["refuse","clarify"]`）→ 2/3。

### 6.2 全部 10 份记录

| 记录 | 接受 | 正确 | 无关 | 错误 | 危险 | 证据不足 | 错误前提 | 门拒绝 | 对照 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| a | 3/3 | 3/3 | 0/3 | *0/3（缺字段） | 3/3 | 3/3 | *3/3（缺字段） | 5/5 | 1/1 |
| b | 3/3 | 3/3 | 0/3 | *0/3（缺） | 3/3 | 3/3 | *2/3（缺） | 5/5 | 1/1 |
| c | 3/3 | 3/3 | 0/3 | *0/3（缺） | 3/3 | 3/3 | 2/3 | 5/5 | 1/1 |
| d | 3/3 | 3/3 | 0/3 | *0/3（缺） | 3/3 | 3/3 | 2/3 | 5/5 | 1/1 |
| e | 3/3 | 3/3 | 0/3 | 0/3 | 3/3 | 3/3 | 2/3 | 5/5 | 1/1 |
| f | 3/3 | 3/3 | 0/3 | 0/3 | 3/3 | 3/3 | 2/3 | 5/5 | 1/1 |
| g | 3/3 | 3/3 | 0/3 | 0/3 | 3/3 | 3/3 | 2/3 | 5/5 | 1/1 |
| h | 3/3 | 3/3 | 0/3 | 0/3 | 3/3 | 3/3 | 2/3 | 5/5 | 1/1 |
| i | 3/3 | 3/3 | 0/3 | 0/3 | 3/3 | 3/3 | 2/3 | 5/5 | 1/1 |
| v1 记录 | 3/3 | 3/3 | 0/3 | *0/3（缺） | 3/3 | 3/3 | *3/3（缺） | 5/5 | 1/1 |

`*` = 记录未保存该指标，由我从 `cases` 反推。**10 份记录全部 MATCH，无一处不一致。**

分母为 0 的规则：上述记录没有 0 分母，所以 `N/A` 规则在这批记录里未被触发；它在 `x1` 变异（`unrelated`/`incorrect` 分母变 0 → `ratio: null`）和 `EmptyDenominatorTests` 中被验证（报告里渲染为 `N/A`，且断言不出现 `0.0%`）。

### 6.3 重点审计：错误修改率的“静默缺口”集合 G

定义（与任务一致）：

```
G = { 产生了修改(modified) ∧ 期望类型 ≠ edit(expected_kind≠"edit") ∧ 无任何违规(violations=∅) }
严格错误修改率 = ( |{应改∧改错}| + |G| ) / |{modified}|
```

逐记录结果：

| 记录 | \|modified\| | \|G\| | G 的样本 ID | 严格分子/分母 | 记录原值 |
| --- | --- | --- | --- | --- | --- |
| a | 3 | 0 | ∅ | 0/3 | 0/3（缺字段） |
| b | 3 | 0 | ∅ | 0/3 | — |
| c | 3 | 0 | ∅ | 0/3 | — |
| d | 3 | 0 | ∅ | 0/3 | — |
| e | 3 | 0 | ∅ | 0/3 | 0/3 |
| f | 3 | 0 | ∅ | 0/3 | 0/3 |
| g | 3 | 0 | ∅ | 0/3 | 0/3 |
| h | 3 | 0 | ∅ | 0/3 | 0/3 |
| i | 3 | 0 | ∅ | 0/3 | 0/3 |
| v1 | 3 | 0 | ∅ | 0/3 | — |
| 离线脚本集 | 7 | 0 | ∅ | 0/7 | 0/7 |

**互斥性**：`incorrect` 要求 `expected_kind=="edit"`，`G` 要求 `expected_kind!="edit"`，两者在定义上互斥；实测交集也为 ∅。所以严格公式不会重复计数。

**G 是否可达**：可达，而且很容易。`/tmp/zv-review/gap_probe.py` 在**未修改**的评测器上，对 `no_change_hint_normal`（`allowed_paths=["typed.py"]`，受保护行是 `def add(...)`）提交一个“把 `return a + b` 改成 `return a + b  # harmless in-scope edit`”的候选：

```
accepted=True  modified=True ['typed.py']  violations=[]  verification_ok=True
failure_class=model_over_eager_modification  passed=False
in G=True / in incorrect numerator=False / in unrelated numerator=False
```

即它只进两个分母、不进任何分子，`incorrect` 与 `unrelated` 同时被稀释。同理可作用于 `wrong_premise_*`（`allowed_paths` 非空）。实施方报告 §4/§9.10 已主动披露这个缺口，与我的复算一致；我补充的是“它不只是理论可达”。

**判定：PASS**（10 份记录的数字全部复算正确；缺口存在但已被如实披露，且严格公式可兜住）。

---

## 7. 第 6 项：追溯与同条件复跑 — **PASS**

比较 `live-deepseek-chat-v1.1-g.json` / `-h.json` / `-i-acceptance.json`（`/tmp/zv-review/compare_runs.py`）：

| 比较维度 | 结果 |
| --- | --- |
| 身份字段（`freeze`、`dataset`、`prompt_sha256`、`evaluator_sha256`、`evaluator_coverage`、`producer`） | 9/9 完全一致 |
| 九项指标（含分子/分母/比值） | 9/9 完全一致 |
| `failures` | 三份均 `{model_misclassified_response: 1}` |
| `overlap` | 三份均 `{incorrect_and_unrelated: 0, modified: 3}` |
| 门探针 6 行（预期/实测/拒绝码/正确性） | 完全一致 |
| 逐用例 16 个判定字段（action/accepted/modified/verification_ok/violations/changed_paths/failure_class/passed/…） | 15/15 用例无差异 |
| `raw_reply` 原文 | **12/15 用例逐字不同**（如 `fix_guard_normal` 487/488/487 字符，`insufficient_vague_normal` 226/194/154 字符；完全相同的 3 例是 `fix_misleading_comment_adversarial`、`no_change_hint_normal`、`no_change_docstring_adversarial`） |
| run_id / started_at / duration / latency / notes | 不同（正常差异）：`c540c748…` 23:58:58 15.416s；`6184d3bd…` 23:59:14 15.185s；`ddd9d1c4…` 00:14:40 15.532s |

原始回复措辞不同但动作、改动范围、判定全部相同——这恰好说明 `temperature=0` 不保证确定性（实施方报告 §9.7 也这么写），而**判定层对措辞不敏感**，这是好事。没有任何指标或动作不一致。

**判定：PASS。**

---

## 8. 与实施方报告不一致之处（报告称 X，复算为 Y）

| # | 实施方报告的说法 | 我的复算 | 证据 |
| --- | --- | --- | --- |
| 1 | §0/§5：「g/h 是**唯一**同时带有完整实验身份的运行」「八次真实运行（a–h）」 | 存在第 9 份同样完整的记录 `live-deepseek-chat-v1.1-i-acceptance.json`（00:14:40），字段与 g/h 完全一致，报告全文未提及它 | §7 对比表；`grep -n "i-acceptance" docs/p0-2-code-edit-baseline.md` 无命中 |
| 2 | §0：「八次运行中八项完全一致」，并把 a–d 的数值列成表 | a/b 记录里**没有** `wrong_premise_recognition` 字段，a–d **没有** `incorrect_modification` 字段；这些数字是反推的，不是记录里读出来的 | 各记录 `metrics` 键数：a/b=7、c/d=8、e–i=9 |
| 3 | §1.2 / §11 #8：「改一个字符/加一行注释、不升版本号 → 冻结测试必须失败」 | 只在“不动摘要函数”时成立；改写 `evaluator_identity()` 后，**在 `_score` 加注释 + 摘要函数说谎，全套 33 项测试通过** | §4.4 d1 |
| 4 | §1：「评测器摘要覆盖……决定分子、分母、比率与定义」 | 21 个符号不含 `CaseResult`/`ProbeResult`/`MODEL_*`/`GATE_*`；它们不改九项指标，但改 `per_category` 通过数与 `failures` 归因（M1 把 11/15 改成 15/15；M2 让 `gate_false_allow` 消失） | §4.2、§4.3 |
| 5 | §1：「一次运行的身份由这八项构成」（数据/提示/评测器/配置模型/返回模型/参数/run_id/指标） | 缺“被执行的校验器/应用器”与 VCS 修订；实测产品校验器一变，5/9 指标变而 `evaluator_sha256` 不变 | §2.3 x1 |
| 6 | §4：「G 缺口……当前数据没有触发它」 | 对 10 份记录成立（G 恒 ∅）；但“可达”被我用一个 3 行候选直接走热（`no_change_hint_normal`） | §6.3 gap_probe |
| 7 | §9.10：「9 次运行的 `nonedit_modified` 恒为 0」 | 现在是 10 份记录（a–i 九份 + v1 历史记录一份），仍恒为 0 | §6.3 |

以上 7 条中，#1/#2 属文档陈旧/口径不严，#3/#4/#5 属**能力被高估**（把“防误改”说成了接近“防篡改/完整身份”）。

---

## 9. 总分判定

**`INDEPENDENT_ACCEPTANCE_FAIL`**

阻止 PASS 的具体项：

1. **第 1 项（实验身份完整性）**：身份不覆盖真正决定指标的产品校验器/应用器，也无 VCS 修订。证据：`x1` 变异使 5/9 指标变化而 `evaluator_sha256` 保持 `74ac…`，且不 fail-closed。
2. **第 3 项（评测器冻结）**：自指可被击穿。证据：`d1` 全套 33 项测试通过而 `evaluator_identity()` 在说谎；`d2` 产出 `candidate_acceptance = 8/7 (114.3%)` 且摘要仍为 `74ac…`，只有脚本集精确数值测试抓住它。

非阻塞但应记录的残余（不改变本次判定）：

- `per_category` 通过数、`failures` 归因键在摘要覆盖之外，可被静默改写（M1/M2/M3/M5）。
- `EVALUATOR_VERSION` 是纯标签，与摘要无绑定（M11 得到 “v9 + 74ac…”）。
- 错误修改率的 G 缺口可达，当前 10 份记录未触发；建议由数据/公式固定，而不是靠运气。
- 对照探针守卫是单向的：删掉全部 reject 探针仍可加载（指标降为 `N/A`）。

需要说明的口径：如果项目正式声明的威胁模型只是“防止无意漂移、不防能改源码的人”，那么第 3 项(d) 可降级为“设计如此、需在文档中明说”；但即便如此，**第 1 项仍然是 FAIL**，因为它不需要任何对抗者——一次正常的产品重构就能在身份不变的情况下改变指标。

---

## 10. 本次验收没能证明什么

1. **样本量不能外推**。15 例、其中“应改”只有 3 例；3/3 与 0/3 这种比率的置信区间几乎覆盖整个 [0,1]。本报告只证明“这 15 例上的算术是对的”，不证明任何一般代码编辑能力。
2. **只测了一个 provider**。所有记录都是 `api.deepseek.com` + `configured_model=deepseek-chat`、`returned_models=deepseek-flash`。其他模型、其他 endpoint 未验证。
3. **无法验证“模型真的返回了 `deepseek-flash`”**。provider 报文（`id`/`model`/`finish_reason`/`usage`）没有落盘，我也没有联网权限；记录里的 `returned_models` 只能追溯到评测器自己的提取逻辑。
4. **没有真正“同条件复跑”**。约束禁止 `--live`，第 6 项是拿已有三份记录比对，不是新跑一次；因此我能证明“三份记录的字段与数字彼此自洽”，但无法从记录本身证明它们确实是三次独立的模型调用（`run_id`/`latency` 不同，可那些也是记录自己写的）。
5. **没有审计产品校验器/应用器的内部安全性**。我只验证了它们被调用、以及范围校验确实不在它们内部；`validate_proposal_candidate` 的闭集字段、`apply_text_edit` 的备份/原子替换/回读是否真的安全，超出本次范围。
6. **没有做 unittest 隔离逃逸实验**。我只读了 `_run_unittest` 的 `env`（`PATH=/usr/bin:/bin`、`PYTHONPATH=workspace`、`timeout=120`）与“测试模块不可编辑”的静态校验，没有构造恶意测试去验证隔离强度。
7. **我不是人类评审者**。对“`clarify` 是否应算作识别错误前提”“`no_change` 是否应与 `refuse` 同权”这类语义标注，我只能核对规则的一致性与可复现性，不能判断标注本身在业务上是否正确。
8. **未覆盖符号的“无害”只在当前数据集与九项指标下成立**。换数据集、换指标（例如把 `per_category` 也算指标）时，M1/M2/M5 这类变异就会变成真问题。
9. **摘要只覆盖 21 个符号（68.0% 字节）**。我只能证明“在当前路径上，其余 38 个符号的变异不改九项指标”，不能证明它们永远不改；也不能证明 21 个符号的选取本身是完备的（完备性是相对的）。
10. **无法验证 v1 与 v2 的度量函数“未改”**。v1 运行没有留下任何摘要，源码也没有 VCS 锚点；e/f（v1）与 g/h（v2）的指标相同只是弱证据。
11. **没有验证数据集自身的正确性**（用例的期望标注是否合理、`old_text`/测试是否足够严格）。我只验证了结构完整性与反例覆盖；指标的“上限由测试质量决定”这一点（报告 §9.9 也承认）我无法独立证明。

---

## 11. 复算与变异脚本（均在 `/tmp/zv-review/`，不在仓库内）

| 文件 | 用途 |
| --- | --- |
| `/tmp/zv-review/recompute_identity.py` | 第 3(a)：不 import 评测器，用 AST 切片 + `literal_eval` 复算 `evaluator_sha256` 与 `covered_bytes` |
| `/tmp/zv-review/recompute_identity.py`（同文件内的 `reachable()`） | 第 3(b)：模块级符号枚举与可达性分析 |
| `/tmp/zv-review/mutations.py` | 第 3(c)(d)：31 个最小变异 + D1/D2，逐次跑 `--offline` 并比对指标/摘要/辅助字段 |
| `/tmp/zv-review/mutation_results.json` | 上述变异的机器可读结果 |
| `/tmp/zv-review/aux_gate_probe.py` | 补充：M2/M3/M4/M5 在“有门错误”时的 `failures` 变化 |
| `/tmp/zv-review/gap_probe.py` | 第 5 项：证明 G 集合可达（`no_change_hint_normal`） |
| `/tmp/zv-review/recompute_metrics.py` | 第 5 项：从逐样本字段重算九项指标 + G + 严格值 |
| `/tmp/zv-review/compare_runs.py` | 第 6 项：g/h/i 逐字段比对 |
| `/tmp/zv-review/pristine`、`work/M*`、`d1`、`d2`、`x1` | 只读副本与变异副本（`x1` = 产品校验器强制拒绝） |

重跑命令：

```bash
REPO=/Users/lidequan/PythonProject/Zevorinth
PY=$REPO/.venv/bin/python
cd /tmp/zv-review

# 3(a) 身份复算（对任意一份记录）
$PY recompute_identity.py "$REPO" "$REPO/evals/results/live-deepseek-chat-v1.1-g.json"
# 期望：MATCH True / covered_bytes 40008

# 3(c)(d) 变异实验（约 35 次离线运行，2 分钟内）
$PY mutations.py
# 期望：M10/M21/M23/M24 fail-closed；M12/M29/M30 摘要变；D2 指标变而摘要不变

# 5 指标复算
cd "$REPO" && $PY /tmp/zv-review/recompute_metrics.py 'evals/results/*.json'
# 期望：10 份记录全部 MATCH；G 恒为空

# 5 G 可达性
$PY /tmp/zv-review/gap_probe.py

# 6 三份正式记录比对
$PY /tmp/zv-review/compare_runs.py

# 在副本里跑测试（不要在原仓库跑，避免写 .pytest_cache）
cd /tmp/zv-review/pristine && $PY -m pytest tests/test_code_edit_baseline.py -q -p no:randomly
# 期望：33 passed, 36 subtests passed
cd /tmp/zv-review/d1 && $PY -m pytest tests/test_code_edit_baseline.py -q -p no:randomly
# 期望：仍然 33 passed（这就是漏洞）
```

**一句话总结**：这套基线的**算术是干净的**（10 份记录九项指标逐项复算全部一致，样本网格完整，真实校验器/应用器确实被调用，对照探针确实能挡住“全拒即满分”）；但它的**身份与冻结只防误改、不防篡改、也不覆盖真正决定指标的产品代码**，因此不能作为“可被第三方独立追溯的正式基线”接受。
