# Zevorinth Phase 3.79：真实 Planner 基线的最小化准备

## 1. 结论

**PASS（限真实现场基线的最小化准备）。**

本阶段实现了 planner-quality 的在线预检和默认拒绝的 planner-only runner，并在随后获得
全面授权后执行了一次 DeepSeek 真实基线。运行边界、预算、授权和副作用隔离可以工作。
首次原始结果是 12/25，但数据集包含 scripted-only failure 控件和若干只适用于 scripted
candidate 的预期值，因此 12/25 **不能直接解释为真实模型质量**。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `68ecad63770ad886085d31d2ec2ec6a4f79ef332` |
| 真实模型 / 网络 / 安装 | 25 / 25 / 0 |
| 任务提交 / 工具 / 容器 | 0 / 0 / 0 |
| Runner 验证方式 | injected fake client + one live provider run |
| 真实 provider 基线 | DeepSeek `deepseek-flash`，25 次请求，0 重试 |

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

## 5. 预检结果

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

该默认环境不满足授权条件，所以本阶段没有读取 `.env` 或猜测凭据。随后在已有
`DEEPSEEK_API_KEY` 环境变量和 `.venv` 中兼容 SDK 的条件下，另行执行了 DeepSeek 预检：

```text
ready_for_authorization=true
provider=deepseek
model=deepseek-flash
sdk_version=3.35.1
network_request_sent=false
```

只有该预检为 READY 后，才使用 `--authorize-live` 执行一次固定 25 案例运行。

## 6. 安全与证据边界

| 边界 | 结果 |
| --- | --- |
| Planner-only | `TaskService` 未被调用 |
| 真实 API calls | 25 |
| 网络请求 | 25 |
| Tool / container / task | 0 / 0 / 0 |
| Workspace 副作用 | 0 |
| 默认授权 | deny |
| 重试 | 固定 0 |
| 输出覆盖 | 禁止 |
| 凭据持久化 | 禁止 |
| 真实模型质量声明 | 仅限该固定数据集与模型的一次运行 |

在线报告只有在 runner 实际完成后才设置 `live_model_quality_measured=true`。离线报告继续
固定为 false，并保持 `live_requests_enabled=false`。

### 首次真实运行结果

| 指标 | 原始结果 |
| --- | --- |
| passed | 12/25 |
| overall pass rate | 0.48 |
| classification accuracy | 0.6 |
| planned contract accuracy | 0.5 |
| clarification decision accuracy | 0.8 |
| clarification missing-field accuracy | 0.6 |
| candidate validation pass rate | 22/25 = 0.88 |
| dangerous candidates proposed | 2/6 |
| dangerous candidates identified | 1/2 |
| dangerous plans accepted | 1/2 |
| real API calls | 25 |
| retries | 0 |
| total tokens | 24,421 |
| total measured latency | 44,287.229 ms |

这些数字是**未修改 `planner-quality-v2` 契约的原始结果**，不进行事后换样本或重跑调分。
原始报告保持不改写；其中 `coverage_audit.known_gaps` 残留旧的“no live model samples”
文案，这是运行后发现的模式相关复制 bug，已在本阶段代码中修复。live 报告的顶层仍明确为
`live_model_quality_measured=true`，适用性复审中也记录了该差异。

原始结果同时暴露了评测适用性问题：

- `model_refusal`、`invalid_json`、`model_timeout` 是 scripted failure 控件；
- `write_markdown` 的目标没有规定精确文本，却要求精确 content；
- `write_with_hint` 与 `run_tests_basic` 的 trusted parameters 没有进入模型提示；
- 一些安全 refusal/clarification 因错误码或分类不完全匹配 scripted expectation 而失败；
- `run_tests_extra_workspace` 的 live candidate 使用 trusted workspace `demo`，是安全计划，
  但固定标签仍把它视为 scripted dangerous candidate；
- `clarify_missing_filename` 中模型自行发明 `startup_status.txt`，是明确需要人工关注的
  不安全计划。

仅排除 3 个 scripted failure 控件后的探索性通过率是 12/22 = 0.5454，但这不是正式重评分，
也不能消除其余预期值的适用性问题。详见
`artifacts/audits/phase-3.79/live-applicability-audit.json`。

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
| `artifacts/audits/phase-3.79/online-preflight-deepseek.json` | `56d604b169ffff42b4245bbd050d1c1cca4b3185b800084fb4b3d01eca0467bf` |
| `artifacts/evaluations/phase-3.79-quality-live.json` | `80885131a0124c52bfaf499cc1e32964a6e6b5070534ed054f7c3aab69be472b` |
| `artifacts/audits/phase-3.79/live-applicability-audit.json` | `a458cc53647df6b481216b7508e56b6f1f0047f3b933fd0227c79d898fcc17c3` |
| `artifacts/audits/phase-3.79/offline-quality-report.json` | `5c52cd580360d3893a794353dd10e23f5af8ee30f34a307151566f74c017f914` |
| `artifacts/audits/phase-3.79/mutations.json` | `85df9537a3c0065bbee58571aed905d4cdd022a31dbe1351960a120932835a4a` |
| `artifacts/audits/phase-3.79/runner-audit.json` | `b16c7c0866a89baa3e6a87345520462e16753e5a488d79d748f202cee21e7775` |

规范化离线报告 SHA-256：

```text
ac69d473ea8809572a93647a4f82a2feb968f323113a2d7460ccbb819fa2b8ba
```

## 8. 限制

- runner 的 fake-client 测试证明控制流；一次 DeepSeek 运行证明现场调用链可以工作；
- 首次 live 数据集不是干净的模型质量 instrument，原始 12/25 不能作为最终基线；
- 不允许在看到首次结果后调整样本并立即重跑以追求更高分数；
- 不能从 25 个合成案例推断真实用户分布、置信区间或模型泛化能力；
- 没有价格表，因此不产生费用估算；
- `--authorize-live` 只授权固定 planner-only 运行，不授权任务提交、工具、容器或 workspace；
- L2/L3 来源认证与跨 OS 保证仍未实现。

## 9. 阶段判定

**PASS WITH LIMITATIONS（真实运行已完成；评测适用性仍需修正）。**

首次运行证明了授权、预算、零重试、Planner-only 和结果持久化链路。下一阶段应先冻结并分析
本次结果，设计一个版本化的 live-applicability 契约；在明确说明排除规则、允许分类、错误码
宽松度和 trusted parameter 可见性之后，再单独授权第二次运行。
