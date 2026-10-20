# Zevorinth Phase 3.20：清理隔离、恢复与证据对账

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 清理先持久化操作意图，再把已校验文件 `rename` 到任务目录内的隔离区，从隔离区重新打开核对 inode 与 SHA-256，确认一致后才删除；新增只读对账，把“意图已写入 / 文件已移动 / 结果已记录”三件事分开表达。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `f9f58dd533f6682427ffe3221b5fe4a52fdbfa64` |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS（Darwin），Python 3.14.6 |

## 2. 操作状态机

| 状态 | 含义 | 持久化位置 |
| --- | --- | --- |
| `planned` | 仅内存计划 | 无 |
| `cleanup_intent` | 意图已持久化（operation_id、task/attempt/generation、路径、期望哈希） | 事件 `cleanup_intent` |
| `quarantined` | 已移入 `.quarantine/<op_id>/` 并重新校验 | 结果字段 |
| `completed` | 隔离对象已删除且确认消失 | 事件 `cleanup_completed` |
| `rejected` | 策略拒绝 | 事件 `cleanup_rejected` |
| `failed` | 文件系统操作失败 | 事件 `cleanup_rejected` |
| `unverified` | 状态无法确认 | 事件 `cleanup_unverified` |

顺序固定为：**写意图 → 动文件 → 写结果**。

## 3. 隔离实现

1. 目录句柄打开根、父目录、目标（`O_NOFOLLOW`）；
2. `fstat` 校验普通文件，从 fd 读取并比对哈希；
3. 比对 `(st_dev, st_ino)`；
4. 在父目录内创建 `.quarantine/<op_id>/`（0700）；
5. `os.rename` 移动到隔离目录（同一文件系统，无复制回退）；
6. **从隔离目录重新打开**对象，核对 inode 与 SHA-256；
7. 不一致 → `unverified` / `quarantine_mismatch`，对象留在隔离区；
8. 一致 → 在隔离目录内 `unlink` 并确认消失。

原始路径在隔离后出现同名新文件时不受影响（`test_new_file_after_quarantine_survives`）。

## 4. 只读对账

`reconcile_cleanup(...)` / `TaskService.reconcile_cleanup_operation()`：

| 现场 | 结论 |
| --- | --- |
| 有意图、原文件仍在、无完成事件 | `reconciled_not_executed` |
| 原文件消失、隔离对象哈希匹配、无完成事件 | `reconciled_pending` |
| 隔离对象哈希不匹配 | `reconciled_conflict` |
| 完成事件 + 两处都不存在 | `reconciled_completed` |
| 完成事件 + 文件仍存在 | `reconciled_conflict` |
| 原文件与隔离对象同时存在 | `reconciled_conflict` |
| 两处都不存在、无完成事件 | `reconciled_unverified` |
| generation 不符 / 无意图 | `reconciled_conflict` / `reconciled_unverified` |

`unverified` 与 `conflict` 不会自动转为成功，也不会触发新的破坏性操作。

## 5. 故障注入矩阵

检查点：`opened` / `verified` / `before_quarantine` / `after_quarantine` / `quarantine_verified` / `before_unlink` / `after_unlink`。

| 中断/注入位置 | 结果 |
| --- | --- |
| 意图写入前 | `intent_persisted=false`，无副作用 |
| 意图写入后、隔离前（抛错） | 原文件仍在，无隔离目录 |
| 隔离后、验证前（篡改隔离对象） | `unverified` / `quarantine_mismatch`，不删除 |
| 隔离验证后、删除前（原路径出现新文件） | `completed`，新文件保留 |
| 校验前目标被替换 / 变成符号链接 | `target_replaced` |
| 父目录改名 + 新建同名目录 | 只删旧目录中的对象 |
| 事件持久化失败 | `evidence_persisted=false` |
| 重复请求 + 同名新文件 | `already_cleaned`，新文件保留 |
| 并发两个请求 | 一 completed、一 rejected，仅 1 条完成事件 |

