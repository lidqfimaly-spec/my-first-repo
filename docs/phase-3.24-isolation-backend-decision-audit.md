# Zevorinth Phase 3.24：隔离后端决策与边界验收

## 1. 结论与基线

**PASS WITH LIMITATIONS**；隔离后端：**BLOCKED**（本机无可验证后端）。

结论要点：Seatbelt 的限制读取会使**任何**二进制（含 `/bin/cat`）直接 abort，因此读取隔离在本机不可验证；Docker CLI 存在但 daemon 未运行；低权限用户需 root。`run_protected()` 继续失败关闭。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `780a0abd8f64e257ce3f6bed009ead6032a3e92e` |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS，Python 3.14.6 |

## 2. Task A：Seatbelt 启动失败定位

| 实验 | 结果 |
| --- | --- |
| 限制读 + Python | exit 71（`execvp` 被拒）或 134（SIGABRT） |
| 限制读 + `/bin/cat`（仅允许读工作区） | **exit 134**（工作区内与工作区外均 abort） |
| 限制读 + `/bin/cat` + `/usr/lib`、`/bin`、`/System`、`/System/Volumes/Preboot`、`/private/var/db/dyld` | **exit 134** |

**结论**：失败不是 Python 特有，而是 `(deny default)` + 受限读在本机导致进程在动态链接阶段终止。由于进程根本没有进入用户代码，无法产生“读取被内核拒绝（EPERM）”的证据——这是 abort，不是 denial。据此读取隔离 **BLOCKED**，不再继续堆叠规则。

## 3. Task B：现有后端评估（仅本机检查，未联网/未安装）

| 后端 | 是否存在 | 证据状态 | 限制 |
| --- | --- | --- | --- |
| Seatbelt (`/usr/bin/sandbox-exec`) | 是 | `partial`（写/网络已验证；读 BLOCKED） | 受限读导致 abort；已被 Apple 弃用 |
| Docker CLI (`/usr/local/bin/docker`) | 是 | `not_executed` | daemon 未运行：`dial unix …/docker.sock: no such file` |
| Podman | 否 | `not_executed` | 未安装 |
| 专用低权限用户 | — | `not_executed` | 创建需 root，超出授权范围 |

`detect_backends()` 只报告可用性与限制，**不把二进制存在当作隔离证据**（`state` 永远不会是 `verified`）。

## 4. Task C：统一边界验收契约

`verify_boundaries(backend, workspace, decoy, credential)` 对以下 7 项给出 `allowed` / `denied` / `not_executed`：

`read_inside_allowed`、`read_outside_denied`、`write_inside_allowed`、`write_outside_denied`、`network_denied`、`credential_denied`、`subprocess_denied`。

只有**全部**符合预期才返回 `verified`；否则 `partial` / `failed` / `blocked` / `unverified`，并把未达成的项列在 `unverified_boundaries`。

本机 Seatbelt 结果：`partial`，未达成项为 `read_outside_denied`、`credential_denied`、`subprocess_denied`（后两项因读取未受限与未实现子进程探针）。

## 5. Task D：失败关闭

`run_protected()` 仅接受 `state == "verified"`；当前不存在该状态，因此：

- 不启动受保护进程；
- `profile_applied=false`、`exit_code=null`；
- 不回退到无隔离执行；
- 测试凭据占位文件未被读取（探针执行路径已被阻断）。

`protected_processes_started_without_verified_isolation = 0`（结构保证）。

## 6. 指标

| 指标 | 值 | 分母/来源 |
| --- | --- | --- |
| isolation_probe_attempts | 6 | profile 实验 + 探针 |
| isolation_probe_verified | 0 | — |
| isolation_probe_partial | 1 | 当前 Seatbelt |
| isolation_probe_blocked | 3 | 受限读 abort |
| read_outside_denied | 0/1 | 探针 |
| write_outside_denied / network_denied | 1/1 · 1/1 | 探针 |
| credential_denied | 0/1（未执行） | 边界矩阵 |
| subprocess_denied | 0/1（未执行） | 边界矩阵 |
| protected_processes_started_without_verified_isolation | 0 | 结构 |
| process_timeout_terminations | 1 | 3.23 生命周期测试 |
| orphan_processes_detected | 0 | pgrep（已执行） |
| cleanup_intent_gate_regressions | 0 | 3.21/3.22 测试 |

## 7. 回归与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 451/451，31.8s / 31.7s | 0 |
| 后端边界专项 | 5/5 | 0 |
| Mutation runner | 41 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

新增变异 `backend_presence_implies_verified`（把“二进制存在”当作 verified）被 `test_detection_reports_evidence_state_per_backend` 捕获。

同时修复了生命周期测试的偶发失败：预检会把上一次运行遗留的 marker 进程（按唯一 marker 精确匹配）清理后再开始，连续两次全量运行通过。

## 8. 安全边界

真实模型 API 请求 0；未联网、未安装依赖；未启动 Docker daemon；未创建系统用户；未读取 `.env`；`.idea/` 未修改未暂存；未删除 quarantine 或用户数据；未推送。

## 9. 已执行 / 静态审查 / 未执行

**已执行**：451 项测试、Seatbelt profile 实验（Python 与 `/bin/cat`）、后端探测、边界矩阵、41 条变异。

**静态审查**：Docker/podman 可用性与 daemon 状态、低权限用户方案。

**未执行 / BLOCKED**：读取隔离、凭据不可达性、子进程限制、容器后端、低权限用户后端。

## 10. 残余风险与建议

- 本机没有可验证的隔离后端；读取隔离与子进程限制仍无证据。
- `sandbox-exec` 弃用；Docker 需要启动 daemon（属于系统状态变更，未授权）。
- `run_tests` 仍是显式非隔离类别，不能描述为受沙箱保护的执行。
- 建议后续在**独立测试机**上启用容器运行时（`--network none`、只挂载工作区、只读根文件系统），用同一 `verify_boundaries` 契约复测；在此之前保持 `run_protected` BLOCKED，不扩大不可信代码执行范围。
