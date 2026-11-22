# Zevorinth（衍序）

Zevorinth 是一个以**可验证执行**为核心的 AI Agent 系统。当前系统级基线为 Phase 3.92：
单机、本地、受控执行切片 Alpha。系统提供确定性 Planner、可选受限 LLM Planner、严格
本地校验、持久化任务生命周期、独立验证、恢复/清理治理、L0/L1 审计和只读本地工作台。

当前提供八个受控任务操作：

| 操作 | 当前状态 |
| --- | --- |
| `write_text_artifact` | 可用：创建任务专属文本产物并独立读回验证 |
| `read_text_artifact` | 可用：只读已验证任务的文本产物并独立复核来源哈希 |
| `modify_text_artifact` | 可用：基于已验证源版本创建新的 replace/append 产物版本，不覆盖源文件 |
| `inspect_workspace` | 可用：有界、只读检查已注册 workspace，排除敏感目录和文件 |
| `edit_text_file` | 受控可用：在已注册 workspace 内以精确源哈希和唯一文本匹配执行原子修改，并保留备份 |
| `research_report` | 离线可用：从已注册 workspace 的本地文本来源生成带来源哈希与精确引文的 Markdown 报告 |
| `run_tests` | 条件可用：需要注册 workspace、有效隔离证据和容器环境；host 后端只允许显式本地维护授权 |
| `controlled_shell` | 受控可用：白名单只读命令（ls/cat/wc/head/tail/grep/find 等）、参数数组（非 shell 字符串）、超时和输出上限 |

LLM 只负责规划。认领、状态转换、Policy、工具执行、独立验证、隔离证据检查和事件持久化全部由本地执行内核完成。

固定 `restore_controlled_marker` 是实验性 Python API，不是通用任务恢复能力。浏览器、任意 shell、自动恢复、自动清理、多机执行和 L2/L3 证据可信仍不可用。

## 当前能力

- 确定性 Planner：接收完整的结构化任务，不调用模型。
- LLM Planner：把自然语言目标转换为一次严格 JSON Schema 响应。
- 本地 Candidate Validator：独立检查字段、类型、操作、文件名、内容、响应大小和澄清信息。
- SQLite 执行租约、fencing、并发互斥和崩溃恢复。
- 真实文件写入、独立读回、内容/SHA-256/工具证据验证。
- 已验证文本产物的只读访问、替换或追加式新版本创建，以及源版本 lineage 记录。
- 容器内的有界 `run_tests`、隔离证据复核、超时/取消和验证器拒绝路径。
- Recovery preview、持久化审批、固定 marker 受控操作及只读生命周期/证据审计。
- `task-audit`、`recovery-audit` 和机器可读 capability registry。
- `recover-prepublish`：仅针对已验证的 `publish_lock_timeout` 创建一次确定性 replacement task。
- `real`、`simulated`、`not_executed` 三态区分。
- loopback-only 只读本地工作台：任务发现、状态/事件详情、Attention Inbox、受控
  artifact preview 和只读 recovery preview。
- 离线 Planner 评测集、统计报告和确定性基线对照。
- 手动启用的在线 Planner 评测入口。

模型不能访问 SQLite、调用文件工具、执行 shell、修改任务状态、跳过 Policy 或自行宣布成功。

## 安装

确定性模式只需要项目和 Python 标准库：

```bash
cd /Users/lidequan/PythonProject/Zevorinth
python3 -m venv /tmp/zevorinth-venv
/tmp/zevorinth-venv/bin/python -m pip install -e .
```

LLM 模式安装可选依赖：

```bash
/tmp/zevorinth-venv/bin/python -m pip install -e '.[llm]'
```

当前声明依赖：`openai>=3.35.1,<4`。离线测试不需要该 SDK，也不需要 API Key。

## 确定性模式

```bash
DEMO_ROOT="$(mktemp -d)"
PYTHONPATH=src python3 -m zevorinth \
  --home "$DEMO_ROOT" \
  run \
  --planner deterministic \
  --goal "创建一份可验证的证明" \
  --artifact-path proof.txt \
  --content "Zevorinth wrote this file." \
  --json
```

成功时退出码为 `0`。结果中的成功标记来自任务状态和独立 Verifier。

查询：

```bash
PYTHONPATH=src python3 -m zevorinth --home "$DEMO_ROOT" status TASK_ID
PYTHONPATH=src python3 -m zevorinth --home "$DEMO_ROOT" events TASK_ID --json
```

## LLM 模式

Provider 必须显式选择，密钥变量和地址不会交叉使用。

| Provider | Key 变量 | Base URL | 当前支持 |
| --- | --- | --- | --- |
| `openai` | `OPENAI_API_KEY` | OpenAI SDK 默认地址 | 保留 |
| `deepseek` | `DEEPSEEK_API_KEY` | `https://api.deepseek.com` | 新增 |

DeepSeek 示例，实际值只在本机 shell 设置：

```bash
export ZEVORINTH_LLM_PROVIDER=deepseek
export DEEPSEEK_API_KEY=...  # 仅在本机设置，不要写入仓库
export ZEVORINTH_MODEL=deepseek-flash
export ZEVORINTH_PLANNER=llm
export ZEVORINTH_LLM_TIMEOUT_SECONDS=30
export ZEVORINTH_LLM_MAX_OUTPUT_TOKENS=1500
```

模型名称必须显式配置，项目不固定默认模型。

自然语言任务：

```bash
PYTHONPATH=src python3 -m zevorinth \
  --home "$DEMO_ROOT" \
  run \
  --planner llm \
  --goal "创建 hello.txt，内容写 Hello from Zevorinth" \
  --json
```

`--planner` 未指定时，读取 `ZEVORINTH_PLANNER`；该变量也未设置时默认 `deterministic`。只有显式选择 `llm` 才允许真实模型请求。

缺少 Key 或模型配置时，任务进入 `planning_failed`，执行模式为 `not_executed`，退出码为 `7`，不会调用文件工具。

DeepSeek Provider 使用官方文档要求的 OpenAI SDK + `base_url=https://api.deepseek.com`。Responses 请求使用 `text.format=json_schema`，不发送 DeepSeek 不支持的 `store` 参数，也不依赖未在其文档中声明的 `strict` 字段。本地 Candidate Validator 始终独立运行。

## Planner 结果语义

| 结果 | 任务状态 | 工具副作用 |
| --- | --- | --- |
| 可执行计划 | 继续进入 `running` | 仍需通过 Policy |
| 需要澄清 | `awaiting_clarification` | 无 |
| 不支持 | `blocked` | 无 |
| 模型/解析/配置失败 | `planning_failed` | 无 |
| 真实执行失败 | `failed` | 工具可能已尝试执行 |
| 写入与独立验证成功 | `succeeded` | 真实文件存在 |

LLM 候选不是执行授权。即使模型返回 `write_text_artifact`，本地仍会执行字段契约、路径、大小、Policy、租约和 Verifier 检查。

## 安全、隐私与费用边界

- Responses API 请求使用 `store=False`、单次调用和 `max_retries=0`。
- 请求有明确超时与 `max_output_tokens` 上限。
- 不向模型提供任何工具、shell、网络或数据库能力。
- 用户目标被视为不可信数据，提示注入不能扩展权限。
- 不持久化完整模型原文；规划事件只保存模型标识、耗时、Token、响应 ID 和输入哈希等诊断信息。
- 不自动估算费用；缺少价格表时明确记为未测量。
- `doctor` 只显示 Key 是否配置，不显示实际值。

## 离线测试与评测

```bash
PYTHONDONTWRITEBYTECODE=1 python3 tests/task_control_center_evidence.py \
  _run-tests --root "$PWD" \
  --result /tmp/task-control-center-result.json \
  --log /tmp/task-control-center-tests.log \
  --cwd-root

PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q src tests

PYTHONPATH=src python3 -m zevorinth eval-planner --offline --json

PYTHONPATH=src python3 -m zevorinth eval-planner-quality --offline --json
```

证据运行只能通过 `_run-tests` 或明确审查过的测试 ID 白名单进入。裸
`unittest discover` 不是批准的正式证据命令，因为可能绕过 blocked-ID 规则。
执行器还会在启动 unittest 前再次拒绝任何 container、Docker、live 或 online
测试 ID。

离线评测使用脚本化候选，不联网、不需要 API Key。结果不能代表真实模型质量。

评测数据集位于 `src/zevorinth/evals/planner_cases.json`，覆盖正常计划、澄清、不支持、shell/代码请求、路径逃逸、覆盖、提示注入、超长输入、拒答、超时、连接失败和无效结构化输出。

`planner_quality_v2.json` 提供 planner-only 的多难度、风险与类别基准，并输出独立的
coverage audit、scoring independence audit，以及危险候选“提出 / 被识别 / 被接受”
三个指标。离线 scripted 模式验证评测器和数据集契约，不代表真实模型质量。

