# P0-2 · V3.1 实施报告（自检完成，待独立验收）

`[V3.1 = IMPLEMENTED_SELF_CHECKED · 独立验收 = NOT_STARTED]` `2026-11-22 01:3x`

前置：`docs/p0-2-v31-v32-design.md`（r2，已批准）、`docs/p0-2-v3-failure-freeze.md`（V3 失败冻结）。
**本报告是实施方自述，不是验收结论。实施方不得自评 PASS。**

授权范围（严格照此执行，未越界）：
① 指标不变量实现 ② 封存与审计防护 ③ 测试与变异矩阵 ④ 两次契约回归运行 ⑤ 实施报告与验收材料。
**未实施 V3.2**：没有 covenant、没有 Prompt AST 提取、没有 `replies_digest`、没有条目自摘要。

---

## 0. 一句话

V3 的问题是"外部流程能准确复算出 `4/3 = 133.3%` 并把它封存"。
V3.1 交付的是：**这个数在生成时造不出来、在封存时进不去、在审计时会被标红**，
而且"拒绝"不会在存储里留下任何痕迹。

## 1. 改了什么

### 1.1 评测器（`src/zevorinth/code_edit_baseline.py`）

| 项 | 变化 |
| --- | --- |
| `EVALUATOR_VERSION` | `code-edit-evaluator-v3` → **`-v4`** |
| `METRIC_CONTRACT_VERSION` | `…-contract-v2` → **`-v3`** |
| `ANCHOR_SCHEMA` | `…-anchor-v3` → **`-v4`** |
| 身份符号 | 48 → **56**（删 `_ratio`，加 `_metric` / `METRIC_SPECS` / `METRIC_NAMES` / `METRIC_CONTRACT_VERSIONS` / `CASE_TABLE_FIELDS` / `case_table_row` / `build_case_table` / `tallies_from_case_table` / `assert_metric_contract`） |
| `_metric(name, num, den)` | 取代 `_ratio`：I1 非负整数、I2 分子≤分母、I3a 空分母→`ratio=None`、I4 ratio 与分数一致；失败抛 `BaselineError`，带机器可读码 |
| `case_table` | 新增，15 行 × 12 字段，只含判定、不含模型原文与补丁文本 |
| `tallies` | 新增，**只能由 `case_table` 聚合**，含 `per_category` / `failures` / `modified` / `wrong_edits` / `in_scope_unwanted` / `counts` / `gate_probes` |
| `assert_metric_contract` | 第二套推导：从 `tallies` 重算九项，与第一套逐项比对（I5–I9） |
| `_result_seal` | 覆盖范围加入 `case_table` + `tallies` |
| `anchor_payload` | 加入 `metrics` / `per_category` / `failures` / `overlap` / `scope_audit` / `case_table` / `tallies`，使审计无需打开记录即可重算 |

### 1.2 封存与审计（`scripts/anchor_code_edit_evidence.py`）

| 能力 | 实现 |
| --- | --- |
| 指标不变量 | `metric_invariants()` 用自己的代码重算 I1–I6；**I1/I2/I3a/I4 先跑，再按版本分派** |
| 三方一致 | `tally_checks()` 从封存表重算 `tallies` 与九项，再与 `scope_audit` 的 ID 清单比对 |
| 完整验证 | `verify --full` → `full_cases_checks()`：读磁盘数据集、比对摘要、用**数据集**取 `category`/`expected_kind`、用 `raw_reply` 解析 `action`、逐行比对表与记录、重新聚合 |
| 证据层级 | 输出 `evidence_level = payload_with_case_table` 或 `full_cases`；数据集缺失/摘要不符**直接失败**，不降级 |
| 声明一致性 | `claim_consistency()`：记录顶层的 `dataset` / `prompt` / `replies` 声明必须等于 `anchor_payload` 里那份（后者在 `combined_sha256` 内，由外部 store 持有）；不等则拒绝，且 `--full` 不再按该声明去找数据集 |
| 审计 | `audit` 逐条做 payload 级算术；v3.1 之前的条目报 `LEGACY`（不是 PASS，也不是 FAIL） |
| 拒绝原子性 | 全部校验跑完才碰磁盘；`verify`/`audit` 用 `create=False`；临时文件 + `os.link`（原子且不覆盖）；输出 `store_fingerprint` |

### 1.3 新增工具与测试

