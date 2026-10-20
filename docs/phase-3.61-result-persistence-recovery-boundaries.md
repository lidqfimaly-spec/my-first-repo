# Zevorinth Phase 3.61：执行结果持久化协议与恢复边界

## 1. 结论与基线

**PASS。** 执行开始、验证开始、最终终态写入失败均有明确恢复行为；旧 worker lease
不能覆盖新 claim；重复/并发恢复最多执行一次；旧 attempt 或 evidence 身份不匹配
会被拒绝。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `8a3fdcf11f7dd7b3db552c51828c59bcc20661c1` |
| 真实模型 API 请求 | **0**（本阶段） |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 写入边界

| 失败/中断点 | 结果 |
| --- | --- |
| `tool_started` 写入失败 | 缺少持久化测试结果；恢复为 `test_execution_unknown`，不重跑 |
| `verification_started` 写入失败 | tool evidence 已持久化；新进程只读验证，不重跑 |
| `task_succeeded` 终态写入失败 | verification 已持久化；新进程幂等完成终态 |
| 旧 worker 迟到写入 | `ClaimLost`，旧 lease 不能覆盖新 claim |
| 重复 `resume` | 终态幂等，`tool_started=1`、`task_succeeded=1` |
| 两个进程并发 `resume` | 至多一次测试执行，最终一个成功终态 |

## 3. 尝试与证据身份

- `task_id`、`attempt_id`、`claim_generation` 必须与最终执行计划和 evidence 一致；
- 旧 attempt 的计划身份会被审计器报告为
  `execution_plan_attempt_id_mismatch`；
- evidence/config/command/target 任一不匹配都拒绝验证；
- 不能通过重新标记、宿主机回退或跳过验证修复身份不一致。

## 4. 恢复安全

- 未知执行结果保持 `test_execution_unknown`；
- 执行任务 `safe_to_retry=false`；
- 恢复使用当前任务、当前 attempt、当前最终计划和当前 evidence；
- 旧 lease 的迟到写入被 fencing 拒绝；
- 重复/并发恢复不会产生第二个测试执行或第二个成功终态。

## 5. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 持久化边界/恢复矩阵 | 8/8 |
| 系统 Python 全量 unittest | 644/644，exit 0 |
| `.venv` 全量 unittest | 644/644，exit 0 |
| Mutation runner | **73 killed / 0 survived / 0 invalid / 0 error** |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

完整矩阵：`artifacts/audits/phase-3.61/persistence-boundary-recovery-matrix.json`。

## 6. 未验证事项与残余风险

1. 本阶段没有真实模型调用；真实模型异常路径使用既有基线。
2. 进程级并发 resume 使用 host backend；真实容器恢复基线来自 3.60。
3. 基于本机 SQLite/Python 进程，未覆盖所有 OS/文件系统锁语义。
4. 自动恢复、自动清理队列和历史迁移未实现。
5. cleanup TOCTOU 与运行期间数据库损坏仍未完整验证。

## 7. 安全边界

- 真实模型 API 请求：0（本阶段）；
- 未读取 `.env` 或输出密钥；
- 未访问互联网、未安装依赖；
- 未启动或关闭 Docker Desktop；
- 未修改、暂存或跟踪 `.idea/`；
- 未执行 reset / clean / rebase / amend / 分支切换；
- 未放宽 policy、隔离证据或独立验证门禁；
- 未发生宿主机回退。

## 8. 产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.61/persistence-boundary-recovery-matrix.json` | `f6b926143e45256f26fe26ad45b94477c176ac56832f14ad9e1a42540c491cde` |
| `artifacts/audits/phase-3.61/mutations.json` | `c53c861c7b638f6888966cc36b4f224481fab86b8638fb1088fa7e96c1e7d2ac` |
| `artifacts/audits/phase-3.61/stable-summary.json` | `d95b63d6937a655e1a5ce90047fbce89d7cb3e31a7c6519635b1715f8c69f674` |

## 9. 阶段判定

**PASS**：执行结果持久化边界有明确未知/失败语义，lease fencing 与重复/并发恢复
幂等，attempt/evidence 身份不匹配 fail-closed。
