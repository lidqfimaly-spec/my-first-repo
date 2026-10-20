# Zevorinth Phase 3.26：容器隔离验证与证据闭环

## 1. 结论与基线

**BLOCKED（容器真实边界未执行）**，整体 **PASS WITH LIMITATIONS**。

本机没有获授权的容器运行时（daemon 未运行，且本阶段禁止启动），因此七项探针全部 **NOT_EXECUTED**。本阶段交付的是**证据契约 + 独立验证器**：即使调用方伪造 `verified=true`/完整证据，验证器也会拒绝缺失、部分、篡改、失配或版本不兼容的记录。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `2e66edb5c3966e93c4ef5b80ce35014e0ce3c896` |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS，Python 3.14.6 |
| 运行时 | Docker CLI 存在，daemon 不可达 |

## 2. 证据契约

`build_evidence()` 生成：

`schema_version`、`verifier_version`、`environment_id`、`runtime_identity`、`image_digest`、`container_config_digest`、`probes[]`、`digest`。

每个 probe：`probe_id`、`boundary`、`status`、`expected_result`、`observed_result`、`exit_code`、`evidence_artifact_ids`、`failure_reason`。状态集合：`PASS` / `FAIL` / `NOT_EXECUTED` / `ERROR` / `INCONCLUSIVE`。

七项边界：`read_inside`、`write_inside`、`read_outside`、`write_outside`、`network`、`credential`、`subprocess`。

`digest` 为除自身外全部字段的规范化 JSON 的 SHA-256，可独立重算。

## 3. 独立验证器

`verify_evidence(evidence, expected_runtime, expected_image_digest, expected_config_digest, expected_verifier_version)` 重新计算判定：

| 条件 | 结果 |
| --- | --- |
| 证据缺失 / 字段缺失 | `blocked` |
| `verifier_version` 不匹配 | `blocked` |
| 摘要不符（被篡改） | `failed` |
| runtime / image / config 身份不符 | `blocked` |
| 重复 probe_id 或重复 boundary | `failed` |
| 未知 boundary | `failed` |
| 缺少任一必需 boundary | `blocked`（`boundary_not_executed:*`） |
| 任一 probe `FAIL` | `failed` |
| 任一 probe `NOT_EXECUTED` | `blocked` |
| 任一 probe `ERROR` / `INCONCLUSIVE` | `unverified` |
| 全部 `PASS` 且身份/摘要一致 | `verified` |

`run_protected_container()` 在提供 `evidence` 时**必须**先通过该验证器；未提供时仍要求 `boundary_result.state == "verified"`。任一不满足 → 不启动进程、不回退宿主机执行。

## 4. 七项探针状态

| 边界 | 状态 | 说明 |
| --- | --- | --- |
| 工作区内读/写 | NOT_EXECUTED | 无 daemon |
| 工作区外读/写 | NOT_EXECUTED | 无 daemon |
| 网络 | NOT_EXECUTED | `--network none` 只是配置 |
| 凭据 | NOT_EXECUTED | 无 daemon |
| 子进程 | NOT_EXECUTED | 无 daemon |

`NOT_EXECUTED` 在验证器中映射为 `blocked`，**不会**被计为通过。

## 5. 负向测试（已执行）

| 场景 | 结果 |
| --- | --- |
| 缺失证据 | `blocked` |
| 少一项 boundary | `blocked`（`boundary_not_executed`） |
| 全部 `NOT_EXECUTED` | `blocked` |
| 任一 `FAIL` | `failed` |
| 任一 `INCONCLUSIVE` | `unverified` |
| 篡改 probe 状态 | `failed`（`evidence_digest_mismatch`） |
| 镜像/配置 digest 不匹配 | `blocked` |
| 旧 verifier 版本 | `blocked` |
| 重复 probe_id | `failed` |
| 运行时可用但无证据 | 不 `verified` |
| 未验证证据下启动容器 | `process_started=false` |

## 6. 回归与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 473/473，37.7s / 32.6s | 0 |
| 证据验证器专项 | 13/13 | 0 |
| Mutation runner | 46 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

新增变异：`evidence_tamper_accepted`、`not_executed_counted_as_pass`、`evidence_identity_mismatch_ignored`，均被捕获。

## 7. Phase 3.21–3.25 回归

持久化意图门禁、quarantine 身份/哈希、Seatbelt `partial`、`run_tests` 非隔离类别、应用层拒绝 ≠ 内核拒绝——全部保持（既有测试继续通过）。

## 8. 指标

| 指标 | 值 |
| --- | --- |
| isolation_probe_attempts（容器） | 0 |
| isolation_probe_blocked | 1（运行时不可达） |
| isolation_probe_verified | 0 |
| evidence_incomplete | 1（负向测试样本） |
| evidence_digest_mismatch | 1 |
| protected_processes_started_without_verified_isolation | 0 |
| cleanup_intent_gate_regressions | 0 |

## 9. 已执行 / 静态 / 未执行

**已执行**：473 项测试、证据验证器全部负向分支、容器启动门禁、46 条变异。

**静态审查**：容器参数语义、镜像候选。

**未执行 / BLOCKED**：容器七项真实探针、镜像 digest 核验、容器超时/取消/残留。

## 10. 安全边界

真实模型 API 请求 0；未联网、未安装依赖；**未启动 Docker daemon**；未拉取镜像；未读取 `.env`；`.idea/` 未修改未暂存；未删除 quarantine 或用户数据；未推送。

## 11. 残余风险

- 没有任何真实容器运行时证据；`verified` 分支仅由单元测试中的合成证据覆盖。
- 证据有效期/失效策略未实现：验证器要求调用方传入**当前** runtime/image/config 身份，但尚无自动失效机制，需要在使用侧保证每次执行前重新验证。
- 容器超时、取消、残留清理未验证。
- `run_tests` 仍为宿主机显式非隔离类别。
