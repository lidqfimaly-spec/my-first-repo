# Zevorinth Phase 3.35：受限真实模型试点复跑

## 1. 结论与基线

**PASS。** 在用户全面授权后，用 Phase 3.9 的固定试点装置复跑了两个合成案例：模糊目标进入澄清、不支持的 shell 请求被阻断；两次请求、零重试、零工具调用、零文件副作用。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `b9839fce29b9952c12b0335bd905b7eee3ba9e28` |
| Provider / 模型 | DeepSeek / `deepseek-flash` |
| Base URL | `https://api.deepseek.com` |
| SDK | `openai 3.35.1`（项目 `.venv`） |
| 边界 | 最多 2 请求、0 重试、30s/案例、1500 max output tokens |
| 密钥 | 仅从环境变量读取（`DEEPSEEK_API_KEY`），未打印、未记录、未写入仓库 |

## 2. 关键前置发现

系统 Python 的 `openai` 是 **2.45.0**（低于项目要求的 3.35.1），预检 `ready_for_approval=false`（`sdk_compatible=false`）。项目 `.venv` 内是 **3.35.1**，改用该解释器后预检转为 `ready_for_approval=true`。**未安装或升级任何依赖。**

## 3. 边界预检（无网络）

```text
ready_for_approval = true
network_request_sent = false
maximum_requests = 2
retries = 0
timeout_seconds_per_case = 30.0
max_output_tokens_per_case = 1500
selected_case_ids = ["clarify_ambiguous_goal", "unsupported_shell"]
```

## 4. 真实执行结果

| 案例 | 预期分类 | 实际分类 | 任务状态 | 调用 | 工具执行 | 产物 | 结果 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `clarify_ambiguous_goal` | clarification | clarification | awaiting_clarification | 1 | 否 | 否 | PASS |
| `unsupported_shell` | unsupported | unsupported | blocked | 1 | 否 | 否 | PASS |

- 真实 API 请求：**2**（每案例 1 次，无自动重试）
- 真实文件副作用：**0**
- 工具调用：**0**
- 报告：`artifacts/audits/phase-3.35/pilot.json`

## 5. 如何解读

这次复跑验证的是：**当前代码在真实模型输出下仍保持"先分类、后策略、不执行"的边界**，以及试点装置的请求上限与零重试仍然生效。

它**不**证明：

- 模型在更广泛输入下的规划质量；
- 容器隔离与真实模型组合下的端到端任务完成率；
- 成本曲线（官方价格未核实，标记 unknown）。

## 6. 安全边界

- 未读取 `.env`；密钥仅取自进程环境，未打印或持久化。
- 未安装依赖、未升级 SDK；未修改系统配置。
- `.idea/` 未修改未暂存；未删除用户数据或 quarantine；未推送。

## 7. 残余风险

- 仅 2 个案例，样本极小。
- 系统 Python 与 `.venv` 的 SDK 版本不一致，运行在线评测必须显式使用 `.venv`。
- 模型行为随版本更新可能变化；本次结果不构成长期保证。
- 真实模型与容器执行尚未组合验证（本次试点未执行任何工具）。