报告中的 `live_baseline_protocol` 是真实模型基线的授权清单，当前固定为
`authorization_required`、`live_runner_implemented=true`、`live_requests_enabled=false`。
runner 默认拒绝运行；未来真实调用必须使用新的、未存在的输出路径，并通过
`--authorize-live` 显式授权。

Planner-quality 在线预检不创建 API client，也不发送请求：

```bash
PYTHONPATH=src python3 -m zevorinth \
  eval-planner-quality \
  --online \
  --preflight \
  --output artifacts/evaluations/phase-3.79-quality-live.json \
  --json
```

真实运行只在该路径、预算和模型范围另行批准后启用：

```bash
PYTHONPATH=src python3 -m zevorinth \
  eval-planner-quality \
  --online \
  --authorize-live \
  --output artifacts/evaluations/phase-3.79-quality-live.json \
  --json
```

真实 runner 保持 planner-only：不提交任务、不运行工具、不启动容器、不修改 workspace。

`planner_live_applicability_v1.json` 在第二次真实运行前固定 case 分组：

- 19 个 `live_applicable` 进入 strict score；
- 3 个 `live_exploratory` 只观察，不计通过率；
- 3 个 `scripted_only` 用于离线故障注入，不进入 live score。

该契约明确标记 `retroactively_applicable=false`，不会用事后规则改写 Phase 3.79 的原始
12/25。Planner 只接收白名单 `trusted_parameters`（artifact path、content、workspace、
runner、target，以及显式声明的 precision mode、expected text、content format）；模型
推断值不能标记为 trusted context，也不会被评分器升级为授权参数。

`eval-planner-quality --online --preflight` 同时也是 live-run readiness review。它校验
contract SHA、19/3/3 分组、trusted context、危险计划归责、零重试预算和输出路径；只有所有
检查通过才返回 `READY_FOR_AUTHORIZATION`，且仍要求真实的 `--authorize-live` 才会调用模型。

Phase 3.82 已在该契约下完成一次 DeepSeek `deepseek-flash` baseline：19 个 strict 案例中
14 个通过；5 个 Planner unsafe candidate 中 4 个被下游拦截、1 个被接受。该结果是固定
applicability v1 下的基线，不能与 Phase 3.79 的旧契约 `12/25` 直接同比。

`planner_intent_fidelity_v1.json` 另行检查原始目标与计划一致性。它会将混合安全/危险目标、
静默删除子目标、篡改文件名、缺失参数和操作替换分别标记为
`planned_faithful`、`lossy_plan`、`clarified` 或 `rejected`。该检查独立于 applicability
strict score，且不回溯修改 Phase 3.82 的 `14/19`。

该检查已强制接入 `LLMPlanner.plan_request()`：可执行 lossy plan 会在返回前转为
`intent_fidelity_failed`；checker 异常或结果不一致转为 `intent_fidelity_check_error`；
下游拒绝/澄清仍保留在原状态，但 `planning_metadata` 会记录 Planner fidelity failure。

`live_evaluation_metric_contract_v1.json` 冻结 dataset、applicability v1、intent v2 和所有
指标分母。applicability strict、intent fidelity、unsafe candidate、downstream block、
accepted unsafe plans 与 execution attempts 必须分别报告，禁止合并成单一总分。

Phase 3.86 已完成第三次 DeepSeek 基线：strict `14/19`，intent fidelity `19/22`，
4/4 unsafe candidates 被拦截，accepted executable unsafe plans 为 `0`。核心
`run_tests_shell_target` 被真实 intent 门禁转为 `intent_fidelity_failed`。

Phase 3.87 新增 `planner_precision_v1.json` 离线回归集，覆盖 quoted content 精确匹配、
中文标点、空/缺失内容、文件名变化、Schema 缺字段/错字段/错类型/未知字段/非法枚举，以及
混合危险目标。active intent contract 升级到 v3，带引号内容必须逐字保留。

Phase 3.88 已完成第四次 DeepSeek 基线：strict `15/19`，intent fidelity `19/22`，
3/3 unsafe candidates 被拦截，accepted executable unsafe plans 为 `0`。`write_with_hint`
恢复通过；`write_simple` 仍因未引用 content 的句末标点发生精确匹配失败。

Phase 3.89 新增 `planner_exact_text_v1.json`，把写文本明确分为 `semantic`、
`format_constrained` 和 `exact_text` 三种模式。未提供 precision 元数据时默认
`semantic`，因此普通写作不会因为系统偏好而增减标点；显式 `exact_text` 必须提供 trusted
`expected_text`，并按 Python Unicode code point 原样比较，不 trim、不规范化 Unicode、
不调整换行或标点。显式 `format_constrained` 支持 `plain_text`、`markdown` 和
`json_object`。契约错误、精确不匹配和格式不匹配均在计划接受前 fail-closed，同时
Intent Fidelity、Schema、Policy、授权和执行前检查继续独立生效。Phase 3.88 的原始报告、
分数和 SHA-256 保持不变，本阶段不重新解释历史分数，也不发起第五次 live run。

Phase 3.90 将三种文本契约贯通到真实临时 workspace 的写入和独立回读。固定 10 案例矩阵中
6 个 semantic/format/exact 计划实际创建 artifact，并由 `artifact-readback-v1` 重新读取
UTF-8 bytes、校验大小、SHA-256、attempt 与 workspace effects；4 个 mismatch/precondition
案例在 planning 阶段失败，不创建 plan、artifact 或工具事件。精确矩阵覆盖多行 Unicode、
首尾空白、尾随换行和 UTF-8 BOM；写入后内容被篡改仍由独立 verifier 拒绝。候选不能通过
自身字段声明 `expected_text`。本阶段只使用脚本 Planner 和临时目录，不发起 live run。

## 在线评测

只有明确配置后才可人工运行：

```bash
PYTHONPATH=src python3 -m zevorinth \
  eval-planner \
  --online \
  --pilot phase-2.5 \
  --preflight \
  --output artifacts/evaluations/phase-2.5-online-v1.json \
  --json
```

在线模式必须提供显式 `--output`；如果目标文件已存在，命令会在启动模型客户端前拒绝覆盖。预检不发送网络请求。需要 Key 和模型配置齐备，并明确批准后才执行：

```bash
PYTHONPATH=src python3 -m zevorinth \
  eval-planner \
  --online \
  --pilot phase-2.5 \
  --output artifacts/evaluations/phase-2.5-online-v1.json \
  --json
```

该命令最多执行指定数量的真实模型规划和对应的临时真实文件任务。它会报告：

- 契约与业务校验通过率；
- 分类和状态正确率；
- 不安全请求阻断率；
- 规划失败率；
- 端到端真实成功率和验证结果；
- 平均延迟、调用次数、Token 使用；
- 离线脚本化结果与真实在线结果的明确区分；
- 未测量成本及未覆盖范围。

固定试点清单位于 `src/zevorinth/evals/online_pilot_v1.json`，包含 5 个明确类别。未使用 `--pilot` 或显式 `--case-id` 时，在线模式会拒绝运行，避免误把数据集前几项当作代表性试点。

Phase 2.5 的 5 案例在线试点与指标口径审计见 `docs/phase-2.5-preflight.md` 和 `docs/phase-2.5-online-results.md`。

第 2.7 阶段的定向复测只允许两个历史失败案例：

```bash
ZEVORINTH_LLM_PROVIDER=deepseek \
ZEVORINTH_MODEL=deepseek-flash \
/tmp/zevorinth-venv/bin/zevorinth \
  eval-planner \
  --online \
  --pilot phase-2.7-retest \
  --preflight \
  --output artifacts/evaluations/phase-2.7-targeted-retest-v1.json \
  --json
```

该预检的 `selected_case_ids` 必须严格为 `clarify_ambiguous_goal` 和 `unsupported_shell`，请求上限为 2，重试为 0。上面的可执行文件必须来自已安装 `openai>=3.35.1` 的隔离环境；本机系统 Python 的旧 SDK 不适用。用户批准后把 `--preflight` 移除才会发起请求。预检记录见 `docs/phase-2.7-preflight.md`，实际 2/2 在线结果与限制见 `docs/phase-2.7-results.md`。

## 会话持久化 CLI

当前提供基础会话命令；`process` 会在安全边界内创建或恢复任务：

