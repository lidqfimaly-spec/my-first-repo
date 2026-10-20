# Zevorinth Phase 3.19：强化清理与文件系统竞态审计

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 清理路径改为目录句柄相对解析、`O_NOFOLLOW` 打开、`fstat` 身份校验、经文件描述符读取哈希，并在 unlink 前再次比对 inode；新增单任务多文件清理与批次语义。POSIX 没有“比较并删除”的原子操作，剩余竞态窗口如实记录。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `47909fef9c0514c4b5935286b948a2b257d37719` |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS（Darwin），Python 3.14.6 |

## 2. 威胁模型

| 风险 | 前提 | 现有防护 | 剩余风险 | 证据 |
| --- | --- | --- | --- | --- |
| 路径穿越 | 请求里含 `..`/绝对路径 | `_normalize_relative` 拒绝 | 无 | `test_escaping_path_is_rejected` |
| 符号链接替换 | 目标在校验后被换成链接 | `O_NOFOLLOW` + `fstat` + inode 比对 | 极小窗口 | `test_replaced_by_symlink_before_unlink`、`cleanup_nofollow_removed` 变异 |
| 校验后被替换 | 同名新文件替换 | 打开 fd 后 fstat，unlink 前比对 `(st_dev, st_ino)` | 比对与 unlink 之间仍有窗口 | `test_replaced_by_another_regular_file` |
| 校验后内容修改 | 同 inode 写内容 | 哈希从 fd 读取；再改会改变内容但 inode 不变 → 仍可能删除改动后的内容 | **未覆盖**：inode 相同、内容在比对后改变 | 见限制 |
| 父目录被替换 | 中间目录换名或换成链接 | 逐级 `O_DIRECTORY|O_NOFOLLOW` 打开父句柄；unlink 用父句柄 | 目录句柄指向旧目录，新目录不受影响 | `test_parent_directory_replaced_keeps_new_tree_intact` |
| 并发清理 | 两个请求同时到达 | 事件中的 `already_cleaned` + 单文件幂等 | 两请求在事件写入前的窗口 | `test_concurrent_requests_do_not_double_delete` |
| Lease/generation 过期 | 任务被接管 | 活跃 Lease 拒绝；generation 必须等于创建者 | 无 | `test_stale_generation_is_rejected` |
| 删除成功但事件失败 | 事件存储不可用 | `evidence_persisted=false`，状态不伪造 | 删除已发生但无证据 | `test_event_persistence_failure_is_reported` |
| 检查/删除/记录之间中断 | 进程被杀 | 结果写入事件；重放时 `already_cleaned`/`missing_target` | 已删除但未记录时，第二次请求会得到 `missing_target` | `test_repeat_request_is_idempotent` |

## 3. 实现要点

`execute_cleanup(decision, workspace_root, checkpoint=...)`：

1. `os.open(root, O_DIRECTORY|O_NOFOLLOW)` 取得根目录句柄；
2. 逐级 `os.open(part, O_DIRECTORY|O_NOFOLLOW, dir_fd=父)` 打开父目录；
3. `os.open(name, O_RDONLY|O_NOFOLLOW, dir_fd=父)` 打开目标；
4. `os.fstat` 确认是普通文件，并记录 `(st_dev, st_ino)`；
5. 从**该 fd** 读取内容（≤1 MiB）并计算 SHA-256；
6. 可选检查点：`opened` / `verified` / `before_unlink` / `after_unlink`（仅测试使用）；
7. `os.stat(name, dir_fd=父, follow_symlinks=False)` 与步骤 4 的 inode 比对；
8. `os.unlink(name, dir_fd=父)`；
9. 再次 `stat` 确认路径消失。

**明确限制**：第 7 步与第 8 步之间不是原子的，平台没有 compare-and-unlink；同 inode 内容在两步之间被改写的情况无法检测。文档不宣称“彻底消除 TOCTOU”。

## 4. 批量清理

`cleanup_workspace_files(task_id, paths, enabled, authorized)`：

- 一次只针对一个 task；
- 每个路径独立执行全部检查（授权、路径、generation、所有权、受保护、类型、哈希）；
- 单文件结果 `completed` / `rejected` / `failed` / `unverified`；
- 批次状态：`completed`（全部完成）/ `rejected`（全部拒绝）/ `partial`（部分完成）/ `unverified` / `failed`；
- 每个文件单独持久化 `cleanup_completed` / `cleanup_rejected` 事件；
- 不推断同目录其他文件的所有权；不做目录、通配或递归删除；
- 重复请求：已成功清理的路径返回 `already_cleaned`，即使同名文件重新出现也不会被删除。

## 5. 竞态与故障注入

全部使用确定性检查点，不依赖 sleep。

