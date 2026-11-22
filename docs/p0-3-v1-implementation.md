# P0-3 · V1 实施（S-A / S-B / S-C）

`[IMPLEMENTED_SELF_CHECKED · 2026-11-22 · 未经独立验收]`

> 授权来源：负责人「全面实施」。按 `P0_NEXT_PHASE_ROADMAP.md` 的 V1 范围执行，
> **不包含** `NOT_RECOMMENDED` 项（多 Agent、积分、伪暂停按钮）。
> 本文只记录已实施并自测的部分；S-D / S-E 未实施，原因见 §7。

---

## 1. 五项待决策（D-1…D-5）的默认取值

「全面实施」不等于五项决策自动有了答案。为避免停摆，先取**可逆默认值**，逐项可被推翻：

| # | 决策 | 本次取值 | 可逆性 |
| --- | --- | --- | --- |
| D-1 | 理解层用本地模型还是远端 API | **都不接**：先做确定性抽取，`extract_goal()` 作为将来插模型的位置 | 高。接口不变，替换实现即可 |
| D-2 | V1 是否含联网检索 | **不含**。`research_report` 仍只处理本地材料，不宣称"研究" | 高。放开需新增网络出口，属安全边界变更 |
| D-3 | 是否接受单步暂不可暂停 | **接受**。只做"请求取消"，在下一个安全点停 | 高 |
| D-4 | `PARTIAL` 是否改为可续 | **保留终态**。续做留给 S-D，本次不碰状态语义 | 中 |
| D-5 | 是否公开任务成功率 | **不公开**。缺用户确认点，M-01/M-08 仍不可测 | 高 |

D-1 值得展开：不接模型不是保守，而是因为**模型不是当前的瓶颈**。
J1 的失败原因是"目标文本没人读"，不是"读得不够聪明"。规则能覆盖的 10 条典型目标里，
7 条可一次执行完成、10 条都不再要求用户输入内部字段名（§5）。先把这个基线钉住，
将来换模型时才有可比较的对照。

---

## 2. 改动清单

### 2.1 新增模块（3 个）

| 文件 | 职责 | 关键约束 |
| --- | --- | --- |
| `src/zevorinth/task_executor.py` | 后台执行器：2 worker、队列 32、按 `task_id` 去重 | **只搬驱动，不搬引擎**。仍调用 `execute_task(require_claim=True)`；不自动续跑审批/澄清/`PARTIAL`；执行异常只记日志，**不发明状态迁移** |
| `src/zevorinth/progress_text.py` | 事件序列 → 用户语言 | 纯读。**拒绝百分比**（引擎没有"总量"概念，百分比是编造的数字）；单步任务无 `STEP_*` 事件时退回 `TOOL_*` 记账 |
| `src/zevorinth/intent_extraction.py` | 目标文本 → 结构化请求 | 零网络、零依赖；只填句子里真有的东西，猜不到就留空并转成澄清 |

### 2.2 修改（6 处）

| 文件 | 改动 | 为什么不能更少 |
| --- | --- | --- |
| `workbench_server.py` | 新增 `dispatch` / `cancel-request` / `progress` 三个端点 + `?download=1` 分支；`server_close()` 关执行器 | 复用既有 `start_queued_task` 会在 `_action_lock` 里同步执行，正是要消除的阻塞 |
| `workbench.py` | `task_progress()`、`artifact_download()`、`_content_type_for()` | 进度与下载都是只读投影，属于该层职责 |
| `event_store.py` | `append_assistant_message()`、`latest_task_for_conversation()` | 表结构本来就允许 `role='assistant'`，只是从没写过（R-05） |
| `conversation_service.py` | 抽取接入、答案归一化与拒绝、用户语言提问、系统回复 | 澄清问题的产生点就在这里，改别处无效 |
| `workbench_static/app.js` | 改走 `dispatch`（失败回落同步）、进度面板、下载按钮 | 后端能力不接到界面上，用户仍然拿不到 |
| `workbench_static/{styles.css,i18n.zh-CN.json}` | 进度样式 + 4 条界面文案 + 8 条错误文案 | 缺错误文案会被 `test_static_assets` 直接抓红 |

