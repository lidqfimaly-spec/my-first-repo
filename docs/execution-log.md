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

---

# 第 1.5 阶段执行记录

## Git 基线

| 命令 | 退出码 | 结果 |
| --- | ---: | --- |
| `git rev-parse --show-toplevel` | 0 | `/Users/lidequan/PythonProject/Zevorinth` |
| `git branch --show-current` | 0 | `main` |
| `git status --short --untracked-files=all` | 0 | 33 个预期项目文件未跟踪 |
| `git diff --cached --check`（修正末尾空行后） | 0 | 无空白错误 |
| `git commit -m "feat: establish verified artifact task loop"` | 0 | 根提交 `752cae3`，33 files |
| `git switch -c phase-1.5-hardening` | 0 | 创建并切换加固分支 |

## 实际测试

完整测试连续运行三次：

| 测试 | 结果 |
| --- | --- |
| Phase 1 基线完整测试 | 32/32 通过 |
| 真实 SIGKILL 恢复 | 2/2 通过 |
| 并发 C1/C2/C3 | 3/3 通过 |
| EventStore 租约/锁/回滚 | 6/6 通过 |
| 异常恢复与幂等 | 4/4 通过 |
| 完整套件第 1 次 | 46/46 通过，1.085 秒 |
| 完整套件第 2 次 | 46/46 通过，1.111 秒 |
| 完整套件第 3 次 | 46/46 通过，1.038 秒 |
| 完整套件最终回归 | 46/46 通过，0.957 秒 |
| `python3 -m compileall -q src tests` | 退出码 0 |

## SIGKILL 测试方式

- 子进程在真实文件写入、工具证据提交、`verifying` 检查点后输出 JSON 同步信号，然后进入 `signal.pause()`；
- 父进程用 `selectors` 等待输出，不使用固定睡眠；
- 父进程发送 `SIGKILL`；
- 测试断言子进程返回码等于 `-signal.SIGKILL`；
- 新运行时使用超过租约期限的测试时钟恢复；
- 正确文件通过重新验证；修改文件失败且不覆盖。

## 并发测试方式

- `multiprocessing` 使用 `spawn` 启动独立 Python 进程；
- 测试使用 Event/Barrier 和 Queue 同步，不依赖随机 sleep；
- Writer 或 Verifier 在明确阶段阻塞，以让竞争执行者真实尝试认领；
- 测试结束后对仍存活进程执行有界 join/kill 清理。

## 隔离安装

| 命令/检查 | 退出码 | 真实结果 |
| --- | ---: | --- |
| 新建 `/tmp/zevorinth-phase1.5.xdT5hm/venv` | 0 | 隔离环境创建成功 |
| `pip install --no-deps -e .` | 0 | editable wheel `zevorinth-0.2.0` 安装成功 |
| `zevorinth --version` | 0 | `0.2.0` |
| `zevorinth --home ... doctor --json` | 0 | `hardening_phase=1.5`，SQLite leases true，distributed false |
| `zevorinth --home ... run ... --json` | 0 | Task `740d2d9d-5cbd-4156-a4c0-10be244f7711` 成功 |
| `zevorinth --home ... status <id> --json` | 0 | `succeeded`、`real`、租约已清除 |
| `zevorinth --home ... events <id> --json` | 0 | 8 个有序事件，含 `task_claimed` |
| `wc -c <artifact>` | 0 | 24 字节 |
| `shasum -a 256 <artifact>` | 0 | `c53fd03b983d0fcd002559618e50026f2d94984f4e711dbe1c633e64e95cdd73` |

## 最终 Git 审计

- 当前分支：`phase-1.5-hardening`
- 基线提交：`752cae3 feat: establish verified artifact task loop`
- 相对基线的已跟踪差异：16 个修改文件，`1161 insertions(+), 191 deletions(-)`
- 新增未跟踪文件：6 个（2 个文档、4 个测试/测试辅助）
- `git diff --check`：退出码 0
- 未设置远程

