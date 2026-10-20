# Zevorinth Phase 3.22：隔离边界验证与失败关闭执行

## 1. 结论与基线

**PASS WITH LIMITATIONS**；OS 隔离边界：**PARTIAL（读隔离未生效），受保护执行 BLOCKED**。

- 统一隔离执行契约与失败关闭：**已实现并通过测试**。
- 内核证据：写入（工作区外）与网络被 Seatbelt **真实拒绝**；读取未受限（profile 全局允许读），因此不构成完整隔离，`run_protected()` 拒绝启动受保护进程。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `ff75c7fe89fd9e96e497a0d7682dea133821fbab` |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS（Darwin），Python 3.14.6，`/usr/bin/sandbox-exec` 存在 |

## 2. 子进程入口清单

| 入口 | 是否受保护 | 现状 |
| --- | --- | --- |
| `local_test_runner.run_unittest` | 否（显式非隔离类别 `run_tests`） | cwd 限定 + 环境白名单，无 OS 边界 |
| `isolation.run_protected` | 是 | 仅在后端 `verified` 时启动，否则 BLOCKED |
| `tests/*` 子进程 | 否 | 测试专用 |

`UNISOLATED_ALLOWED_OPERATIONS = {"run_tests"}` 是显式策略声明，不是异常回退。

## 3. 隔离契约

`run_protected(argv, workspace, probe)` 返回：

`isolation_backend` / `isolation_state` / `profile_applied` / `process_started` / `exit_code` / `blocked_reason` / `argv`。

后端未 `verified` 时：`process_started=false`、`profile_applied=false`、`exit_code=null`，**不启动进程、不回退到无隔离执行**。

## 4. 最小 Seatbelt profile 与探针证据

Profile：`(deny default)(allow process*)(allow file-read*)(allow file-write* (subpath <ws>))(deny network*)`。

| 探针 | 期望 | 实际 | 层 |
| --- | --- | --- | --- |
| 工作区内写入 | allowed | **allowed** | 内核允许 |
| 工作区外写入 | denied | **denied（PermissionError）** | **内核拒绝** |
| 网络连接 | denied | **denied（PermissionError）** | **内核拒绝** |
| 工作区内读取 | allowed | allowed | 内核允许 |
| 工作区外读取 | denied | **allowed** | **未受限** |

结论：写与网络边界**已验证**；读边界**未生效**。因此 `classify()` 返回 `partial`，`REQUIRED_BOUNDARIES` 中 `read_outside_denied` 缺失。

失败排查记录：
- 最初的 profile 在 `sys.executable` 路径上 `execvp` 被拒（exit 71）；
- 追加 homebrew 与 `file-read-metadata` 后进程 `SIGABRT`；
- 改为全局允许读后进程可启动，但工作区内写入因 `/var` → `/private/var` 符号链接被误拒；
- 使用 `Path.resolve()` 后工作区内写入恢复，得到上表结果。

## 5. 分类与失败关闭

`classify()` 只有四个必需边界全部满足才返回 `verified`：
`write_outside_denied`、`network_denied`、`read_outside_denied`、`write_inside_allowed`。

- 任一缺失 → `partial`（读未受限时即为该状态）；
- 工作区内写入被拒（profile 失效/路径错误）→ `failed`；
- 进程未启动/超时/无输出 → `blocked`。

`run_protected()` 只接受 `verified`。

## 6. 回归与保护

| 检查 | 结果 |
| --- | --- |
| Phase 3.21 意图门禁 | 保持不变（store 不可用 → `store_unavailable`，意图未确认 → `intent_not_persisted`，均无文件系统副作用） |
| quarantine 只读清单 | 保留；不实现自动过期删除 |
| 系统 Python 全量 unittest | 440/440，exit 0 |
| 隔离专项 | 8/8 |
| Mutation runner | 38 killed / 0 survived / 0 invalid / 0 error |
| `compileall` / `git diff --check` | exit 0 |

新增变异：`isolation_fail_open`（移除 verified 门禁）、`isolation_read_confinement_faked`（把未生效的读边界伪报为已拒绝），均被捕获。

同时修复了变异 runner 的一个门禁缺陷：补丁无效（`invalid`）时未携带 `expected`，导致 `invalid` 不会被计入 `unexpected_results`。现在无效补丁会使门禁失败。

## 7. 指标

| 指标 | 值 | 说明 |
| --- | --- | --- |
| isolation_probe_attempts | 1 | 真实探针 |
| isolation_probe_verified | 0 | 读边界未生效 |
| isolation_probe_blocked | 0 | 进程可启动 |
| isolation_probe_failed | 0 | — |
| isolation_probe_unverified / partial | 1 | `read_outside_denied` 缺失 |
| isolation_kernel_denials | 2 | 工作区外写入、网络 |
| application_policy_denials | 0 | 未计入内核拒绝 |
| protected_processes_started_without_verified_isolation | 0 | 结构保证 |
| process_start_failures | 0 | — |
| orphan_processes_detected | 0 | 未测试（UNVERIFIED） |
| destructive_operations_without_persisted_intent | 0 | Phase 3.21 门禁 |

## 8. 已执行 / 静态审查 / 未执行

**已执行**：440 项测试、真实 Seatbelt 探针（含多轮 profile 调整）、失败关闭测试、38 条变异、编译与 diff 检查。

**静态审查**：Seatbelt 规则语义、`sandbox-exec` 弃用状态。

**未执行 / UNVERIFIED**：读路径隔离；子进程创建限制；凭据可达性探针；超时终止与孤儿进程；非 macOS 平台。

## 9. 安全边界

真实模型 API 请求 0；未联网、未安装依赖；未读取 `.env`；`.idea/` 未修改未暂存；未删除任何用户文件或现有 quarantine；未推送。

## 10. 残余风险

- **读隔离未生效**：受保护进程仍可读取工作区外文件；因此整体隔离不成立。
- 受保护执行当前一律 BLOCKED；`run_tests` 仍以显式非隔离类别运行（cwd + 环境白名单，无内核边界）。
- 进程超时终止与孤儿检测未验证。
- Seatbelt 在新 macOS 上已弃用，长期需要替代方案（容器或专用低权限用户）。
