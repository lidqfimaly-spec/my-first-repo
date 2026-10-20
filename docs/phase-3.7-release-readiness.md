# Zevorinth Phase 3.7：Release Readiness & Provider Boundary Audit

状态：**PASS WITH LIMITATIONS**

本阶段未调用真实模型 API、未读取 `.env`、未联网安装依赖、未推送远程。

## 1. 基线

- 基线：`5009ede3f5b9a03c3ab81c16952a7eacb0d5c021`
- 分支：`phase-2.6-response-hardening`
- Schema：v4。

## 2. 冻结的核心安全不变量

| 领域 | 必须保持 |
| --- | --- |
| Lease | token/attempt/generation/expiry 同时校验；旧 generation 不能提交；活动 Lease 不可强制取消 |
| 执行预算 | 首次 claim 持久化 deadline；续租只延长 authority，不重置预算；超时不启动新工具 |
| 计划快照 | TOOL_STARTED 固化 plan version/snapshot；执行证据不能证明其他计划 |
| 文件发布 | attempt staging；file lock 后 SQLite authority；最终文件不可覆盖 |
| 取消 | request 与 cancelled 分离；只有当前 owner 在检查点处理 |
| 独立验证 | Verifier 读真实文件；不信任工具 success；异常/无效结果 fail closed |
| 恢复 | 有 evidence 只读验证；无 evidence 且文件存在时拒绝覆盖 |
| 终态 | task_succeeded 与成功状态同事务；失败事务不留等价成功状态 |

禁止操作：

- 外部进程直接把活动 Task 改为 CANCELLED；
- 续租复活已过期或被接管的 Lease；
- 计划修订清除 tool/artifact/预算；
- 修订后使用旧验证结果提交成功；
- 旧执行者覆盖已发布文件；
- 模拟执行被报告为真实文件成功；
- Context 摘要替代原始消息或 Verifier evidence。

## 3. 交付可用性

已验证：

- `python -m zevorinth --version`；
- `doctor --json` 报告 schema v4；
- 全新 home 初始化 v4；
- 确定性 CLI 创建并验证文件；
- 不存在 Task 返回退出码 2 且无 traceback；
- package-data 包含 evals JSON；
- 现有 v2/v3/v4 迁移与回滚测试继续通过；
- 现有 SIGKILL、lease/fencing、预算与恢复测试继续通过。

Phase 3.8 已完成真正的干净环境离线安装：

- 使用本地、离线的 Python 3.12 构建环境构建 wheel/sdist；
- 构建环境：setuptools 81.0.0、wheel 0.46.3、build 1.4.1；
- 目标为全新 Python 3.14.6 venv；
- `pip install --no-index --no-deps zevorinth-0.3.0-py3-none-any.whl` 成功；
- wheel、sdist、CLI、v4 初始化、资源加载和最小独立验证任务全部通过；
- `pip check` 通过；
- runtime dependencies 为空。

构建工具只存在于本地构建环境，不复制到目标 venv；目标 venv 不需要构建后端。

最终离线构建证据：

```text
artifacts/packaging/phase-3.8-release-1792461734/
zevorinth-0.3.0-py3-none-any.whl
sha256=adf1749a6d9c2c4f441cf6e942c8a40d57f15720805401d5daa4e64b1515e947
zevorinth-0.3.0.tar.gz
sha256=0cb9110c1a2dc00bbc169858a2ab79cae9f8833d281e839b1b8f9575ba403369
release-check.json
sha256=2a4738027b31ad5e8f3d1f9a0088f39acd832455e78483f13b7102f251b75532
```

构建依赖文件清单哈希：

```text
setuptools 81.0.0 cc160ff8d9690eaaa87a6850a76b40aa59a321fe1e92e0417454d04688884bf5
wheel 0.46.3 dc24e9d541ccafc794ed1b062378fda06e9044b7766150b131261bf617d4a06b
build 1.4.1 17d3fbb657e416e01612dbf1585d90a2a86f326d6c534c5402b331383c11e2fb
```

## 4. Provider Boundary

审计的离线测试：

