# Zevorinth Phase 3.39：run_tests 协议扩展（离线）

## 1. 结论与基线

**PASS WITH LIMITATIONS（协议扩展已实现，离线验证通过）。** LLM 候选 schema 与运行时校验器现在都接受 `run_tests`，但模型只能提供受限的 `test_target`；workspace、runner、镜像、执行后端与证据仍由系统注入。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `88995f7a7d4bbf590f8f8709e667e8ada1826509` |
| 分支 | `phase-2.6-response-hardening` |
| 真实模型 API 请求 | **0** |

## 2. 协议变更

`src/zevorinth/llm_planner.py` `CANDIDATE_JSON_SCHEMA`：

- `operation.enum` → `["write_text_artifact", "run_tests"]`
- 新增 `test_target`（string, maxLength 200，或 null）
- 加入 `required`

`src/zevorinth/planning_validation.py`：

- `CANDIDATE_FIELDS` 增加 `test_target`；`_DEFAULTS` 同步；
- `ALLOWED_MISSING_FIELDS` 增加 `workspace`、`test_target`（澄清可请求它们）；
- `_plan` 按 operation 分流；写路径保持"缺 artifact_path/content → `missing_candidate_fields`"的原有语义；
- 新增 `_plan_test`：只接受模型给出的 `test_target`，用 `TARGET_PATTERN` 校验；`workspace`/`runner` 等取自**可信的 base_request**；随后调用 `validate_parameters` + `build_test_plan`。

## 3. 信任边界

| 字段 | 来源 | 模型可否提供 |
| --- | --- | --- |
| `target` | 模型（受 `^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$` 约束） | 是 |
| `workspace` | 任务请求（可信） | **否**（不在 schema，出现即 `unknown_candidate_fields`） |
| `runner` | 默认 `unittest` | 否 |
| `image` | 任务参数 / 默认 | 否 |
| `execution_backend` | 默认 `container` | 否 |
| `evidence_id` / `task_id` / `execution_id` | 编排层 | 否 |
| `config_digest` / `command_digest` | 执行器派生 | 否 |

实测：模型候选带 `evidence_id` → `failed` / `unknown_candidate_fields`。

## 4. 安全门禁未改变

- 规划只产生计划；执行仍需 `EvidenceStore` 中已验证的证据（TTL/请求/命令/运行时/镜像/配置）。
- 无证据 → FAILED `isolation_unverified`，进程不启动。
- 容器失败不回退宿主机；`test_isolation` 相关测试全部保留。
- `TestResultVerifier` 仍是成功判定的必经环节。

## 5. 离线测试

`tests/test_llm_run_tests_capability.py`（10 项）：

1. 两个 provider 的 schema 都含 `run_tests` 与 `test_target`；
2. schema 不含 workspace/evidence_id/backend/image/command_digest/host_authorization/runner；
3. schema 封闭（`additionalProperties: false`）；
4. 合法写入候选 → planned；
5. `run_tests` 缺 target → `invalid_test_target`；
6. 带合法 target → planned，`tool_name=run_tests`，`workspace` 来自请求，`execution_backend=container`，`evidence_id` 为空；
7. shell/穿越/绝对路径 target → `invalid_test_target`；
8. 缺 workspace → 澄清（请求 `workspace`）；
9. 模型自带 `evidence_id`/`task_id`/`execution_id` → `unknown_candidate_fields`；
10. 确定性 Planner 仍支持 `run_tests`（对照）。

## 6. 顺带修复

`_live_image_digest()` 在 `docker image inspect` 失败（Docker Desktop 偶发丢失 tag→镜像映射）时回退到 `docker images --digests`，避免容器测试被误跳过。此前一次全量运行出现 14 个 skip 即由该问题引起。

## 7. 回归

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 547/547，52.0s / 49.8s | 0 |
| 能力审计专项 | 10/10 | 0 |
| Mutation runner | 60 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

新增变异：`llm_run_tests_target_unchecked`、`llm_run_tests_trusted_workspace_ignored`，均被捕获。

## 8. 未验证 / 局限

- **未做真实模型调用**：模型是否真的会选 `run_tests`、目标是否合理，仍未知。
- 计划中 `evidence_id` 为 None，需要编排层在执行前 provisioning 并注入；这条"自动注入"链路尚未由模型驱动验证。
- DeepSeek 的 schema 只强制 `classification`，模型可能省略 `test_target`，届时会得到 `invalid_test_target`（不是崩溃）。
- 未验证模型对 workspace 的猜测（它看不到 workspace，只能要求澄清）。

## 9. 下一步

具备一次有界真实调用验证的条件：

1. 预置 workspace 与证据（provisioning）；
2. 用一句明确的"运行 tests.test_x"作为 goal；
3. 最多 1 次模型调用、0 重试；
4. 断言：分类为 plan、`test_runts` 目标正确、证据门禁生效、容器执行成功且经独立验证。

在此之前，模型驱动容器执行仍属"协议就绪、行为未验证"。
