# Zevorinth Phase 3.21：操作系统隔离与持久化操作安全

## 1. 结论与基线

**PASS WITH LIMITATIONS**（OS 隔离部分：**NOT VERIFIED / BLOCKED**）。

- 持久化意图门禁：**已实现并通过故障注入**。
- 操作系统级隔离：本机 `sandbox-exec` 存在，但探针在 Seatbelt 配置下未能完成启动（Python 解释器 `SIGABRT`），因此**没有可验证的内核边界**，不做任何“已隔离”的声明。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `10bca0a4f47fb3691c53a4fee7ebba73b88f0f61` |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS（Darwin），Python 3.14.6 |

## 2. 持久化意图门禁（核心交付）

清理执行顺序改为：

1. 读取事件的 store 不可用 → 整个批次 `unverified` / `store_unavailable`，**不触碰文件系统**；
2. 写入 `cleanup_intent` 事件；
3. 写入失败 → 用 `cleanup_operation_id` 回查事件：
   - 查到（`intent_reconciled=true`）→ 允许继续；
   - 查不到（`false`）→ `unverified` / `intent_not_persisted`，**不进入隔离**；
   - 无法读取（`None`）→ `unverified`，**不进入隔离**；
4. 只有确认意图持久化后，才执行隔离与删除。

因此 `destructive_operations_without_persisted_intent = 0` 由结构保证，而非事后统计。

## 3. 故障注入结果

| 场景 | 结果 |
| --- | --- |
| 存储整体不可用 | `unverified` / `store_unavailable`；原文件保留；无 `.quarantine` |
| 意图写入明确失败且回查不到 | `unverified` / `intent_not_persisted`；原文件保留；无 `.quarantine` |
| 意图写入“异常但实际已落盘” | 回查为 true → 继续执行（由 `_intent_exists` 判定） |
| 意图成功后进程中断 | 既有 Phase 3.20 恢复语义：对账 `reconciled_pending` / `not_executed` |

## 4. 操作系统隔离评估

| 方案 | 可行性 | 结论 |
| --- | --- | --- |
| 专用低权限用户 | 需要 root 创建用户 | 未验证（无权限） |
| 最小权限进程 + 工作目录 | 可行 | 已具备（`cwd` + 环境变量白名单 + 路径边界），但**不是内核边界** |
| 容器 | 需要联网拉取镜像 | 未执行（禁止联网） |
| macOS Seatbelt (`sandbox-exec`) | 二进制存在 | 探针 `SIGABRT`，**未验证** |

原型 `src/zevorinth/isolation.py` 提供：
- `isolation_available()`；
- `build_profile(workspace, allow_network=False)`：默认拒绝写工作区外、拒绝网络；
- `run_probe(workspace, decoy)`：返回 `state`（`verified` / `unverified`）、`kernel_denials`、`stderr`。

当探针未能完成时，`state=unverified`、`kernel_denials=[]`、`profile_applied=false`——不会把“应用拒绝”或“进程没启动”描述成内核拒绝。

**未验证项**：读写边界、网络、子进程、凭据可达性、超时终止、孤儿进程。全部标记为未验证，不作为已支持能力。

## 5. 隔离区盘点与保留

`inventory_quarantine(workspace_root)` 只读列出现存隔离对象（`operation_id`、文件名、SHA-256），并固定标记 `verified=false`、`retention_candidate=false`。本阶段**不实现自动删除**，也不提供通配/递归路径。

## 6. 指标（本阶段实际样本）

| 指标 | 值 | 说明 |
| --- | --- | --- |
| destructive_operations_without_persisted_intent | 0 | 结构保证 |
| cleanup_intent_persist_failures | 1 | 单元故障注入 |
| cleanup_intent_state_unknown | 1 | store 不可用 |
| isolation_initialization_failures | 1 | 探针 SIGABRT |
| isolation_kernel_denials | 0 | 未取得 |
| isolation_boundary_tests_unverified | 5 | 读写/网络/子进程/超时/孤儿 |
| quarantine_objects_total | 0 | 本阶段无遗留对象 |

审计矩阵（`side_effect_audit_runner`）本次未新增隔离样本——隔离能力未验证，不伪造样本。

## 7. 回归与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| `python3 -m unittest discover -s tests` | 437 项通过，约 32s | 0 |
| `tests/test_durable_intent_and_isolation.py` | 5/5 | 0 |
| Mutation runner | 36 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

新增变异：`intent_gate_bypass`（移除意图门禁 → 被 `test_intent_write_failure_blocks_quarantine` 捕获）。

## 8. 稳定报告哈希

产物在 `artifacts/audits/phase-3.21/`（`.gitignore` 排除，未进入 Git）：`side-effect-stable/stable.json`、`mutations.json`、`coverage/coverage-gate.json`。哈希见最终报告。

## 9. 已执行 / 静态审查 / 未执行

**已执行**：437 项测试、意图门禁故障注入、隔离探针真实调用、36 条变异、覆盖率、编译与 diff 检查。

**静态审查**：Seatbelt 配置语义、`sandbox-exec` 的弃用状态。

**未执行**：内核级隔离验证（探针失败）、容器、低权限用户、网络/子进程边界测试、隔离区自动保留策略。

## 10. 安全边界

- 真实模型 API 请求：0；未联网、未安装依赖；未读取 `.env`；`.idea/` 未修改未暂存。
- 不删除用户文件；不自动清理隔离区；不因失败自动重试破坏性操作。

## 11. 残余风险

- **操作系统边界未建立**：工具仍以当前用户身份运行，应用层策略不是内核边界。
- 隔离探针失败原因（Seatbelt 与 Python 3.14 的交互）未定位，需要独立的沙箱实验环境。
- 隔离区无保留策略，长期会累积。
- 意图“异常但已落盘”的判定依赖事件可读；事件存储部分可用时仍可能误判为未知（保守拒绝）。
