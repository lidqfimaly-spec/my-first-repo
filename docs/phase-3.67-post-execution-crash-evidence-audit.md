# Zevorinth Phase 3.67：执行后崩溃一致性与副作用证据闭环

## 1. 结论与范围

**PASS。** Phase 3.67 没有新增恢复 action，也没有扩大执行权限。`restore_controlled_marker`
仍然是唯一允许的真实副作用。

本阶段新增两个安全能力：

1. completion evidence 与 intent、materialization 和独立验证结果绑定后计算
   `evidence_digest`；
2. 只读 `audit_controlled_recovery()` 将持久化审批、intent、历史验证证据和当前真实
   文件状态交叉核对。

审计不会修改审批记录、任务、事件、workspace 或文件，也不会将 `consuming`
自动升级为成功。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `1e9926101d2122f62a094f60e5e7e330d92f9d13` |
| 新增恢复 action | **0** |
| 真实模型请求 | **0** |
| 网络 / 安装依赖 | 否 / 否 |
| Docker / 任意 shell | 0 / 0 |

## 2. Completion Evidence 绑定

成功 completion 的 result 包含：

- 不可变 execution intent；
- materialization 结果；
- 最终独立 no-follow 验证结果；
- `evidence_digest = SHA256(canonical(intent, execution, verification))`。

`verification_result` 同时持久化 `evidence_sha256=<digest>`。审计会重新计算摘要，
并检查：

- result 内 intent 与审批持久化 intent 完全一致；
- attempt、action、target、action digest 一致；
- 预期内容 SHA-256、大小和权限一致；
- execution 状态为 `materialized`；
- verification 状态为 `verified`；
- execution 与 verification 的 device/inode 一致；
- approval 的独立验证结论为通过。

任何缺项或不一致均返回 `stored_evidence_inconsistent + manual_review`。

## 3. 当前真实状态对账

审计器重新使用 no-follow 文件描述符读取当前文件，并生成
`observation_digest`。它不会信任 completion 中保存的旧布尔值。

| 历史记录 | 当前文件 | 审计结果 |
| --- | --- | --- |
| `consumed` | 内容、权限和 inode 均匹配 | `consistent` |
| `consumed` | 内容相同但 inode 被替换 | `unverified` |
| `consumed` | 当前内容或权限不再匹配 | `unverified` |
| `consumed` | 文件被人工补偿删除 | `unverified` |
| `consuming` + intent | target absent | `unverified`，疑似副作用前中断 |
| `consuming` + intent | target verified | `unverified`，副作用存在但 completion 未持久化 |
| `consuming` + intent | target mismatch | `unverified`，副作用状态不确定 |
| `consuming` 无 intent | target absent | `unverified`，claim 后 intent 前中断 |
| `failed` | 任意当前状态 | `verified_failure + manual_review` |
| `revoked` / `rejected` | 任意当前状态 | `denied` |

所有不确定状态都保持 `safe_to_retry=false`。

## 4. 只读审计元数据

审计结果包含：

- `auditor_version=controlled-recovery-audit-v1`；
- `checked_at`；
- approval record digest；
- approval state digest；
- execution attempt ID；
- intent digest；
- current observation digest；
- `scope=controlled_recovery_consistency_only`；
- `security_certification=false`。

给定相同记录和文件状态，固定 `checked_at` 的重复审计输出完全一致。

## 5. 真实进程故障注入

本阶段在独立 Python 进程中于以下边界执行真实 `SIGKILL`：

| 检查点 | 持久化/真实状态 |
| --- | --- |
| `after_claim` | `consuming`，无 intent，target absent |
| `after_intent` | `consuming`，intent 已持久化，target absent |
| `during_side_effect` | 文件已写入但尚未 fsync，状态不确定 |
| `after_side_effect` | 文件存在，尚未独立验证 |
| `after_verification` | 首次验证存在，尚未执行 completion 前复核 |
| `before_completion_write` | completion 未写入 |
| `after_completion_write` | `consumed` 已持久化，进程在返回前被杀 |

除 `after_completion_write` 外，所有崩溃点均进入人工复核。重复执行请求均被拒绝，
已有文件不会被再次创建或覆盖。

## 6. 二次验证压缩 TOCTOU 窗口

执行流程在 `after_verification` 中断点后再次读取真实文件，最终 completion 只使用
第二次读取结果。测试在第一次验证后以同大小错误内容替换目标，执行器必须拒绝进入
`consumed`。

这没有形式化消除本机 TOCTOU，但保证中断点之后发生的目标变化会进入失败路径，而
不是沿用旧验证结果。

## 7. 测试与变异

| 检查 | 结果 |
| --- | --- |
| Phase 3.67 专项矩阵 | 11/11 |
| 真实进程崩溃检查点 | 7/7 |
| 系统 Python 全量 unittest | 711/711，exit 0 |
| `.venv` 全量 unittest | 711/711，exit 0 |
| Mutation runner | **111 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 111/111 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异覆盖：最终复核绕过、evidence digest 绕过、verification binding 绕过、
stored verification 状态绕过、当前内容绕过、当前 inode 绕过，以及把一致性审计
误标为安全认证。

## 8. 未验证事项与残余风险

1. 文件描述符、inode 和 no-follow 语义只在当前 macOS/POSIX 环境验证。
2. 最终读取与 completion 写入之间仍有极小的本机 TOCTOU 窗口。
3. `record_digest` 与 `evidence_digest` 检测内部不一致，但不是密码学身份认证。
4. 审计只能证明记录与当前观察在其检查时点一致，不能证明所有历史外部行为。
5. 没有新增操作、自动恢复、自动清理、补偿执行或历史迁移。

## 9. 安全边界

- 未读取 `.env`；
- 未访问互联网、未安装依赖；
- 未调用真实模型；
- 未启动或关闭 Docker；
- 未执行任意 shell、task executor 或宿主机执行回退；
- 未扩大受控恢复 action 集合；
- 未自动恢复、清理或重试；
- 未修改、暂存或跟踪 `.idea/`；
- 未放宽 evidence、approval 或独立验证门禁。

## 10. 阶段判定

**PASS**：现有单一恢复操作在真实进程崩溃、历史 evidence 篡改、文件内容/inode
替换和状态矛盾下均保持 fail-closed；只读对账能够区分一致、已验证副作用但未完成、
内容/身份漂移、失败、撤销和存储不可信状态。