| 场景 | 结果 |
| --- | --- |
| 打开后内容被修改 | `rejected` / `hash_mismatch`，文件保留 |
| 删除前替换为符号链接 | `unverified` / `target_replaced`，链接与目标都保留 |
| 删除前替换为另一个普通文件 | `unverified` / `target_replaced`，新文件保留 |
| 校验后目标消失 | `rejected`/`unverified`，不报 removed |
| 父目录被改名并新建同名目录 | 只删除旧目录中的文件，新目录文件保留 |
| generation 失效 | `stale_generation` |
| 并发两个请求 | 一 completed、一 rejected（already_cleaned），仅 1 条完成事件 |
| 事件持久化失败 | `evidence_persisted=false` |
| 重复请求 + 同名新文件 | `already_cleaned`，新文件保留 |
| 越界路径 `../x`、`/etc/passwd` | `invalid_path` |
| 批次部分完成 | `batch_status=partial`，逐文件结果独立 |

## 6. 指标（独立复算）

`tests/side_effect_audit_runner.py` 从持久化任务/事件重算：

| 指标 | 值 |
| --- | --- |
| cleanup_requests | 8 |
| cleanup_completed | 1（另 1 计入批次完成） |
| cleanup_rejected | 4 |
| cleanup_failed / cleanup_unverified | 0 / 0 |
| cleanup_partial_batches | 1 |
| cleanup_duplicates | 1（already_cleaned） |
| cleanup_stale_generation | 0（单元测试覆盖） |
| cleanup_hash_mismatch | 1 |
| cleanup_path_rejections | 0（本矩阵无；单元测试覆盖 unsafe/protected/not_owned） |
| recovery_reruns | 0 |

## 7. 回归与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| `python3 -m unittest discover -s tests` | 418 项通过，约 32s | 0 |
| `tests/test_cleanup_races.py` | 12/12 | 0 |
| `tests/test_workspace_cleanup.py` | 16/16 | 0 |
| 稳定结果双次运行 | `d3050abe…` 一致 | 0 |
| Mutation runner | 32 killed / 0 survived / 0 invalid / 0 error | 0 |
| 覆盖率门禁 | 418 项通过，11/11 PASS | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

覆盖率：`workspace_cleanup.py` 87.36%/80.30%，`workspace_effects.py` 95.12%/90.91%，`workspace_snapshot.py` 86.40%/80.68%，`service.py` 88.51%/77.45%。

新增/更新的清理相关变异：`cleanup_hash_check_removed`、`cleanup_inode_check_removed`、`cleanup_nofollow_removed`、`cleanup_protected_check_removed`、`cleanup_generation_removed`、`cleanup_already_cleaned_removed`、`cleanup_batch_partial_as_completed`、`cleanup_disabled_bypass`。全部被捕获且证明改写行已执行。

## 8. 稳定报告哈希

`artifacts/audits/phase-3.19/`（`.gitignore` 排除，未进入 Git）：

| 文件 | SHA-256 |
| --- | --- |
| `side-effect-stable/stable.json` | `d3050abe21af4d3dfe6443ff12ec6e2db1f82ec406e059500825351c2f83f898` |
| `side-effect-stable/metadata.json` | `5818f5519a56f46cace671179554150b4c9b34417d1bf5647b0958188a87eab0` |
| `mutations.json` | `981b8a5567f0bde0ed8841bc9f61a658e10fc24b8c91fbaf26a3eee029fe6c8a` |
| `coverage/coverage-gate.json` | `078e9e3d2c16a8494fd79ae86722f187d9afdf90ad6e5cdb35f9023b1595fbfc` |

## 9. 已执行 / 静态审查 / 未执行

**已执行**：418 项测试、12 个确定性竞态场景、32 条变异、覆盖率、双次稳定复现、真实文件删除与保留。

**静态审查**：`O_NOFOLLOW`/`O_DIRECTORY` 在 macOS 的语义、`unlinkat` 按名称删除的事实。

**未执行**：非 macOS 平台验证；同 inode 内容在最终比对后被改写的竞态（平台无原子原语）；自动变异框架。

## 10. 安全边界

- 真实模型 API 请求：0；未联网、未安装依赖；未读取 `.env`；`.idea/` 未修改未暂存。
- 不删除目录、不递归、不通配、不越界；不删除用户预先存在的文件；不因失败自动清理。

## 11. 残余风险

- **TOCTOU 未消除**：inode 比对与 `unlink` 之间仍有窗口；同 inode 内容改写无法检测。
- 根目录本身若是符号链接，`O_NOFOLLOW` 会拒绝打开（保守失败）。
- 事件写入失败时，删除已发生但无持久化证据；只能通过文件状态推断。
- 批次不是事务：部分文件删除后后续事件失败会留下“已删未记”的状态。
- 依赖 POSIX 的 `dir_fd` 支持；Windows 未验证。
