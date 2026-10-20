# Zevorinth Phase 3.53：任务生命周期一致性与进程级恢复审计

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 进程级清理竞争、SIGKILL 恢复、事件/状态事务原子性和
任务/清理状态分离已通过离线验证。真实模型异常路径本阶段没有新增调用；Phase 3.52
的真实超时基线保留，但取消、容器启动失败和验证器拒绝真实模型路径仍未执行。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `1229348a6a3ab0cf4d56a64c760fe84af08f6120` |
| 真实模型 API 请求 | **0**（本阶段） |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 进程级并发

两个独立 Python 进程同时清理同一 provisioning 文件：

- 一个 `completed`；
- 另一个 `already_cleaned`；
- 实际删除最多一次；
- `cleanup_completed` 事件恰好一次。

这不是进程内线程锁的替代证明，而是使用独立 SQLite/EventStore 进程验证。

## 3. SIGKILL 恢复

worker 在关键检查点输出 operation ID 后被 `SIGKILL`，由新的 TaskService 进程读取
持久化意图并 reconcile：

| 杀进程位置 | 新进程结果 |
| --- | --- |
| `after_quarantine` | `reconciled_pending`，quarantine 对象保留 |
| `after_unlink` | `reconciled_unverified`，不伪造完成 |

## 4. 任务状态与事件一致性

新增事务原子性测试：注入 event insert 失败后，任务状态更新不会单独持久化；
不存在的 `atomic_event` 也不会出现。

清理结果与原任务状态继续保持独立：

- 清理成功不改变任务状态；
- 清理失败不伪造任务失败；
- 清理完成事件未持久化时返回
  `unverified / completion_event_not_persisted`；
- 历史任务缺少最终执行计划时继续 `no_final_execution_plan`。

## 5. 生命周期矩阵

完整矩阵：`artifacts/audits/phase-3.53/lifecycle-process-matrix.json`。

| 类别 | 数量 |
| --- | --- |
| 总计 | 13 |
| 通过 | 13 |
| 真实进程竞争/SIGKILL | 3 |
| fixture / injected / unit | 10 |
| 意外成功 | 0 |

Phase 3.52 的真实模型超时基线记录为 prior phase baseline，本阶段未重复消耗模型额度。

## 6. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 生命周期/进程专项 | 13/13 |
| 系统 Python 全量 unittest | 619/619，exit 0 |
| `.venv` 全量 unittest | 619/619，exit 0 |
| Mutation runner | **71 killed / 0 survived / 0 invalid / 0 error** |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

## 7. 未验证事项与残余风险

1. 本阶段没有新增真实模型调用；Phase 3.52 的超时基线不等于取消/启动失败/验证器拒绝已验证。
2. 进程级竞争基于本机 SQLite 和 Python 进程；未覆盖所有操作系统/文件系统组合。
3. cleanup 的 TOCTOU 窗口仍未被证明完全消除。
4. `inside.txt` 所有权不明确，继续保留。
5. 历史任务迁移未实现，继续 fail-closed。
6. 清理仍为显式调用，尚未实现自动队列和生命周期钩子。

## 8. 安全边界

- 真实模型 API 请求：0（本阶段）；
- 未读取 `.env` 或输出密钥；
- 未访问互联网、未安装依赖；
- 未启动或关闭 Docker Desktop；
- 未修改、暂存或跟踪 `.idea/`；
- 未执行 reset / clean / rebase / amend / 分支切换；
- 未放宽 policy、隔离证据或独立验证门禁；
- 未发生宿主机回退。

## 9. 产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.53/lifecycle-process-matrix.json` | `225b300808176312da3ec70b323e405e98d960d0f71c860e014da1151c9dbf28` |
| `artifacts/audits/phase-3.53/mutations.json` | `766a7ab8619a4e475e1ff46f146dd288a7d3f817a46b454842b6cee0ab1f18cb` |
| `artifacts/audits/phase-3.53/stable-summary.json` | `59c8f04a094954e92f1787304221e338906f4c0a1322afc13ea0ba5ad34cc993` |

## 10. 阶段判定

**PASS WITH LIMITATIONS**：离线任务生命周期与进程级恢复核心能力通过；真实模型
取消/启动失败/验证器拒绝仍未补齐，清理自动化和历史迁移仍待后续阶段。
