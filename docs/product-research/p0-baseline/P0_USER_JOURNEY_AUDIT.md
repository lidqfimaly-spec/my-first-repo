# P0 · 真实用户旅程审查

`[P0_USER_JOURNEY_AUDIT · 2026-11-22 · 真实运行]`

> **运行方式**：真实启动 `control-center --port 8791 --home /tmp/zv-audit-home`，
> 用 `urllib` 打真实 HTTP（与前端 `app.js` 完全相同端点与请求体），另用 `TaskService`
> 直测产品不可达的引擎路径并**单独标注**。
> **不是**单元测试、不是 mock、不是"读代码推测"。
> 数据全部落在 `/tmp/zv-audit-home`，项目内零写入。

---

## 0. 判定汇总

| ID | 场景 | 结论 | 一句话原因 |
| --- | --- | --- | --- |
| J1 | 首次进入 + 简单目标 | **FAIL** | 说"把内容存成 note.txt"，被要求提供 `operation` / `artifact_path` / `content` 三个内部字段 |
| J2 | 多步骤复杂目标 | **FAIL**（产品） / `PARTIAL`（引擎） | 引擎能跑，但产品面无法启动 `execute_plan` |
| J3 | 执行中追加新要求 | **FAIL** | 被当成澄清答案或"修改待办任务"，无"追加要求"语义 |
| J4 | 修改已有计划 | `PARTIAL` | 有 `replan_pending_steps`，但只在多步任务上；且审批可被绕过 |
| J5 | 暂停 / 恢复 / 取消 | **FAIL** | 运行中不可取消；单步不可暂停；只有多步步边界生效 |
| J6 | 工具执行失败 | **PASS**（引擎） / `PARTIAL`（产品） | 失败安全且错误码准确，但用户只能看到代码级文案，且无"改一下再试"入口 |
| J7 | 部分成功 | `PARTIAL` | 引擎正确判 `partial`；产品层无入口触达，且 `partial` 不可续做 |
| J8 | 检查 / 修改 / 重新交付成果 | **FAIL** | 能预览 64 KiB 文本，无下载、无版本、无"基于此继续" |
| J9 | 想知道 Agent 正在做什么 | **FAIL** | 问"现在任务进行到哪了？"被判成"澄清答案"；只有精确输入"进度"/"状态"才行 |
| J10 | 知道哪些操作需要授权 | **PARTIAL** | 多步审批有 UI；但 host-test 授权只在 CLI，用户无从得知 |

`PASS` 1（且仅引擎级）· `PARTIAL` 4 · `FAIL` 5。

---

## J1 · 首次进入 + 简单目标 —— `FAIL`

**用户输入**（真实）：`把这段内容保存成 note.txt：今天下午三点在 3 楼会议室开会`

**系统响应**（真实 JSON，节选）：

```
outcome: awaiting_clarification
missing_fields: ["operation", "artifact_path", "content"]
clarifications: ["请提供 operation", "请提供 artifact_path", "请提供 content"]
task.operation: null
```

**发生了什么**：
1. 意图分类为 `new_task`（`reason: "no active task in this conversation"`）。
2. 确定性规划器只看 `request.operation`，为 `None` → 三个字段全缺 → 生成 3 条澄清。
3. 目标文本里的 `note.txt` 与冒号后的正文**完全没有被解析**——它们就在 `goal` 字段里，但没有代码读它。

**用户按界面提示回答 `写文件`**（真实）：
```
outcome: awaiting_clarification | missing: ["artifact_path","content"]
task.operation: "写文件"      ← 非法值被原样写入
```
无任何枚举校验。这个任务后续一定在 policy 阶段失败（`unsupported_operation`），但用户不会知道原因。

**用户输入 `你好`**（真实，新会话）：
```
intent: new_task | outcome: awaiting_clarification
questions: ["请提供 operation","请提供 artifact_path","请提供 content"]
```
→ 一句问候被变成一个待办任务，并索要三个内部字段名。

**能不能完成？** 能，但要 4 轮，且用户必须知道：枚举值 `write_text_artifact`、参数名 `artifact_path`、并把正文再输入一遍。