```bash
PYTHONPATH=src python3 -m zevorinth \
  --home "$DEMO_ROOT" \
  conversation create --conversation-id demo --json

PYTHONPATH=src python3 -m zevorinth \
  --home "$DEMO_ROOT" \
  conversation send demo \
  --content "继续完善这个任务" \
  --client-message-id request-1 \
  --json

PYTHONPATH=src python3 -m zevorinth \
  --home "$DEMO_ROOT" \
  conversation history demo --limit 20 --json

PYTHONPATH=src python3 -m zevorinth \
  --home "$DEMO_ROOT" \
  conversation process demo \
  --content "创建一个文件" \
  --client-message-id request-2 \
  --json

PYTHONPATH=src python3 -m zevorinth \
  --home "$DEMO_ROOT" \
  conversation recover TASK_ID --json
```

需要把任务绑定到已有 turn 时，可使用：

```bash
PYTHONPATH=src python3 -m zevorinth \
  --home "$DEMO_ROOT" \
  run \
  --conversation-id demo \
  --turn-id TURN_ID \
  --goal "创建绑定会话的任务" \
  --artifact-path "result.txt" \
  --content "verified" \
  --json
```

相同 `client-message-id` 和相同内容返回原消息与 turn；相同 ID 但内容不同会明确报冲突。`process` 返回结构化 outcome 和待回答的问题 ID；自然语言的补充/修改不会直接覆盖已有计划，需要结构化更新或确认。

## 只读本地工作台

工作台只投影现有 SQLite、事件、验证和 recovery preview，不保存第二套任务状态，也不提供
提交、重试、取消、恢复或删除操作：

```bash
PYTHONPATH=src python3 -m zevorinth \
  --home "$DEMO_ROOT" \
  workbench --port 8765
```

然后打开 `http://127.0.0.1:8765/`。服务只允许绑定和访问 loopback Host，SQLite 以
`mode=ro` 打开。任务列表和恢复预演不会改变任务；artifact preview 不接受任意路径，
只读取任务已登记的 artifact evidence，并同时显示 registered/current SHA-256。页面加载
或工作台进程退出不影响执行内核、任务持久化或审计记录。事件 payload 只展示有界、
脱敏后的摘要；工作台没有多用户认证，不应暴露到非本机网络。

需要受控写操作时使用独立的控制台入口：

```bash
PYTHONPATH=src python3 -m zevorinth \
  --home "$DEMO_ROOT" \
  control-center --port 8766
```

控制台 v1 保留原有只读 `/api` 和 `405` 行为，通过独立 `/control/api` 开放真实任务
创建、任务说明、`answer_clarification`、`revise_unstarted_task`、
`start_queued_task` 和 `cancel_queued_task`。动作必须来自同一 loopback Origin、
携带当前进程令牌、显式确认，并重新校验任务状态、更新时间、计划版本和定义摘要。
`POST /control/api/tasks` 使用客户端 UUIDv4 作为任务 ID，同一 ID 与相同定义重放时
返回 `already_exists`，相同 ID 携带不同定义时返回 `task_creation_conflict`。
“创建排队任务”只持久化规划预览和 `queued` 状态；“创建并提交”随后才重新读取当前
action preview 并调用受控启动。创建成功不等于任务已执行。受控启动使用独立且可查询
的 request ID；响应丢失时页面保留“查询操作结果”入口，并阻止在同一请求未解决前创建
新的提交，不会自动生成新 ID 重试。

任务说明通过 `add_task_note` 追加脱敏审计事件，只用于人工交流；它不修改任务定义、
计划版本或生命周期状态，普通说明文字也不会被解释为执行指令。
修订操作只修改尚未产生执行证据的任务，不启动任务或调用工具；操作类型保持不变，
参数按 JSON 对象进行键级增量合并。

控制动作的 `request_id` 预留、任务更新时间和计划版本校验在同一个 SQLite 写事务内
完成，因此多个控制进程并发使用同一 ID 时也只能登记一次；相同 ID 携带不同内容会在
产生第二个操作事件或副作用之前返回冲突。

取消只适用于从未取得执行权、未产生执行证据的排队任务。取消与修订共享计划版本
乐观检查，不能同时提交；取消后任务进入终态，不提供通过控制台重新排队的能力。

受控启动当前只开放确定性 `write_text_artifact` 任务。启动会取得执行权、写入任务
artifact 并执行独立验证；执行结果和失败以权威任务状态为准，不自动重试。控制台可以
创建受条件约束的 `run_tests` 任务并在缺少参数时进入澄清流程，但“创建并提交”对该
类型禁用，只允许“创建排队任务”；控制台不能启动 `run_tests`，因为其容器、隔离证据
和既有授权链尚未由本控制契约开放。服务端也不会对 `run_tests` 提供
`answer_clarification`，因此不能通过“补充澄清后继续”绕过启动限制。其他执行后端
同样尚未开放。

澄清回答与受控启动在同一 SQLite `BEGIN IMMEDIATE` 事务中完成 request ID
幂等判定、任务状态/计划版本校验、执行权获取，并同时写入 requested/claimed
证据；若另一进程或 CLI 已经取得执行权，本次动作以
`task_claim_not_acquired` 失败关闭。过期但尚未释放的执行租约不会显示可写操作，
而是返回 `task_claim_recovery_required`；预览不会自动恢复或释放 lease。修订通过既有乐观计划版本和 `plan_revision`
原子提交，活动 lease 存在时不能修订。控制动作结果以事件记录，重复 `request_id`
不会再次执行。其他测试执行、重试、执行中取消、恢复、工具治理和高风险操作尚未开放。

若提交结果不明确，可通过只读请求状态接口按 `request_id` 查询持久化回执。该接口
要求 loopback Host 与进程 CSRF 令牌；只有存在完成事件时才报告完成，不会
根据任务状态推断成功，也不会自动重试。修订请求还可用持久化的同 ID
`plan_revision` 对账完成结果；取消和受控启动也可通过关联的
`task_cancelled`/`task_claimed` 事件对账。

离线验收证据由固定入口生成，而不是手工编辑摘要：

```bash
RUN_DIR="artifacts/audits/task-control-center-v1/final-validation-$(date -u +%Y%m%dT%H%M%SZ)"
PYTHONPATH=src python3 tests/task_control_center_evidence.py run \
  --review-manifest artifacts/audits/task-control-center-v1/independent-review/current-review-manifest.json \
  --previous-summary artifacts/audits/task-control-center-v1/stable-summary.json \
  --output-dir "$RUN_DIR"
PYTHONPATH=src python3 tests/task_control_center_evidence.py summarize \
  --run-metadata "$RUN_DIR/run-metadata.json" \
  --output artifacts/audits/task-control-center-v1/stable-summary.json \
  --replace-reason "说明旧摘要被替换的原因" \
  --trusted-previous-sha256 "$TRUSTED_PREVIOUS_SHA256"
```

需要最终接受评估时，`run` 和 `summarize` 必须同时传入相同的外部 bundle 与
显式策略，例如：

```bash
PYTHONPATH=src python3 tests/task_control_center_evidence.py run \
  --evidence-bundle /path/to/evidence-bundle.json \
  --acceptance-policy target_head_review_required \
  --output-dir "$RUN_DIR"

PYTHONPATH=src python3 tests/task_control_center_evidence.py summarize \
  --run-metadata "$RUN_DIR/run-metadata.json" \
  --evidence-bundle /path/to/evidence-bundle.json \
  --acceptance-policy target_head_review_required \
  --output artifacts/audits/task-control-center-v1/stable-summary.json \
  --replace-reason "说明旧摘要被替换的原因" \
  --trusted-previous-sha256 "$TRUSTED_PREVIOUS_SHA256"
```

当前 macOS 运行时尚未提供可证明的持久发布 custody。快照、review binding 与
summary 的公开 writer 会以 `UNSUPPORTED` 在写入前失败关闭；下列命令保留为未来
具备 custody 能力时的正式入口。

该流程会失败关闭于缺失/损坏证据、哈希或统计不一致、代码指纹漂移、container/
Docker/live/online 用例混入、审查 HEAD/指纹不匹配、零文件审查，以及冻结证据
变化。审查清单分别记录代码审查和架构审查状态；`REQUEST CHANGES`、`WATCH`、
`BLOCK` 与 `UNAVAILABLE` 不会被转换成通过状态。
完成报告的 Markdown 必须声明 `Independent review conclusion:`，且其
归一化结论必须与机器结果一致；审查文件集合必须与当前变更候选完全一致，
`.idea/` 与 `.env*` 不能作为证据输入路径。
review manifest 的 `repository` 和各 lane 的 `target_repository` 必须与调用方
选定的精确规范仓库路径字符串一致。相对路径、`..`、`/tmp` 别名以及指向同一或
其他仓库的 symlink 都会被拒绝；不能通过 `Path.resolve()` 把别名解析成相同路径
后接受。
`run` 输出目录必须为空；上面的 `RUN_DIR` 每次运行都会生成新的版本化目录，并把
`--output-dir` 与 `--run-metadata` 绑定到同一次执行。
`TRUSTED_PREVIOUS_SHA256` 必须来自 Bundle 之外的独立受信配置。省略该参数是允许的
失败关闭降级：仍可校验历史结构，但不会产生 merge-ready 接受结论。