- `scripts/code_edit_metric_matrix.py`：**23 行**变异矩阵（数学 9 / 版本 6 / 集合双录 3 加 1 / 拒绝完整性 4 / 正样本 2）。
- `tests/test_code_edit_metric_invariants.py`：**27 个测试**，覆盖生成端、封存端、审计端、`full_cases`、声明一致性、拒绝留痕。
- 既有测试更新：`_ratio`→`_metric`、指标字典加 `status`、冻结摘要换成 V3.1 值、`check_record` 检查名由"恰好五项"改为"前五项 + 指标不变量"。

## 2. 三个边界，各自独立成立

```
生成边界  _metric / assert_metric_contract      → 记录根本写不出来（rc=3）
封存边界  anchor record 的 check_record         → REFUSED，store 指纹不变（rc=1）
审计边界  anchor audit 的 payload 算术          → 已封存条目被标 FAIL（rc=1）
```

审计端的独立性用一个测试证明：把已入库条目的 `correct_modification` 改成 `4/3`，
`audit` 输出 `metric_denominator_exceeded` 且 rc≠0（V3 时代这条路径是绿的）。

### 2.1 实施中发现并补上的一个缺口：记录的数据集声明不在封印内

写验收材料第 8 节第 3 问（"把数据集换成另一份会怎样"）时，先自己试了一遍，结果发现一个
**设计里没写、V3 验收也没查到的侧门**：

- 一条记录里有两份 `dataset`：顶层的 `record["dataset"]`（`full_cases` 用它去找磁盘文件）
  和 `anchor_payload["dataset"]`（在 `combined_sha256` 内、由外部 store 持有）。两者是**不同对象**。
- `_result_seal` 封的是**结论**（指标、逐样本判定、`case_table`、`tallies`），**不包含** `dataset` / `prompt`。
- 所以顶层的 `dataset` / `prompt_sha256` 声明可以单独改，封印与链条都不断。

实测（改 `record["dataset"]["sha256"]` 为 64 个 0，不重签）：`check_record` **零个失败项**。
后果：`--full` 会照着被改过的声明去找数据集。如果同时把磁盘上那份数据集换成内容不同但
结构合法的另一份，摘要对得上，就只剩行级比对能挡 —— "让新数据集冒充旧版本" 由此有了一条侧门。

补法（`claim_consistency()`，加在 `check_record` 末尾）：`dataset` / `prompt` / `replies`
三处声明必须**逐字段等于** `anchor_payload` 里那份。要绕过就得同时改 `anchor_payload`
→ `combined_sha256` 变 → 与外部 store 持有的原值不符。并且 `full_cases_checks` 一进门
先查这条：声明不一致就**拒绝按该声明找数据集**，不再打印"验证通过"。

反向验证（4 例，先注入再确认转红）：

| 注入 | 结果 |
| --- | --- |
| 只改顶层 `dataset.sha256` | `dataset_claim_consistency` FAIL；`--full` 直接拒绝找文件 |
| 只改顶层 `prompt_sha256` | `prompt_claim_consistency` FAIL |
| 换磁盘数据集**且**两份摘要同步改成新值 | `dataset_expected_kind_mismatch` FAIL（摘要已对得上，靠内容比对抓住） |
| 数据集文件不存在（两份声明一致） | `dataset_missing` FAIL（仍是失败，不是降级） |

不误杀历史：`v3-a/b/c` 与 `v31-a/b` 五条记录的 3 项声明一致性**全部 PASS**（逐条实测）。

**边界**：`audit` 层**做不了**这项 —— payload 里只有 pinned 副本、没有顶层声明。所以
"audit 通过" 仍不等于 "声明一致"，这一点必须写在证据层级说明里，不能混。
另外**没有扩矩阵**：设计矩阵是 23 行契约，事后加行会动验收口径，是否补行交给验收方判。

## 3. 证据

### 3.1 变异矩阵

```
scripts/code_edit_metric_matrix.py  →  23/23 rows behaved as promised
scripts/code_edit_mutation_matrix.py（V3 的 12 行，仍在跑）→ 12/12
```

23 行逐行结果：