未跟踪文件必须单独计入差异，不能把 `git diff --stat` 的结果误读为全部改动：

```text
docs/phase-1.5-audit.md
docs/phase-1.5-results.md
tests/sigkill_worker.py
tests/test_concurrency.py
tests/test_hardening.py
tests/test_sigkill.py
```

## 旧项目最终状态

- AutoCodePlay：HEAD `7516cfd`，10 项既有/并发未提交变化；
- Hermes：HEAD `bbf27ba`，0 项变化；
- zh：HEAD `e68f74f`，0 项变化。

本阶段未修改旧项目。

---

# 第 2 阶段执行记录

## Git 基线封存

| 命令/检查 | 结果 |
| --- | --- |
| 当前分支 | `phase-1.5-hardening` |
| Phase 1.5 提交 | `177c3c3 feat: harden task execution and recovery` |
| Phase 2 分支 | `phase-2-llm-planner` |
| Phase 1.5 文件检查 | 16 个预期修改、6 个预期新增，无清单外文件 |
| Phase 1.5 完整测试 | 46/46 通过 |
| `git diff --cached --check` | 退出码 0 |
| Phase 1.5 commit | 成功，22 files，2200 insertions，191 deletions |

## 官方 SDK 核对

- `developers.openai.com`、`platform.openai.com` 返回 HTTP 403；
- 在隔离环境安装官方 `openai 3.35.1`；
- 核对 `responses.create` / `responses.parse` 签名；
- 采用 strict JSON Schema + Responses API；
- 客户端 `max_retries=0`，调用次数上限 1，`store=False`；
- 没有使用未启用的真实 API Key，没有发起真实模型请求。

## 实现验证

| 命令 | 退出码 | 真实结果 |
| --- | ---: | --- |
| 完整离线 unittest | 0 | 68/68 通过，1.874 秒；原 A1–A6、并发、SIGKILL 测试继续通过 |
| `compileall -q src tests` | 0 | 源码与测试语法通过 |
| `eval-planner --offline` | 0 | 28/28 脚本化案例通过 |
| 确定性基线 | 0 | 4/28 分类正确，模型调用 0 |
| `eval-planner --online`（无配置） | 7 | 明确报告缺少 API Key，无网络请求 |
| 隔离安装 `pip install -e '.[llm]'` | 0 | 安装 `zevorinth 0.3.0` 与 `openai 3.35.1` |
| 安装后 `doctor --json` | 0 | `llm_planner_available=true`，Key 未配置，不打印 Key |
| 安装后确定性真实任务 | 0 | 32 字节文件，Verifier 通过 |
| 安装后 LLM 模式无配置 | 7 | 任务 `planning_failed`、`not_executed`，无 artifact |
| 隔离环境完整 unittest | 0 | 68/68 通过，1.954 秒 |
| `pip check` | 0 | No broken requirements found |

实际确定性产物：

- Task：`58481b9d-5325-4ef4-862a-7e6f59de46a2`
- 文件：`/tmp/zevorinth-phase2.GxQUda/runtime/artifacts/58481b9d-5325-4ef4-862a-7e6f59de46a2/phase2.txt`
- 大小：32 字节
- SHA-256：`4d2ff5c373c96752b73e95acace6cc1c1b0bb3ed4c9d6fc4f7c8be498df37cab`

## 离线评测

```text
mode: offline-scripted
cases: 28/28
contract_validity_rate: 1.0
classification_accuracy: 1.0
unsafe_block_rate: 1.0
end_to_end_success_rate: 1.0
model_calls: 26
deterministic_baseline: 4/28
```

这些指标来自脚本化候选和测试时钟，不代表真实模型质量。未执行在线评测，因此没有在线准确率、Token、延迟或端到端结果。

## 第 2 阶段旧项目审计

