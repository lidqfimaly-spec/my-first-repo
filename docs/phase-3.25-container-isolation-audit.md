# Zevorinth Phase 3.25：容器隔离后端验证

## 1. 结论与基线

**BLOCKED（容器边界未执行）**，整体 **PASS WITH LIMITATIONS**。

Docker CLI 存在但 **daemon 未运行**，且本阶段被明确禁止启动 daemon。因此容器探针 **NOT_EXECUTED**，容器后端状态保持 `blocked`，`run_protected_container()` 不启动任何进程。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `2919c52f9460676da4a3acc90a620fba84018351` |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS，Python 3.14.6 |
| Docker CLI | `/usr/local/bin/docker` |
| Daemon | 不可达：`docker.sock: no such file` |

## 2. 后端状态

`container_runtime_state()` 只读检查（不启动 daemon）：

| 字段 | 值 |
| --- | --- |
| state | `unavailable` |
| binary | `/usr/local/bin/docker` |
| server_version | `null` |
| reason | daemon is not reachable (not started by this phase) |

状态词汇：`unavailable` / `available_unverified` / `initialization_failed` / `boundary_verification_failed` / `verified` / `blocked` / `unverified`。daemon 可达也只是 `available_unverified`，**绝不自动成为 `verified`**。

## 3. 受保护容器配置

`build_container_command()` 生成：

```
docker run --rm
  --network none
  --read-only
  --user 65534:65534
  --cap-drop ALL
  --security-opt no-new-privileges
  --pids-limit 64
  --memory 256m
  --cpus 0.5
  --tmpfs /tmp:rw,noexec,nosuid,size=16m
  --mount type=bind,src=<workspace>,dst=/workspace
  --workdir /workspace
  <image> <argv...>
```

`assert_container_command_safe()` 在启动前拒绝：`--privileged`、`--network=host`、`--pid=host`、`--ipc=host`、任何非 `none` 的网络、非 `/workspace` 的挂载、`docker.sock`、`-e/--env/--env-file`、缺失 `--read-only` 或 `no-new-privileges`、以及未用 tag/digest 固定的镜像。

镜像：本机未运行 daemon，无法确认本地存在可用镜像；`python:3.13-slim` 仅作为配置中的**待验证候选**，未拉取、未使用。

## 4. 七项核心边界

| 边界 | 结果 | 说明 |
| --- | --- | --- |
| 工作区内读取 | NOT_EXECUTED | 无 daemon |
| 工作区外读取 | NOT_EXECUTED | 无 daemon |
| 工作区内写入 | NOT_EXECUTED | 无 daemon |
| 工作区外写入 | NOT_EXECUTED | 无 daemon |
| 网络 | NOT_EXECUTED | 无 daemon（`--network none` 只是配置，不是证据） |
| 凭据占位文件 | NOT_EXECUTED | 无 daemon |
| 子进程创建 | NOT_EXECUTED | 无 daemon |

`verify_container_boundaries()` 在 daemon 不可达时返回 `blocked` 并把所有检查标为 `not_executed`；daemon 可达但未实际跑探针时返回 `unverified`，同样不是 `verified`。

## 5. 失败关闭

`run_protected_container()` 只在 `boundary_result.state == "verified"` 时构造并执行命令。当前状态为 `blocked`：

- `process_started=false`、`profile_applied=false`、`exit_code=null`；
- 不回退到宿主机执行；
- 未挂载任何宿主机路径，未继承环境变量。

`protected_processes_started_without_verified_isolation = 0`（Seatbelt 与容器两条路径都只能通过 `verified` 门禁）。

## 6. Phase 3.21–3.24 回归

| 项 | 结果 |
| --- | --- |
| 持久化意图门禁 | 保持（store 不可用 → `store_unavailable`；意图未确认 → `intent_not_persisted`） |
| quarantine 身份/哈希不匹配不删除 | 保持（3.20/3.22 测试通过） |
| Seatbelt `partial` 不被覆盖为 `verified` | 保持 |
| 容器状态不因二进制存在而 `verified` | 新增测试保证 |
| `run_tests` 显式非隔离类别 | 保持 |
| 应用层拒绝 ≠ 内核拒绝 | 保持（探针结果只按 `denied`/`allowed` 记录） |

## 7. 指标

| 指标 | 值 | 来源 |
| --- | --- | --- |
| isolation_probe_attempts | 0（容器） | daemon 不可达 |
| isolation_probe_blocked | 1 | `verify_container_boundaries` |
| isolation_probe_verified | 0 | — |
| container_runtime_state | `unavailable` | `docker info`（未启动 daemon） |
| protected_processes_started_without_verified_isolation | 0 | 结构保证 |
| process_timeout_terminations | 1 | 3.23 生命周期测试（宿主机路径） |
| orphan_processes_detected | 0 | 3.23 pgrep 检测 |
| cleanup_intent_gate_regressions | 0 | 3.21 测试 |

## 8. 回归与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 460/460，32.5s / 32.0s | 0 |
| 容器后端专项 | 9/9 | 0 |
| Mutation runner | 43 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

新增变异：`container_privileged_allowed`、`container_network_not_none`，均被捕获。

## 9. 已执行 / 静态 / 未执行

**已执行**：460 项测试、容器配置构建与安全断言、daemon 状态探测（只读）、失败关闭测试、43 条变异。

**静态审查**：容器参数语义、镜像候选。

**未执行 / BLOCKED**：容器边界七项探针、镜像拉取与核验、容器超时/取消/清理、宿主机凭据可达性。

## 10. 安全边界

真实模型 API 请求 0；未联网、未安装依赖；**未启动 Docker daemon**；未创建系统用户；未读取 `.env`；`.idea/` 未修改未暂存；未删除 quarantine 或用户数据；未推送。

## 11. 残余风险

- 容器后端完全没有运行时证据，`--network none` 等参数只是配置意图。
- 本地无已核验镜像；未拉取。
- 容器超时/取消/残留清理未验证。
- `run_tests` 仍是宿主机上的显式非隔离类别。
- 需要一台允许运行容器的授权环境，用同一 `verify_boundaries` 契约复测后才能考虑 `verified`。
