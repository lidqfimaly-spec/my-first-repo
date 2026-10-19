# 第 2 阶段：受限 LLM Planner 架构

日期：2026-10-19
起点提交：`177c3c3 feat: harden task execution and recovery`
工作分支：`phase-2-llm-planner`

## 1. 目标与实际范围

本阶段让模型从自然语言目标生成结构化候选，而不是把模型变成执行器。

```text
自然语言目标
  -> LLM Planner（单次调用）
  -> strict structured candidate
  -> 本地字段与业务校验
  -> TaskRequest + 单步 Plan
  -> TaskService 认领与状态机
  -> Policy
  -> 受控文件工具
  -> 独立 Verifier
  -> EventStore
```

当前仍只有一个操作和工具：在任务专属产物目录创建一个文本文件。

## 2. 模块职责

| 模块 | 职责 |
| --- | --- |
| `planner.py` | Planner 协议、确定性 Planner、统一 PlanningOutcome、Plan 构建 |
| `llm_planner.py` | 配置、单次 Responses API 调用、错误分类、调用元数据 |
| `planning_validation.py` | 独立校验不可信候选并构造内部请求 |
| `service.py` | 在租约内调用 Planner，持久化规划结果并继续既有执行链 |
| `evaluation.py` | 离线脚本化与在线评估、指标汇总、确定性基线比较 |
| `evals/planner_cases.json` | 机器可读固定案例 |

## 3. Planner 选择

- 默认：`deterministic`
- 显式 CLI：`run --planner deterministic|llm`
- 环境默认：`ZEVORINTH_PLANNER`
- 未明确选择 LLM 时不会发出模型请求

LLM 模式配置：

```text
OPENAI_API_KEY
ZEVORINTH_MODEL
ZEVORINTH_LLM_TIMEOUT_SECONDS
ZEVORINTH_LLM_MAX_OUTPUT_TOKENS
```

模型名称完全由用户配置，项目不内置固定模型。

## 4. OpenAI SDK 适配

使用官方 `openai` Python SDK 的 Responses API：

- `responses.create`
- strict JSON Schema `text.format`
- `store=False`
- `max_retries=0`
- 单次调用
- 请求级和客户端级 timeout
- `max_output_tokens`

实施时官方文档域返回 403，因此 API 形状通过隔离环境的官方 SDK `3.35.1` 签名及源码确认。在线实际调用未在本轮未授权环境下执行。

## 5. 失败语义

| 失败 | 错误码示例 | 文件工具 |
| --- | --- | --- |
| Key 缺失 | `missing_api_key` | 0 次 |
| 模型未配置 | `missing_model` | 0 次 |
| 拒答 | `model_refusal` | 0 次 |
| 不完整 | `model_response_incomplete` | 0 次 |
| 超时 | `model_timeout` | 0 次 |
| 连接失败 | `model_connection_error` | 0 次 |
| 无效 JSON | `invalid_structured_output` | 0 次 |
| 未知字段 | `invalid_candidate_fields` | 0 次 |
| 越权操作 | `unsupported_operation` | 0 次 |
| 越权路径 | `invalid_artifact_path` | 0 次 |
| 超大目标 | `goal_too_large` | 0 次 |
| 超大候选 | `candidate_too_large` | 0 次 |

这些情况进入 `planning_failed` 或 `blocked`，不会进入 `succeeded`。

## 6. 数据与隐私

- 不记录 API Key。
- `doctor` 只报告 Key 是否配置。
- 不持久化完整模型响应。
- 事件记录模型名、耗时、Token、响应 ID 和输入哈希。
- Task 快照保存执行所必需的最终请求。
- 模型响应不直接写入事件原始载荷。

## 7. 评测

离线：

```bash
PYTHONPATH=src python3 -m zevorinth eval-planner --offline --json
```

在线：

```bash
OPENAI_API_KEY=... \
ZEVORINTH_MODEL=... \
PYTHONPATH=src python3 -m zevorinth \
  eval-planner --online --limit 5 --json
```

两种结果在报告中用 `mode=offline-scripted` 和 `mode=online` 明确区分。

## 8. 当前限制

- 只支持单个文本文件操作。
- 无多轮澄清会话记忆。
- 无复杂多步计划。
- 在线模型质量与成本尚未测量。
- 不保证模型输出的语义正确性，只保证本地契约、权限和产物验证。
- 模型随机性、服务端变化和账号可用性需要后续在线重复评测。
