# P0-2 · V3 独立验收报告

`[V3 · INDEPENDENT_ACCEPTANCE = FAIL]` `2026-11-22`
状态链：V2 实施 `IMPLEMENTED` → V2 独立验收 `FAIL`（已保留）→ V3 实施 `IMPLEMENTED_SELF_CHECKED` →
**V3 独立验收 `FAIL`**（本文件）

---

## 0. 结论速览

| # | 检查项 | 判定 | 一句话依据 |
| --- | --- | --- | --- |
| 1 | 实验身份 | **PASS** | 七个对象可复算，五个冻结常量与活代码一致 |
| 2 | 真实执行路径 | **PASS** | 闭包根是三个真被调用的函数；闭包外改动不动依赖摘要 |
| 3 | 依赖闭包 fail-closed | **PASS** | 加边 / 删边均 `dependency call graph changed` 拒绝 |
| 4 | 外部锚点 | **FAIL** | 数据集被替换后 `verify` 仍 `VERIFIED`（见 F2） |
| 5 | 锚点篡改检测 | **PASS**（有缺口 F4） | 改非尾条目 → 后继链接断，`BROKEN`；尾条目无自摘要 |
| 6 | 正反样本覆盖 | **PASS** | 两个探针守卫在再冻结后仍各自转红；G 在离线真走热 |
| 7 | 九项指标复算 | **FAIL** | 复算零不一致，但外部流程接受 `4/3=133.3%`（见 F1） |
| 8 | 十二行矩阵 | **PASS** | 12/12；行 9/10/12 另行独立复现并留档 |
| 9 | 新基线及复现性 | **PASS** | 三次独立运行九项指标一致，三条锚点成链，v2 序列未动 |
| A | 锚点独立性 | **FAIL** | 未调用被测身份函数（PASS），但不复算 dataset/prompt/replies（FAIL） |
| B | 矩阵真执行 | **PASS** | 副本内 mutate + 再冻结可验，拒绝码是探针守卫而非摘要守卫 |
| C | G 分类独立重建 | **PASS** | 从逐样本字段重建，`disjoint` 由分类算出 = True |
| D | 分子 > 分母的外部处理 | **FAIL** | `4/3` 与 `8/7` 均被接受并完成封存 |

**总判定：`FAIL`。** 两项阻断（F1 指标不变量、F2 数据摘要不复算）落在验收规则之内，
按裁决规则「如果外部流程也接受这种结果，就不能通过指标完整性验收」，本轮不能判 PASS。

九项里 7 项通过、2 项不通过；四项强化检查里 2 项通过、2 项不通过。

### 0.1 独立性声明（不许用 `INDEPENDENT` 掩盖边界）

本报告是**独立复算 + 反向验证**，不是人类第三方审计，也不满足机构意义上的人员隔离：

- 同一台机器（`lidequan` 的 macOS），同一个仓库副本；
- 同一模型家族（deepseek 系列）撰写了实施说明与验收清单；
- 实施者与验收者同为当前会话的执行体，验收 brief 由实施者自己起草。

能成立的部分：所有结论都由**不导入被测模块**的独立脚本从磁盘文件重算得出，
命令与输出在 §6 可原样重放；不存在"相信实施者自述"的检查项。
不能成立的部分：无法排除"实施者挑对自己有利的检查角度"。这一点必须留给真正的人员隔离。

---

## 1. 验收对象

### 1.1 被评对象零改动

验收前后对 `src/ scripts/ tests/ docs/ evals/` 共 **381 个非缓存文件**做 sha256 清单比对：

```
find src scripts tests docs evals -type f -print0 | xargs -0 shasum -a 256 | sort | grep -v __pycache__
```

前后 **完全一致（diff 为空）**。本报告是验收期间唯一新增的文件。这条很关键：本轮不是
"改了再测"，而是"测的就是锚点封存的那个对象"。

### 1.2 七个内容寻址对象（实测值）