- AutoCodePlay HEAD 仍为 `7516cfd`，未提交条目在本阶段期间由 10 增至 14；
- Hermes HEAD `bbf27ba`，工作树 0 项变化；
- `zh` HEAD `e68f74f`，工作树 0 项变化；
- 本阶段只读取旧项目 Git 状态，没有修改、清理或还原任何旧项目文件。

---

# 第 2.5 阶段预检执行记录

## Git

- 从干净工作树创建 `phase-2.5-online-evaluation`；
- 起点提交：`7917773b20972ce73b128fc755e263c289c2dee9`；
- 未设置远程，未推送。

## 指标与案例审计

- 逐行审计 `evaluation.py` 的分子、分母和案例过滤；
- 数据集版本 1，共 28 个案例：
  - planned 6；
  - clarification 4；
  - unsupported 9；
  - failed 9；
  - unsafe 10。
- 修正混淆指标命名，保留 `legacy_metrics`；
- 新增 `online_pilot_v1.json` 固定 5 案例；
- 在线模式必须显式 `--pilot phase-2.5` 或 `--case-id`；
- 新增 `--preflight`，不实例化 API Client、不发网络请求。

## 初次配置预检（DeepSeek Provider 适配前）

系统 shell 安全探测：

```text
OPENAI_API_KEY=missing
ZEVORINTH_MODEL=missing
ZEVORINTH_PLANNER=<unset>
ZEVORINTH_LLM_TIMEOUT_SECONDS=<unset>
ZEVORINTH_LLM_MAX_OUTPUT_TOKENS=<unset>
```

- 系统 Python 的 `openai`：`2.45.0`，低于最低 `3.35.1`，判定不兼容；
- 隔离项目环境 `/tmp/zevorinth-phase2.GxQUda/venv` 的 `openai`：`3.35.1`，兼容；
- 两个环境都缺少 Key 和模型；
- 预检 `ready_for_approval=false`；
- 预检没有发送网络请求；
- 计划输出文件没有被创建。

## 测试

| 命令/环境 | 退出码 | 结果 |
| --- | ---: | --- |
| 系统环境完整 unittest（ResourceWarning 视为错误） | 0 | 81/81 通过，2.177 秒 |
| 隔离安装环境完整 unittest | 0 | 81/81 通过，2.124 秒 |
| `compileall -q src tests` | 0 | 语法编译通过 |
| 离线评测 | 0 | 28/28；真实 API 调用 0 |
| 安装环境离线评测 | 0 | 28/28；真实 API 调用 0 |
| DeepSeek preflight（系统 Python） | 7 | Provider/Key/模型均识别；SDK 2.45.0 不兼容；网络请求 false |
| DeepSeek preflight（隔离环境） | 0 | Provider/Key/模型均识别；SDK 3.35.1；ready=true；网络请求 false |

## 当前状态

代码变更保留在 `phase-2.5-online-evaluation` 工作树中，等待模型配置和用户明确批准后，才能执行固定的 5 案例在线试点。

## DeepSeek Provider 适配预检

- `DEEPSEEK_API_KEY`：存在；
- `OPENAI_API_KEY`：缺失；
- `ZEVORINTH_LLM_PROVIDER`：当前 shell 未设置；
- `ZEVORINTH_MODEL`：当前 shell 缺失；
- Provider 与 Key 变量明确绑定，未发生交叉读取；
- DeepSeek Base URL：`https://api.deepseek.com`；
- DeepSeek structured output：`json_schema` + `name/schema`；
- DeepSeek 请求不发送不受支持的 `store`，不发送未文档化的 `strict`；
- SDK 最低版本仍为 `3.35.1`，低版本请求前 fail-closed；
- 系统 Python 的 `openai 2.45.0` 不兼容；
- 隔离项目环境的 `openai 3.35.1` 兼容。

使用非秘密变量：

```text
ZEVORINTH_LLM_PROVIDER=deepseek
ZEVORINTH_MODEL=deepseek-flash
```

在隔离项目环境重新预检，结果：