```
turn1 把这段内容保存成 note2.txt：下午三点开会  → awaiting_clarification
turn2 write_text_artifact                      → awaiting_clarification
turn3 note2.txt                                → awaiting_clarification
turn4 下午三点开会                              → completed / succeeded
产物 note2.txt 18 B  sha256 f9072e86…  verification.passed=true
```

**会话记录实际内容**（`GET .../messages`）：
```
[('user','把这段内容保存成 note2.txt：下午三点开'),
 ('user','write_text_artifact'),
 ('user','note2.txt'),
 ('user','下午三点开会')]
```
**只有用户消息。系统一句话都没说过。** 澄清问题不在会话里，刷新后用户看到的是自己对自己说了四句话。

**用户体验影响**：这是首次进入的默认路径。一个正常用户在这里 100% 卡住，且不知道自己被要求的是什么。

---

## J2 · 多步骤复杂目标 —— 产品 `FAIL` / 引擎 `PARTIAL`

**产品路径**（真实）：`POST /control/api/tasks` 建 `execute_plan` 三步计划 → 成功创建（`queued`）。
```
GET .../actions → [revise_unstarted_task, cancel_queued_task, pause_task, add_task_note]
                   ← 没有 start_queued_task
POST .../actions {action: start_queued_task}
  → {"error":{"code":"task_start_not_allowed","message":"only an unstarted supported deterministic task can be started"}}
```
任务永远停在 `queued`。用户能看到"排队中"，等到天荒地老。

**引擎路径**（`TaskService` 直测，产品不可达，仅证明引擎有效）：
```
submit(execute_plan, 3 步, fail_fast) → awaiting_approval（s2 要求审批）
approve_step(s2)                      → partial
step_results: s1 succeeded / s2 succeeded / s3 failed
error: multi_step_partial
```
产物 `a.txt` 在 `artifacts/<child_s1>/`，`b.txt` 在 `artifacts/<child_s2>/`，父任务目录为空。

**结论**：多步能力**存在于引擎、缺席于产品**。这是"复杂任务"这条产品承诺最直接的断点。

---

## J3 · 执行中追加新要求 —— `FAIL`

**真实**（同一会话，存在 `awaiting_clarification` 的任务）：
```
输入：现在任务进行到哪了？
→ intent: clarification_answer  reason: "message is treated as an answer to open clarification"
→ outcome: needs_question_binding
```
问进度被当成"回答澄清"，因为 `_STATUS_QUERIES` 是**精确字符串集合**：`{"status","状态","进度","现在怎么样","完成了吗"}`（`intent.py:15-16`）。

```
输入：帮我之前那个 note2.txt 改成 note3.txt
→ intent: modify_pending  outcome: requires_confirmation
```
有"修改待办"意图，但它作用在"当前未开始的任务"上，不认 `note2.txt`（无产物记忆）；且系统不会告诉用户"我准备怎么改"。

**执行期间追加**：执行是同步的（阻塞在请求线程），期间浏览器只能等；没有"边跑边插话"的通道。对比 Cursor 的做法（注记里 follow-up 排队到下一个 tool call），这里连通道都没有。

---

## J4 · 修改已有计划 —— `PARTIAL`

- 有 `replan_pending_steps` 动作，仅在多步任务 `awaiting_approval`/`paused` 时出现（`workbench_control.py`）。
- 真实（引擎级）：`replan` 只允许替换**未完成**步骤（`service.py:981-993`）。
- **缺口**：`approved_step_ids` 从不清空，replan 后同 `step_id` 的新步骤不再要求审批（`service.py:1026-1055` vs `3340`）。也就是说"改了计划但审批还留着"。
- 产品层：审批 UI 只显示 `step_id` + `tool_name` + `expected_result` 文本，**没有参数、没有 diff、没有风险说明**——用户无法判断自己在批准什么。

---

## J5 · 暂停 / 恢复 / 取消 —— `FAIL`