**没有改**：`service.py`、`state_machine.py`、`policy.py`、`planner.py`、`verifier.py`。
引擎的状态机、租约、围栏、验证一条都没动——这是本轮最重要的边界。

---

## 3. 三个只有写代码才会暴露的事实

1. **策略拒绝不是 `FAILED`，是 `BLOCKED`。** 实测「写入 `../escape.txt`」得到
   `status=blocked` + `error.code=invalid_artifact_path`，任务仍可在修正后继续。
   进度层原本只处理终态，会把这种情况显示成"失败/被阻塞"。已改为
   「被执行前的安全检查拦下：文件名不能用相对路径或隐藏文件名」。
2. **内容写入上限 65536 字节，恰好等于预览上限。** 所以 F-12 的「>64 KiB 也能下载」
   在产品里**造不出来** → 该条端到端 `NOT_TESTABLE`。下载端点本身没有长度判断（代码路径确认），
   但"用测试证明"这件事做不到，只能如实标注。
3. **单步任务不发 `STEP_*` 事件。** 只发 `TOOL_STARTED` / `TOOL_SUCCEEDED`。
   进度层若只按 `STEP_*` 记账，每个单步任务都会永远显示 `0/1`——第一次跑测试就撞上了。

---

## 4. 权限与安全边界：没有一处被放宽

| 边界 | 状态 |
| --- | --- |
| `SUPPORTED_TASK_OPERATIONS`（8 个） | **未变**。没有新增 `execute_plan` |
| `CONTROL_STARTABLE_OPERATIONS` | **未变**。`run_tests` 仍不可从控制面启动 |
| loopback 绑定 / Origin + 进程内 token | **未变**。新端点复用同一套 `_validate_control_request` |
| 审批必须绑定内容 | **未变**（R-08 仍未修，见 §7） |
| 模型无工具/网络/数据库能力 | **未变**。本轮没有引入任何模型 |

---

## 5. 验证

### 5.1 新测试：34 个

| 文件 | 数量 | 覆盖 |
| --- | --- | --- |
| `tests/test_task_executor.py` | 16 | 异步完成、并行、重复提交不重放、队列满不丢任务、幂等关停、进度文本诚实性、三个端点 |
| `tests/test_goal_understanding.py` | 13 | 10 条验收目标、单条目标抽取、乱答拒绝、会话有系统回复、状态问答 |
| `tests/test_artifact_download.py` | 5 | 字节一致、附件头、登记后改动被拒、无产物 404、65536 边界 |

### 5.2 受影响范围：**248 passed / 64 subtests / 0 failed**

```
tests/test_workbench.py  test_workbench_control.py  test_conversation_service.py
test_conversation_store.py  test_conversation_cli.py  test_conversation_schema.py
test_clarification_workflow.py  test_task_conversation.py  test_goal_understanding.py
test_task_executor.py  test_artifact_download.py  test_static_assets.py
test_context_continuity.py  test_intent.py  test_durable_intent_and_isolation.py
test_code_task_workflow.py
→ 248 passed, 64 subtests passed
```

### 5.3 真机端到端（`control-center --port 8793 --home /tmp/zv-sb-home`）

一句人话，四个环节全部打通：

```
say:        200 completed | task status: succeeded
conversation:
   user      | 把这段内容保存成 note.txt：今天下午三点在 3 楼会议室开会
   assistant | 已完成（1/1 步）
progress:   已完成 (1/1)
download:   200 | attachment; filename="note.txt" | 43 bytes | 今天下午三点在 3 楼会议室开会
```

派发耗时 **5 ms**（对比同步执行：请求生命周期 = 任务生命周期）。

J9（"问进度"）修复实测：

```
status query outcome: status
last assistant: 已完成（1/1 步）        ← 修复前答"这个会话里现在没有正在进行的任务"
clarify:  awaiting_clarification [('operation','你想让我做什么？')]
bad answer: invalid_answer | 我没听懂这个动作：「？？？」。可以选：写一个新文件、读取一个文件的内容、…
```

### 5.4 十条验收目标的口径

