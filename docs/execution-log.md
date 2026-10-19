# 第 0 阶段执行记录

日期：2026-10-19（Asia/Shanghai）

## 只读环境核查

| 命令或检查 | 真实结果 |
| --- | --- |
| `pwd` | `/Users/lidequan/Documents/ChatGPT/Zevorinth` |
| `uname -a` | Darwin 25.6.0，arm64 |
| `python3 --version` | Python 3.14.6 |
| `python3 -c ...` | CPython，可执行文件为 Homebrew Python 3.14 |
| `git status --short --branch` | 当前工作区 `master`，尚无提交 |
| 对五个精确路径执行 `stat` / 读写检查 | 父目录和旧项目可读写；新目标初始不存在 |
| `git status --short --branch`（三个旧项目） | AutoCodePlay 有 5 项既有变化；Hermes 和 `zh` 干净 |
| 精确 `find` 浅层盘点 | 只查看项目根两层和相关配置/文档，没有扫描整个磁盘 |
| `git ls-files` 计数 | AutoCodePlay 154 个跟踪 Python 文件；Hermes 580；`zh` 151 |
| 读取 README、DESIGN、架构蓝图、pyproject 和 requirements | 完成；未读取任何 `.env` |

## 外部官方信息核查

| 来源 | 真实结果 |
| --- | --- |
| OpenAI Agents SDK GitHub API | 最新 release `v0.24.0`，发布日期 2026-10-15；许可证 MIT |
| LangGraph GitHub API | 最新 release `1.2.16`，发布日期 2026-10-16；许可证 MIT |
| Microsoft Agent Framework GitHub API | 最新 release `python-1.22.0`，发布日期 2026-10-15；许可证 MIT |
| Anthropic “Building effective agents” | 官方页面可访问，HTTP 200；文章不是软件包，因此不标版本或许可证 |
| 三个官方产品文档入口复查 | OpenAI Agents SDK、LangGraph persistence、Microsoft Agent Framework 均返回 HTTP 200 |

## 新项目创建

| 操作 | 真实结果 |
| --- | --- |
| 检查目标路径 | 初始不存在，无覆盖风险 |
| `mkdir /Users/lidequan/PythonProject/Zevorinth` | 成功 |
| `git init -b main` | 成功，创建独立空仓库 |
| 写入工程骨架和文档 | 见 `docs/file-manifest.md` |

## 验证

