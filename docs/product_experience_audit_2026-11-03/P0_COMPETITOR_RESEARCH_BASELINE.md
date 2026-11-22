# P0 主流产品研究基线

> 资料检索日：2026-11-03（Asia/Shanghai）  
> 证据分级：**实际观察**（本轮亲手打开/调用） / **公开资料确认**（官方文档/帮助中心/产品页） / **推测**（未证实） / **无法实际访问**  
> 说明：竞品付费/登录墙功能未登录验证；不以宣传词当作体验证据。


## 研究框架（12 维）

对每款产品尽量覆盖：1 布局与信息架构 2 对话与多轮上下文 3 任务创建/管理/执行 4 规划与执行过程呈现 5 工具与授权 6 文件与成果 7 记忆与知识 8 进度与状态 9 异常与恢复 10 用户干预 11 多任务/多会话 12 个性化与设置。

---

## 1. ChatGPT（OpenAI）

| 维 | 要点 | 证据级 |
| --- | --- | --- |
| 布局 | Chat / Work / Projects / Codex 分面；Recents 统一 | 公开资料确认（Help Center 2026） |
| 对话 | 多轮聊天为主；Temporary chat；Writing/Code blocks 替代旧 Canvas | 公开资料确认 |
| 任务 | **ChatGPT Work** 承接长任务与交付物；Scheduled Tasks；旧 Agent mode 官方称不再可用 | 公开资料确认 |
| 规划呈现 | Work 可跟随进度、澄清、改向、批准重要动作（文档描述） | 公开资料确认 |
| 工具授权 | 连接 apps；高影响动作需确认；watch mode 等（文档） | 公开资料确认 |
| 文件成果 | 文档/表/演示/报告/Sites；Projects 放源 | 公开资料确认 |
| 记忆 | Memory + custom instructions；项目级 memory 模式 | 公开资料确认 |
| 多会话 | Projects 组织；pin/archive/search | 公开资料确认 |

**对本项目启示（建议，非照搬）**：把「长任务 Work 面」与「日常 Chat 面」分开；批准点显性化。Zevorinth 已有任务证据账本气质，可学其「交付物中心」而非聊天气泡中心。

**无法实际访问**：本轮未登录 ChatGPT 付费 Work/桌面本地文件能力。

## 2. Claude（Anthropic）

| 维 | 要点 | 证据级 |
| --- | --- | --- |
| 成果 | Artifacts：文档/幻灯/设计/仪表盘；可编辑分享导出 | 公开资料确认 |
| 项目 | Claude Code Projects：协调多线程云会话、PR/测试 | 公开资料确认 |
| 电脑使用 | Computer use 研究预览；Dispatch 手机派发 | 公开资料确认 |
| 干预 | 可停止；新应用需许可 | 公开资料确认 |

**启示**：产物侧栏 + 会话并行线程对「过程可见」强。Zevorinth workbench 已是证据侧栏雏形，可强化产物与事件同屏。

**无法实际访问**：未登录 Claude Pro/Max 实测 computer use。

## 3. Gemini（Google）

| 维 | 要点 | 证据级 |
| --- | --- | --- |
| Deep Research | 计划→搜索（可含 Gmail/Drive/Chat）→推理面板→多页报告；可异步完成 | 公开资料确认（gemini.google Deep Research 页） |
| Canvas | 报告转互动页/测验/音频等 | 公开资料确认 |
| Skills | Gems→Skills 迁移时间表 | 公开资料确认 |

**启示**：长任务「先出示计划供用户改」对降低失控感极有效——Zevorinth 有 plan/revise，但缺用户友好的计划预览步进 UI。

## 4. OpenAI Codex

| 维 | 要点 | 证据级 |
| --- | --- | --- |
| 形态 | ChatGPT 内、IDE 扩展、CLI、Cloud VM；可并行多 agent | 公开资料确认（openai.com/codex、Help） |
| Cloud | 可复用环境；关电脑仍跑；跨设备续跑 | 公开资料确认 |
| 边界 | 文档称部分 computer/browser、GitLab 等限制 | 公开资料确认 |

**启示**：编码 Agent 与通用聊天分产品线。Zevorinth 若定位「可验证本地执行内核」，勿与云端全能 Codex 对标功能全集。

## 5. Cursor

| 维 | 要点 | 证据级 |
| --- | --- | --- |
| Agent | 搜代码、编辑、终端、浏览器、提问澄清；工具次数无硬顶（文档） | 公开资料确认 |
| 授权 | Run Modes：Auto-review / Allowlist / Run Everything；sandbox；permissions.json | 公开资料确认 |
| 形态 | Desktop IDE 为主，亦有 web/CLI/mobile 叙述 | 公开资料确认 |

