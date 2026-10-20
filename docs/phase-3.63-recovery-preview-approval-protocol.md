# Zevorinth Phase 3.63：恢复预演与人工审批协议

## 1. 结论与基线

**PASS。** 只读恢复预演可为成功、中断、清理 pending 和存储不可读状态生成结构化
动作；预演与审批均不执行任务、不启动容器、不删除文件。审批绑定预演版本和状态摘要，
状态变化后变为 stale。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `728840c047e392f16accdb75b9d39725a132ca1d` |
| 真实模型 API 请求 | **0**（本阶段） |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 恢复预演

新增只读 `TaskService.recovery_preview(task_id)`：

- `preview_version=recovery-preview-v1`
- `preview_id`
- `state_digest`
- `confirmed`
- `unknown`
- `actions`
- `execution_authorized=false`

每个 action 包含：

- target
- current_state
- operation
- preconditions
- expected_result
- risk
- verification
- requires_approval
- executable

本阶段所有 action 的 `executable=false`。系统不会从预演直接进入执行。

## 3. 预演场景

| 场景 | 预演动作 |
| --- | --- |
| 成功任务 | `none`，无执行 |
| 中断/阻塞任务 | `manual_review` |
| cleanup pending/expired/unverified | `manual_reconcile_cleanup` |
| storage_unreadable | `manual_review`，检查临时副本 |

对于中断任务和不可读存储，必须人工复核；不提供自动 retry 或自动修复。

## 4. 审批绑定

审批记录包含：

- `approval_id`
- `preview_id`
- `preview_version`
- `task_id`
- `state_digest`
- `approver`
- `approved_at`
- `scope=plan_acknowledgement_only`
- `execution_authorized=false`

状态摘要绑定 task、events、audit issues、evidence state 和 cleanup state。任务状态
改变后，旧审批返回 `stale`，不能用于新的预演。

即使审批有效，也不授权执行；审批只表示对具体计划的确认。预演、审批和未来执行结果
是不同记录。

## 5. 只读与数据保护

- 预演前后任务 JSON、事件和 workspace 文件均不变；
- 预演不启动容器、不重跑任务、不清理文件；
- 存储不可读时只生成人工检查方案；
- 原始数据库和 evidence 不被自动修复或覆盖。

## 6. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 恢复预演矩阵 | 5/5 |
| 系统 Python 全量 unittest | 652/652，exit 0 |
| `.venv` 全量 unittest | 652/652，exit 0 |
| Mutation runner | **76 killed / 0 survived / 0 invalid / 0 error** |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异：

- `recovery_approval_stale_accepted` — killed；
- `recovery_preview_execution_authorized` — killed。

## 7. 未验证事项与残余风险

1. 本阶段没有真实模型调用。
2. 审批记录目前由协议返回，尚未实现独立的持久化审批库/服务。
3. 没有实际执行已审批恢复方案的接口。
4. 自动恢复、自动清理和历史迁移未实现。
5. cleanup TOCTOU、跨 OS 行为和数据库页级完整性仍未覆盖。

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
| `artifacts/audits/phase-3.63/recovery-preview-matrix.json` | `f635687168f9b83a1a30ac97b541b6e422892c8db1780ecf62426a76937d118e` |
| `artifacts/audits/phase-3.63/mutations.json` | `facc6c4c6631fa11a0d276f2456906c1030dbed96ac04cee7eb5c8ee258d6293` |
| `artifacts/audits/phase-3.63/stable-summary.json` | `8cef2610087884741e9b892fc05259fc24cf97e5b87533292366dd070eb7e3d2` |

## 10. 阶段判定

**PASS**：恢复预演可解释、只读、不执行；审批绑定具体计划与状态摘要，并在状态变化
后失效；没有未授权的自动恢复。
