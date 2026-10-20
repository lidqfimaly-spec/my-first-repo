# Zevorinth Phase 3.62：数据库与证据完整性故障审计

## 1. 结论与基线

**PASS。** 损坏的数据库副本会显式打开失败；可打开但 task_json 损坏时，生命周期
审计返回结构化 `storage_unreadable` 并要求人工介入。Evidence 缺失或损坏时状态
保持 `blocked/unverified`，成功任务不能被审计为一致。所有故障注入仅作用于临时副本。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `4ccf40b1a772ba30a896a0f2d26900964bc5b2d6` |
| 真实模型 API 请求 | **0**（本阶段） |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 数据库损坏

### 不可打开的 DB 副本

- 在独立临时副本写入非 SQLite 内容；
- `TaskService` 初始化显式抛出 `sqlite3.DatabaseError`/`EventStoreError`；
- 原始数据库 SHA-256 保持不变；
- 原始任务仍可读取，没有被故障副本影响。

### 可打开但 task_json 损坏

将临时数据库中的 `task_json` 替换为无效 JSON 后：

- `audit_lifecycle()` 返回 `storage_unreadable`；
- `task_recovery.safe_to_retry=false`；
- `manual_intervention=true`；
- 数据库文件哈希在审计前后不变；
- 不尝试自动修复、重建或重跑。

## 3. Evidence 完整性

| 场景 | 结果 |
| --- | --- |
| evidence 文件缺失 | `evidence_missing` → blocked/unverified |
| evidence JSON 损坏 | `evidence_corrupt` → blocked/unverified |
| 成功任务的 evidence 不可验证 | `succeeded_with_unverified_evidence` |
| 审计前后 evidence 文件 | 字节完全不变 |

不重算摘要、不重新标记 evidence、不绕过验证、不回退宿主机。

## 4. 审计与恢复边界

- 存储不可读时审计结果是“未知/人工”，不是成功；
- `safe_to_retry=false`，不会自动重新执行；
- evidence 不可验证时成功结论被拒绝；
- 原始数据库和 evidence 文件都只在临时副本上注入故障；
- 审计器仍保持只读。

## 5. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 存储完整性矩阵 | 4/4 |
| 系统 Python 全量 unittest | 647/647，exit 0 |
| `.venv` 全量 unittest | 647/647，exit 0 |
| Mutation runner | **74 killed / 0 survived / 0 invalid / 0 error** |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异 `storage_failure_reported_success` 已被杀死。

## 6. 未验证事项与残余风险

1. 本阶段没有真实模型调用。
2. 不可打开的数据库会显式失败，但没有自动恢复/修复能力。
3. 损坏 DB 的深度取证、页级校验和备份恢复未实现。
4. SQLite 以外的持久化后端未覆盖。
5. 历史任务迁移、自动清理队列和 cleanup TOCTOU 仍未解决。

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
| `artifacts/audits/phase-3.62/storage-integrity-matrix.json` | `8c5d2bcd8dae06fa4e14dda5584344a8cee8c05e9b5a577c2e757096b9ed0ee6` |
| `artifacts/audits/phase-3.62/mutations.json` | `94416bfdfc4d3d90e0e318110423bf3af4f0b176b45d2487c966ee44f8273686` |
| `artifacts/audits/phase-3.62/stable-summary.json` | `55393a6786d45e1c44e2a568d11d96d7c9c7f9b12e57ed38db937c60362b1f25` |

## 9. 阶段判定

**PASS**：数据库损坏和 evidence 完整性故障都能被明确拒绝或标记未知，原始数据不被
修改，恢复建议不会自动重试或伪造成功。