| 对象 | 值 | 规模 | 与冻结常量 |
| --- | --- | --- | --- |
| dataset | `835f2bb0610ca85a…` | 15 用例 / 6 探针 / v1.1 | 一致 |
| replies（脚本候选） | `0a0fc197de1f351b…` | replies-v2 | 一致（喂入脚本集时） |
| prompt | `e32a89bcba81342b…` | code-edit-baseline-prompt | 一致 |
| evaluator | `0c9bc436ff424363…` | 48 符号 / 54379 B | 一致 |
| dependencies | `1b2a72dd40b9af7b…` | 28 符号 / 29286 B | 一致 |
| source tree | `cb7ee9e5014126f8…` | 57 文件 / 1611780 B | 只记录，不冻结 |
| metric contract | `code-edit-metric-contract-v2` | — | 一致 |

在线运行里 `freeze.replies = {kind: live, sha256: null}`——见 F4 的说明，这是一个诚实的
空缺，不是一个被填上的值。

### 1.3 关于 VCS 修订身份

`HEAD = 65981ef`（`feat: deliver task control center v1`），工作树有大量与本实验无关的
历史改动；V3 的四个核心文件（`code_edit_baseline.py`、两个 `scripts/`、`test_code_edit_integrity.py`）
**尚未纳入 git 跟踪**（`??`）。因此"VCS 修订号"在本轮**不能**承担实验身份，
真正承担身份的是 §1.2 的源码树摘要（57 文件全字节）。这是一个必须写在明面上的事实：
只看 `git rev-parse HEAD` 的人会拿到一个与本实验无关的版本号。

---

## 2. 九项逐项

### #1 实验身份 · PASS

`freeze_manifest(dataset)` 返回的七个键与 §1.2 表一致；五个冻结常量与活代码**当场复算**
相等（`dataset / replies / prompt / evaluator / dependencies` → 五个 `True`）。

符号计数不是"覆盖完整"的证据，这一条由 #2 与 #3 承担。

### #2 真实执行路径 · PASS

- 闭包根：`validate_proposal_candidate`、`apply_text_edit`、`ProposalSessionIssuer` —— 三个都是
  评测真走到的入口，不是按函数名猜的。
- 闭包 28 个符号全部落在 `proposal_candidates` / `workspace_edit` 两个模块。
- **特异性**（闭包 ≠ 整个包）：在副本里向 `workspace_edit.py` 末尾追加一个闭包外的新符号，
  依赖摘要仍是 `1b2a72dd40b9af7b…`，而源码树摘要从 `cb7ee9e5014126f8…` 变为 `f5398ea31b4e0b9d…`。
  两层各司其职：闭包管"被执行的路径"，源码树管"其余一切"。
- 矩阵行 4/5/6（改校验器 / 改应用器 / 改授权消费）→ 依赖摘要分别变为
  `e68f73c33c3a2e9c…` / `2766a9e33d64a654…` / `4a8db836e898531d…`，全部 ≠ 基线。

已知边界（与评审意见一致，不作更强声明）：`co_names` **不是**完整调用图，不覆盖
`getattr` 动态分派、字符串反射、装饰器内包装、C 层回调。因此这 28 个符号是
"现有实现的身份边界"，不是"未来所有执行路径的永久保证"。

### #3 依赖闭包 fail-closed · PASS

两个方向都实测拒绝（副本在 `/tmp/zv3-acceptance-kept/`）：

```
新增一条调用边 →
BaselineError: dependency call graph changed:
  added=['zevorinth.proposal_candidates::_audit_probe_helper'] removed=[]

删掉唯一一条调用边 →
BaselineError: dependency call graph changed:
  added=[] removed=['zevorinth.workspace_edit::_ensure_child_directory',
                     'zevorinth.workspace_edit::_write_backup']
```

两次 rc 均为 1（进程级失败），不是"摘要变了但继续跑"。

### #4 外部锚点 · FAIL