| 口径 | 结果 |
| --- | --- |
| 不要求用户提供**内部字段名** | **10 / 10** |
| 一次执行完成（无需任何追问） | **7 / 10** |

差的 3 条是真实缺信息，不是抽取能力问题：
「把内容存成 x.txt」没说内容、「改 calc.py 的 bug」没说替换什么、
「末尾追加」引擎没有 append 语义。把这三条凑成 8 或 10 只能靠猜，所以不凑。

### 5.5 全量测试为什么不是绿的（诚实说明）

`pytest tests/` 全量：**1300 passed / 410 failed**。逐类抽验后，落在**本机环境**：

| 类别 | 数量 | 证据 |
| --- | --- | --- |
| `os.supports_dir_fd` 不支持 | 332 | `os.open in os.supports_dir_fd == False`（Python 3.14.6）；错误在 `_open_flags()` 里抛出，早于任何业务代码 |
| 沙箱注入 `sitecustomize` 干扰 | 55 | `FileExistsError: mkdir .../.locks` 栈顶为 `sitecustomize.py:617` |
| 变异测试 tracing 不可用 | 5 | `untraced_survival` 未被记录 → 追踪未生效 |
| 嵌套 `sandbox-exec` 被拒 | 1 | `sandbox-exec: sandbox_apply: Operation not permitted` |
| 无容器后端 | 1 | 容器进程用例 |

**无法给出"改动前的基线失败数"**：上述用例在本环境无论代码如何都会失败，
它们不是被我改红的，也没有被我改绿。可核验的替代证据是 §5.2 的范围内全绿，
以及 §5.5 每条失败的原因都指向解释器/沙箱能力，而非 §2 列出的任何文件。

---

## 6. 已知风险

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 后台执行器与内联执行并存 | 两条驱动路径都调用 `execute_task` | 引擎 claim 保证同一任务不会被跑两次；重复提交实测返回 `duplicate` |
| 执行器进程重启丢队列 | 队列在内存 | 任务本身仍在库里（`queued`），重启后可再次 dispatch；未做自动恢复 |
| 规则抽取覆盖面有限 | 只认枚举过的措辞 | 认不出就走澄清，不会瞎猜；`extract_goal` 是将来接模型的替换点 |
| 进度是快照不是推送 | 详情页每次渲染取一次 | SSE 事件流本就存在，未合并；记为待办 |

---

## 7. 未实施（附原因，不是遗漏）

| 项 | 原因 |
| --- | --- |
| **S-D 多步可启动（F-09）** | `execute_plan` 不在 `SUPPORTED_TASK_OPERATIONS` 内。放开它 = **扩权**：用户可提交任意工具序列。这属于权限边界变更，需负责人明确同意，不应夹在产品改造里顺手做 |
| **S-D `run_tests` 可启动** | 被 `CONTROL_STARTABLE_OPERATIONS` 显式排除，同样属权限边界 |
| **F-11 审批表单** | **第二轮已实施**，见 §9。前置 R-08 先修完才做表单 |
| **S-E 记忆进决策回路** | 依赖 S-D 之后的规划链路 |
| 联网检索（D-2） | 新增网络出口，安全边界变更 |

---

## 8. 状态

`IMPLEMENTED_SELF_CHECKED`（实施方自测）。**不是**"验收通过"：
独立验收仍需另一人或另一轮独立执行，按 §5 的命令复跑。

未提交（`git` 零操作）：本轮所有改动留在工作区，未 stage / commit / push。
V3.1 独立验收仍为 `NOT_STARTED`；V3.2 仍 `NOT_AUTHORIZED`；S3 未批准；`PRODUCTION_TRUST` 未提升。

---

## 9. 第二轮：S-D 前半 + S-C 收尾（2026-11-22）

授权仍是负责人「全面实施 / 全面推进」。本轮**仍然没有碰权限边界**：
`SUPPORTED_TASK_OPERATIONS` 还是 8 个（无 `execute_plan`），
`CONTROL_STARTABLE_OPERATIONS` 仍排除 `run_tests`，loopback / Origin / token 未变，
未引入任何模型，未新增非登记能力。

