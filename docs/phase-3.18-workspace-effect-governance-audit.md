# Zevorinth Phase 3.18：工作区副作用治理与安全清理

## 1. 基线与提交

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `8a3deb1ab6a849462f2403aa30d96ecd64a810c1` |
| 分支 | `phase-2.6-response-hardening` |
| Schema 版本 | v4（未变更） |

开始时工作树除 `.idea/` 外干净，未执行 reset/rebase/checkout/clean。

## 2. 统一副作用证据契约

新增 `src/zevorinth/workspace_effects.py`，`write_text_artifact` 与 `run_tests` 共用同一结构（挂在既有 `ToolResult.workspace_effects`）：

```
schema_version, operation, source, workspace,
task_id, attempt_id, claim_generation,
applicable, complete, detail_complete,
total_changes, retained_changes, truncated, unclassified_changes,
policy {allowed_side_effects, protected_paths},
before {entries, unreadable, limits_hit, digest, complete},
after  {...},
diff   {counts, changes, change_count, retained_count, truncated, detail_complete},
cleanup
```

`validate_workspace_effects()` 拒绝：缺字段、未知操作、diff 非对象、截断标记矛盾、`total_changes`/`retained_changes` 与实际不符、`complete` 与快照不符、身份不一致，以及**差异与快照重算不一致**。

## 3. 两条实际调用路径

### 文件写入

```
_execute_tool
  ├─ TOOL_STARTED
  ├─ 前快照 artifacts/<task_id>（不存在则空快照）
  ├─ RealArtifactWriter.write（原子发布、不可覆盖）
  ├─ 后快照
  ├─ ToolResult.workspace_effects = build_workspace_effects(
  │      operation="write_text_artifact",
  │      allowed=["<artifact_path>", ".staging"])
  ├─ TOOL_SUCCEEDED
  └─ _verify_and_finish：ArtifactVerifier + 副作用复核
```

副作用复核要求：记录自洽、快照完整、无截断、无 unexpected/unknown、且“新增”集合恰好是该产物（`.staging` 是写入器自己的恢复目录，显式列入允许前缀而不是被隐藏）。

### 测试执行

沿用 Phase 3.17 的前后快照与差异；现在同一份 `workspace_effects` 会与 `test_evidence` 一起持久化，两套数据必须一致（`TestResultVerifier` 从快照重算并比对）。

## 4. 差异截断语义

`diff_snapshots()` 新增字段：`retained_count`、`detail_complete`、`truncated`。

- 明细 ≤ 200：`detail_complete=true`，Verifier 可逐项复核；
- 明细 > 200：保留前 200 条，`total_changes` 仍为真实总数，`detail_complete=false`；
- `validate_diff_record()` 拒绝“截断却声明完整”“保留数与明细长度不符”“截断标记互相矛盾”；
- 副作用审计中，`truncated` 或快照不完整 → 失败（`workspace_audit_incomplete`），不谎称完整审计。

真实生成 205 个文件验证了该行为。

## 5. 清理：授权、所有权、验证

`src/zevorinth/workspace_cleanup.py`：

**所有权**只来自持久化证据——`diff` 中 `change=added` 且 `classification=expected` 的条目，携带创建时 SHA-256。目录名、文件名、时间戳都不构成所有权。

**清理前检查**（任一失败即拒绝）：`enabled` → `authorized` → 证据自洽 → generation 必须等于创建它的 generation → 非受保护路径 → 存在所有权记录 → 非符号链接 → 在真实工作区内 → 是普通文件 → 当前哈希等于创建时哈希。

**删除**时再次读取哈希（计划与删除之间被修改 → `target_changed`），`unlink` 后确认路径消失；状态为 `completed` / `rejected` / `failed` / `unverified`。

**默认关闭**：`cleanup_workspace_file(..., enabled=False, authorized=False)` 直接拒绝；CLI 需要 `--confirm`。

**证据**：`cleanup_completed` / `cleanup_rejected` 事件持久化完整记录；写入事件失败时 `evidence_persisted=false`，不谎报成功。清理不改变原任务状态，也不删除原始执行证据。

## 6. 并发与恢复

| 情况 | 行为 |
| --- | --- |
| 任务仍有活跃 Lease | 拒绝 `active_lease` |
| generation 与创建者不同 | 拒绝 `stale_generation` |
| 计划与删除之间内容变化 | 拒绝 `target_changed` |
| 文件已不存在 | 拒绝 `missing_target`（不推断“已成功清理”） |
| 重复请求 | 第二次为 `missing_target`，无重复副作用 |
| 清理事件写入失败 | `evidence_persisted=false`，状态不报成功 |

## 7. 场景矩阵

### 统一审计与截断

| 场景 | 结果 |
| --- | --- |
| 写入任务产生 `workspace_effects` | 2 个 expected（产物 + `.staging`），complete |
| 写入证据与快照不一致 | `workspace_effects_invalid_*` / `unexpected_workspace_change` |
| 205 个新增文件 | `truncated=true`、`retained=200`、`total=205` |
| 截断却声明完整 | `validate_diff_record` 抛错 |
| 伪造计数 | 抛错 |