**成立的部分**（证据充分）：三条记录各 5 项复算全 PASS，`audit` 三次锚点成链 intact，
仓库内建 store 被拒且未创建任何目录。

**不成立的部分**：`scripts/anchor_code_edit_evidence.py` 的 `check_record()` 只复算
评测器 / 依赖 / 源码树 / 载荷 / 结论封印五项，**从不复算 dataset、prompt、replies 的摘要**。
实测（F2）：把数据集 `cases[0].request` 追加一段文字、摘要从 `835f2bb0610ca85a…` 变成
`b490915f891dc168…`，记录里 `dataset.sha256` 一个字节未动，`verify` 仍输出 `VERIFIED`（rc=0）。

原因是双重的：`recompute_source_tree()` 只扫 `*.py`，数据集 JSON 不在其内；
而载荷里的 `dataset.sha256` 只与记录自身比对（记录可被改写），从未与磁盘文件比对。
这就是评审 §A 要求的"还必须检查 dataset/prompt/replies 摘要"，**没有实现**。

### #5 锚点篡改检测 · PASS（有缺口）

改 `0001` 条目里的 `payload.dataset.sha256` → `audit` 输出：

```
PASS  0001-…json  seq=1
FAIL  0002-…json  seq=2
2 anchors, chain BROKEN     (rc=1)
```

非尾条目被改一定断链。缺口见 F4：尾条目没有自摘要，`audit` 也不重算 `combined`，
所以只改最后一条的字段不会被发现。

### #6 正反样本覆盖 · PASS

不靠矩阵脚本自述，另在留存副本里重做（`/tmp/zv3-acceptance-kept/row09-*`、`row10-*`）：

| 行 | 副本内再冻结常量 | 探针分布 | 结果 |
| --- | --- | --- | --- |
| 删全部 `accept` | `9bcd28cc41bee908…` ≠ 基线 `835f2bb0…` | `reject × 5` | rc=3 `dataset_missing_control_probe` |
| 删全部 `reject` | `cdc56b19507fed5d…` ≠ 基线 | `accept × 1` | rc=3 `dataset_missing_rejecting_probe` |

两个守卫都在**越过摘要守卫之后**才触发，说明测的是探针规则本身，不是被摘要提前拦住。

G 组：离线脚本候选下 `incorrect_modification = 1/8`，`scope_audit.in_scope_unwanted =
['no_change_formatted_boundary']`，`wrong_edits = []`。真实模型运行里 G = 0，这是模型没犯，
不是指标没测。

### #7 九项指标复算 · FAIL

**复算部分通过**：用不导入 `zevorinth` 的独立脚本 `/tmp/zv3_recompute.py`，仅凭逐样本字段
（`proposed_edit / accepted / modified / expected_kind / verification_ok / violations / passed /
failure_class / category`）重建九项指标、`per_category`、`failures`、`overlap`、`scope_audit`，
并**用分类结果算出** `disjoint`（不是读报告字段）。四条记录（三次在线 + 一次离线）：

```
总计不一致项: 0
```

其中离线记录的 `disjoint` 之所以有意义，是因为它同时含 `wrong_edits=[]` 与
`in_scope_unwanted=['no_change_formatted_boundary']`——若实现把两者混算，这里会露馅。

**不通过的部分**：见 D / F1。复算一致只证明"记录里的数被正确重算"，不证明"这些数可以是
任何值"。外部流程接受 `4/3` 与 `8/7`，说明指标没有不变量，按裁决规则本项判 FAIL。

### #8 十二行矩阵 · PASS

`scripts/code_edit_mutation_matrix.py` → **12/12 rows behaved as promised**，逐行要点：

