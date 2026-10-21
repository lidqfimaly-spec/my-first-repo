# Zevorinth Phase 3.79：真实 Planner 基线的最小化准备

## 1. 结论

**PASS（限真实现场基线的最小化准备）。**

本阶段实现了 planner-quality 的在线预检和默认拒绝的 planner-only runner，但没有执行任何
真实 provider 请求。当前验证证明运行边界、预算、授权和副作用隔离可以工作；它**不证明
真实 LLM 的规划质量**。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `68ecad63770ad886085d31d2ec2ec6a4f79ef332` |
| 真实模型 / 网络 / 安装 | 0 / 0 / 0 |
| 任务提交 / 工具 / 容器 | 0 / 0 / 0 |
| Runner 验证方式 | injected fake client |
| 真实 provider 基线 | 未执行 |

## 2. 本阶段补齐的缺口

Phase 3.78 只有协议清单，没有 planner-quality live runner。Phase 3.79 增加：

```text
zevorinth eval-planner-quality --online --preflight --output PATH [--json]
zevorinth eval-planner-quality --online --authorize-live --output PATH [--json]
```

第一条命令只检查配置、SDK 兼容性、固定案例和输出路径，不导入或创建 API client，不发送
网络请求。第二条命令才可能调用模型，且必须显式提供 `--authorize-live`。

未提供 `--authorize-live` 时返回配置错误和退出码 7。输出路径已存在时，在模型调用前拒绝
覆盖。

## 3. 固定运行边界

在线 runner 对固定 25 个案例执行：

- 每个案例恰好一次 `LLMPlanner.plan_request()`；
- 每个计划最多一次 provider 调用；
- 总请求上限 25；
- 0 次重试；
- 每例 timeout 30 秒；
- 每例最多 1500 output tokens；
- 任务提交、工具执行和容器执行均为 0；
- 不修改 workspace；
- 结果路径不可覆盖。

runner 捕获解析后的 canonical candidate 并记录 SHA-256。它不持久化 provider 原文，以维持
现有凭据和敏感文本边界；报告保留解析候选、评分、失败原因、Token、延迟和请求状态。

## 4. 默认拒绝与 CLI 隔离

程序 API 同样默认关闭：

```python
run_live_planner_quality()
# raises EvaluationConfigurationError
```

只有显式传入 `authorized=True` 才进入 runner。CLI 的 `--authorize-live` 是这层授权的人工
入口，不能由环境变量、已有 config 或先前阶段批准自动推导。

测试还验证：

- 预检不会调用 `LLMPlanner.from_environment()`；
- 未授权 CLI 不会启动 runner；
- 授权 CLI 只负责调用 runner 和写入指定路径；
- fake runner 使用 25 次调用，且 `TaskService.submit` 调用即失败；
- 在线报告不声明 `execution_side_effects_enabled=true`。

## 5. 当前环境预检

本阶段实际执行了无副作用预检，结果为：

```text
ready_for_authorization=false
configuration_error=missing_api_key
sdk_version=2.45.0
sdk_compatible=false
network_request_sent=false
```

项目最低 SDK 要求为 `3.35.1`。本阶段没有安装依赖、没有读取 `.env` 或秘密文件，也没有为了
获得 READY 而修改环境或权限。

因此当前环境仍不能授权真实基线。该结果是环境事实，不是本阶段测试失败。

## 6. 安全与证据边界

| 边界 | 结果 |
| --- | --- |
| Planner-only | `TaskService` 未被调用 |
| 真实 API calls | 0 |
| 网络请求 | 0 |
| Tool / container / task | 0 / 0 / 0 |
| Workspace 副作用 | 0 |
| 默认授权 | deny |
| 重试 | 固定 0 |
| 输出覆盖 | 禁止 |
| 凭据持久化 | 禁止 |
| 真实模型质量声明 | false |

在线报告只有在 runner 实际完成后才设置 `live_model_quality_measured=true`。离线报告继续
固定为 false，并保持 `live_requests_enabled=false`。

## 7. 验证结果

| 检查 | 结果 |
| --- | --- |
| Planner quality tests | 19/19 |
| Capability registry tests | 9/9 |
| 定向相关测试 | 58/58 |
| 系统 Python 全量 unittest | 792/792，exit 0 |
| `.venv` 全量 unittest | 792/792，exit 0 |
| Mutation runner | **176 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 176/176 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

全量测试仍有既有 SQLite `ResourceWarning`，本阶段未扩大范围修改 SQLite 生命周期。

审计产物：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.79/online-preflight.json` | `12c7de3691f1bf649e8db1f89eab010beacb3b0f1dbb885f430dc35c3d632bbb` |
| `artifacts/audits/phase-3.79/offline-quality-report.json` | `0df1438985a00f466e05b5656e83660463916f5e4b35d30a6b38f28ab1c6bdd6` |
| `artifacts/audits/phase-3.79/mutations.json` | `679ebfac67e242f13548982d1e71a540f5a000c17a352c68724f91d1583d2616` |
| `artifacts/audits/phase-3.79/runner-audit.json` | `e7d06686e312da89fec3296b255bd7f087f0106eb9f21c470c50e625e24f5c3d` |

规范化离线报告 SHA-256：

```text
ed5a2f3a0e137eb09c11c1c1856d01305584d937f0f7c7d8314b1b95c87e76d9
```

## 8. 限制

- 在线 runner 只用 injected fake client 验证，尚未对任何真实 provider 执行；
- 当前环境 API Key 与 SDK 条件不满足，预检按设计为 NOT READY；
- 不能从 25 个合成案例推断真实用户分布、置信区间或模型泛化能力；
- 没有价格表，因此不产生费用估算；
- `--authorize-live` 只授权固定 planner-only 运行，不授权任务提交、工具、容器或 workspace；
- L2/L3 来源认证与跨 OS 保证仍未实现。

## 9. 阶段判定

**PASS（限于真实模型基线的最小化准备）。**

下一步不是自动执行在线评测，而是在明确批准 provider、精确模型、预算、样本范围、输出路径
和停止条件后运行一次受控预检与基线。若未获得该授权，应转向一个代表性的端到端任务可靠性
评测，而不是继续增加同类离线审计阶段。