| 命令 | 真实结果 |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v` | 3 个测试全部通过，耗时约 0.001 秒 |
| `PYTHONPATH=src python3 -m zevorinth doctor` | 退出码 0；人类可读输出明确列出六项能力均未实现 |
| `PYTHONPATH=src python3 -m zevorinth doctor --json` | 退出码 0；输出可解析 JSON，`phase=0` |
| `python3 -m compileall -q src tests` | 退出码 0 |
| 用 `tomllib` 解析 `pyproject.toml` | 成功；名称 `zevorinth`、版本 `0.1.0`、运行时依赖为空 |
| `git rev-parse --show-toplevel` | `/Users/lidequan/PythonProject/Zevorinth` |
| `git status --short --branch` | `main`，尚无提交；项目文件为未跟踪的初始骨架 |
| 遍历新项目符号链接 | 0 个符号链接 |
| 搜索新项目运行时对旧项目的路径引用 | 除核查文档中的说明外，0 个运行时引用 |
| 复查三个旧项目 HEAD | AutoCodePlay `7516cfd`；Hermes `bbf27ba`；`zh` `e68f74f`，与初始核查一致 |

未执行：

- 没有运行旧项目测试；第 0 阶段不需要，也不能据此声称旧项目可用。
- 没有安装项目或外部 Agent 框架。
- 没有调用任何模型 API。
- 没有执行 `pip install -e .`，因此可编辑安装入口只写入配置，尚未实测。

额外观察：AutoCodePlay 在核查期间新增了一个既有工作树变化 `scripts/webui_js_harness.js`。本任务没有在该仓库执行写操作；该变化按并发外部修改记录，未触碰。

---

# 第 1 阶段执行记录

日期：2026-10-19

## 起始状态

| 命令 | 真实结果 |
| --- | --- |
| `pwd` | `/Users/lidequan/PythonProject/Zevorinth` |
| `git -C ... rev-parse --show-toplevel` | `/Users/lidequan/PythonProject/Zevorinth` |
| `git -C ... status --short --branch` | `main`，尚无提交，Phase 0 文件全部未跟踪 |
| `git -C ... log -1 --oneline` | 退出码 128：当前分支尚无提交 |
| `realpath` 对照 | 目标仓库与 `/Users/lidequan/Documents/ChatGPT/Zevorinth` 明确不同 |

## 实现与验证命令

| 命令/检查 | 退出码 | 真实结果 |
| --- | ---: | --- |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q src` | 0 | 所有源码通过语法编译 |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v` | 0 | 32 项测试通过，耗时 0.193 秒 |
| `python3 -m venv /tmp/zevorinth-phase1.4Ax2rK/venv` | 0 | 创建隔离环境 |
| `pip install --no-deps -e .` | 0 | editable wheel 构建和安装成功 |
| 安装后 `zevorinth --version` | 0 | `0.1.0` |
| 安装后 `zevorinth doctor --json` | 0 | `phase=1`，任务运行时/验证/持久化/恢复为 true，模型为 false |
| 检查 console script 元数据 | 0 | `zevorinth.cli:main` |
| 安装后真实任务写入 | 0 | Task `79eea6a4-1097-40fb-a42a-e7ec411ab30d` 成功 |
| `zevorinth status <task-id> --json` | 0 | `succeeded`、`real`、`artifact_verified` |
| `zevorinth events <task-id> --json` | 0 | 7 个有序事件，最后为 `task_succeeded` |
| `wc -c <artifact>` | 0 | 26 字节 |
| `shasum -a 256 <artifact>` | 0 | `98dc2fe9222851d43618172f571c9f5e97fe4fb00c24b1cd9df6c03494b919e9` |
| 最终安装入口复跑 | 0 | Task `33729bda-3d1f-46b6-a60a-c7d45dc4ee5f`，24 字节，SHA-256 `d355873498018fc790e082dea31a57087b630abba0582677cdb5000c6261c321` |
| 最终 `status --json` 复查 | 0 | `succeeded`、`real`、`artifact_verified`，字节数与 SHA-256 和外部 `wc`/`shasum` 一致 |

一个辅助诊断命令首次使用 Python 3.14 的 `EntryPoints[0]` 形式读取 console script，因该对象不支持整数下标而返回 1。该错误属于诊断脚本用法，不是安装失败；改用迭代器后确认入口为 `zevorinth.cli:main`。

## 测试覆盖

- A1 真实成功：1 项；
- A2 工具失败：1 项；
- A3 澄清与继续：1 项；
- A4 路径穿越、绝对路径、任务目录/产物根符号链接和覆盖阻断：3 项；
- A5 写后验证前中断、重启识别、安全恢复、子进程异常退出、修改文件不覆盖：3 项；
- A6 真实/模拟/未执行三态：1 项；
- 状态机、真实工具、Verifier、EventStore、CLI 其他测试：22 项。

## 中断恢复测试边界

实际测试了确定性进程重启模拟：

- 真实文件已写入；
- 工具证据已保存；
- 状态推进到 `verifying`；
- 故障钩子在 Verifier 前抛出异常；
- 新服务实例通过同一 SQLite 数据库识别 `verifying` 并转化为 `interrupted`；
- `resume` 重新验证已有文件，Writer 调用次数为 0；
- 修改文件后恢复失败且不覆盖。
- 测试还启动独立子进程，在真实写入并提交 `verifying` 后以 `os._exit(23)` 退出；父进程启动新运行时后完成相同恢复。

没有执行真实 OS `kill -9`、断电或文件系统损坏测试，因此不声称完成了真实崩溃恢复。

## 未执行

- 未调用模型 API；
- 未读取或输出任何凭据；
- 未安装或调用 LangGraph、OpenAI Agents SDK、Microsoft Agent Framework；
- 未运行旧项目测试；
- 未修改任何旧项目文件。

## 旧项目最终审计

- Hermes 与 `zh` 工作树仍为 0 项变化，HEAD 分别为 `bbf27ba`、`e68f74f`。
- AutoCodePlay HEAD 仍为 `7516cfd`，但未提交条目在本次任务期间由 6 增至 10，新增变化包括 `tests/_gui_harness.py`、`tests/test_body_contract.py`、`tests/test_gui_gates.py`、`tests/test_web_routes.py`。
- 本任务只在 AutoCodePlay 执行只读 Git/stat 检查，没有写入、还原或清理这些并发外部变化。