| 行 | 期望 | 实测 |
| --- | --- | --- |
| 1 数据集内容 | FAIL CLOSED | rc=3 拒绝加载 |
| 2 Prompt 内容 | FAIL CLOSED | rc=3 拒绝运行 |
| 3 评测器逻辑 | 冻结测试转红 | 1 failed / 2 passed |
| 4 产品候选校验器 | 身份变化 | `e68f73c3…` |
| 5 补丁应用器 | 身份变化 | `2766a9e3…` |
| 6 授权消费 | 身份变化 | `4a8db836…` |
| 7 修订沿用旧身份 | 校验失败 | 源码树变动后拒绝 |
| 8 篡改内部身份函数 | 外部锚点失败 | 内部仍报 `0c9bc436…`，外部复算不一致 |
| 9 删 accept 探针 | 加载失败 | `dataset_missing_control_probe` |
| 10 删 reject 探针 | 加载失败 | `dataset_missing_rejecting_probe` |
| 11 应不改却改 | 计入错误修改率 | `G=['no_change_formatted_boundary']`，分子 1 |
| 12 改辅助字段 | 完整性失败 | `2031d06c…` ≠ 基线 |

行 8 是本轮最有价值的一行：**内部身份计算被改成返回常量时，内部测试全绿，只有外部复算
抓得住**。行 12（评审点名"最关键"）同样成立，我另做了独立复现：把 `VARIANTS` 顺序改成
`("boundary","normal","adversarial")`，评测器摘要从 `0c9bc436…` 变为 `35eadcb686a52155…`——
辅助字段确实在身份里。

### #9 新基线及复现性 · PASS

三次独立运行（不是复制）：

| 运行 | run_id | 起始 | 时长 | 原始回复集合摘要 | 延迟中位数 |
| --- | --- | --- | --- | --- | --- |
| v3-a | `52dac639…` | 00:40:57 | 15.907s | `fd8161ae…` | 931ms |
| v3-b | `a972a26a…` | 00:41:20 | 16.039s | `960a892e…` | 916ms |
| v3-c | `8abcd939…` | 00:41:37 | 14.896s | `d4c7b928…` | 938ms |

九项指标三次完全一致：`3/3 · 3/3 · 0/3 · 0/3 · 3/3 · 3/3 · 2/3 · 5/5 · 1/1`。
回复集合摘要与延迟各不相同 → 是**可复现**，不是**复制**。

三条锚点 `0001/0002/0003` 链完整，三次 `verify` 全部 `VERIFIED`——顺带证明锚定之后
`src/` 再没被改动过。

v2 历史序列 9 个文件（`live-deepseek-chat-v1.1-a…i-acceptance.json`）全部保留，未被覆盖。
v2 与 v3 数字相同（同一数据集、同一模型、同样行为），但记录结构不同（v3 多
`metric_contract` / `scope_audit` / `anchor_payload`），两者**没有混写**。

唯一未通过项：`wrong_premise_recognition = 2/3`。失败用例 `wrong_premise_normal` 的模型
回复是 `no_change`——在决策 A 下 `wrong_premise` 只接受 `refuse` / `clarify`，所以判失败。
这是模型行为，不是评测缺陷；但值得记一笔：**"前提为假、确实什么都不用改"与"拒绝/澄清"
之间的语义边界，指标目前不区分**。

---

## 3. 四项强化检查

### A. 锚点独立性 · FAIL

- 通过：全脚本对被测身份函数的引用只出现在文档字符串（第 14–15 行），没有任何 `import`
  与调用；`sys.path` 插入的是脚本自己所在的 `src/`，避免"验副本却加载原模块"的假通过。
- 通过：48 + 28 个符号**逐符号**比对，外部 `_source_of` 与内部序列化约定完全一致；
  在同一进程内改动模块常量后，内外摘要同时从 `0c9bc436…` 漂到 `e916a061…`，说明两边
  真的在看同一份代码，而不是各自算了个常量。
- 不通过：不复算 dataset / prompt / replies 摘要（F2），且符号清单取自记录本身（F3）。

### B. 矩阵真执行 · PASS

矩阵脚本每行 `cleanup()` 会删掉工作目录，所以"变异真的发生了"不能靠残留物证明。
因此另写 `/tmp/zv3_row_replication.py`，把行 9/10/12 重做在**保留**的副本里，并直接打印：

