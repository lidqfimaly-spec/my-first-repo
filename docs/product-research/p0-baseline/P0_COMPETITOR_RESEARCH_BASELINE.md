# P0 · 主流 Agent 产品机制研究基线

`[P0_COMPETITOR_RESEARCH_BASELINE · 2026-11-21/22]`

> **研究目的不是列功能清单，而是抽取机制**：哪些交互/执行/授权/交付模式值得搬，
> 哪些复杂度不值得。
>
> **诚实边界**：本轮只对 4 个产品取得了**官方一手来源**（Claude / Manus / Cursor / Dify）。
> 其余产品未在本轮取得官方文档，标注为 `INFERENCE`（基于既有认知）或 `UNKNOWN`，
> **不得**当作用户可引用的竞品事实。任何"某产品支持 X"的断言，只有标 `OFFICIAL_SOURCE`
> 或 `DIRECTLY_OBSERVED` 的才可以被写进对外材料。

---

## 0. 证据等级

| 等级 | 含义 | 本轮数量 |
| --- | --- | --- |
| `DIRECTLY_OBSERVED` | 我本人实际操作观察 | 0（本轮未登录任何第三方产品） |
| `OFFICIAL_SOURCE` | 官方文档/官方博客/官方 changelog，附链接 | 4 个产品 |
| `THIRD_PARTY_SOURCE` | 可靠第三方资料 | 2 处（Manus 使用体验、Cursor 综述） |
| `INFERENCE` | 基于既有认知的推断，未本轮核实 | 7 个产品 |
| `UNKNOWN` | 无法确认 | 若干具体细节 |

---

## 1. Claude（`OFFICIAL_SOURCE`）

来源：
- Artifacts 机制：<https://support.anthropic.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them>
- 项目/会话/权限卡/计划审批：<https://claude.com/docs/claude-science/core-concepts>
- 记忆与项目记忆：<https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context>
- Artifact 版本与溯源：<https://claude.com/docs/claude-science/artifacts>

**值得借鉴的四个机制**

1. **Artifact = 一等对象**，不是"聊天气泡里的代码块"。它在侧栏有独立空间，可打开、可版本化、可分享、可下载、可 Pin。
2. **版本 + diff + 溯源**：同一文件名再次保存 = 新版本；有版本步进器与 diff 开关；`Provenance` 分五个页签（Messages / Code / Execution Log / Environment / Review），并明确"**如果 Code 与 Execution Log 冲突，以 Log 为准**"。这是把"证据优先"做成产品界面的范例。
3. **权限卡（permission card）按范围授权**：读/写文件夹、跑代码、跑 shell、装包、访问网络主机、连接器工具、凭据——每个都可选 `Once / This conversation / This project / Global`，并在设置里可撤销。**授权是"一次性 vs 长期"的显式选择**，不是布尔值。
4. **多选题式的计划审批**："Claude 可能选择先提一个计划再开始多步工作，你批准或与它迭代修改"。

**对 Zevorinth 的直接映射**

| Claude 机制 | Zevorinth 现状 | 差距性质 |
| --- | --- | --- |
| Artifact 版本 + diff | 每次 modify 生成新任务+新文件，无版本视图 | 产品层，数据结构已够 |
| Provenance（含执行日志优先） | 有 evidence（sha256/attempt）但无 UI | 产品层，数据已够 |
| 授权范围 Once/Conversation/Project/Global | host-test grant 是一次性 TTL，但只在 CLI | 机制有了，入口没有 |
| 计划审批 | 有 `approve_step`，只显示 step_id + 文本 | 信息量严重不足 |

---

## 2. Manus（`OFFICIAL_SOURCE` + 1 处 `THIRD_PARTY_SOURCE`）

来源：
- 快速上手：<https://manus.im/docs/zh-tw/introduction/quickstart>
- Sandbox 机制：<https://media@manus.im/blog/manus-sandbox>（官方博客）
- 第三方使用体验（长任务/审核习惯）：<https://www.iaifeed.com/how-to-delegate-a-research-task-to-manus-im>（`THIRD_PARTY_SOURCE`）

**机制要点**