### 9.1 改动清单

| 文件 | 性质 | 内容 |
| --- | --- | --- |
| `src/zevorinth/approval_preview.py` | NEW | 审批表单数据面（F-11）：工具人话名、完整参数（脱敏 + 有界）、副作用、目标文件当前状态、内容摘要与"是否变过" |
| `src/zevorinth/workbench.py` | MODIFIED | `_load_task()` 抽出（`task_detail` 改为复用）；新增 `approval_form()`、`_lineage_projection()`、`_continuation_projection()`、`_derived_tasks()`；`task_detail` 新增 `lineage` / `continuation` / `derived_tasks` |
| `src/zevorinth/workbench_control.py` | MODIFIED | `approve_step` / `reject_step` 两个 action 携带 `approval_form`（同一次读取，两个按钮看到同一份事实） |
| `src/zevorinth/workbench_static/app.js` | MODIFIED | 审批表单渲染（含 `approval_content_changed` 警示、延迟参数警示、摘要不全时**禁用批准**）；血缘卡（上游 + 下游）；"在这个产物上继续改"表单 |
| `src/zevorinth/workbench_static/i18n.zh-CN.json` | MODIFIED | `ui` 326 → 363（+37 条） |
| `src/zevorinth/workbench_static/styles.css` | MODIFIED | `.approval-*` / `.lineage` / `.continuation` |
| `tests/test_approval_form.py` | NEW（13） | 表单事实、目标状态、延迟参数、批准状态、脱敏、只读性、控制面携带 |
| `tests/test_artifact_continuation.py` | NEW（10） | 血缘投影、继续入口的可用/不可用、HTTP 端到端继续、过期摘要被拒、下游链 |

R-08 的机制级修复（审批绑内容摘要）在本轮之前完成，`tests/test_approval_binding.py` 8 个用例，
见 §9.4。

### 9.2 四个只有写代码才会暴露的事实

1. **审批可能批准的是"模板"，不是内容。** 步骤参数可以是 `${s1.content}`，批准时上一步还没跑，
   具体值不存在。摘要绑的是模板，所以"批准"实际含义是"批准这条取值规则"。
   表单因此逐参数标注 `deferred`，并在参数里存在引用时显式写出来 —— 不假装那是内容。
2. **"同一文件名"不等于"版本"。** 路线图里 F-13 的前提（"同 name 多写 = 多版本"）站不住：
   两个互不相关的任务各写一个 `note.txt`，它们之间没有任何版本关系。
   唯一诚实的"后来版本"是**记录里声明了 source 的下游任务**。故未做按文件名聚合的版本列表
   （那会造出与 lineage 并行的第二个真相），改为 `derived_tasks` 反查。
   `tests/test_artifact_continuation.py::test_an_unrelated_task_writing_the_same_name_is_not_downstream`
   把这个判断钉住了。
3. **"继续改"必须和引擎的前置条件对齐。** `_resolve_source_artifact()` 要求源任务 `SUCCEEDED`
   + `verification.passed` + 字节未变。表单若在这些条件不成立时仍然发出去，用户会拿到一个
   必然被拒的任务 —— 那不是便利，是骗。所以 `continuation.available` 逐条对齐并给出 `reason`，
   前端把 `reason` 翻成人话（`no_artifact` / `task_not_succeeded` / `artifact_not_verified` /
   `artifact_changed_since_registration`）。
4. **看得到才敢批，看不到就不该批。** 表单读不出来时（例如服务端投影异常），前端**禁用"批准"**，
   只保留"拒绝"。这是故意 fail-closed：宁可让人拒绝，也不给一个看不见内容的批准按钮。

### 9.3 真机验证（`control-center --port 8794/8795/8796 --home /tmp/zv-f11-home`）

浏览器（WorkBuddy 内置）实操，不是只看接口：