```text
ready_for_approval=true
network_request_sent=false
real_api_calls=0
persistent_report_exists=no
```

该结果只表示配置满足审批条件；密钥有效性、模型可用性和真实结构化输出尚未通过在线请求验证。

## 用户批准后的 DeepSeek 在线试点

用户明确批准后执行命令：

```text
/tmp/zevorinth-phase2.GxQUda/venv/bin/zevorinth \
  eval-planner --online --pilot phase-2.5 \
  --output artifacts/evaluations/phase-2.5-online-v1.json --json
```

非秘密 Process 环境：

```text
ZEVORINTH_LLM_PROVIDER=deepseek
ZEVORINTH_MODEL=deepseek-flash
```

实际结果：

- 命令退出码：1（存在失败案例）；
- 模型请求：5；
- 重试：0；
- 端到端通过：3/5；
- real API calls：5；
- total tokens：2894；
- average latency：1674.4124 ms；
- `unsafe_side_effect_block_rate=1.0`；
- planned 案例真实端到端成功率：2/2；
- 失败案例：`clarify_ambiguous_goal`、`unsupported_shell`；
- 没有文件工具越权副作用；
- 第二个失败没有自动重试。

报告：

- `/Users/lidequan/PythonProject/Zevorinth/artifacts/evaluations/phase-2.5-online-v1.json`
- 10187 bytes
- SHA-256 `9379a1efa8484ddb03c8f8c6ed9da44447b6d0087e5c8b8f6fccf86e1f006476`

## 旧项目并发状态（最终审计）

本任务没有修改旧项目，但最终 Git 审计观察到外部并发变化：

- AutoCodePlay：HEAD 从 `7516cfd` 变为 `85e6f61`，当前 5 项工作树变化；
- `hermes_seed_副本11`：HEAD 仍为 `bbf27ba`，当前 2 项变化；
- `zh`：HEAD 仍为 `e68f74f`，当前 1 项变化。

这些变化不属于 Zevorinth，本次没有清理、还原或提交它们。

---

# 第 2.6 阶段执行记录

## 阶段封存

- Phase 2.5 提交：`e4abcb1 eval: record DeepSeek online pilot results`；
- 精确暂存 17 个预期文件；
- `.idea/` 与 ignored 在线 JSON 未进入提交；
- 从该提交创建 `phase-2.6-response-hardening`。

## 代码路径审计

- 旧 Candidate Validator 对所有分类使用同一个字段集合；
- 缺少与分类无关的字段会被错误分类为 `invalid_candidate_fields`；
- 旧响应适配器只读取 `response.output_text`，JSON 解析失败时丢失状态和结构诊断；
- 新 `response_normalization.py` 提取白名单诊断，不保存原始文本；
- 候选契约改为按 classification 检查必需字段；
- 未知字段继续拒绝，缺少无关字段不再拒绝；
- DeepSeek Schema 共同要求 `classification`，本地验证器强制分类业务字段；
- 未知 response status、非 2xx、response error、空文本、无效 JSON 均 fail-closed。

## 诊断隐私

- 不保存完整用户目标；
- 不保存完整模型文本、拒答文本或候选字段值；
- 不保存异常字符串；
- 未知字段名仅保留安全标识符格式，否则 `<redacted>`；
- Key/认证头相关测试通过。

## 测试

| 环境 | 结果 |
| --- | --- |
| 系统 Python 完整 unittest | 91/91 通过，2.526 秒 |
| 隔离安装环境完整 unittest | 91/91 通过，2.459 秒 |
| ResourceWarning-as-error | 通过 |
| 离线 Planner 评测 | 28/28 |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

## API 与历史结果

- 本阶段真实 API 请求：0；
- 在线评测：未运行；
- 新在线结果文件：未创建；
- 原 Phase 2.5 报告未改写；
- 原报告 SHA-256：`9379a1efa8484ddb03c8f8c6ed9da44447b6d0087e5c8b8f6fccf86e1f006476`。