1. **异步是默认心智**：描述目标 → 它自己规划、在自己的虚拟计算机里做 → 交付结果。"启动后可以关页面，回来看"。
2. **Plan Mode**：先给计划，用户可改，再执行（官方博客明确"在它花掉时间和额度之前纠正方向"）。
3. **Branch**：从任意一条回复分叉，尝试不同方向而不破坏原任务。
4. **Sandbox = 每任务一台隔离虚拟机**，有网络、文件系统、浏览器；"View all files in this task" 是**产品级入口**；睡眠/回收策略明确（Free 7 天 / Pro 21 天），唤醒可恢复 artifacts。
5. **交付物在任务内**：报告/幻灯片/网站直接在任务里给，可在 Studio 里继续改（视频给可编辑时间轴，不是成品）。

**对 Zevorinth 的映射**：`artifacts/<task_id>/` 已经是"每任务一个目录"，等价于 Sandbox 的文件区；缺的是"任务内查看全部文件"的入口与生命周期语义。

---

## 3. Cursor Cloud Agents（`OFFICIAL_SOURCE` + 1 处 `THIRD_PARTY_SOURCE`）

来源：
- Changelog（2026-08-19）：<https://www.cursor.com/changelog/08-19-26>
- Cloud Agents release notes：<https://cursor.com/docs/release-notes/cloud-agents>
- 机制综述：<https://www.simular.ai/articles/cursor-background-agents>（`THIRD_PARTY_SOURCE`）

**机制要点（与本项目最相关的三条）**

1. **Steering without interruption**："你可以在 agent 工作时发消息引导它，而不会打断它；**follow-up 会等到下一次 tool call** 才被处理，而不是把它从动作中途切断。"
   → 这正是 Zevorinth 缺的"边跑边插话"的语义设计：**不是**抢占，而是排队到安全点。
2. **交付 = PR + 截图 + 视频 + 日志 + 可接管远程桌面**；`/goal` 给长期目标，`/loop` 定期检查，订阅事件自动唤醒。
3. **环境与失败处理明确**：磁盘满就停并提示删文件；模型响应失败/重试时不应用那次编辑。

**对 Zevorinth 的映射**：Zevorinth 已有"安全点"概念（步边界 + `_cancel_if_requested` 6 个检查点），完全可以做"追加要求排队到下一个检查点"，成本远低于异步重构。

---

## 4. Dify Human Input（`OFFICIAL_SOURCE`）

来源：<https://docs.dify.ai/zh/cloud/use-dify/nodes/human-input>、官方博客 <http://dify.ai/blog/the-human-input-node-bringing-human-judgment-into-automated-workflows>

**机制要点**：人工介入是一个**一等节点**，配置四件事：提交方式（Web app / 邮件）、表单内容（Markdown + 变量，可预填上游输出）、用户动作（多个决策按钮，各自映射分支，如 Confirm / Regenerate / Forward）、**超时策略**（超时走专门分支，可升级给另一个人）。

**对 Zevorinth 的映射（差距最刺眼的一组）**

| Dify | Zevorinth |
| --- | --- |
| 审批表单展示上游产物 + 可编辑字段 | 只显示 `step_id / tool_name / expected_result` 文本 |
| 多分支决策（批准/返工/转交） | 只有 `approve_step` / `reject_step` |
| 超时策略 + 升级 | 无超时（任务可以永久 `awaiting_approval`） |
| 审批人可外部（邮件链接，无需账号） | 审批人无身份概念（`approver` 是自由字符串） |

---

## 5. 其余产品（`INFERENCE`，本轮未核实官方文档）

> 以下仅作研究方向登记，**不得**写进对外材料或作为设计依据；需要时再补官方来源。

