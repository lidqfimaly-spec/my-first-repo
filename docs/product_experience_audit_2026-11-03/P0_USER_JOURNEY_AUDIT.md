# P0 用户真实操作体验审查

> 审查日期：2026-11-03（Asia/Shanghai）  
> 审查对象：`/Users/lidequan/PythonProject/Zevorinth`  
> 标签：**[事实]** / **[推断]** / **[建议]** / **[无法确认]** / **[能力缺口]**  


运行时 home：`/tmp/zevorinth_audit_2026-11-03`（与仓库 `data/` 隔离）。规划器默认 deterministic；LLM 路径无 Key。

## 场景总表

| # | 场景 | 结果 | 判定 |
| --- | --- | --- | --- |
| 1 | 首次进入+简单目标 | CLI `run` 结构化写 `hello.txt` → `succeeded`+verified | 通过（CLI 专家路径） |
| 2 | 多步骤复杂目标 | 自然语言多目标 → 默认当 write_text 缺参 → `awaiting_clarification` | **能力缺口**（无法真正多步） |
| 3 | 执行中追加要求 | 短任务瞬时完成；运行中追加 API 不存在；仅未执行可 revise | **能力缺口**（运行中） |
| 4 | 修改已有计划 | queued `revise --confirm` content 更新，plan_version++ | 通过（未执行） |
| 5 | 暂停/恢复/取消 | 无 pause；cancel queued → `cancelled`；resume 澄清成功 | 部分通过 / **暂停缺口** |
| 6 | 工具/策略失败 | 空 content → `blocked`/`empty_content` exit 4 | 通过（失败可观测） |
| 7 | 部分成功部分失败 | 单步任务模型无法「部分」；多目标会被压成单操作或澄清 | **能力缺口** |
| 8 | 检查修改再交付 | status/events/task-audit/产物读回可用；改内容需新任务（不覆盖） | 部分通过 |
| 9 | 知道 Agent 正在做什么 | events 时间线+workbench 列表/详情；无实时「思考流」UI | 部分通过 |
| 10 | 知道哪些操作需授权 | Policy 错误码、control 确认、doctor/registry；无统一授权面板 | 部分通过 / UX 弱 |

---

### 场景 1：首次进入 + 简单目标

**操作**  
`python -m zevorinth --home $AUDIT_HOME run --goal "写一份简短问候文件" --operation write_text_artifact --artifact-path hello.txt --content "你好，Zevorinth audit" --json`

**[事实]** `status=succeeded`，`phase=verified`，产物 24 bytes，sha256 `26144269…`，文件位于 `artifacts/<task_id>/hello.txt`。  
**[事实]** `doctor` 可报告能力；workbench 可列出该任务。  
**[推断]** 「首次用户」若只打开 workbench 只读模式，**不能**提交任务，必须 CLI 或 control-center。  
**[建议]** 首版引导应明示：只读台 vs 控制中心 vs CLI 三入口。

### 场景 2：多步骤复杂目标

**操作**  
`run --goal "请帮我：1)写报告 2)跑测试 3)发邮件" --json`（未给结构化参数）

**[事实]** `status=awaiting_clarification`，`missing_fields=[artifact_path,content]`，`execution_mode=not_executed`。未创建多步计划，也未发送邮件（无此工具）。  
**[能力缺口]** 用户自然语言多目标无法被执行内核展开为多工具流水线。  
**[事实]** 后用 `resume --artifact-path report.txt --content "..."` 可完成**单文件**交付，原 goal 字符串仍保留多目标文案——语义上「完成」≠ 用户原意图全覆盖。

### 场景 3：执行中增加新要求

**[事实]** `write_text_artifact` 实测毫秒级完成，难以人工插入中途消息。  
**[事实]** `revise_plan` 在存在 `tool_result`/`artifact` 或非 `NOT_EXECUTED` 时抛 `PlanRevisionConflict`。control actions 无 running-task 修订。  
**[能力缺口]** 无「执行中追加/改向」产品路径；仅有 `request-cancel` 协作取消语义（本轮未做长跑实测）。