输出目录从已固定的仓库根开始，通过逐级 no-follow 目录 FD 打开。previous
summary、候选身份、两套测试结果与日志、mutation cases/report、metadata、
bootstrap/error 状态以及 acceptance 状态都通过这个已持有的目录 FD 写入。
这些 staging 写入会拒绝 symlink/越界组件，但不会把目录 FD 解释为“路径永远不变”；
同主体 rename 后的持久发布保证由下文的显式 custody 能力决定。
测试和 mutation 子进程只写新建的私有临时目录，父进程通过另行固定的目录 FD
读取结果，并在成功和受控失败路径删除临时目录。
公开的 candidate snapshot、review binding 与 summary writer 在持久发布前要求
`PERSISTENT_PUBLICATION_ANCESTRY_SUPPORTED`。当前 macOS 实现无法原子阻止同一主体
在最终检查后 rename 目标目录，因此该能力明确为 `UNSUPPORTED`：这些公开 writer
在创建输出目录、临时文件、摘要或 acceptance status 之前失败关闭。
`TrustedEvidenceRoot.write_bytes_atomic()` 只用于要求该能力的正式发布；内部
`_write_bytes_staging()` 仅用于受控 staging，不构成正式发布或接受依据。
这不会把“检查后 replace”描述成安全围栏；在没有独立 custody 发布机制前，正式摘要
保持 `NOT_VERIFIED`，不会产生 `merge_ready=true`。

历史摘要现在是显式输入。缺失时生成器写入 `previous-summary-status.json`，
状态为 `BOOTSTRAP_INCOMPLETE`、`acceptance_supported=false`、
`merge_ready=false`，随后在运行测试前失败关闭。
历史摘要还必须通过完整语义验证，而不能只靠 Bundle 内可同步修改的哈希。当前
schema v2 会检查 code、tests、mutations、冻结证据、review、acceptance gate、
candidate identity 和 validation input；候选与 code 身份必须一致，审查状态必须
支持声明的接受状态，成功接受不得带 blocking/unverified scope，替换理由哈希必须
绑定 superseded reason，历史链必须非空、唯一并包含 superseded digest。完整
legacy v1 stable summary 通过独立封闭规则兼容；缺失字段、未知 schema 或伪造接受
字段均失败关闭。
语义有效不等于来源真实。调用方提供的 digest 与 history 都位于同一不可信输入，
不能自证历史。接受路径还必须提供独立受信配置给出的
`--trusted-previous-sha256`，且必须匹配实际 predecessor 字节；没有独立锚点时摘要
可标为 `AVAILABLE_UNTRUSTED`，但不得进入 `ACCEPTED_FOR_MERGE_REVIEW`。
`--trusted-previous-candidate-snapshot` 不再只做结构、repo 字符串和 Git OID
解析。它必须通过与当前 candidate validator 相同的权威载荷验证：固定仓库根会
重建 candidate mode、Git 状态、status hash、fingerprint、manifest 和完整文件集，
snapshot 必须与重建结果一致。合法 OID 或自洽 SHA-256 本身不建立信任；无法从
可信仓库状态重建的历史工作树必须失败关闭。
v2 历史摘要还按统一状态机校验 current review、review gate、binding、resolution、
runtime、unverified scope、merge-ready 与发布 custody；`DIRECT_CLEAR` 与当前直接
审查不可用、content-equivalent 缺少 binding、`UNVERIFIED` 却声称 accepted 等组合
均失败关闭。
content-equivalent accepted history 还必须具有完整非空的顶层 review binding；
accepted `special_review` 必须包含引用路径和哈希，`merge_ready=true`、空 blocking
原因、空 uncovered scope，并与顶层 code/architecture review 状态一致。v2
历史必须记录且匹配 gate 的 `review_status.review_gate`；
`special_review.candidate_acceptance` 只能由权威 resolution 派生为
`INDEPENDENT_REVIEW_CLEAR` 或 `CONTENT_EQUIVALENT_REVIEW_CLEAR`。每个完成 lane
还必须绑定完整 reviewed-file manifest、候选 target HEAD/fingerprint、
`completed=true`、一致 conclusion、必需 coverage 及 report/result 身份；lane
内的未覆盖 scope 必须投影到权威 rollup，不能被顶层空列表隐藏。
历史 binding 记录还必须包含唯一、类型正确的 `unverified_scope` 数组，且每一项
都必须存在于权威 gate 的 `unverified_scopes`；accepted gate 不允许任何未验证范围，
因此历史也不能把非空 binding scope 藏在空摘要字段之外。

也可通过 `--evidence-bundle <manifest.json>` 提供版本化外部证据包。包内相对路径
只能落在 bundle 根目录，禁止绝对路径、越界、符号链接、`.idea/`、`.env*` 和
未登记文件；生成器会重新读取并校验候选快照、审查报告、机器结果、review
binding、稳定摘要、冻结证据和候选绑定的浏览器证据，不只信任 manifest 中的哈希。
可先使用 `validate-bundle --bundle <manifest.json>` 做只读校验。
Bundle schema v2 还拒绝重复 JSON 键、未知字段、重复/错误角色、Unicode 或大小写
路径别名，并对 JSON 大小/深度、单文件大小和文件数量设置明确上限。
JSON 十进制解析还会拒绝所有非有限结果，包括 `1e999`、`-1e999`、显式
`NaN`/`Infinity`；拒绝发生在 canonical JSON 或摘要写入之前。
所有 Bundle 入口在验证前保留调用方传入的原始路径；只有验证器检查通过后才会
使用受控路径。文件读取由根目录文件描述符开始，逐级使用
`O_NOFOLLOW`/`dir_fd` 打开，不会在父目录检查完成后按普通路径重新打开。
平台缺少所需原语时直接失败关闭。
Bundle 根目录只安全打开一次，manifest 的解析、大小检查和 SHA-256 来自同一个
已打开文件。验证期间后续读取会核对根目录的 device/inode，防止路径被替换成
另一个真实目录。绝对路径从文件系统根目录开始逐级打开；macOS 上只对系统提供且
不可由普通用户修改的 `/private` 根级别名（`/etc`、`/tmp`、`/var`）作固定规范化，
其他符号链接组件均拒绝。逐组件打开期间，包括 `KeyboardInterrupt` 和
`SystemExit` 在内的所有 `BaseException` 退出路径都会关闭尚未移交的 FD；
正常返回时只把最终目录 FD 移交调用方，close failure 与原始异常分别诊断。
absolute-directory helper 在每次真实 `open` 与所有权登记期间仅对调用线程屏蔽
SIGINT，待 FD 登记完成后再恢复 mask；正常发送到该线程的 SIGINT 因而在登记后
交付并由清理路径关闭 FD。`pthread_sigmask` 不改变其他线程的 mask，也不是
SIGTERM、SIGKILL、进程崩溃或任意 `BaseException` 的通用隔离机制。
若任意 Python callable 故意隐藏已经成功打开但尚未返回的 FD 再抛出异常，
Python 层所有权模型无法发现该编号；这一边界是显式限制，不声称可以修复。
候选快照、审查清单、review binding、浏览器证据和最终文件枚举全部复用这个
受保护根，不会再次对证据根调用 `.resolve()`。最终清单从固定目录 FD 递归枚举；
目录、普通文件、符号链接和其他文件类型逐项检查，因此替换根路径不会枚举外部
目录。
`TrustedEvidenceRoot` 持有已打开目录 FD 和 device/inode，只在完整验证调用或
摘要接受生命周期结束时关闭。公开嵌套验证 API 自行持有此对象；`build_summary()`
和 `write_summary()` 的外层生命周期覆盖证据校验、摘要构建、摘要重算和最终状态
写入，验证返回后不会退化为普通路径读取。
仓库根和外部 Bundle 根是两个独立安全对象。所有入口先固定仓库根，再选择 Bundle
并固定 Bundle 根；两层身份都持续到最终接受。仓库根构造后、Bundle 选择后、验证后
或摘要写入前替换根路径都会失败关闭。若根对象的身份初始化失败，已打开 FD 会先
关闭再原样抛出身份错误；重复失败不会累积文件描述符。
固定根建立后不再使用 `Path.resolve()` 重新解析仓库根。staging 输出路径按固定根
FD 转换为相对路径；正式摘要、状态与审查产物只有在独立 custody 能力可用时才会
进行持久发布。
信任锚点是文件系统根、当前进程权限和调用方选定的初始仓库/Bundle 路径。调用方
对已信任目录内的并发内容写入、同一主体直接替换测试输入以及无签名 Bundle 的
来源真实性仍负责。
哈希与 schema 校验只证明内部一致性，不是证据来源的密码学证明；当前 bundle
没有签名，仍需可信证据来源或独立人工复核。

