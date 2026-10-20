# Zevorinth Phase 3.36：真实模型驱动的执行闭环

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 在用户授权使用真实模型后，完成一次有界的"模型规划 → 策略 → 真实文件写入 → 证据 → 独立验证"闭环：1 次模型调用、1 个产物、`artifact_verified`、0 个 unexpected 副作用。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `e0e523da00d214ab88d7f4f3dbdc7ea9c3987de9` |
| Provider / 模型 | DeepSeek / `deepseek-flash` |
| SDK | `openai 3.35.1`（项目 `.venv`；系统 Python 为 2.45.0，不兼容） |
| 模型调用 | **1**（`max_calls_per_plan=1`，重试 0） |
| 密钥 | 仅环境变量 `DEEPSEEK_API_KEY`，未打印/未落盘 |

## 2. 执行链与结果

`TaskService(planner=LLMPlanner.from_environment())` 提交一个结构化写入请求：

| 观察项 | 值 |
| --- | --- |
| planner_mode | `llm` |
| classification | `planned` |
| model_call_attempted | `true` |
| task status | `succeeded` |
| verification | `artifact_verified`（独立 Verifier） |
| artifact | `hello.txt`，sha256 `589c1b56371a85bbbadf97a7a382c0e1efeb76a6b56ce9d8b3dd1e0e32651364` |
| 磁盘文件 sha256 | 与证据一致 |
| 内容 | `hello from the real model` |
| effects.diff.counts | added 2 / expected 2 / unexpected 0 / unknown 0 |

产物证据：`artifacts/audits/phase-3.36/llm-execution.json`（SHA-256 `95ff0195f5cf5e1b2533aedd9a7a6866e7f462f39059b12481378b5db2c9d0c8`）。

## 3. 这个闭环实际验证了什么

1. 真实模型输出经过 `LLMPlanner` 的候选规范化与本地验证，分类为 `planned`；
2. 策略层允许单步写入，执行器在真实文件系统完成原子发布；
3. 证据（`ArtifactEvidence` + `workspace_effects`）与磁盘内容一致；
4. 独立 `ArtifactVerifier` 复核通过，未被执行器自报状态左右；
5. 副作用审计显示只有预期的两处新增（产物 + `.staging`），无越界变化。

## 4. 没有验证什么

- 模型**不负责**生成产物内容：本次内容来自请求参数，模型的作用是分类与规划。让模型自由撰写内容需要另行设计并扩大授权。
- 未在容器后端执行（本次是 `write_text_artifact` 路径，不经过 `run_tests`）。
- 未验证模型在模糊/恶意输入下的规划质量（3.35 只覆盖 2 个合成分类案例）。
- 成本：官方价格未核实，标记 unknown。

## 5. 本次会话的真实模型调用总计

| 阶段 | 请求数 | 重试 | 结果 |
| --- | --- | --- | --- |
| 3.35 试点复跑 | 2 | 0 | clarification / unsupported 均符合预期 |
| 3.36 执行闭环 | 1 | 0 | planned → 产物已验证 |
| **合计** | **3** | **0** | — |

## 6. 安全边界

- 未读取 `.env`；密钥仅取自进程环境。
- 未安装依赖、未升级 SDK（改用项目 `.venv`）。
- 写入仅发生在临时目录；`.idea/` 未修改未暂存；未推送。
- 无容器操作（本次路径不涉及 Docker）。

## 7. 残余风险

- 单一样本，不能代表模型稳定性。
- 模型分类正确不等于工具使用安全；本闭环依赖本地策略与验证器兜底。
- 未覆盖模型对 `run_tests` 的规划（容器路径）。
- 未做成本核算。