### 清理

| 场景 | 结果 |
| --- | --- |
| 默认调用 | `cleanup_disabled`，文件保留 |
| 启用但未授权 | `cleanup_unauthorized`，文件保留 |
| 本次创建且未修改 | `completed`，文件消失，事件记录 |
| 内容被修改 | `hash_mismatch`，文件保留 |
| 用户预先存在的文件 | `not_owned`，文件保留 |
| 目标被替换为越界符号链接 | `unsafe_path`，链接目标未被删除 |
| 受保护路径 | `protected_path` |
| 旧 generation | `stale_generation` |
| 计划后内容变化 | `target_changed` |
| 重复清理 | `missing_target`，无重复删除 |
| 伪造证据 | 记录校验失败，文件保留 |
| 清理后任务状态 | 不变（仍为 SUCCEEDED） |

## 8. 指标独立复算

`tests/side_effect_audit_runner.py`（8 个副作用场景 + 7 条清理记录）：

| 指标 | 值 |
| --- | --- |
| started / audited / complete_audit | 7 / 6 / 6 |
| complete_snapshot_rate | 1.0（6/6） |
| diff_consistency_rate | 1.0（6/6） |
| side_effect_verification_rate | 0.667（4/6，负向场景刻意产生 unexpected） |
| expected / unexpected / unknown | 3 / 2 / 0 |
| cleanup_requests / completed / rejected | 7 / 1 / 4 |
| cleanup_duplicates | 0 |
| recovery_reruns | 0 |

门禁拒绝：succeeded 无完整审计、succeeded 含 unexpected、blocked 却启动、重复场景 ID、恢复重跑。

## 9. 回归与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| `python3 -m unittest discover -s tests` | 406 项通过，约 31s | 0 |
| `tests/test_workspace_cleanup.py` | 16/16 | 0 |
| `tests/test_workspace_side_effects.py` | 29/29 | 0 |
| `tests/side_effect_audit_runner.py` | PASS | 0 |
| 稳定结果双次运行 | `bb16aa94…` 一致 | 0 |
| Mutation runner | 28 killed / 0 survived / 0 invalid / 0 error | 0 |
| 覆盖率门禁 | 406 项通过，11/11 PASS | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

覆盖率：`workspace_effects.py` 95.12%/90.91%，`workspace_cleanup.py` 91.95%/84.38%，`workspace_snapshot.py` 86.40%/80.68%，`service.py` 87.82%/76.73%。

新增 6 条变异全部被捕获并证明改写行被执行：`cleanup_disabled_bypass`、`cleanup_hash_check_removed`、`cleanup_generation_removed`、`cleanup_delete_race_check_removed`、`effects_validation_skip`、`diff_truncation_claimed_complete`。

## 10. 稳定报告哈希

`artifacts/audits/phase-3.18/`（`.gitignore` 排除，未进入 Git）：

| 文件 | SHA-256 |
| --- | --- |
| `side-effect-stable/stable.json` | `bb16aa942686afb0d04eb99c7ef948ac0d872f38bcf53b83f1d4dfebeb04baba` |
| `side-effect-stable/metadata.json` | `060368d85ce3d498cc369f4cdea7a4a2f5d80f333b71fc4e50ee129b6fbb737e` |
| `mutations.json` | `95608bc92cfc24602e76dbed191fc7306226a1ddbbc8de9919601595c20b453e` |
| `coverage/coverage-gate.json` | `8816d6413f1c60b0154d3bf037984b6f40a9861dc5c744632853dc03eef5edfb` |

## 11. 已执行 / 静态审查 / 未执行

**已执行**：406 项测试、28 条变异、覆盖率、8 个副作用场景 + 7 条清理记录、双次稳定复现、真实 205 文件截断、真实文件删除与保留。

**由测试证明**：两条工具路径产生同构证据；截断不会被当作完整；清理默认关闭、需授权、拒绝修改过的文件、拒绝越界符号链接、拒绝旧 generation、重复清理无副作用。

**仅静态审查**：`unlink` 与哈希检查之间仍存在的 TOCTOU 窗口（见下）。

**未执行**：自动变异框架、非 macOS 平台文件系统语义、操作系统级沙箱、真实模型调用。

## 12. 安全边界与残余风险

- 真实模型 API 请求：0；未联网、未安装依赖；未读取 `.env`；`.idea/` 未修改未暂存。
- **TOCTOU 未完全消除**：`plan_cleanup` 与 `execute_cleanup` 都会校验哈希，但在最后一次读取与 `unlink` 之间仍存在极短窗口。当前采取保守策略（再次校验 + 失败即拒绝），未使用 `openat`/`O_PATH` 等平台特定原语。
- **不是操作系统级隔离**：只能观察工作区，无法阻止或证明工作区之外的访问。
- 差异明细上限 200 条；超出即视为不可逐项审计并失败。
- `.staging` 作为写入器内部目录显式列入允许前缀；它的存在属于实现细节。
- 清理只针对单个普通文件，不支持目录、通配或批量。
- 清理证据写入失败时不会重试，也不会补偿删除；会标记 `evidence_persisted=false`。