## 6. 负向审计样本与指标

审计矩阵新增真实样本：路径穿越、绝对路径、符号链接、缺少所有权、旧 generation、哈希不匹配、已清理后的同名新文件、对账完成。

| 指标 | 值 |
| --- | --- |
| cleanup_requests | 14 |
| cleanup_completed | 1 |
| cleanup_rejected | 9 |
| cleanup_failed / unverified | 0 / 0 |
| cleanup_partial_batches | 1 |
| cleanup_duplicates | 1 |
| cleanup_hash_mismatch | 1 |
| cleanup_stale_generation | 1 |
| cleanup_path_rejections | 4 |
| cleanup_intents_created | 14 |
| quarantine_operations_completed | 1 |
| quarantine_identity_mismatches | 0 |
| reconciliation_completed | 1 |
| reconciliation_pending / conflicts / unverified | 0 / 0 / 0 |

## 7. 回归与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| `python3 -m unittest discover -s tests` | 432 项通过，约 32s | 0 |
| `tests/test_cleanup_reconciliation.py` | 12/12 | 0 |
| `tests/test_cleanup_races.py` | 14/14 | 0 |
| 稳定结果双次运行 | `ea33fe62…` 一致 | 0 |
| Mutation runner | 35 killed / 0 survived / 0 invalid / 0 error | 0 |
| 覆盖率门禁 | 432 项通过，11/11 PASS | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

覆盖率：`workspace_cleanup.py` 88.00%/83.33%，`workspace_effects.py` 95.12%/90.91%，`service.py` 88.77%/77.40%。

新增变异（全部被捕获）：`quarantine_hash_skipped`、`reconcile_completed_ignores_fs`、`reconcile_pending_as_completed`。

## 8. 稳定报告哈希

`artifacts/audits/phase-3.20/`（`.gitignore` 排除，未进入 Git）：

| 文件 | SHA-256 |
| --- | --- |
| `side-effect-stable/stable.json` | `ea33fe629e7a5ce668f1912eeb891411ad71615b42c1a64af1b92e2aafcca462` |
| `side-effect-stable/metadata.json` | `867b9917c133a827d34e0112cad3a5360875b62b24e338ae462f6e752191dcc6` |
| `mutations.json` | `980dfd0bef5eee1138b00dc83cf2b667e1d8b69d56324b4c7e0a1cf01a893883` |
| `coverage/coverage-gate.json` | `286216942247125871269e600518ff5f5f327a51c7ac3e7567693c518c015bf3` |

## 9. 已执行 / 静态审查 / 未执行

**已执行**：432 项测试、14 个清理审计场景、12 个对账用例、35 条变异、覆盖率、双次稳定复现、真实重命名与删除。

**静态审查**：`rename(2)` 的同文件系统语义、`dir_fd` 在 macOS 的行为。

**未执行**：非 macOS 平台；外部进程持有 fd 在校验后继续写（无法用确定性钩子在该进程外触发）；自动变异框架。

## 10. 安全边界

- 真实模型 API 请求：0；未联网、未安装依赖；未读取 `.env`；`.idea/` 未修改未暂存。
- 隔离区位于任务目录内（`.quarantine/<op_id>/`），0700，不对外暴露。
- 不删除目录、不递归、不越界；不删除用户预先存在的文件。

## 11. 残余风险

- **TOCTOU 未消除**：校验与 `rename`、隔离校验与 `unlink` 之间仍有窗口。
- 同一 inode 被外部 fd 持续写入：隔离前后哈希不同会以 `quarantine_mismatch` 拒绝删除，但若写入发生在最终校验之后仍无法阻止。
- 隔离对象保留，长期运行会累积目录，需要独立保留策略。
- 意图事件写入失败时仍可能执行文件操作（结果里标记 `intent_persisted=false`）。
- 批次仍非事务：部分删除后事件失败会留下“已删未记”。
