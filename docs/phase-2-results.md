# 第 2 阶段：Planner 与评测结果

日期：2026-10-19
基线提交：`177c3c3 feat: harden task execution and recovery`
工作分支：`phase-2-llm-planner`

## 1. SDK 与官方资料

- OpenAI Python SDK：`3.35.1`
- 安装方式：隔离虚拟环境执行 `pip install -e '.[llm]'`
- API：Responses API，strict JSON Schema
- 客户端：`max_retries=0`
- 调用：每次规划最多一次
- 请求：`store=False`、有界 timeout、有界 output tokens
- 工具：不提供 tools、shell、code interpreter、MCP 或文件访问

实施时 `developers.openai.com` 与 `platform.openai.com` 返回 HTTP 403。API 形状通过隔离环境中的官方 SDK 3.35.1 签名和源码核对；未把搜索摘要当作实现证据。

## 2. 离线测试

完整测试命令：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s tests -v
```

完整套件结果：

```text
Ran 68 tests in 1.874s
OK
```

隔离安装环境中的同一套件：

```text
Ran 68 tests in 1.954s
OK
pip check: No broken requirements found.
```

第 2 阶段新增测试覆盖：

- 有效结构化计划进入现有 TaskService/Verifier/EventStore；
- strict Schema 禁止额外字段；
- 拒答、不完整、超时、连接失败和无效 JSON；
- 缺少 API Key 或模型配置时不调用 Client；
- 越权路径、绝对路径和 shell 操作被本地拒绝；
- LLM 候选仍必须通过 Policy；
- 执行失败仍为 `failed + real`；
- 模拟仍为 `simulated`；
- 模型规划失败不回退到确定性成功；
- 官方 SDK Adapter 的请求不包含 tools，并设置 `max_retries=0`、timeout 和 strict format；
- A1–A6、租约、并发、SQLite 事务和 SIGKILL 测试继续通过。

普通测试完全离线，不读取 API Key 值，不发起网络请求。

## 3. 离线脚本化评测

命令：

```bash
PYTHONPATH=src python3 -m zevorinth eval-planner --offline
```

结果：

```text
Planner evaluation: offline-scripted (28/28 cases passed)
contract_validity_rate: 1.0
classification_accuracy: 1.0
status_accuracy: 1.0
unsafe_block_rate: 1.0
invalid_plan_rate: 0.6428571428571429
planning_failure_rate: 0.32142857142857145
end_to_end_success_rate: 1.0
average_latency_ms: 10.0
model_calls: 26
total_tokens: 3182
```

确定性基线在同一组自然语言案例上：

```text
4/28 cases passed
classification_accuracy = 0.14285714285714285
model_calls = 0
```

解释：

- 这里的 1.0 和 10ms 来自脚本化候选与确定性测试时钟；
- 不能据此声称真实模型具有 100% 分类或端到端能力；
- 26 次调用和 3182 Token 也是脚本化诊断值，不是账单；
- 基线较低主要因为确定性 Planner 不会从自然语言推断缺失的文件名和内容。

## 4. 在线评测状态

未执行真实在线 API 调用。

原因：

- 本次没有提供或确认可用于评测的模型配置；
- 在线运行必须由用户显式启动；
- `eval-planner --online` 在缺少 Key/模型时退出码为 7，且没有网络请求。

在线入口：

```bash
OPENAI_API_KEY=... \
ZEVORINTH_MODEL=... \
PYTHONPATH=src python3 -m zevorinth \
  eval-planner --online --limit 5 --json
```

因此**没有在线准确率、在线延迟、真实 Token 或真实端到端成功率结果**。

## 5. CLI 回归

实际通过：

- `--planner deterministic` 真实任务成功；
- `status` 和 `events` 从 SQLite 查询成功；
- `--planner llm` 缺配置时 `planning_failed`、`not_executed`、退出码 7；
- `doctor` 报告 LLM SDK/Key/模型配置状态，不输出 Key；
- `eval-planner --offline` 退出码 0；
- `eval-planner --online` 缺配置时退出码 7。

隔离安装结果：

- `zevorinth 0.3.0`
- `openai 3.35.1`
- 包内评测 JSON 可由 `importlib.resources` 读取；
- 安装后离线评测仍为 `28/28`，确定性基线 `4/28`；
- 安装后确定性任务 `58481b9d-5325-4ef4-862a-7e6f59de46a2` 产生 32 字节文件；
- 外部 SHA-256 与 Verifier 均为 `4d2ff5c373c96752b73e95acace6cc1c1b0bb3ed4c9d6fc4f7c8be498df37cab`。

## 6. 已知限制

- 在线模型质量、随机性、服务可用性和真实成本尚未测量。
- 只有单文件创建操作。
- 无多轮澄清记忆和复杂计划。
- 本地校验能限制能力和权限，但不能保证模型语义判断总是正确。
- 无长任务租约心跳。
- 无跨主机分布式执行。
- 未验证断电、磁盘故障或网络文件系统。
