# Zevorinth（衍序）

Zevorinth 是一个以**可验证执行**为核心的 AI Agent 系统。当前完成第 2 阶段：在已验证的可靠执行内核上，增加可选的受限 LLM Planner、严格结构化输出、本地安全校验和离线模型评测体系。第 2.7 阶段已完成两个历史失败案例的定向在线复测，两个固定案例均通过；该结果不代表通用规划能力。

当前只支持一种真实操作：在任务专属产物目录中创建一个文本文件。LLM 只负责规划；认领、状态转换、Policy、真实写入、独立验证和事件持久化全部继续由本地执行内核完成。

## 当前能力

- 确定性 Planner：接收完整的结构化任务，不调用模型。
- LLM Planner：把自然语言目标转换为一次严格 JSON Schema 响应。
- 本地 Candidate Validator：独立检查字段、类型、操作、文件名、内容、响应大小和澄清信息。
- SQLite 执行租约、fencing、并发互斥和崩溃恢复。
- 真实文件写入、独立读回、内容/SHA-256/工具证据验证。
- `real`、`simulated`、`not_executed` 三态区分。
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
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s tests -v

PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q src tests

PYTHONPATH=src python3 -m zevorinth eval-planner --offline --json
```

离线评测使用脚本化候选，不联网、不需要 API Key。结果不能代表真实模型质量。

评测数据集位于 `src/zevorinth/evals/planner_cases.json`，覆盖正常计划、澄清、不支持、shell/代码请求、路径逃逸、覆盖、提示注入、超长输入、拒答、超时、连接失败和无效结构化输出。

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

- 只有一种操作和一种真实工具。
- 只支持单次规划，不支持多轮会话记忆或复杂任务分解。
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
- [执行记录](docs/execution-log.md)
- [文件清单](docs/file-manifest.md)
