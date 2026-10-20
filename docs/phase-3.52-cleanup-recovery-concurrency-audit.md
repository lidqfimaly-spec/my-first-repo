# Zevorinth Phase 3.52：清理恢复、并发与真实异常路径审计

## 1. 结论与基线

**PASS。** Provisioning 清理已覆盖并发请求、检查后替换、quarantine 后崩溃、
unlink 后崩溃、完成事件持久化失败和历史任务拒绝。另用唯一一次真实模型调用验证了
真实容器超时路径：容器终止确认、任务失败、没有 `task_succeeded`。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `d79c6b324114d06c1a3bd06e695aab593bca1a71` |
| 真实模型 API 请求 | **1**（授权上限 1） |
| 自动重试 | **0** |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 清理并发与中断恢复

### 并发幂等

两个线程同时清理同一 `probe-written.txt`：

- 一个请求 `completed`；
- 另一个请求 `already_cleaned`；
- 实际删除最多一次；
- `cleanup_completed` 事件恰好一次。

### 中断点

| 中断点 | 结果 |
| --- | --- |
| intent 持久化失败 | `intent_not_persisted`，文件保留 |
| `after_quarantine` 崩溃 | `reconciled_pending`，quarantine 中的对象保留 |
| `after_unlink` 崩溃 | `reconciled_unverified`，不报告完成事件 |
| 完成事件持久化失败 | `unverified / completion_event_not_persisted`，文件状态明确记录 |

文件已删除但完成事件未持久化时，不会把清理报告为 `completed`。

## 3. 文件替换与路径保护

| 场景 | 结果 |
| --- | --- |
| 校验前内容改变 | `hash_mismatch`，不删除 |
| 校验后替换为符号链接 | `target_replaced` / `unsafe_path`，不删除 |
| 校验后替换为另一个普通文件 | `target_replaced`，不删除合法身份以外的对象 |
| 路径逃逸 | `invalid_path` |
| 目标不存在 | `missing_target` |

## 4. 任务状态分离与历史任务

- 清理成功或失败都不改变原任务状态；
- 清理失败不会伪造任务失败；
- 任务成功也不会掩盖未完成的清理；
- 没有最终执行计划的历史任务拒绝清理，返回
  `no_final_execution_plan`，不补造计划、不迁移。

## 5. 真实模型异常路径

使用真实模型的 `run_tests` 候选，workspace 中的 `test_ok` 故意 sleep 30 秒，
可信 timeout 为 1 秒：

| 观察项 | 实际值 |
| --- | --- |
| 真实请求 / 重试 | 1 / 0 |
| 响应 ID | `7e5f07b0-f76b-49d2-b32a-27a2e206ee5e` |
| tokens | 741 输入 / 115 输出 / 856 总计 |
| 候选校验 | `passed` |
| 计划工具 | `run_tests` |
| 容器 | `zv-1a5982846a84-85f03281` |
| 容器超时 | true |
| 终止确认 | true |
| 容器退出码 | null（被终止） |
| 任务状态 | `failed` |
| 错误码 | `test_timeout` |
| `TestResultVerifier` | `test_timeout` |
| evidence 独立复核 | `verified` |
| 计划/证据比对 | `None`（一致） |
| `task_succeeded` 事件 | 不存在 |
| 宿主机回退 | 0 |

这证明真实模型路径下的超时不会被错误报告为成功，且最终执行计划与证据身份保持
一致。

## 6. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 清理/恢复专项 | 14/14 |
| 清理 + 真实异常矩阵 | 15/15 |
| 系统 Python 全量 unittest | 616/616，exit 0 |
| `.venv` 全量 unittest | 616/616，exit 0 |
| Mutation runner | **71 killed / 0 survived / 0 invalid / 0 error** |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异：

- `provisioning_cleanup_concurrency_not_deduplicated` — killed；
- `provisioning_cleanup_completion_event_ignored` — killed。

## 7. 未验证事项与残余风险

1. 真实模型只验证了超时路径，未覆盖真实模型下的取消、容器启动失败和验证器拒绝。
2. 并发清理测试使用 SQLite/EventStore 与线程；更复杂的进程级并发仍需单独验证。
3. cleanup 原语的 TOCTOU 窗口被缩小，但没有被证明完全消除。
4. `inside.txt` 所有权仍不明确，继续保留。
5. 历史任务迁移未实现，继续 fail-closed。
6. 清理仍为显式调用，没有自动化队列或任务生命周期钩子。

## 8. 安全边界

- 真实模型 API 请求：1（授权上限 1）；
- 自动重试：0；
- 未改写模型输出；
- 未回退宿主机；
- 未读取 `.env`；密钥仅取自进程环境，未打印或落盘；
- 未联网安装依赖、未拉取镜像、未启停 Docker Desktop；
- `.idea/` 未修改、未暂存；
- 未推送远程仓库。

## 9. 产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.52/real-timeout-pilot.json` | `a99d9e143490eaaa44c9f2d0034c30d40624887208c1b7f2fe3db20505d75efe` |
| `artifacts/audits/phase-3.52/mutations.json` | `b95976a3167c8e6e76c2541a7743041510428addd925786679cf7678a1a28376` |
| `artifacts/audits/phase-3.52/stable-summary.json` | `1d161898f3e7f9b2e69c8b49f7ad41b7c214e7167610beaf811086cf767cf46f` |

## 10. 阶段判定

**PASS**：清理并发、故障恢复、文件替换防护、完成事件失败关闭、任务状态分离、
历史任务拒绝和真实模型超时路径均有实际证据。下一次真实模型调用仍需单独授权。
