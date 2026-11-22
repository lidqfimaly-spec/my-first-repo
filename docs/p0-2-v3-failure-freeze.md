# P0-2 · V3 失败基线冻结记录

`[FROZEN · 不得覆盖]` `2026-11-22 01:15 +0800`

本文件只做一件事：把 **V3 独立验收 = `FAIL`** 这件事连同其证据钉死，防止后续
V3.1 / V3.2 的修复结果把它改写成"V3 其实是过的"。

**冻结规则**（对后续所有工作生效）：

1. `docs/p0-2-v3-independent-acceptance.md` **不得修改、不得重写、不得删除**。
   如需订正事实，另建 `…-addendum-N.md`，并在本文件追加一行。
2. `docs/p0-2-code-edit-baseline.md`（v2 自检）、`docs/p0-2-independent-acceptance.md`
   （v2 验收 `FAIL`）、`docs/p0-2-code-edit-v3.md`（v3 实施）三份同样不得覆盖。
3. `evals/results/live-deepseek-chat-v1.1-*`（v2 序列 9 个）与
   `evals/results/live-deepseek-chat-v3-{a,b,c}.json` **不得修改**；新版本的运行另起文件名。
4. 任何新证据只能**追加**，不能替换。判断依据是下面第 3 节的摘要，不是"我记得没改"。

---

## 1. 冻结的结论

| 项 | 状态 |
| --- | --- |
| V3 实施 | `IMPLEMENTED_SELF_CHECKED` |
| **V3 独立验收** | **`FAIL`** |
| 九项检查 | 7 通过 / 2 不通过（#4 外部锚点、#7 九项指标复算） |
| 四项强化检查 | 2 通过 / 2 不通过（A 锚点独立性、D 分子>分母的外部处理） |
| V3.1 | `NOT_STARTED` |
| V3.2 | `NOT_STARTED` |
| S3 | `NOT_APPROVED` |
| 模型编辑权限 | 未扩大 |
| `PRODUCTION_TRUST` | 未提升 |

## 2. 冻结的缺陷（承重结论，逐条可复现）

| 编号 | 级别 | 断言 |
| --- | --- | --- |
| F1 | 阻断 | 外部流程接受并封存 `4/3 = 133.3%`、`8/7 = 114.3%`；封印自洽，`rc=0` |
| F2 | 阻断 | 锚点从不复算 dataset / prompt / replies 摘要；数据集 `835f2bb0…` 换成 `b490915f…` 后 `verify` 仍 `VERIFIED` |
| F3 | 严重 | `evaluator_coverage` 取自记录，48 符号缩到 1 仍被接受（`复算 2030be85… / 记录 2030be85…（1 符号，2937 字节）`） |
| F4 | 中 | 尾锚点无自摘要、`audit` 不重算 `combined`；`raw_reply` 不在结论封印内，事后替换仍 `VERIFIED` |
| F5 | 边界 | `co_names` 非完整调用图；源码树摘要变化只改身份、不阻止运行 |

一句话：**V3 把"谁能改结论"抬到了"改函数 + 改依赖 + 改整棵树 + 改锚点"，
但没堵住"改实验输入"（F2）、"缩小声称的覆盖范围"（F3）、"写出数学上不可能的比率"（F1）。**

## 3. 证据摘要（sha256，用于证明"没被改过"）

```
dd613dd0a3ab281a2487853a1c6ff2f3cab18a32a58ae3705682aa92b96eda23  docs/p0-2-v3-independent-acceptance.md
ffa9860d0b2828cca87b3241f23e04b9e5109f1c9da3f7196c2196c906e1c7d0  docs/p0-2-code-edit-v3.md
4e67de5d9e25636735026402c4c8021e480be0403f4d592d743f102314033c81  docs/p0-2-independent-acceptance.md
17614e1d8738a8db85d6fb47373044290e9311c8593a74e8dcb01a33779a4504  docs/p0-2-code-edit-baseline.md
dda8b9bee3842a76b8b99d6812e354e6180ab32ebb75a7218edcdb27967c6e8a  evals/results/live-deepseek-chat-v3-a.json
49b245988155220515ca78fb5792e9b38eed282fd7348ea8c5265334410c4b4f  evals/results/live-deepseek-chat-v3-b.json
7c86d00b7c2e2c9573002450681d68b676b555680fe4238d0307420162e506bf  evals/results/live-deepseek-chat-v3-c.json
fe2c367eecb945a3aeb76224b55005ffc3f951d6db49a69bc43301ba67512985  scripts/acceptance/v3_metric_recompute.py
8f1134328f88104ab9d858237d74269f270fc60ca98f687469a6cd4408510730  scripts/acceptance/v3_row_replication.py
fee962e032d977ca4e42b4323087d9f2b80fce1b498344813c4c84c3848a81ad  scripts/acceptance/v3_adversarial_checks.py
```

