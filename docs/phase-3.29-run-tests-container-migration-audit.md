# Zevorinth Phase 3.29：run_tests 容器化迁移审计

## 1. 结论与基线

**BLOCKED（真实容器化 run_tests 未执行）**，整体 **PASS WITH LIMITATIONS**。

本机只有 `alpine:3.20`，**没有可运行 `unittest` 的 Python 镜像**；本阶段禁止联网拉取。因此容器化测试执行无法真实跑通，`run_tests` 的默认后端**没有切换**（仍为 `host`），但容器分支已实现并强制 fail-closed。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `56ebced8973d2657aff020d793f9b7ac8184a897` |
| 分支 | `phase-2.6-response-hardening` |
| 本机镜像 | `alpine:3.20`（无 Python） |

## 2. 旧路径与新路径

| 路径 | 行为 |
| --- | --- |
| `execution_backend="host"`（当前默认） | 宿主机 `python -m unittest`，显式非隔离；结果仍受 workspace 副作用审计约束 |
| `execution_backend="container"` | 从 `EvidenceStore` 加载证据 → 实时重验（TTL/请求/命令/运行时/镜像/配置）→ 仅 `verified` 才执行；否则 FAILED `test_isolation_unverified` |
| 其他取值 | BLOCKED `invalid_execution_backend` |

**没有静默回退**：容器分支失败只返回 isolation 失败，不会转去宿主机执行（测试断言 `tool_result is None`）。

## 3. 证据与门禁集成

`_blocked_container_execution()`：

1. 从 `<project_root>/evidence` 构造 `EvidenceStore`；
2. 需要 `evidence_id`，否则 `evidence_id_missing`；
3. `verify_stored_evidence(expected_runtime, expected_image_digest, expected_config_digest, expected_request_id=task/attempt, expected_command_digest)`；
4. 非 `verified` → FAILED `test_isolation_unverified`，附带 `isolation_state` 与 `reasons`。

证据缺失、过期、失效、请求不符、命令摘要不符、运行时/镜像/配置变化都会被拒。

## 4. 为什么默认没有切换

阶段要求"默认只通过已验证的容器执行"，但：

- 本机无 Python 镜像；
- 拉取镜像需要联网，本阶段明确禁止；
- 切换到容器默认会让所有离线测试（约 30 项 + 用户验收 B2 + 审计矩阵）在无镜像环境下全部 BLOCKED，而这不是测试失败，而是环境缺失。

因此保持 `host` 为默认并**显式标注**为非隔离，容器分支作为可选路径先行落地；待有 Python 镜像的授权环境再切默认。

## 5. 测试与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 514/514，32.6s / 32.8s | 0 |
| run_tests 容器门禁专项 | 5/5 | 0 |
| Mutation runner | 54 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

新增变异：`run_tests_host_fallback_enabled`、`invalid_backend_accepted`，均被捕获。

修复过程中发现：`build_test_plan` 原先只复制 5 个字段，导致 `execution_backend` 被丢弃（容器模式静默退回 host）。已补齐 `execution_backend`/`evidence_id`/`image`/`config_digest`/`command_digest`，并加测试锁定。

## 6. 真实执行 vs 模拟

- **真实**：宿主机 `run_tests` 全链路（514 项测试内）；容器后端门禁的拒绝路径。
- **未执行**：容器内 `python -m unittest`（无镜像）；容器超时/取消/产物回收；容器内 stdout/stderr 采集上限。

## 7. 安全边界

真实模型 API 请求 0；未联网、未安装依赖、未拉取镜像；未启动/关闭 Docker Desktop；未读取 `.env`；`.idea/` 未修改未暂存；未删除 quarantine 或用户数据；未推送。

## 8. 残余风险

- `run_tests` 默认仍在宿主机执行（显式非隔离），这是本阶段未完成的目标。
- 容器分支未做真实执行验证：超时、取消、stdout/stderr 上限、产物回收均未测试。
- 证据的 `config_digest` / `command_digest` 目前由调用方提供，尚未由执行器强制派生。
- 产物回收（测试报告、日志引用）尚未实现。
