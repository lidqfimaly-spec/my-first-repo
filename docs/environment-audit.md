# 第 0 阶段环境核查报告

核查日期：2026-10-19（Asia/Shanghai）

## 1. 核查边界

- 只检查了当前 Codex 工作区、指定父目录、三个候选旧项目和新项目目标路径。
- 没有扫描整块磁盘，没有访问 Obsidian Vault，没有读取任何 `.env` 文件内容。
- 旧项目只做了浅层目录、配置、文档、Git 状态和入口盘点；没有修改任何旧文件。
- 没有运行旧项目测试，因此本报告不把它们的测试描述为通过或失败。

## 2. 执行宿主事实

| 项目 | 实测结果 |
| --- | --- |
| 初始工作目录 | `/Users/lidequan/Documents/ChatGPT/Zevorinth` |
| 操作系统 | Darwin 25.6.0，arm64 |
| 系统 Python | CPython 3.14.6，`/opt/homebrew/opt/python@3.14/bin/python3.14` |
| 初始工作区权限 | 可读、可写、可进入；独立 Git 工作区，尚无提交 |
| 新项目父目录 | `/Users/lidequan/PythonProject`，可读、可写、可进入 |
| 新项目目标 | `/Users/lidequan/PythonProject/Zevorinth`，核查开始时不存在 |
| 路径冲突 | 未发现；创建后位于独立 Git 仓库 |

说明：`/Users/lidequan/Documents/ChatGPT/Zevorinth` 与最终项目目录 `/Users/lidequan/PythonProject/Zevorinth` 是两个不同路径。最终代码只在后者创建。

## 3. 候选旧项目盘点

### AutoCodePlay

事实：

- 路径：`/Users/lidequan/PythonProject/AutoCodePlay`
- Git：`master`，HEAD `7516cfd`，无远程配置。
- 规模：202 个跟踪文件，154 个跟踪 Python 文件，87 个跟踪测试文件。
- 用途：README 自述为“LLM 驱动的自动化编程元系统”，同时包含生成式与 Agent 式范式。
- 技术栈：Python 3.12.14 虚拟环境；`openai`、`requests` 为运行依赖；FastAPI/Uvicorn 为 Web 可选依赖；CLI/TUI/Web/GUI 多前端；pytest。
- 主入口：`main.py` 调用 `autocode.cli:main`。
- 主要模块：agent loop、审批/策略、沙箱、文件工具、工作区、会话历史、事件协议、MCP、并行 worktree、测试与评测。
- 文档：根 README 含大量架构和能力说明；`docs/` 有架构、审计、红队、压力测试和评测报告。
- 当前工作树：核查开始时已有 5 项未提交变化，其中 3 个修改文件、2 个未跟踪示例文件。它们是既有用户状态，本阶段没有触碰。
- 核查期间又观察到 `scripts/webui_js_harness.js` 出现新的未提交变化，使完成核查时的条目数变为 6。该文件修改时间晚于核查开始，本任务没有对其执行写入；按并发外部变化处理，没有清理或还原。
- 许可证：根目录未发现 `LICENSE`、`COPYING` 或 `NOTICE` 文件。

推断：

- 该项目的 permission/policy、事件协议、会话恢复和负向安全测试最有参考价值。
- 它的实现和历史债务较重，不能把“目录中存在模块”当作该能力已验证。

未知：

- 未核实全量测试是否可重复通过。
- 未确认整体代码的许可证与再分发条件。
- 未确认当前用户改动是否会让某些测试基线失效。

### hermes_seed_副本11

事实：

- 路径：`/Users/lidequan/PythonProject/hermes_seed_副本11`
- Git：`main`，HEAD `bbf27ba`，工作树干净，无远程配置。
- 规模：698 个跟踪文件，580 个跟踪 Python 文件，215 个跟踪测试文件。
- 用途：`pyproject.toml` 自述为“声明式工作流 runner + 证明尾迹（manifest DSL v1）”。
- 技术栈：Python 3.12.13 虚拟环境；`networkx`、`python-dotenv`、`openai` 为基础依赖；Flask/FastAPI 等为 Web 可选依赖；pytest。
- 入口：`hermes-run = src.hermes.manifest_runner:main`。
- 主要模块：任务/状态管理、规划、DAG、执行、审批、恢复、证据日志、产物审计、技能、记忆、Web。
- 文档：DESIGN、架构蓝图、事件契约、问题台账、前端与测试审查材料。
- 风险线索：项目根存在 `.env`，本次未读取；规模大，且文档记录了多种“机制存在但消费点错误”的历史故障。
- 许可证：根目录未发现 `LICENSE`、`COPYING` 或 `NOTICE` 文件。

推断：

- 任务状态、manifest、证据日志、恢复和产物审计适合作为设计对照。
- 直接复制会把大量历史抽象和隐式导入方式带入新项目。

未知：

- 未核实 215 个测试文件的真实通过率、运行时长和外部依赖。
- 未确认许可证与代码复用授权。

### zh（BLASTEMA/FAMA）

事实：

- 路径：`/Users/lidequan/zh`
- Git：`master`，HEAD `e68f74f`，工作树干净，无远程配置。
- 规模：279 个跟踪文件，151 个跟踪 Python 文件，62 个跟踪测试文件。
- 用途：README 将系统定位为“芽基/BLASTEMA”生命层与“砝码/FAMA”判定内核。
- 技术栈：Python 3.12 的 uv 虚拟环境；`pyproject.toml` 未声明运行依赖；大量文档驱动设计。
- 主要入口：`scripts/live.py`、`scripts/pursue.py`、`scripts/serve.py`、`scripts/smoke.py`、`scripts/doctor.py`。
- 主要模块：目标/规划/执行/反思、上下文、资源、判据、反证、盲区、运行与恢复、证据台账。
- 文档：README 和近百篇设计/审计记录；其中明确强调三态、失败归因、注入反证和可恢复性。
- 根 README 明确建议对上游 `hermes_seed_副本11` “搬规格，不搬代码”。
- 许可证：根目录未发现 `LICENSE`、`COPYING` 或 `NOTICE` 文件。

推断：

- 该项目最值得复用其“规格与判据”，而不是实现。
- 文档中“已完成”或“已实测”的陈述仍需由其测试或新项目实测再次证明。

未知：

- 未核实测试套件的真实结果和耗时。
- 未确认许可证与代码复用授权。

## 4. 凭据与隐私检查

- 没有读取或输出任何 `.env`、API Key、Token、密码、Cookie 或私钥值。
- 本次没有发现需要读取模型凭据才能完成第 0 阶段的工作。
- `.env.example` 只含变量名和空占位符。

## 5. 事实、推测与未知项

事实：

- 三个候选旧项目都存在且可读。
- 三者都是独立 Git 仓库，均未配置远程。
- 三个项目根目录都没有可见许可证文件。
- `AutoCodePlay` 有既有未提交改动；另外两个在核查时干净。
- 新项目目标初始不存在，父目录可写。

推测：

- 三个旧项目包含可借鉴的安全、证据和恢复经验，但代码复用价值低于规格复用价值。
- 旧项目的规模和历史复杂度使“整体迁移”风险高于重新建立最小实现。

未知：

- 旧项目所有能力的真实可运行程度。
- 旧项目代码的授权状态。
- 旧项目现有测试在当前 Python 3.14 宿主上的兼容性。
- 用户最终希望旧项目如何参与后续测试或对照；第 0 阶段不需要该决定。

## 6. 环境结论

环境可访问，目标路径可安全创建，没有必须立即停止的权限或路径安全冲突。新项目已按独立目录和独立 Git 仓库创建；旧项目未修改。