最终接受状态分别记录候选身份、历史审查、内容等价 binding、当前目标 HEAD 的
独立审查、测试、冻结证据和运行时验证。默认策略
`target_head_review_required` 不接受仅靠历史 binding 满足当前审查；
只有显式选择 `content_equivalence_allowed` 且所有当前验证都通过时，binding
才可参与最终审查门禁。binding 本身始终不会单独产生 `merge_ready=true`。
直接 target-head review 在最终 gate 会重新打开 review manifest、两份报告和两份
机器结果，重新计算哈希与 lane 身份；早期验证后删除或替换证据会失败关闭。
授权 gate 会把这次读取验证得到的 manifest、lane、report/result 哈希、reviewed
文件、coverage 和状态固定为一次不可变 normalized evidence snapshot，后续状态机
只使用该快照。因此一次授权的语义是“基于这次成功捕获并验证的字节快照作出决定”；
之后修改原路径不会回写该次决定，而下一次决定必须重新捕获并在缺失或内容变化时
失败关闭。该语义不把普通 `exists()` 或事后重算哈希描述成跨文件系统锁。
调用方 candidate、review 和 binding Mapping 只做一次 canonical freeze。direct
路径先验证捕获快照，再与冻结调用方视图比较，之后只从快照派生 status、coverage
和 resolution；content-equivalent 路径只从重验 binding 派生出的 detached summary
做决定。状态化 Mapping 不能给验证和授权提供两套状态。
content-equivalent binding 的 `unverified_scope` 会完整合并进权威
`unverified_scopes`，非空时即使内容等价审查仍为 clear，也只能得到
`NOT_VERIFIED`、`review_binding_unverified_scope` 和 `merge_ready=false`。
content-equivalent gate 现在从已验证 binding 派生完整 normalized review summary，
并要求调用方摘要与它 canonical 全量相等；只允许 `bundle/...` manifest 路径的
等价表示和等义 acceptance 标签，不能再用部分字段一致掩盖 report/result、reviewed
文件、coverage 或 target identity 不一致。
binding 与 candidate 的 `base_head` 必须直接相等；target tree 相等不能替代该
授权字段比较。
接受策略只能由受信任的 CLI/调用方提供；bundle 中同名声明必须与调用方一致，
冲突、缺失或未知策略均阻断。
公共 `evaluate_acceptance_gate()` 只提供诊断状态，不再接受调用方自报的
`tests_verified`、`frozen_verified`、`previous_summary_trusted` 或 runtime 成功映射，
因此不能自行授予 accepted。实际授权使用内部 proof；该 proof 只在完整 run
metadata、双测试环境、mutation、冻结证据、predecessor 和 runtime 验证均成功后由
权威构建路径签发。
proof 绑定 canonical candidate、review summary 和 prerequisite evidence 摘要，
并由进程内 authority key 签名。修改、序列化重建或跨 repository/HEAD/fingerprint/
manifest/review/evidence 复用都会失败关闭；旧的 flag-only issuer/token 接口已移除。
该签名绑定是受支持 API 的证据绑定机制，不等同于防御同一信任进程中的任意恶意代码；
抵御后者需要独立进程或操作系统隔离边界。
每次签发使用不可猜测 handle 和强引用记录。当前 prerequisite evidence 的
canonicalization、摘要和 proof/candidate/review 验证准备在全局锁外完成；锁内只核验
issuance 状态并原子执行 `ACTIVE -> CONSUMED`。证据不匹配、非法字段、签名或
canonicalization 失败会把 ACTIVE 记录转为 `REVOKED`，`status()` 会同步应用 TTL 并
返回 `EXPIRED`。公共诊断 gate 在签发后若在 consume 前因输入准备或 canonicalization
失败，会撤销该次 proof，并保留原始异常。公共诊断 gate 与正式 summary producer
现在共用同一个 proof ownership transaction：签发后由该事务持有 exact proof，
正常路径必须消费，异常路径必须撤销，正常返回但尚未消费时也会撤销。
正常退出不再以可失败的 status 查询决定是否撤销；事务总是执行 owner cleanup，
已消费记录保持终态，未消费记录转 REVOKED。status 失败不能绕过 owner cleanup。
事务在调用 issuer 前先创建不可猜测的 owner token，并把该 token 与 ACTIVE 记录
绑定。即使 issuer 已登记记录、但在返回 proof handle 前抛异常，事务仍可按 owner
找到并撤销该记录。撤销状态转换先提交 `REVOKED`，再执行可失败的 clock/终态清理；
清理失败不会让已撤销记录恢复为 ACTIVE。
owner token 是进程内 bearer capability：持有同一 token 值的 owner 对象可执行撤销。
它不是独立 issuer 身份认证；同一信任进程任意代码仍不受该机制隔离。
诊断 note 写入失败时会退化为异常对象上的 `diagnostic_notes`，不得覆盖原始异常。
已消费、撤销或过期 proof 不能再次授权。
终态记录通常保留 60 秒后机会式清理；容量紧张时可提前清理终态记录，但被清理
issuance 的状态查询返回稳定的 `NOT_ACTIVE`，consume/revoke 则以 "not active"
失败关闭。`terminal_retention_seconds=0` 时，首次 status 仍先返回 `EXPIRED`，
清理由后续状态维护操作完成，不会把本次查询的终态变成异常。记录总数硬上限
1,024；容量仍被 ACTIVE 占用时新签发失败关闭，而不是丢弃待防重放状态。该保护
仅限当前进程，不提供跨进程或崩溃持久化保证。
浏览器状态从实际场景结果重新计算，明确失败或缺失的结果不能支持 runtime
`VERIFIED`。runtime 为 `VERIFIED` 时，声明列表不得为空，且每条已声明记录都
必须同时声明并通过评估为 `VALID_FOR_CANDIDATE`；一条成功记录不能掩盖另一条
失败、不可用、未知或模糊记录。`valid_evidence_count` 必须等于完整声明数量。
浏览器记录必须使用
`schema_version=task-control-center-browser-validation-v1`，未知/缺失版本、
未知字段和畸形嵌套字段均拒绝；HTTP 状态、工具启动数、事件数和视口宽度使用
严格整数检查，布尔值、浮点数和数字字符串不能冒充计数。
候选身份覆盖所有 tracked 文件和非忽略的 untracked 文件，包括 YAML、INI、
TOML、构建配置、测试配置、资源文件及未知扩展名；只按明确规则排除运行时目录、
缓存、`.idea/` 和秘密类文件。排除项及未知扩展名策略记录在候选快照中。
候选 snapshot 先枚举一次 scope，再把每个包含文件一次性捕获为不可变字节映射；
逐文件哈希、变更文件 manifest 和完整 code fingerprint 全部从这份映射派生。
完整捕获结束后会重读全部捕获字节，并逐组件 no-follow 核对每个文件的
device/inode/mode/size/mtime/ctime。该全量文件 pass 之后重新核对 Git
HEAD/status、branch、完整候选路径集合和 change set，再执行第二轮全量文件 pass，
最后再次核对 Git/path 集合并以该结果作为返回边界。每个检查点必须与捕获代次一致；
持续到检查点仍存在的中间目录 symlink、跨文件修改、恢复式修改、文件替换、路径增删
或 Git 状态变化会失败关闭，否则快照不能标记为 `CANDIDATE_IDENTITY_VERIFIED`。
两个检查点之间发生、并在下一检查点前恢复的瞬态 ABA 不保证被检测。该保证是
检查点一致性，不是对整个时间区间的连续原子性，也不是物理文件系统快照；最终检查
结束后忽略协调的并发写入同样超出保证范围。
摘要 `status=PASS_EVIDENCE_INTEGRITY` 只表示已提供的证据内部一致且完整；
`candidate_acceptance=ACCEPTED_FOR_MERGE_REVIEW` 且
`review_status.merge_ready=true` 才表示最终接受条件满足。只使用 legacy
`run` + `summarize` 而没有当前候选运行时证据时，仍会保持
`NOT_AVAILABLE`、`NOT_VERIFIED`、`BOOTSTRAP_INCOMPLETE` 或 `BLOCKED` 等非接受状态。
review lane 必须提供唯一、无重复的 `covered_scope` 与 `uncovered_scope`，manifest
与机器结果必须一致；任一未覆盖 scope 会进入权威 `unverified_scopes` 并阻断接受，
direct 与 content-equivalence 路径均不例外。
摘要是唯一权威记录。`acceptance-status.json` 使用
`task-control-center-acceptance-status-v2`，明确标记
`authoritative=false`，不再包含 `merge_ready=true`。每次写入摘要前先移除旧状态，
再原子写入 `IN_PROGRESS`；失败时写 `BLOCKED`，成功时只记录摘要身份与哈希并
写 `COMPLETED`。如果状态失效或原子替换失败，结果只能是无状态或非权威状态；
历史摘要字节不会被删除或覆盖。消费者必须调用
`validate_summary_is_current()`/`validate_acceptance_status()`，不能仅凭状态文件
判断成功。真实进程终止仍未进行故障注入，属于明确未验证边界。
摘要包含版本化 `validation_input`：运行元数据路径、运行元数据 SHA-256、Bundle
路径和生成器版本。`validate_summary_is_current()` 会重新读取当前元数据、重新执行
Bundle/审查/测试/冻结证据门禁并重建摘要，只有逐字节一致才返回成功。候选身份
正确但证据、审查或接受字段被伪造的摘要会被拒绝。非权威状态中的
`evidence_integrity_status` 和 `candidate_acceptance` 也必须与权威摘要一致。
替换理由必须通过调用方的独立 `expected_replacement_reason` 提供；摘要中的理由
只参与比较，并用 `validation_input.replacement_reason_sha256` 绑定。