| 用户想做的 | 实际 |
| --- | --- |
| 暂停一个正在跑的写文件任务 | 动作面不提供 `pause_task`（只对 `execute_plan` 提供，`workbench_control.py:213,273-297`）；即使提供，引擎对 RUNNING 单步任务只写 `pause_requested_at`，工具不会停 |
| 取消正在跑的任务 | 无 HTTP 路由；引擎 `cancel_task` 对活跃租约直接抛错（`event_store.py:2163-2166`） |
| 取消排队中的任务 | 可以（`cancel_queued_task`，实跑可见） |
| 恢复 | `resume_task` 存在；但有已持久化证据时只重做验证，无证据时整步重跑——用户拿到的是"又跑了一遍"，不是"接着跑" |

唯一可被真正打断的工具是 `run_tests`（进程级 kill），而它又无法从产品启动。

---

## J6 · 工具执行失败 —— 引擎 `PASS` / 产品 `PARTIAL`

真实两例（均为"用户操作失误"的典型）：

```
case A: old_text 写错
→ task_status: blocked | error: {"code":"text_match_count","message":"old_text must match exactly once"}
→ events: … task_planned → policy_rejected → control_action_completed

case B: 文件已被别人改过（expected_sha256 过期）
→ task_status: blocked | error: {"code":"source_file_changed","message":"source file digest does not match expected_sha256"}
→ events: … task_planned → policy_rejected
```

**好**：两次都在 policy 阶段被拦，工作区文件**一个字节都没动**（`grep` 事件止于 `policy_rejected`，无 `tool_started`）。这是本系统最扎实的部分。

**不好**：
1. 状态是 `blocked`，而 `BLOCKED` 的出边是 `QUEUED/AWAITING_CLARIFICATION/FAILED/CANCELLED`——**不能直接重试**，用户必须先"修订"再重新提交。
2. 错误文案是开发者语言（`old_text must match exactly once`），没有"你要改的那句话现在不存在，是不是文件已经变了？"这类可执行提示。
3. 没有"按当前文件重新算摘要"的一键修复。

---

## J7 · 部分成功 —— `PARTIAL`

引擎（真实）：`partial` + `multi_step_partial` + 逐步骤状态，语义正确。
产品：
- 无入口触达（J2）；
- `PARTIAL` 是终态、不可重试（`retry_policy.py:147-153`），也不可"只补做失败那一步"；
- 已成功的产物在子任务目录里，父任务页面上看不到路径。

---

## J8 · 检查 / 修改 / 重新交付成果 —— `FAIL`

| 能力 | 实际 |
| --- | --- |
| 查看内容 | `GET /api/tasks/{id}/artifact` 返回 JSON 预览，**上限 64 KiB**，超出 413 |
| 下载 | 无端点、无 `Content-Disposition`，前端 0 处 `Blob`/`download` |
| 版本 | 无版本列表；每次 `modify_text_artifact` 生成**新任务+新文件**，lineage 记在 evidence 里但 UI 不展示 |
| 血缘 | `source_task_id` / `source_sha256` 在数据结构里有，UI 无 |
| 基于此继续 | 必须在高级表单里手填 `source_task_id` + `artifact_path` + `expected_sha256`（后者得自己去算） |
| 校验状态 | 页面显示 `verified / unverified / modified_since_registration`，这一点做得好 |

`FACT`：用户拿到 42 字节的 `note.txt` 后，想"再改一个字"，需要自己去 `shasum` 拿摘要填进表单。这就是当前"成果交付"的完整形态。

---

## J9 · 想知道 Agent 正在做什么 —— `FAIL`

| 输入 | 真实结果 |
| --- | --- |
| `现在任务进行到哪了？` | `clarification_answer` / `needs_question_binding` |
| `进度` | `status_query` / `status` ✅ |
| `状态` | `status_query` / `status` ✅ |

也就是说：**只有背下这五个词之一才能查进度**。

即使查到了，返回的也是状态枚举 + 事件类型列表；事件 payload 被投影成 `{"count":3,"type":"list"}` 这类形状（`workbench.py:1048-1056`、`1145-1182`），用户看不到"正在写第 2 个文件"这种句子。

副作用：`GET /api/tasks/{id}` 一次性返回**全部事件**（无分页，`workbench.py:224-233`），长任务会越来越慢；前端整块重渲染。

---

## J10 · 知道哪些操作需要授权 —— `PARTIAL`