| 边界 | 测试 |
| --- | --- |
| 有效结构化计划 | `test_valid_structured_plan_uses_existing_execution_core` |
| Schema 禁止额外能力 | `test_structured_schema_forbids_extra_capabilities` |
| SDK 请求无工具 | `test_official_sdk_adapter_uses_bounded_no_tool_request` |
| malformed / invalid JSON | `test_empty_invalid_json_and_invalid_status_have_safe_diagnostics` |
| refusal / incomplete / timeout / connection | `test_refusal_incomplete_timeout_and_connection_are_safe_failures` |
| provider error 白名单 | `test_api_error_keeps_only_whitelisted_diagnostics` |
| 不可重试 SDK 版本错误 | `test_incompatible_sdk_version_is_rejected_before_client_creation` |
| 缺配置不调用 client | `test_missing_configuration_never_calls_client` |
| 模型候选仍经过 Policy | `test_policy_still_runs_after_valid_llm_candidate` |
| 越权路径/操作 | `test_unsafe_path_and_operation_are_locally_rejected` |
| 模型不能直接调用工具 | LLMPlanner 接口不暴露 writer/tool/store；TaskService 始终独立执行 |

离线测试只使用 fake/scripted client 或注入构造器，真实 API 调用为 0。

## 5. 扩展验收

`tests/e2e_acceptance_runner.py` 现有 11 场景：

| 类别 | 场景 |
| --- | --- |
| success | 目标到独立验证文件 |
| revision | 追加约束与版本化修订 |
| cancellation | 协作取消 |
| recovery | SIGKILL 后读回恢复 |
| recovery_evidence_gap | 文件存在但 evidence 缺失 |
| verification_veto | Verifier 否决 |
| idempotency | 重复/过期请求不重复副作用 |
| budget_cancel_race | 预算耗尽与取消竞争 |
| revision_execution_race | 执行证据阻止修订 |
| evidence_corruption | 文件被改动后拒绝恢复成功 |
| verifier_exception | Verifier 异常 fail closed |

最近指标：

```text
scenario_count=11
success_rate=1.0
executable_success_rate=1.0
recovery_success_rate=1.0
correctly_blocked=5
duplicate_side_effects=0
cancellation_handled=true
evidence_complete_rate=0.45454545454545453
failure_class_counts={"budget":1,"verification":3}
```

`evidence_complete_rate` 包含故意无最终证据的正确阻断场景，不代表产品失败率。

机器报告：

```text
artifacts/audits/phase-3.7-acceptance.json
```

## 6. 真实模型试点门槛

必须另行取得用户明确授权，不能由本阶段自动进入。

授权前条件：

- 明确 Provider、模型标识和 API 地址；
- 对应 Key 仅存在于用户环境，不写入项目或聊天；
- 允许发送的数据仅限合成评测案例，不含用户文件或隐私；
- 明确单次预算上限和累计调用上限；
- 当前预检必须 `network_request_sent=false`。

建议边界：

- 案例：`clarify_ambiguous_goal`、`unsupported_shell`；
- 最大真实请求：2；
- 自动重试：0；
- 超时和 max output tokens：沿用配置并记录；
- 独立临时 SQLite/artifact 目录；
- 输出文件不得覆盖历史结果。

失败停止条件：

- 配置或 Provider 不匹配；
- 任一请求超时、拒答、无效结构或供应商错误；
- 澄清/不支持案例出现工具调用或文件副作用；
- 请求数超过批准上限；
- 证据归属、SQLite 完整性或退出码异常。

独立验收：

- 两个案例分类正确；
- 工具调用和真实文件副作用均为 0；
- 失败记录保留；
- 只允许声明“这两个固定案例在本次运行中的结果”，不推断通用能力。

## 7. 测试结果

| 项目 | 结果 |
| --- | --- |
| 系统完整 unittest | 219/219，exit 0 |
| 隔离环境完整 unittest | 219/219，exit 0 |
| release readiness | 3/3，连续 20 轮 0 失败 |
| E2E acceptance | 11/11，连续 10 轮 0 失败 |
| clean Python 3.14 wheel install | passed |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

coverage / mutation framework：NOT RUN，环境未安装且未联网安装。

## 8. 剩余风险

- 目标安装已离线验证；构建工具链本身没有作为独立 wheel 集合提交，复现构建
  仍要求具备相同版本的本地离线 setuptools/wheel/build。
- 真实模型在线行为没有在本阶段测试。
- 文件系统与 SQLite 仍不能共同原子提交。
- 多机、共享文件系统和网络文件系统未验证。
- E2E 使用确定性本地 Planner，不代表真实 LLM 通用规划能力。