测试计数必须使用严格整数，布尔值不接受。成功结果还要求
`failed/errors/expected_failures/unexpected_successes` 全为零；被跳过的必需
控制测试会导致失败关闭。测试结果 schema v3 还记录由当前过滤规则生成的测试
发现清单；验证时会重新执行发现，要求 discovered/selected/filtered 的精确排序
集合、规则版本哈希及结果清单哈希完全一致。每个已发现测试必须恰好属于
selected 或 filtered，filtered ID 必须命中当前规则，skipped ID 必须唯一且属于
selected。current schema 还需记录实际交给 runner 的固定 collection 以及每个 ID
的终态。
discovery、执行清单和 counts 来自同一 collection；缺少、重复、额外或无法归属的
测试 ID 会使结果失败关闭，执行后不会再次 discovery 来补写“实际执行”证据。
mutation 的 `execution_evidence` 使用逐 scope 的封闭 schema，`in_process` 必须有
命中 trace 与真实失败命令；aggregate 从逐项记录重算。验证器还会从当前候选源在
隔离项目快照中独立重跑全部 selected mutation，报告只有与重跑结果完全一致才可
参与接受，不能只靠同步修改 JSON 字段及其哈希。
该重跑仍使用候选提供的 runner，因此只称为“隔离重跑”，不声称为执行真实性
独立证明。生产默认 `MUTATION_EXECUTION_TRUST_SUPPORTED=false`，权威 gate 会把
`mutation_execution_trust` 标为 `UNSUPPORTED`，不会产生成功接受。
Mutation patch 路径现在必须是规范化相对路径；runner 通过目录 FD、`O_NOFOLLOW`
逐组件打开目标，拒绝绝对路径、反斜杠、驱动器语法、`..`、符号链接、特殊文件、
文件身份变化和验证后内容变化。混合合法/非法 patch 在写入前整体拒绝。
源项目复制把 `pyproject.toml`、`src` 和 `tests` 纳入同一个聚合源代次：
先打开并持有全部源对象，统一复制，再核对每个组件和完整 source tree，最后重新
打开源根和组件路径比较 device/inode，并在返回前再执行一次完整 held-tree/file
身份复核。mutation private staging 本身也先捕获一份不可变候选 byte map，逐文件
直接从该 map 写入，并在 staging 前后核对源路径集合和代次；因此不会先形成混合
代次的 staging 再交给聚合复制。最终 held-tree/file 复核是声明的有界返回边界；
此后发生且未在后续上层 fingerprint 复核中持续存在的瞬态源变化不在该函数保证内。
目标根和单文件目标父目录同样由已持有目录 FD 访问；target 路径替换不会把写入重定向
到外部。每个创建的文件和目录都会登记父目录相对路径及 device/inode；失败清理只
移除同一事务 ledger 中 identity 仍匹配的路径项，不匹配或无法验证的对象保留并记录
为 residual。创建动作开始前先记录 pending intent；若创建成功但 identity 查询或
ledger 终结失败，该路径保持 pending，不被视为不存在，清理会保留并报告 residual。
目录创建先使用不可预测临时名，随后打开并 fstat 该 held directory FD，以 FD 身份
完成 ledger 绑定后才提交到目标名。macOS 使用 `renameatx_np(RENAME_EXCL)` 提供
原子 no-replace 提交：目标已存在时保留原对象并失败；该原语不可用或其他平台
无法提供同等语义时失败关闭，不退化到普通 rename。最终再核对目标名仍指向同一
device/inode。空源目录复制成功后 target root 会被保留，而不是进入失败清理。
tree target root 在 mkdir 前同样登记 intent，并在打开后复核私有权限。
symbolic link、hard link、
special file、权限/读取错误和平台原语缺失均失败关闭。
cleanup 采用严格的独占私有 staging tree 契约：target/staging 根必须是当前有效用户
拥有、无 group/other 权限的已持有目录；macOS 还必须通过 held-FD ACL 检查，
发现扩展 ACL 或 ACL 能力无法确认时失败关闭。该检查也覆盖由上级目录继承到
mutation staging root 的真实 ACL。staging root 先创建、打开并固定 device/inode，
完成 owner/mode/ACL 校验后才写入任何 payload；后续写入始终通过同一 held root 完成。
ACL API 只有明确返回 `ENOENT` 时才视为没有扩展 ACL，`NULL/errno=0`、查询错误和
能力不确定都失败关闭。tree target root 在创建并打开后会再次执行同一
owner/mode/ACL 校验。所有仍需关闭的文件/目录 FD 会逐个尝试关闭，close 期间即使
出现 `KeyboardInterrupt`/`SystemExit` 也不会停止其余 owned FD 的清理；存在首要
异常时 close failure 只追加诊断与 residual，不覆盖首要异常，成功操作后的 close
failure 则使调用失败并按最终文件系统状态报告 residual 或 `UNKNOWN`。FD close
诊断与对象 residual 分开表示：已经成功删除的对象不会仅因后续 close failure 被
误报为仍存在。POSIX 没有原子 compare-and-unlink，
因此不支持同一主体在 identity check 与 unlink 之间并发替换内部路径；能观察到的
替换会保留并诊断，不能据此宣称任意并发删除安全。tree copy 在创建 target 前比较
held source/target-parent 的 device/inode 与 source 目录树身份，拒绝
source/target 相同、包含或已存在目标，避免递归自复制。
mutation 结果中的 `residual.status` 与 `residual.paths` 会把保留对象传到实际
机器结果，而不是只存在于内部异常 note；无法可靠判断对象最终状态时使用
`UNKNOWN`，不伪造 `NONE` 或 `PRESERVED`。snapshot replacement 或 tree commit
成功后的异常会通过 held directory FD 重新观察目标内容和模式，只有确认旧目标才视为
未提交；确认新目标时报告已提交 residual，无法确认时报告 `UNKNOWN`。namespace
replace 的成功与 durability 确认分开记录：replace 已返回后，即使目录 `fsync`
失败或目标随后被外部替换，`transaction_state` 仍为 `COMMITTED`，
`durability_state` 可为 `UNKNOWN`，`target_state` 单独记录 `COMMITTED`、
`CHANGED` 或 `UNKNOWN`。原生 replace 与内存提交事实的写入位于同一 SIGINT
屏蔽区间；若异常发生在调用方局部标志赋值之前，异常处理仍读取已经记录的提交事实。
如果当前平台无法建立 SIGINT mask，replace 不会开始，而是以 `UNSUPPORTED`
失败关闭。mask 恢复失败或 pending SIGINT 出现时，原 primary error 保持为顶层
错误，恢复失败写入 `secondary_exceptions` 和 `signal_mask_state`。
staging owner
通过上下文交接，调用方未成功接管前由原 owner 清理；`dup`、child open、初始
dot-open、临时 FD 和 traversal FD 都必须先登记到 owner，再恢复 SIGINT mask。
ledger 清理区分已确认保留、已确认删除、存在性/所有权未知和 close 歧义；
`UNKNOWN` 在机器结果中优先于路径性 `PRESERVED`，不会把未确认状态降级为干净结果。
Evidence staging 只有最终内容与 mode 都被 held-FD 检查确认后才返回成功；
`UNCOMMITTED` 和 `UNKNOWN` 都必须失败。异常包装会把 transaction state、
residual paths、unknown 标志和 close diagnostics 复制到公开顶层异常，而不是只留在
`__cause__`。FD 注册时保存原始 device/inode，并创建一个不对调用方暴露的 `dup`
锚点。close 前要求当前 FD 仍具有注册身份，并通过锚点上的 advisory `flock`
确认当前数字 FD 与注册记录共享同一打开实例；相同 inode 但后来重新打开的对象会
因独立打开描述无法共享该锁而拒绝关闭。没有注册记录的裸整数 FD、锁能力不可用、
外部关闭后复用、观察失败或其他不确定情形均不执行 native close。anchor 自身也必须
在当前数字 FD 上保持可观察的注册对象身份；anchor 被关闭、替换、无法观察，或
anchor/owner 无法证明共享同一打开实例时，两个相关数字 FD 都保留，状态记为
`UNKNOWN` 或 `REUSED_OR_REPLACED`，绝不会通过关闭 anchor 来清理不确定性。观察与
close 之间仍存在不可原子的窗口，因此不支持任意同进程恶意代码或无协调外部写入。
同一 owner 集合的 register、adopt、single-FD cleanup、bulk cleanup 和 anchor
transfer 使用 process-local re-entrant retirement lock 串行化；遵守该生命周期协议
的并发调用者不会通过两次 `close` 消耗同一个退休机会。primary close 失败时 anchor
和 ownership record 保留，自动清理队列不会立即重试同一数字 FD；只有显式后续处置
可以再次检查。`LOCK_UN` 失败在需要显式解锁的 transfer/bulk 路径中会保留 FD 并报告
`UNKNOWN`，不会静默继续为成功。
可执行所有权会退役，但状态写入 `fd_close_states`，未解决项同时写入
`unresolved_fds`。`close()` 返回 `EINTR`/`OSError` 本身不被视为关闭证据。
Evidence CLI 的 `--json-errors` 在失败时向 stderr 输出
`task-control-center-error-v1`：transaction state 只能是
`COMMITTED`/`UNCOMMITTED`/`UNKNOWN`，residual 独立使用
`PRESENT`/`ABSENT`/`UNKNOWN`，同时保留 durability/target state、
`fd_close_states`、`unresolved_fds`、close diagnostics、primary exception 和
exception notes、context exception，以及 `signal_mask_state`、
`secondary_exceptions`；residual 或 signal-state 属性访问失败、signal-state
非法类型以及 cause/context getter 失败均按 `UNKNOWN` 输出并记录诊断，不能覆盖
primary error。属性存在性通过静态查找与值读取分离判断，因此 property 抛出的
`AttributeError` 不会被误报为 `NOT_APPLICABLE`；状态字段要求精确的内建 `str`
类型。正常序列化失败时使用不依赖 `json.dumps` 的最小 fallback，并继续保留原始
primary type/message。fallback 输出或 stderr 写入失败时返回受控失败退出码，
不会声称 JSON 已成功交付。
测试执行同样默认
`TEST_EXECUTION_TRUST_SUPPORTED=false`，因为候选模块与 unittest runner 共享
Python 进程。当前实现还检测 `TextTestRunner`、suite、test case 和 result 原语是否
被候选测试替换；检测失败立即关闭。即使候选伪造 runner 产生全 passed，权威 gate
仍保持 `test_execution_trust=UNSUPPORTED`、`merge_ready=false`。
`unexpectedSuccesses` 使用独立于 `(test, detail)` 集合的处理路径：保留 TestCase
ID，记录为 `unexpected_success`，只计入 `unexpected_successes`，并拒绝整体成功；
result 与 log 仍会完整写出，不能因元组解包错误丢失结果。
`subTest` 按父测试方法作为一个 selected 执行单元；同一父方法的多个成功或失败
subtest 事件合并为一个终态，冲突终态失败关闭。
测试执行与 rediscovery 共用同一 `sys.path` 初始化；任何 `_FailedTest`、导入失败
或 discovery 异常都会作为证据错误失败关闭。
Mutation 验证会从固定根重新读取 `tests/mutation_cases.json`、重新执行版本化过滤，
并逐项比较 selected/filtered case ID、cases 文件和 report results。遗漏、伪造、
重复、未知 case，或只调整 filtered count 保持算术相等，都会失败关闭。
候选指纹和 Git 候选枚举中的候选内容始终从已打开的候选根 FD 读取。Git、测试
发现、测试执行和 mutation 子进程不再使用路径型 `cwd`：父进程安全打开并核对仓库
目录 FD，子进程只接收该 FD 并在 exec 前调用 `fchdir()`。已支持的 macOS/POSIX
环境因此把子进程固定到选定 inode；执行期间将仓库路径替换为另一目录再恢复，不能
让子进程扫描替代仓库。平台缺少 `fchdir`/`pass_fds` 时失败关闭。
committed-range 清单改用 `git diff -z` 的 NUL 分隔输出，合法 Unicode、空格及
换行文件名不再经过 Git 的 quoted-path 转义；原有越界、绝对路径、保护路径和
symlink 检查保持不变。
content-equivalence 接受路径记录
`current_target_review_resolution=CONTENT_EQUIVALENT_CLEAR`，表示该 scope 已由
验证过的内容等价 binding 满足；direct target-head review 记录 `DIRECT_CLEAR`。
未验证 scope 不能与 accepted 状态共存。
`termination_failure_reported_success` 与
`container_start_failure_misclassified` 已通过非容器测试替身执行；未确认终止继续
失败关闭，进程启动 `OSError` 明确归类为 `container_start_failed`，无需 Docker、
镜像或网络。真实镜像可用时，另有保留的端到端测试使用真实 evidence/runtime
验证路径并断言失败后没有遗留容器；镜像不可用时该测试明确跳过，替代测试仍保留
相同的无容器泄漏断言。
正式摘要只在发布前完成 currentness 重建验证后写入，
因此后续验证异常不会覆盖历史摘要。若最终 non-authoritative 状态写入失败，
摘要保持有效，状态停留为 `IN_PROGRESS`，不会产生 `COMPLETED` 成功标牌。