- 多步审批：任务详情里有 `attention: {kind:"clarification"|...}`，动作面板给 `approve_step`/`reject_step`，但**不展示参数**（见 J4）。
- `host-test` 授权：只存在于 CLI（`authorize-host-test` / `revoke-host-test` / `host-grants`），产品面 grep `grant` = 0。用户在产品里永远看不到"这次要动你的宿主机"。
- `GET /control/api/session` 自述 `"authentication":"none"`，并以同一个响应把 token 发出来——本机信任边界的如实声明，但对用户没有任何解释。

---

## 11. 十条旅程的共同根因（不是十个独立问题）

1. **产品层没有理解层**：目标文本进入 `TaskRequest.goal` 后无人读取；理解退化为"字段是否齐全"。
2. **输入被设计成表单**：`operation`/`artifact_path`/`content` 是内部参数名，直接暴露给用户。
3. **会话不是对话**：只存用户消息、不产生系统回复、澄清问题不落会话。
4. **能力与入口不匹配**：引擎会多步、会审批、会 partial，产品只放行单步且不含 `run_tests`/`execute_plan`。
5. **执行是同步阻塞**：导致"边跑边看/边跑边改"在架构上无处安放。

`INFERENCE`：把 J1、J2、J5 修好（理解层 + 异步执行 + 能力对齐），J3/J7/J9 会跟着好转；反之逐条修 UI 不会改变任何结论。

---

## 11.1 旅程 → 风险的交叉映射（供自审与验收方核对）

> 用途：确保"旅程失败"不是孤立叙事，每条都能落到 `P0_GAP_AND_RISK_REGISTER.md` 的编号上；
> 反向也成立——只修一个风险，应该能说清它让哪几条旅程从 FAIL/PARTIAL 前进。

| 旅程 | 结论 | 对应风险编号 | 若只修这一条风险，能改善的旅程 |
| --- | --- | --- | --- |
| J1 首次进入 | FAIL | R-01、R-18、R-05 | R-01 → J1/J3/J9；R-18 → J1 的后半段；R-05 → J1 的"看起来像对话" |
| J2 多步骤 | FAIL | R-02 | J2 直接转 PASS；J7 获得产品入口 |
| J3 追加要求 | FAIL | R-03、R-05、R-06 | R-03 → J3/J5；R-06 → J3 的多轮纠错 |
| J4 修改计划 | PARTIAL | R-08 | 只提升安全性，不改变 PARTIAL（编辑器属 F-10，已 DEFERRED） |
| J5 暂停/恢复/取消 | FAIL | R-04、R-03 | R-04 → J5 的"请求取消"部分；单步暂停仍不可用 |
| J6 工具失败 | PASS（引擎） | R-19、R-20 | R-20 → J6 从 PARTIAL 转 PASS（产品侧）；R-19 → J7 |
| J7 部分成功 | PARTIAL | R-19、R-02 | 两者同修才可能转 PASS，单独修任一不成立 |
| J8 检查/改/重交付 | FAIL | R-10 | J8 直接转 PASS |
| J9 知道在做什么 | FAIL | R-01、R-05、R-22 | R-01 → J9；R-22 只解决长任务变慢，不解决分类错误 |
| J10 知道要授权什么 | PARTIAL | R-08 | 审批参数补齐可转 PASS；**host 授权在产品面不可见这一条未单独编号**，属已知缺口 |

反查失败项（诚实记录）：`R-11`（`isolation.py` 无调用方）、`R-12`（`safety_gate` 不在运行时）、
`R-13`（必填字段表重复）、`R-14`（事件落库不校验迁移）、`R-16`（无 health）、`R-21`（无深链）、
`R-23`（operation 文案缺 3 个）**没有对应的旅程失败**——它们是工程侧缺陷，不影响本轮十条旅程的结论。
不把它们硬塞进旅程，是为了避免"用工程问题冒充产品问题"。

---

## 12. 证据限制

1. 每条旅程都记录了真实请求/响应；原始 JSON 未全部贴出（体积），关键字段已摘。
2. 未做多用户、多浏览器、移动端验证。
3. 未验证容器/宿主测试路径（本机条件不足）。
4. J2/J7 的"引擎可行"来自 `TaskService` 直测，**明确标注为产品不可达**，不得当作产品能力。
