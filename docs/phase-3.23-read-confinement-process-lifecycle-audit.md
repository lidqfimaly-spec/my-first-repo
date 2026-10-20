# Zevorinth Phase 3.23：读取隔离与进程生命周期验证

## 1. 结论与基线

**PASS WITH LIMITATIONS**。

- 读取隔离：**BLOCKED**——Seatbelt 在本机限制读取后解释器无法启动（exit 71 / SIGABRT 134），未能取得“工作区外读取被内核拒绝”的证据。
- 写入/网络边界：**已验证**（内核 `PermissionError`）。
- 进程生命周期：**已验证**——超时 `SIGKILL` 进程组后子进程树无残留（`pgrep` 检测，分母明确）。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `a585c7ad44172e492a6a5ede0067aa230ab34950` |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS，Python 3.14.6，`/usr/bin/sandbox-exec` 存在 |

## 2. 读取隔离最小化实验

逐条放行，记录实际结果：

| profile 组合 | 退出 | 说明 |
| --- | --- | --- |
| 仅允许读工作区 | 71 | `execvp` 被拒，解释器未启动 |
| + `file-read-metadata` | 134 | SIGABRT |
| + `sys.prefix` + `/usr/lib` + `/System` + `/dev` + 解释器字面量 | 134 | SIGABRT |
| 全局允许读（当前实现） | 0 | 可启动；写入/网络仍被拒 |

结论：在本机无法在“限制读取”的同时让 Python 3.14 启动，读取隔离 **BLOCKED**。按任务书要求，不采用“全局允许读”作为通过依据，`classify()` 因此维持 `partial`。

## 3. 读取边界矩阵（当前 profile 下的实测）

| 对象 | 结果 | 层 |
| --- | --- | --- |
| 工作区内普通文件 | allowed | 内核允许 |
| 工作区外普通文件 | **allowed** | **未受限制** |
| 工作区外符号链接 `escape-link` | allowed（跟随） | 未受限制 |
| 指向工作区内的外部链接 | allowed | — |
| 测试凭据占位文件 | 可被 `/bin/cat` 读取（受保护执行被 BLOCKED，未实际执行） | 未验证 |
| 不存在路径 | 不存在 | — |
| 路径遍历 | 未测试（读取未受限，无意义） | — |

`run_protected()` 在 `partial` 下拒绝启动，因此凭据占位文件的读取**没有发生**。

## 4. 进程生命周期

| 场景 | 结果 |
| --- | --- |
| 超时终止父进程 + 后代（`subprocess.Popen` 后代） | `killpg(SIGKILL)` + `wait` 确认；`pgrep -f <marker>` 在 10s 内降为 0 |
| 终止结果确认 | `_terminate()` 返回 `True` 且 `returncode` 非空 |
| 检测器不可用 | 返回 `None`（unknown），**不报告为 0** |

测试使用确定性轮询等待（出现 ≥2 个 marker 进程 → 终止 → 等待归零），不依赖固定 sleep 时序；`pgrep` 不可用时跳过并标记为未验证。

## 5. 失败关闭

`run_protected()` 仅在 `state == "verified"` 时启动；当前状态为 `partial`，因此：

- `process_started=false`、`profile_applied=false`、`exit_code=null`；
- 不回退到无隔离执行；
- `protected_processes_started_without_verified_isolation = 0`（结构保证）。

## 6. 替代后端评估（仅静态，不联网/不安装）

| 后端 | 读限制 | 写限制 | 网络 | 子进程 | 结论 |
| --- | --- | --- | --- | --- | --- |
| Seatbelt | 未实现（解释器无法启动） | 已验证 | 已验证 | 未验证 | 部分 |
| 专用低权限用户 | 需 root 创建 | 可限制 | 需额外配置 | 可限制 | NOT_EXECUTED |
| 本机容器运行时 | — | — | — | — | 未检测到可用运行时，未执行 |
| 最小权限进程 + cwd | 应用层 | 应用层 | 应用层 | 应用层 | 非内核边界 |

## 7. 指标

| 指标 | 值 | 分母/来源 |
| --- | --- | --- |
| isolation_probe_attempts | 4 | profile 组合实验 |
| isolation_probe_verified | 0 | — |
| isolation_probe_partial | 1 | 当前 profile |
| isolation_probe_blocked | 3 | 读限制导致无法启动 |
| isolation_kernel_denials | 2 | 写、网络 |
| read_inside_allowed | 1/1 | 探针 |
| read_outside_denied | **0/1** | 探针（未拒绝） |
| write_inside_allowed | 1/1 | 探针 |
| write_outside_denied | 1/1 | 探针 |
| network_denied | 1/1 | 探针 |
| process_timeout_terminations | 1 | 生命周期测试 |
| child_processes_remaining_after_timeout | 0 | pgrep 检测 |
| orphan_processes_detected | 0 | pgrep 检测（已执行） |
| protected_processes_started_without_verified_isolation | 0 | 结构保证 |
| cleanup_intent_gate_regressions | 0 | 3.21/3.22 测试仍通过 |

## 8. 回归与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 446/446，31.6s / 31.9s | 0 |
| 新增读取边界与生命周期专项 | 6/6 | 0 |
| Mutation runner | 40 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

新增变异：`timeout_kills_parent_only`（只杀父进程，后代残留）、`partial_isolation_as_verified`（把 partial 报成 verified），均被捕获。

## 9. 安全边界

真实模型 API 请求 0；未联网、未安装依赖；未读取 `.env`；`.idea/` 未修改未暂存；未删除 quarantine 或用户数据；未推送。

## 10. 残余风险

- **读取隔离未实现**：进程可读工作区外文件；`run_protected` 因此保持 BLOCKED。
- 子进程创建限制、凭据可达性、取消路径、恢复后进程存活均未验证。
- `pgrep` 检测依赖进程命令行含 marker，长期运行的进程若改写 argv 会漏检。
- `sandbox-exec` 已弃用，长期需要容器或低权限用户方案。