## 模拟与未执行

```bash
PYTHONPATH=src python3 -m zevorinth --home "$DEMO_ROOT" run \
  --planner deterministic \
  --goal "模拟写入" \
  --artifact-path simulated.txt \
  --content "not a real file" \
  --simulate
```

结果为 `status=simulated`、`execution_mode=simulated`，没有 artifact 或真实验证证据。

策略阻断为 `blocked + not_executed`；规划失败为 `planning_failed + not_executed`；真实执行失败仍为 `failed + real`。

## 持久化与恢复

- 数据库：`<home>/data/zevorinth.sqlite3`
- 产物：`<home>/artifacts/<task-id>/`
- 状态与事件由 SQLite 事务共同提交。
- 活动租约不会被新实例误判为中断。
- 租约过期后，已有工具证据只做重新验证；无证据的现成文件不会被覆盖。
- 同一任务由 SQLite 原子认领，旧 token 无法覆盖新执行者。
- 真实 `SIGKILL` 恢复测试仍保留。

当前支持同机多进程共享本地 SQLite；不支持跨机器分布式锁或分布式文件系统围栏。默认租约 60 秒，尚无长任务心跳。

## 当前限制

- 只有 `write_text_artifact` 和被条件约束的 `run_tests` 两种操作。
- 每个任务只进行一次 Planner 调用；持久会话支持澄清，但尚无复杂任务分解。
- 在线模型行为仍需用户显式评测，离线结果不能替代。
- 不执行 shell、网络、动态代码、删除或任意路径写入。
- 无多 Agent、自我修改或无限自主循环。
- 未验证断电、磁盘故障和网络文件系统语义。

## CLI 退出码

| 退出码 | 含义 |
| ---: | --- |
| `0` | 真实执行并通过独立验证，或评测全部通过 |
| `1` | 执行/验证失败，或评测存在失败案例 |
| `2` | 参数解析失败或任务不存在 |
| `3` | 等待澄清、模拟完成或取消 |
| `4` | 计划/策略阻断 |
| `5` | 中断或非终态 |
| `6` | SQLite 忙、存储错误或执行权围栏失效 |
| `7` | LLM 配置、规划或结构化输出失败 |

## 文档