外部锚点（仓库外，`~/.zevorinth/evidence-anchors/code-edit/`）：

```
0001-52dac639007543d0a215582808357eb1.json  combined df04438baa99c44e…
0002-a972a26ad833444dad2e25adfffe2976.json  combined 75707d403c316355…
0003-8abcd939fe3347cea5ce67254201612c.json  combined 7df6a99ee5bc5412…
chain intact
```

## 4. 失败复现（三条命令，任何人在任何时间都能重放）

```bash
cd /Users/lidequan/PythonProject/Zevorinth
PY=./.venv/bin/python

# F1：外部流程接受 4/3 与 8/7
$PY scripts/acceptance/v3_adversarial_checks.py        # 见 D-1 / D-2 段

# F2：改磁盘数据集后 verify 仍通过
#     同上脚本 A-1 段（内部会复制一份树到 /tmp/zv3-acceptance-kept/dataset-swap）
$PY scripts/acceptance/v3_adversarial_checks.py

# F3：48 符号缩到 1 仍被接受
#     同上脚本 D-3 段
```

`v3_metric_recompute.py`（九项指标独立复算）、`v3_row_replication.py`（行 9/10/12
留存副本复现）同目录，均**不属于被评对象**：源码树身份只扫 `src/zevorinth/`，
`scripts/acceptance/` 不在其内，故这三份脚本的存在不会改变任何实验身份。

## 5. 与后续版本的关系

- V3.1 / V3.2 **通过各自独立验收**，只意味着"F1/F2/F3/F4 被修好"，
  **不意味着 V3 的验收结论变成 PASS**。历史结论按发生时的证据成立。
- 若 V3.1/V3.2 的实现推翻本文件第 2 节的任何一条（例如证明 F2 其实不存在），
  必须**追加**订正说明并给出反向证据，不得删除原结论。
- 本文件的摘要本身也会在 V3.1 立项时被重新记录一次；届时若有差异，
  默认认为"文件被改过"，而不是"摘要记错了"。

---

## 附录 A（2026-11-22 01:2x 追加）：被评实现的终态摘要

以下摘要钉住"产生 FAIL 这一结论的那一版实现"。V3.1 一旦开工，这些值**必然**变化，
届时不得用新值回填本附录——新值只能写进 V3.1 的实施记录。

```
c71844a8308c600e0c80bb10c555d4171896f7051b4dfb74df647ac5ad864816  src/zevorinth/code_edit_baseline.py
56f7c5e48ebee8d574d8e665e00fd0d5de94d7d3dc16c66d1684edf99dcc30bc  scripts/anchor_code_edit_evidence.py
000dfe560ca162d824c600e903e6f3cb073aae6f2192a1dee2b262440e273f97  scripts/code_edit_mutation_matrix.py
503171f8788d6937909ca47ebb698d84efcf2e02239b5eb060499188080167e8  scripts/run_code_edit_baseline.py
835f2bb0610ca85a538779f15c74b50e177644ac3fee6931a8eff7165cb4193e  src/zevorinth/evals/code_edit_baseline_v1.json
0a0fc197de1f351b7ccd7cf43dea11cf56fcf95b3b08ca5290eb5d13aaf90155  src/zevorinth/evals/code_edit_baseline_v1_replies.json
f23491225e541026a492b84b655397fe6bbd9dfea5015436dc9278468f9bcc29  tests/test_code_edit_integrity.py
bcde204e9b83bc92219529fbf0efc60ebf7102d277a8014e311b2525c3c5c3cc  tests/test_code_edit_baseline.py
```

数据集与脚本候选集的摘要与 `FROZEN_DATASET_SHA256` / `FROZEN_REPLIES_SHA256` 一致，
表示"冻结常量 ↔ 磁盘文件"在这一刻是自洽的。