**实际观察**：本审计执行环境即 Cursor/Agent 工具体系——Auto-review、沙箱、批准卡片与工具时间线是一等公民。  
**启示**：Zevorinth 的 Policy/确认更「硬闸门」；Cursor 更「分级自动」。产品可借鉴**运行模式命名**，但应保持 Zevorinth fail-closed 默认。

## 6. Windsurf / Devin Desktop 线

| 维 | 要点 | 证据级 |
| --- | --- | --- |
| 命名演进 | 公开文档出现 Windsurf Cascade 与 Devin Desktop；Cascade 在部分文档标 legacy，Devin Local 为本地 agent | 公开资料确认（存在品牌迁移噪音） |
| 能力叙述 | 多模式（Code/Smart/Ask/Plan 等）、沙箱、权限规则、MCP、hooks | 公开资料确认 |

**推测**：IDE agent 赛道功能快速合流。  
**无法实际访问**：未安装 Windsurf/Devin Desktop 实测。  
**启示**：对 Zevorinth——勿绑定易变品牌功能清单；学「Plan 模式与执行模式分离」。

## 7. Manus

| 维 | 要点 | 证据级 |
| --- | --- | --- |
| 定位 | 提示→完成态交付（研究/站点/视频/幻灯等） | 公开资料确认 |
| Studio | 桌面访问本地文件/终端/应用；权限模式；Remote Control | 公开资料确认 |
| 自动化 | Automations、Cloud Computer、Cue 多 agent | 公开资料确认 |

**启示**：交付物品类丰富 vs Zevorinth 当前仅文本产物+测试——差距是品类不是「再做一个聊天框」。  
**无法实际访问**：未登录 Manus。

## 8. Perplexity

| 维 | 要点 | 证据级 |
| --- | --- | --- |
| Computer | 自主多步：研究、浏览器、代码、文档、集成；可长时 | 公开资料确认（产品页/博文；WebFetch 超时，以 Search 摘要为准） |
| MCP | 本地/远程 MCP；可连 NotebookLM 等 | 公开资料确认 |

**无法实际访问**：未使用 Perplexity Computer。  
**启示**：长时自治+集成数量不是 Alpha 目标；Zevorinth 应先做「短任务可信」。

## 9. NotebookLM（Gemini Notebook）

| 维 | 要点 | 证据级 |
| --- | --- | --- |
| 核心 | 源材料笔记本；Audio/Video Overview 等 | 公开资料确认 |
| 交互 | 基于上传源的问答与概览，非通用 OS agent | 公开资料确认 |

**启示**：知识工作「源→产物」闭环清晰。Zevorinth 暂无知识库，若做研究类应另立能力，而非塞进 write_text。

## 10. n8n

| 维 | 要点 | 证据级 |
| --- | --- | --- |
| 定位 | 工作流自动化 + Agent 一等制品；工具/技能/知识/记忆/子代理/渠道/日程 | 公开资料确认（docs.n8n.io） |
| 交互 | 画布编排为主；Agent 可聊天触发 | 公开资料确认 |

**启示**：若用户要「稳定业务流程」，可视化工作流与对话 Agent 是不同产品。Zevorinth 当前更近「受控任务运行时」，不要过早画 n8n 式节点编辑器。

## 11. Dify

| 维 | 要点 | 证据级 |
| --- | --- | --- |
| 定位 | Agentic workflow / Chatflow / Agent / RAG 平台；可视化节点 | 公开资料确认（dify.ai / docs） |
| Agent | 可独立聊天或作为工作流节点；沙箱执行叙述 | 公开资料确认 |

**启示**：平台型「搭建 Agent」vs Zevorinth「自带强契约执行内核」——可互为集成对象，不宜功能镜像。

## 12. 横向对照（相对 Zevorinth 现状）

| 维度 | 主流常见 | Zevorinth 3.92 Alpha（事实） |
| --- | --- | --- |
| 自然语言直接干活 | 强 | 弱（deterministic 需结构化；LLM 条件） |
| 多步工具自治 | 强 | 无（单步双工具） |
| 过程可视 | 中~强 | 事件账本强、现场直播弱 |
| 批准/沙箱 | 分级自动 | 硬 Policy + 显式确认 |
| 成果品类 | 广 | 极窄 |
| 证据/审计 | 弱~中 | **强（差异化）** |
| 记忆知识 | 常见 | 无 |
| 部署 | 云+桌面 | 单机 loopback Alpha |

## 竞品结论（给决策用）

1. **不要机械搬迁** ChatGPT Work / Manus 品类 / n8n 画布进首版。  
2. **可借鉴**：计划先确认、运行模式命名、产物与过程同屏、Projects 级上下文（在有多会话需求后）。  
3. **应坚持差异化**：fail-closed、独立验证、事件因果、能力注册表诚实披露。  
4. 所有「公开资料确认」项若进入需求，下一阶段需补**实际观察**再立项。