### 场景 4：修改已有计划

**操作** enqueue → `revise <id> --expected-plan-version 0 --source-id audit-src-1 --confirm --content new-v2`

**[事实]** `plan_version=1`，request.content=`new-v2`，status 仍 `queued`，phase=`plan_revised`；随后 `cancel` → `cancelled`。  
**判定**：未启动任务可改计划——通过。

### 场景 5：暂停 / 恢复 / 取消

**[事实]** CLI 无 pause/suspend。  
**[事实]** `cancel` 取消无活跃 lease 的任务成功。  
**[事实]** 澄清态 `resume` 成功转为 verified。  
**[能力缺口]** 缺少用户可理解的「暂停并稍后继续同一执行现场」；`interrupted` 状态机存在，但产品层无暂停按钮/命令。

### 场景 6：工具执行失败 / 策略拒绝

**操作** `--content ""`

**[事实]** `status=blocked`，`error.code=empty_content`，exit code **4**，`not_executed`。  
**[事实]** LLM 无 Key：`planning_failed` / `missing_api_key` / exit **7**。  
失败可区分——通过。

### 场景 7：部分成功部分失败

**[事实]** Policy 仅允许单步；无并行子任务编排。  
**[能力缺口]** 无法表达「3 步中 2 成功 1 失败」的部分交付模型（除非未来多步）。相关：conversation 一次 process 绑定一个任务。

### 场景 8：检查、修改、重新交付

**[事实]** `status`/`events`/`task-audit` 可检查；artifact preview API 存在（单测覆盖）。  
**[事实]** 同任务拒绝覆盖；重新交付需**新任务**写新文件或新路径。  
**[推断]** 对「迭代改同一交付物」不友好，但对取证友好。

### 场景 9：知道 Agent 正在做什么

**[事实]** 事件类型完整（created/claimed/planning/tool/verification/succeeded）。workbench 有进行中指标与详情。  
**[事实]** 无流式 token/工具直播面板；任务极短时用户几乎看不到 running。  
**判定**：审计员可追溯；终端用户「现场感」弱。

### 场景 10：知道哪些操作需要授权

**[事实]** 可见信号：Policy 拒绝码、clarification 问题、control-center 强制 `confirmed`、host backend token、live `--authorize-live`、doctor/registry limitations。  
**[事实]** 无统一「权限与批准中心」UI；用户需读文档/错误。  
**[建议]** 首版在控制中心与 CLI 输出增加「为何需要确认 / 什么尚未授权」的固定字段。

## 对话路径补充

**[事实]** `conversation process` 带结构化参数可一站式完成写文件任务，并关联 `conversation_id`。  
**[事实]** 历史目前偏用户输入记录；助手叙事弱。  
**[事实]** send 强制 `--client-message-id`——对脚本友好，对真人 CLI 门槛高。

## Workbench / Control Center 体验

| 观察 | 证据 |
| --- | --- |
| 只读台可发现任务、防 POST 变异 | curl 405 |
| Attention 列出 planning_failed / blocked | `/api/attention` |
| 控制中心 session 声明 5 个动作 | add_task_note, answer_clarification, cancel_queued_task, revise_unstarted_task, start_queued_task |
| 非浏览器 Origin 的变更被拒 | `invalid_origin` |
| 只读模式下创建面板依赖 control session | HTML 中 create panel；无 control 则 hidden |

**[无法确认]** 本轮未用真实浏览器完成控制中心「勾选确认→创建并提交」完整点击。

## 与「不懂如何使用 Agent」目标的差距

**[推断]** 当前成功路径假设用户理解：operation 枚举、参数、home、planner 选择、exit code、确认语义。自然语言「随便说一句就干活」仅在配置好的 LLM planner 下才接近，且仍受双工具单步约束。  
**[建议]** 下一阶段优先：引导式首跑、目标→参数向导、能力边界常驻提示——而非堆竞品功能。