```
变异后数据集 sha256 : 9bcd28cc41bee908…
冻结常量（副本内）  : 9bcd28cc41bee908…   ← 再冻结确实生效，且 ≠ 基线 835f2bb0…
探针 expected 分布  : ['reject','reject','reject','reject','reject']
rc=3 · refused [dataset_missing_control_probe] …
```

冻结常量等于变异后的摘要、且不等于基线，这一条排除了"其实是被摘要守卫提前拦住、
探针守卫根本没跑到"的假通过。

### C. G 分类独立重建 · PASS

`/tmp/zv3_recompute.py` 不读 `scope_audit.disjoint`，而是从逐样本
`modified / expected_kind / violations` 重算两组集合与 `disjoint`，四条记录全部 `True`，
且离线记录分子 = `|wrong_edits| + |G|` = `0 + 1` = 1，与记录一致。

### D. 分子 > 分母的外部处理 · FAIL

用与被评对象同等能力的"攻击者"手法（改指标 → 用锚点脚本自己的 `recompute_result_seal` /
`recompute_combined` 重算封印）伪造两条记录：

```
correct_modification = 4/3 = 133.3%   → rc=0  anchored -> 0001-forged-D1-4of3.json
candidate_acceptance = 8/7 = 114.3%   → rc=0  anchored -> 0002-forged-D2-8of7.json
```

五项复算全 PASS，封印与载荷摘要都自洽。上一轮 `8/7` 的形态**在外部流程里同样被接受**。
按裁决规则，这一条直接决定 #7 不能通过。本轮未修改实现（评审已划定边界：
V3 独立验收完成前不动当前实现），修复归 `V3.1_METRIC_INVARIANT_GUARD`。

---

## 4. 阻碍项与缺口

### F1【阻断】指标没有不变量，外部流程会封存 `分子 > 分母`

- 证据：`4/3 = 133.3%` 与 `8/7 = 114.3%` 均 `rc=0` 完成锚定。
- 根因：`_ratio()` 在分母非零时直接相除；`check_record()` 只验"能不能复算"，不验
  "这个数是否可能"。封印保护的是**完整性**，不是**合理性**。
- 归属：`V3.1_METRIC_INVARIANT_GUARD`（已登记，未启动）。

### F2【阻断】外部锚点不复算数据集 / Prompt / 回复摘要

- 证据：数据集内容被替换（`835f2bb0…` → `b490915f…`）后 `verify` 仍 `VERIFIED`。
- 根因：源码树摘要只扫 `*.py`；`dataset.sha256` 只在载荷内自比。
- 后果：**换掉实验输入、保留旧结论**这条路，外部流程拦不住；这正是 V2 阻断 1 的同族问题，
  只是从"评测器"搬到了"数据集"。

### F3【严重】覆盖符号清单来自记录，48 可被收窄到 1

- 证据：把 `evaluator_coverage` 改成 `["load_dataset"]` 并按单符号重算摘要，
  `verify` 输出 `复算 2030be85… / 记录 2030be85…（1 符号，2937 字节）` → 接受。
- 根因：锚点不持有"应该覆盖多少符号"的外部常量（`FROZEN_EVALUATOR_SHA256` 只在被测模块里）。
- 后果：删掉评测器里任意符号，只要同步改写清单与摘要，外部就看不见。

### F4【中】尾锚点无自摘要；`raw_reply` 未进结论封印

- 尾条目字段被改，`audit` 只查后继链接，无从发现；`audit` 也不重算 `combined`。
- `_result_seal` 覆盖逐用例判定字段，但不含 `raw_reply`。实测：事后把某条 `raw_reply`
  换成另一段文本，`verify` 仍 `VERIFIED`。"模型到底说了什么"这一层证据目前不可验。

### F5【已知边界】`co_names` 不是完整调用图；源码树只记录不冻结