| 行 | 结果 |
| --- | --- |
| 01 `4/3` | `metric_denominator_exceeded`，store 0 文件 |
| 02 `8/7` | 同上 |
| 03 空分母报 `0.0` | `metric_empty_population_misreported` |
| 04 v3 缺 `status` | `metric_contract_field_missing` |
| 05 负分子 | `metric_value_invalid` |
| 06 `9/8` 且 ratio 同步改 | `metric_denominator_exceeded` |
| 07 `3/3` 但 ratio=0.9 | `metric_ratio_inconsistent` |
| 08 同一样本计两次 | 生成边界 `metric_population_mismatch` |
| 08b 内部计算器改常量 | rc=3，记录未写入 |
| 09 伪造 v2 标签 | `metric_contract_field_mismatch` |
| 10 伪造 v3 缺字段 | `metric_contract_field_missing` |
| 11 缺契约版本 | `metric_contract_missing` |
| 12 未知版本 v99 | `metric_contract_version_unknown` |
| 13 `tallies` 漂移 | `metric_tally_mismatch` |
| 14 v3 少一项指标 | `metric_set_complete` |
| 15 指标改名 | `metric_set_complete` |
| 16 `scope_audit` 漂移 | `metric_tally_mismatch:scope_audit` |
| 18 同一非法请求连发两次 | 两次 rc=1，store 0 文件 |
| 19 拒绝前后 store 指纹 | `e3b0c442…` → `e3b0c442…`（相同） |
| 20 临时文件残留 | `[]` |
| 21 拒绝后合法记录 | rc=0，序号 `0001-` |
| 22a 正常记录 | rc=0 |
| 22b 空人口（0/0） | rc=0，以 `N/A` 通过 |

### 3.2 测试

```
pytest tests/test_code_edit_baseline.py tests/test_code_edit_integrity.py \
       tests/test_code_edit_metric_invariants.py -q
→ 80 passed, 45 subtests passed in 23.53s
```

（§2.2 的声明一致性补上之后，`metric_invariants` 从 24 例增至 27 例，总数 77 → 80。）

### 3.3 两次契约回归运行（授权项 ④）

| 运行 | run_id | 起始 | 耗时 | 九项指标 |
| --- | --- | --- | --- | --- |
| `v31-a` | `1f5474c3…` | 01:26:22 | 16.595s | 3/3 · 3/3 · 0/3 · 0/3 · 3/3 · 3/3 · 2/3 · 5/5 · 1/1 |
| `v31-b` | `248f13f0…` | 01:26:43 | 15.940s | 同上（**逐项相同**） |

两次都 `evidence_level = full_cases`、`VERIFIED`（各 **66 条 PASS**：原 63 条 + 声明一致性 3 条）。
**与 v3-a/b/c 的九项数字逐项相同** —— 这正是契约回归要达到的效果：
换了契约、加了不变量，口径没动。

**这两次不构成统计复现**（决策 3）：样本量 15、`temperature=0.0`、同一模型，
只能说"指标口径未变，且两次分别独立封存"，不能外推模型稳定性。

### 3.4 外部锚点

```
~/.zevorinth/evidence-anchors/code-edit/
0001-52dac639…  LEGACY（v3 时代，payload 无 metrics）
0002-a972a26a…  LEGACY
0003-8abcd939…  LEGACY
0004-1f5474c3…  PASS  evidence_level=payload_with_case_table
0005-248f13f0…  PASS
5 anchors, chain intact
```

v3 的三个条目**没有被删除或改写**，只是如实标注为"早于 v3.1 载荷契约"。

## 4. 冻结值（V3.1 终态）

```
evaluator     f14266fc8f2529488ccf7f313bf4c2ef9d6ec09bbf6a373b89ac3b437b06f2ee   56 符号 / 63991 B
dependencies  1b2a72dd40b9af7b695a49c63f52fd4aada784fba7569fd33379dd9073642a27   28 符号（未变）
source tree   1bc1f9fc16b4dbc2…                                                   57 文件 / 1627497 B
dataset       835f2bb0610ca85a…（未变）  prompt e32a89bc…（未变）  replies 0a0fc197…（未变）
metric        code-edit-metric-contract-v3      anchor schema zevorinth-code-edit-anchor-v4
运行记录      v31-a bbf1dbeb…  v31-b 17dcf4f2…（55845 / 55945 B）
锚点条目      0004 15019 B（V3 时代 0001 为 4725 B，约 3.2 倍）
```

## 5. 已知限制与本次偏离（必须写清楚，不能藏在正文里）