| 产品 | 我据既有认知认为的机制重心 | 待核实项 |
| --- | --- | --- |
| ChatGPT | 对话 + Projects（文件/指令分组）+ 深度研究/Agent 模式的长任务进度条 | Agent 模式的任务中断/接管语义、artifact 版本 |
| Gemini | 与 Workspace 深度绑定的"在文档/邮件里干活" | 权限与授权粒度 |
| OpenAI Codex | 云端任务 + PR 交付 + 本地 CLI 双形态 | 任务队列与并发上限 |
| Windsurf | 编辑器内 Cascade 流式多文件编辑 + 检查点回滚 | 回滚的粒度（文件/步骤） |
| Perplexity | 以"来源可核查"为核心的研究型回答 | 长任务能力边界 |
| NotebookLM | 以"语料为边界"的封闭知识库问答 + 引用 | 是否支持执行动作 |
| n8n | 可视化工作流 + 人工等待节点 + 自托管 | 人工节点的表单/超时设计（可能与 Dify 类似） |

**结论**：这 7 个产品的机制在我这里目前只是"印象"，不能作为 P0 结论的证据。若下一阶段要做差异化定位，应优先补齐其中 2–3 个的官方来源。

---

## 6. 横向对比（只填有证据的格；`?` = 未核实）

| 维度 | Claude | Manus | Cursor | Dify | Zevorinth |
| --- | --- | --- | --- | --- | --- |
| 首次进入 | 对话框 + 模板 | 目标 + 模式选择 | 编辑器内 | 画布 | 对话框（但要求内部字段名） |
| 计划审批 | 有（可迭代） | Plan Mode | 任务内自治 | Human Input 节点 | 有（信息极少） |
| 异步执行 | ? | 是（默认） | 是（云 VM） | 是（工作流） | **否**（同步阻塞） |
| 边跑边插话 | ? | Branch/追加 | 是（排队到下一 tool call） | 否 | **否** |
| 审批范围/时效 | Once/会话/项目/全局 | ? | ? | 超时分支 + 转交 | 无 |
| 产物版本 | 有（版本 + diff） | ?（文件区） | PR + 附件 | ? | 无 |
| 产物溯源 | 五页签 provenance | 执行日志 | 截图/视频/日志 | ? | 有 evidence，无 UI |
| 中断/恢复 | ? | Sandbox 睡眠/唤醒 | 环境快照 | ? | 整步重跑 |
| 记忆影响决策 | 有（项目记忆） | Projects 学任务 | ? | 知识库 | 存储有，**不生效** |

`FACT`（跨产品共同点，只基于已核实来源）：**没有一个产品把"内部字段名"暴露给用户**；**没有一个产品靠同步阻塞 HTTP 完成长任务**；**审批都要给足信息才能做决定**。

---

## 7. 值得借鉴（按对 Zevorinth 的性价比排序）

1. **追加要求排队到安全点**（Cursor）。Zevorinth 已有 6 个检查点，实现成本低，直接解 J3。
2. **审批表单带产物与可编辑字段 + 超时/转交**（Dify）。解 J4/J10，且不需要模型。
3. **产物版本 + diff + 溯源页签**（Claude）。数据结构已有 80%，主要是 UI。
4. **授权范围显式化（Once/会话/项目/全局）**（Claude）。把 host grant 从 CLI 搬进产品时顺手做。
5. **每任务文件区的产品入口**（Manus）。`artifacts/<task_id>/` 已有，缺"查看全部文件"。

## 8. 值得避免

1. **把工作流画布当产品**（n8n/Dify 的路径）：Zevorinth 的用户不是编排者。
2. **多模式/多 agent 版本堆叠**（Manus 有 Max/标准/Lite/Flex）：在理解层还没做好时，多模式只会放大困惑。
3. **用"积分/额度"解释复杂度**：会诱导产品把不确定性成本转嫁给用户。
4. **在同步执行上叠加"暂停/取消"按钮**：会做出伪功能（Zevorinth 现在就差一步掉进去）。

## 9. Zevorinth 的差异化机会（`RECOMMENDATION`）

已核实的竞品都在"能干活"上竞争，**没有一家把"我没干成"讲清楚**。Zevorinth 的
`SUCCEEDED` 只能从 `VERIFYING` 进入、独立回读验证、`artifact_observation` 检测事后漂移——
这套机制目前是"埋在代码里的优点"。

**建议的定位句（可检验）**：
> 一个会告诉你"这一步我真做了、我真验了、以及哪里没做到"的本地 Agent 工作台。

这三个"真"都能用现有机制证明（状态机、readback verifier、evidence 摘要），不需要新造概念。