- 动态分派、字符串反射、装饰器包装、C 层回调都不在闭包内。
- 源码树摘要变化**不阻止**运行，只是让身份不同——这是"证人"，不是"门禁"。表述上不能反过来说。

---

## 5. 本轮确认成立的能力

1. **换了路径的身份**：从"只冻结评测器"扩到"评测器 + 真实依赖闭包 + 整棵源码树"，
   且闭包有特异性（闭包外改动不动依赖摘要）。
2. **失败即关闭**：数据 / Prompt / 探针 / 调用图四类漂移都是拒绝运行（rc=3 或异常），
   不是"记一笔继续跑"。
3. **信任根搬出模块**：锚点脚本零导入被测身份函数，独立重算并在仓外成链；内部身份被
   改成返回常量时，只有它能抓住（矩阵行 8）。
4. **G 真的走热**：负样本存在、G 进分子、与"应改却改错"按 `expected_kind` 互斥，
   `disjoint` 由分类算出。
5. **摘要确定**：四个全新进程（`PYTHONHASHSEED` 随机）得到同一对摘要；`frozenset` 换构造
   顺序同源，`tuple/list` 顺序被保留，`mapping` 键序无关；外部与内部逐符号一致。
6. **可复现基线**：三次独立运行九项指标一致，三条锚点成链，v2 历史未被覆盖。

---

## 6. 复现命令

```bash
cd /Users/lidequan/PythonProject/Zevorinth
PY=./.venv/bin/python

# 身份与冻结常量
$PY -c "import sys;sys.path.insert(0,'src');from zevorinth import code_edit_baseline as m;print(m.evaluator_identity()['sha256'],m.dependency_identity()['sha256'],m.source_tree_identity()['sha256'])"

# 外部复算 + 锚点链
$PY scripts/anchor_code_edit_evidence.py verify evals/results/live-deepseek-chat-v3-a.json
$PY scripts/anchor_code_edit_evidence.py audit

# 九项指标独立复算（不导入被测模块）
$PY scripts/run_code_edit_baseline.py --offline --out /tmp/off.json
$PY /tmp/zv3_recompute.py /tmp/off.json evals/results/live-deepseek-chat-v3-{a,b,c}.json

# 十二行反向变异矩阵
$PY scripts/code_edit_mutation_matrix.py

# 行 9/10/12 留存副本复现
$PY /tmp/zv3_row_replication.py

# 对抗检查（D1/D2/D3、A1、A2、A3）
$PY /tmp/zv3_adversarial.py

# 测试
$PY -m pytest tests/test_code_edit_baseline.py tests/test_code_edit_integrity.py -q
# → 53 passed, 36 subtests passed in 13.80s
```

留档目录：`/tmp/zv3-acceptance-kept/`（row09 / row10 / row12 / closure-specificity /
closure-drift / closure-removed / dataset-swap）、`/tmp/zv3-adversarial-store/`（伪造锚点）。

---

## 7. 裁决建议与边界

| 项 | 状态 |
| --- | --- |
| V3 实施 | `IMPLEMENTED_SELF_CHECKED`（保持） |
| V3 独立验收 | **`FAIL`**（本报告） |
| `V3.1_METRIC_INVARIANT_GUARD` | 候选项，**未启动**；F1 是其必做项 |
| F2 / F3 / F4 | 未登记为版本项，建议并入 V3.1 或另开 `V3.2_ANCHOR_COVERAGE_COMPLETION` |
| S3 | `NOT_APPROVED` |
| S4 | `NOT_STARTED` |
| 模型编辑权限扩大 | `未授权` |
| `PRODUCTION_TRUST` | `未提升` |

一句话总结：**V3 把"谁能改结论"从"改一个函数"抬到了"改一个函数 + 改依赖 + 改整棵树 + 改锚点"，
但还没堵住"改实验输入"（F2）、"缩小声称的覆盖范围"（F3）和"写出数学上不可能的比率"（F1）。
前两项让外部锚点看起来比它实际强，第三项让九项指标看起来比它实际可信。**