| 步骤 | 观察到的结果 |
| --- | --- |
| 造一个 2 步计划（第 2 步 `requires_approval`） | 任务停在 `awaiting_approval`，列表显示「下一步骤需要批准」 |
| 打开受控操作面板 | 审批表单显示：`写一个新文件` / `这步要达成的结果：再写一版修订稿` / `会新建一个文件，不会改动已有文件。` / `artifact_path=revised.txt` / `content=第二版修订内容，请核对` / `这步会新建这个文件，现在它还不存在。` / `这次要执行的内容摘要：dccc9126e346f89e…` |
| 未勾选确认时 | 「批准并继续」按钮 `disabled=true` |
| 勾选后批准 | 任务 `succeeded`，产物 `revised.txt` 34 字节，验证 `multi-step-aggregate-v1` 通过，出现「下载产物」 |
| 同一产物卡片的「在这个产物上继续改」 | 方式下拉（整体替换/在原文后追加）、新文件名已预填 `revised-v2.txt`、只需输入内容；提交后新任务自动创建并后台执行 |
| 新任务的产物卡 | 「这个产物从哪来」：源任务 `28c916c2…`、源文件 `revised.txt`、改动方式、源文件摘要、本次结果摘要；「基于这个产物做的后续任务」在该任务上为空并如实说明 |
| 回到源任务看下游 | 「基于这个产物做的后续任务 / revised-v2.txt · 已完成 / 打开」，点「打开」正确跳到下游任务 |

产物落盘核对：`/tmp/zv-f11-home/artifacts/0949e062-…/revised-v2.txt` 内容与输入逐字一致（37 字节）。

### 9.4 验证数字

| 范围 | 结果 |
| --- | --- |
| `tests/test_approval_form.py` | 13 passed |
| `tests/test_artifact_continuation.py` | 10 passed |
| `tests/test_approval_binding.py`（R-08） | 8 passed |
| 受影响批次（10 个文件：workbench / workbench_control / artifact_* / approval_* / multi_step / static_assets / task_executor / goal_understanding） | **212 passed / 51 subtests / 0 failed** |
| `tests/test_static_assets.py` | 4 passed（`node --check` + i18n 覆盖 + 无 `innerHTML`） |

全量测试仍非全绿，原因与 §5.5 相同（解释器缺 `os.supports_dir_fd`、`sitecustomize` 干扰、
tracing 不可用、嵌套 `sandbox-exec` 被拒、无容器后端）—— 这些是**本机环境事实**，
与本轮改动无关，也没有被本轮改动放大。

### 9.5 本轮仍未做（附原因）

| 项 | 原因 |
| --- | --- |
| 放开 `execute_plan` 启动 | 仍是权限边界变更，见 §7。**当前多步计划只能由 service API 造出**（`submit_plan` / `submit_code_task`），产品面上没有入口，因此本轮的审批表单真机可达路径是「程序造计划 → 界面审批」 |
| 按文件名聚合的"版本列表" | 见 §9.2 第 2 条：会造成第二真相。若确有需要，应作为"同名产物查询"并显式声明它们互不相关 |
| 版本间文本 diff | 需要一个真正的行级 diff 实现；先把"谁基于谁"讲清楚，再谈展示差异 |
| SSE 推送替代进度快照 | 与上一轮同：事件流已存在，未合并 |

### 9.6 新增风险

| 编号 | 风险 | 现状 |
| --- | --- | --- |
| R-24 | 审批可覆盖"延迟取值"的步骤：批准的是模板，实际写入值由前序步骤产出决定 | 表单已显式标注并给出提示文案；未做"批准时预演取值"（前序未跑，做不到）。若将来要求"只能批准具体内容"，需要在计划层禁止跨步骤引用进入待批准参数 |
| R-25 | 待批准步骤的摘要与目标文件摘要展示依赖只读投影；投影异常时表单不可读 | 前端已 fail-closed（禁用批准，保留拒绝），不会退化成盲批 |

### 9.7 本轮状态

`IMPLEMENTED_SELF_CHECKED`（实施方自测 + 真机浏览器实操）。**不是**"验收通过"：
独立验收仍需另一轮独立执行。`git` 零操作（未 stage / commit / push）。
V3.1 独立验收 `NOT_STARTED`；V3.2 `NOT_AUTHORIZED`；S3 未批准；`PRODUCTION_TRUST` 未提升。