1. **`full_cases` 不是全字段重算。** 字段来源分三层，报告里也逐层标注：

   | 字段 | 来源 | 强度 |
   | --- | --- | --- |
   | `dataset.sha256` | 磁盘文件 | 独立 |
   | `category` / `expected_kind` | 数据集 `expected.kind` | 独立 |
   | `action` | 从 `raw_reply` 重新解析 | 独立 |
   | `accepted` / `modified` / `verification_ok` / `violations` | 记录内 | 仅一致性（未重放补丁） |

   要独立重算后四项，需要把"解析候选 → 门禁校验 → 应用补丁 → 跑验证"再实现一遍；
   本版没有做，属于明确未覆盖项，**不是**"已经验证"。

2. **v3 的三条记录在新代码下无法重验身份。** 评测器身份从 `0c9bc436…` 变成 `f14266fc…`，
   源码树也变了，所以 `verify` 对 v3-a/b/c 会报身份不符。能验的只有算术：
   `metric_invariants` 对它们仍然通过（`4/3` 这类值会被同一套规则抓住）。
   设计文档 §1.8 第 11 项原来写"v3-a/b/c 仍 VERIFIED"，这个表述**过强，已订正**为
   "历史记录的算术不变量仍成立，身份只能在当时的代码状态下复算"。

3. **旧矩阵的一行锚点同步更新过。** `scripts/code_edit_mutation_matrix.py` 的
   `evaluator_logic` 行原来 patch `_ratio(...)`，函数改名后该行会报"锚点出现 0 次"。
   已改为 patch `_metric("candidate_acceptance", len(accepted) + 1, …)`，**语义不变**
   （改计算器 → 冻结测试转红）。代价是该文件的哈希不再等于 V3 冻结附录 A 的值；
   附录 A 记的是 V3 终态，历史结论仍以 `p0-2-v3-independent-acceptance.md` 为准。

4. **旧锚点条目在审计层是 `LEGACY`。** 它们没有 `metrics` 可算。这是如实标注，
   不是"通过"：任何 v3 契约条目缺 `metrics` 会被判 FAIL，所以不能靠伪装成旧条目逃过审计。

5. **数据集替换只有 `verify --full` 才查得到。** payload 级审计不读磁盘，这是设计如此
   （V3.2 的 covenant 才会把"期望值"也搬出记录）。当前若只跑 `audit`，
   换数据集不会被发现。

6. **同用户信任边界未改变。** 锚点仍在同一用户权限内，`--store` 仍强制在仓库外，
   但"有权限的人重写 store 与代码"依然不可防。名称仍是 `LOCAL_EXTERNAL_EVIDENCE_ANCHOR`。

7. **声明一致性只有 `verify` 查得到，`audit` 查不到。** §2.1 补的
   `claim_consistency` 读的是记录顶层声明，而 `audit` 只拿到 `anchor_payload`（没有顶层声明）。
   所以「`audit` 通过」**不等于**「声明一致」。这是本版的一个真实盲区，不是表述偷懒。

8. **23 行矩阵没有覆盖声明一致性。** 该防护只有 4 个测试（注入 → 转红）。
   设计矩阵是 23 行契约，事后加行会动验收口径，所以**没加**，补不补由验收方判（§2.1）。

## 6. 复现命令

```bash
cd /Users/lidequan/PythonProject/Zevorinth
PY=./.venv/bin/python

$PY scripts/code_edit_metric_matrix.py                    # 23/23
$PY scripts/code_edit_mutation_matrix.py                  # 12/12（V3 那套）
$PY -m pytest tests/test_code_edit_baseline.py tests/test_code_edit_integrity.py \
   tests/test_code_edit_metric_invariants.py -q           # 77 passed

$PY scripts/anchor_code_edit_evidence.py verify \
   evals/results/live-deepseek-chat-v31-a.json --full     # 66 PASS / VERIFIED / full_cases
$PY scripts/anchor_code_edit_evidence.py audit            # 5 anchors, chain intact（3 LEGACY + 2 PASS）
```

## 7. 边界与状态

| 项 | 状态 |
| --- | --- |
| V3.1 实施 | **`IMPLEMENTED_SELF_CHECKED`**（本报告） |
| V3.1 独立验收 | `NOT_STARTED`（实施方不得自评 PASS） |
| V3.2 | 设计已批准，**`NOT_AUTHORIZED`**，一行未动 |
| V3 历史 `FAIL` | 冻结保留，未被覆盖 |
| S3 | `NOT_APPROVED` |
| 模型编辑权限 | 未扩大 |
| `PRODUCTION_TRUST` | 未提升 |

**不因为自测通过就启动 V3.2 或 S3。** 等独立验收。