- [架构与设计决策](docs/architecture.md)
- [第 1.5 阶段可靠性审计](docs/phase-1.5-audit.md)
- [第 1.5 阶段结果](docs/phase-1.5-results.md)
- [第 2 阶段架构](docs/phase-2-llm-planner.md)
- [第 2 阶段评测结果](docs/phase-2-results.md)
- [第 2.5 阶段预检](docs/phase-2.5-preflight.md)
- [第 2.5 阶段 DeepSeek 在线结果](docs/phase-2.5-online-results.md)
- [第 2.6 阶段响应加固](docs/phase-2.6-response-hardening.md)
- [第 2.6 阶段结果](docs/phase-2.6-results.md)
- [第 2.7 阶段定向复测预检](docs/phase-2.7-preflight.md)
- [第 2.7 阶段定向复测结果](docs/phase-2.7-results.md)
- [第 3 阶段会话与任务关联设计](docs/phase-3-design.md)
- [第 3.3 阶段多轮意图与安全编排设计](docs/phase-3.3-design.md)
- [第 3.4.1 阶段可靠性审计](docs/phase-3.4.1-audit.md)
- [第 3.4.2 阶段崩溃一致性审计](docs/phase-3.4.2-crash-consistency-audit.md)
- [第 3.4.3 阶段文件副作用 Fencing 审计](docs/phase-3.4.3-side-effect-fencing-audit.md)
- [第 3.4.4 阶段 Lease 接管协调审计](docs/phase-3.4.4-lease-takeover-coordination-audit.md)
- [第 3.4.5 阶段 Lease 续租审计](docs/phase-3.4.5-lease-renewal-audit.md)
- [第 3.4.6 阶段执行预算与恢复审计](docs/phase-3.4.6-execution-budget-recovery-audit.md)
- [第 3.5 阶段上下文连续性与计划修订](docs/phase-3.5-design.md)
- [第 3.6 阶段协作取消与端到端验收](docs/phase-3.6-acceptance.md)
- [第 3.7 阶段发布准备与 Provider 边界](docs/phase-3.7-release-readiness.md)
- [第 3.9 阶段受限真实模型定向试点](docs/phase-3.9-real-model-pilot.md)
- [第 3.10 阶段离线模型评测与安全门禁](docs/phase-3.10-safety-gate.md)
- [第 3.11 阶段指标、覆盖率与变异审计](docs/phase-3.11-metric-coverage-mutation-audit.md)
- [第 3.12 阶段可复现门禁与证据归属审计](docs/phase-3.12-reproducible-gates-audit.md)
- [第 3.13 阶段 E2E 证据来源与复合变异审计](docs/phase-3.13-e2e-provenance-compound-mutation-audit.md)
- [第 3.14 阶段生命周期证据与可复现审计产物](docs/phase-3.14-lifecycle-evidence-reproducibility-audit.md)
- [第 3.15 阶段离线端到端用户验收](docs/phase-3.15-offline-user-acceptance-audit.md)
- [第 3.16 阶段受控本地测试执行审计](docs/phase-3.16-controlled-test-execution-audit.md)
- [第 3.17 阶段工作区副作用审计与恢复](docs/phase-3.17-workspace-side-effect-audit.md)
- [第 3.18 阶段工作区副作用治理与安全清理](docs/phase-3.18-workspace-effect-governance-audit.md)
- [第 3.19 阶段强化清理与文件系统竞态审计](docs/phase-3.19-hardened-cleanup-audit.md)
- [第 3.20 阶段清理隔离、恢复与证据对账](docs/phase-3.20-cleanup-quarantine-recovery-audit.md)
- [第 3.21 阶段操作系统隔离与持久化操作安全](docs/phase-3.21-os-isolation-durable-safety-audit.md)
- [第 3.22 阶段隔离边界验证与失败关闭执行](docs/phase-3.22-isolation-boundary-audit.md)
- [第 3.23 阶段读取隔离与进程生命周期验证](docs/phase-3.23-read-confinement-process-lifecycle-audit.md)
- [第 3.24 阶段隔离后端决策与边界验收](docs/phase-3.24-isolation-backend-decision-audit.md)
- [第 3.25 阶段容器隔离后端验证](docs/phase-3.25-container-isolation-audit.md)
- [第 3.26 阶段容器隔离验证与证据闭环](docs/phase-3.26-container-isolation-verification-audit.md)
- [第 3.27 阶段隔离证据生命周期与执行前重验](docs/phase-3.27-isolation-evidence-lifecycle-audit.md)
- [第 3.28 阶段隔离证据持久化与完整性保护](docs/phase-3.28-isolation-evidence-persistence-audit.md)
- [第 3.29 阶段 run_tests 容器化迁移审计](docs/phase-3.29-run-tests-container-migration-audit.md)
- [第 3.30 阶段 run_tests 容器执行链验收](docs/phase-3.30-run-tests-container-execution-audit.md)
- [第 3.31 阶段容器生命周期、产物回收与 host 授权](docs/phase-3.31-container-lifecycle-artifat-audit.md)
- [第 3.32 阶段容器超时/取消闭环与产物清单](docs/phase-3.32-container-timeout-cancel-artifact-audit.md)
- [第 3.33 阶段资源边界与失败恢复验收](docs/phase-3.33-resource-limits-failure-recovery-audit.md)
- [第 3.34 阶段内核文件上限、预算与恢复幂等](docs/phase-3.34-fsize-budget-recovery-audit.md)
- [第 3.35 阶段受限真实模型试点复跑](docs/phase-3.35-real-model-pilot-rerun.md)
- [第 3.36 阶段真实模型驱动的执行闭环](docs/phase-3.36-real-model-execution-audit.md)
- [第 3.37 阶段隔离证据自动准备](docs/phase-3.37-isolation-evidence-provisioning-audit.md)
- [第 3.38 阶段 LLM Planner 的 run_tests 能力审计](docs/phase-3.38-llm-planner-run-tests-capability-audit.md)
- [第 3.39 阶段 run_tests 协议扩展（离线）](docs/phase-3.39-run-tests-protocol-extension-audit.md)
- [第 3.40 阶段单次真实模型闭环验证](docs/phase-3.40-single-call-real-model-closure-audit.md)
- [第 3.41 阶段 LLM Planner instructions 审计](docs/phase-3.41-llm-planner-instructions-audit.md)
- [第 3.41 阶段修复后单次真实调用验证](docs/phase-3.41-post-fix-single-call-verification.md)
- [第 3.42 阶段候选诊断与供应商字段兼容性](docs/phase-3.42-candidate-diagnostics-audit.md)
- [第 3.43 阶段诊断契约与测试隔离审计](docs/phase-3.43-diagnostics-contract-test-isolation-audit.md)
- [第 3.44 阶段端到端验证任务书](docs/phase-3.44-end-to-end-verification-taskbook.md)
- [第 3.44 阶段端到端验证审计（BLOCKED）](docs/phase-3.44-end-to-end-verification-audit.md)
- [第 3.45 阶段可信参数契约与离线执行闭环](docs/phase-3.45-trusted-parameter-contract-audit.md)
- [第 3.46 阶段真实模型端到端验证](docs/phase-3.46-real-model-end-to-end-audit.md)
- [第 3.47 阶段执行计划可追溯性与恢复加固](docs/phase-3.47-execution-plan-traceability-audit.md)
- [第 3.48 阶段真实模型最终执行计划验证](docs/phase-3.48-real-model-final-plan-verification.md)
- [第 3.49 阶段执行失败与恢复可靠性审计](docs/phase-3.49-failure-recovery-audit.md)
- [第 3.50 阶段恢复边界与资源生命周期审计](docs/phase-3.50-recovery-resource-lifecycle-audit.md)
- [第 3.51 阶段真实模型执行后的受控清理验证](docs/phase-3.51-real-model-cleanup-verification.md)
- [第 3.52 阶段清理恢复、并发与真实异常路径审计](docs/phase-3.52-cleanup-recovery-concurrency-audit.md)
- [第 3.53 阶段任务生命周期与进程级恢复审计](docs/phase-3.53-task-lifecycle-process-recovery-audit.md)
- [第 3.54 阶段真实模型取消路径验证](docs/phase-3.54-real-model-cancel-verification.md)
- [第 3.55 阶段真实模型验证器拒绝路径](docs/phase-3.55-real-model-verifier-rejection.md)
- [第 3.56 阶段真实模型容器启动失败](docs/phase-3.56-real-model-startup-failure.md)
- [第 3.57 阶段持久任务生命周期与进程级恢复审计](docs/phase-3.57-durable-task-lifecycle-process-recovery-audit.md)
- [第 3.58 阶段生命周期审计覆盖与恢复决策完整性](docs/phase-3.58-lifecycle-audit-coverage-recovery-integrity.md)
- [第 3.59 阶段持久化故障与恢复决策验证](docs/phase-3.59-persistence-fault-recovery-decisions.md)
- [第 3.60 阶段运行时故障与恢复一致性](docs/phase-3.60-runtime-fault-recovery-consistency.md)
- [第 3.61 阶段执行结果持久化与恢复边界](docs/phase-3.61-result-persistence-recovery-boundaries.md)
- [第 3.62 阶段数据库与证据完整性故障审计](docs/phase-3.62-database-evidence-integrity-audit.md)
- [第 3.63 阶段恢复预演与人工审批协议](docs/phase-3.63-recovery-preview-approval-protocol.md)
- [第 3.64 阶段持久化审批与授权边界](docs/phase-3.64-persistent-approval-authorization.md)
- [执行记录](docs/execution-log.md)
- [文件清单](docs/file-manifest.md)
