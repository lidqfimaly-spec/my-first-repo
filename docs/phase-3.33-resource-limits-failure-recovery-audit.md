# Zevorinth Phase 3.33：资源边界与失败恢复验收

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 磁盘写入现在有**硬配额**（tmpfs 工作区 + 只读输入，实测 5MB 写入在 1MiB 配额下失败）；终止失败被注入并验证失败关闭；重复取消幂等；先前逃逸的 `collection_failure_ignored` 变异已被真正捕获。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `1d270c26afe633b4d5ede5ad4d35e7516c055737` |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS，Python 3.14.6，Docker 29.2.1 |
| 镜像 | `python:3.13-alpine` |

## 2. 磁盘与输出边界

### 磁盘（真实配额）

`build_container_command(..., disk_limit_bytes=N)`：

- 宿主工作区以 `--mount type=bind,src=<ws>,dst=/input,readonly` **只读**挂载；
- `/workspace` 改为 `--tmpfs /workspace:rw,noexec,nosuid,size=N`；
- 入口为 `sh -c "cp -a /input/. /workspace/ && cd /workspace && exec <argv>"`。

宿主目录因此**不可能**被容器写入撑大；写入超出 tmpfs 配额会以 ENOSPC 失败。

**真实测试**：1 MiB 配额下写 5 MB → 任务 FAILED（容器非零退出）。

**为什么不用 `--storage-opt size`**：它只作用于镜像/容器层，不限制 bind mount，且依赖存储驱动；本机未采用。

### 输出

- 客户端保存量受 `max_output_bytes` 限制并标记截断；
- 读线程并发排空管道，避免 64KiB 缓冲区死锁（3.32 修复）；
- 容器内 stdout 本身不受限——记录为已知限制。

## 3. 终止失败注入

`TaskService.container_terminator` 可注入。测试将其设为始终返回 `False`，超时后：

- 任务 FAILED `container_termination_unverified`；
- 不报告成功，不重试；
- 测试用 `docker rm -f` 清理残留容器（生产路径中由执行器 `docker kill` 处理）。

## 4. 重复取消幂等

同一任务两次 `request_cancel` → `cancel_requested_at` 相同（幂等），执行后状态为 CANCELLED；再取消仍为 CANCELLED。

## 5. 产物完整性

- `_collection_status(required, artifacts)` 纯函数，必需产物缺失 → `failed`；
- 任务在此情况下 FAILED `collection_failed`；
- 变异 `collection_failure_ignored`（让函数恒返回 complete）现在被 `test_collection_status_helper` 捕获。

## 6. 测试与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 533/533，42.9s / 42.2s | 0 |
| 资源/生命周期专项 | 10/10（含真实容器） | 0 |
| Mutation runner | 58 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

新增变异：`collection_failure_ignored`（重新纳入并捕获）、`termination_failure_reported_success`。

## 7. 真实 vs 模拟

- **真实容器**：正常、超时、取消、大输出、必需产物缺失、磁盘配额超限、终止失败注入（共 7 条端到端）。
- **单元**：`_collection_status`、产物收集边界、重复取消。
- **未执行**：容器内 stdout 上限；Docker 客户端中断；恢复后幂等（跨进程重启）。

## 8. 安全边界

真实模型 API 请求 0；未联网、未安装依赖；未启动/关闭 Docker Desktop；未读取 `.env`；`.idea/` 未修改未暂存；未删除用户数据或 quarantine；未推送。

## 9. 残余风险

- 磁盘配额只在使用 `disk_limit_bytes` 时生效；默认路径仍是 bind mount（无配额）。
- 容器内 stdout/临时文件（tmpfs 之外）不受限。
- 终止失败注入未覆盖"Docker 完全不可用"（`docker` 缺失）分支。
- 恢复（进程重启）后的幂等未单独测试。
- `host_authorization` 仍是固定字符串。
