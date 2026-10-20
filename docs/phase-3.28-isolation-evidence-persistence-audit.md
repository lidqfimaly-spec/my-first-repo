# Zevorinth Phase 3.28：隔离证据持久化与完整性保护

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 证据现在可以原子写入、重启后读取，并在读取时重新校验结构、字段与摘要；损坏、截断、篡改、ID 冲突、失效标记、路径穿越与不可写存储都会失败关闭。持久化证据**不替代** Phase 3.27 的执行前重验：加载后仍必须通过 TTL、请求绑定、命令摘要与实时运行时检查。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `465f27f6d3d86d4d8e67b9352e1bbcba4972618a` |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS，Python 3.14.6 |

## 2. 数据模型与存储位置

| 项 | 值 |
| --- | --- |
| 模块 | `src/zevorinth/evidence_store.py` |
| 存储目录 | `<root>/evidence/`，权限 `0700` |
| 文件名 | `<evidence_id>.json`；`evidence_id` 限定 `[A-Za-z0-9_-]{1,128}` |
| 失效标记 | `<evidence_id>.invalidated`（记录原因） |
| 序列化 | 规范化 JSON（键排序、紧凑分隔符、UTF-8、末尾换行） |
| 记录字段 | Phase 3.27 的 12 个必需字段 + `digest` |

接口：`save_evidence`、`load_evidence`、`list_evidence`、`invalidate_evidence`、`verify_stored_evidence`。

## 3. 原子写入与恢复

1. 校验必需字段与摘要；
2. `mkstemp` 在目标目录内创建临时文件（0700 目录）；
3. 写入 → `flush` → `fsync(fd)`；
4. `os.replace` 原子替换；
5. 对目录 `fsync`（失败时忽略并记录）；
6. **重新加载**并比对摘要，只有一致才返回 `saved`；
7. 任何失败会删除临时文件并抛 `store_write_failed`，不会返回未持久化的 ID。

失败语义：`evidence_incomplete` / `evidence_digest_mismatch` / `evidence_conflict` / `store_write_failed` / `store_unavailable` / `evidence_missing` / `evidence_corrupt`。

**不承诺**：`os.replace` + `fsync` 不等于掉电后必然持久（依赖文件系统与硬件），文档如实记录。

## 4. 完整性能力与威胁模型

| 能力 | 是否具备 | 说明 |
| --- | --- | --- |
| 意外损坏检测 | 是 | 结构校验 + 规范化摘要 + 截断/JSON 解析失败 |
| 来源认证 | **否** | 无签名密钥、无信任根 |
| 抵抗同权限篡改 | **否** | 能写文件的主体也能重写摘要 |

因此：摘要证明“内容与签发时一致”，**不证明“由可信流程产生”**。本阶段不生成临时密钥、不读取环境秘密。

## 5. 与 Phase 3.27 生命周期集成

`verify_stored_evidence()` = 加载（结构+摘要） → 失效标记检查 → `verify_evidence()`（TTL、未来签发、请求绑定、命令摘要、运行时/镜像/配置、探针完整性）。

- 重启后**不会**自动恢复 `verified`：必须重新跑完整验证；
- 过期证据不能靠改字段续期（摘要会失效）；
- 失效记录加载即 `blocked`（`evidence_invalidated`）；
- 加载失败 → `blocked`，不启动进程、不回退宿主机。

## 6. 故障注入结果

| 场景 | 结果 |
| --- | --- |
| 目录不存在 | 自动创建（0700） |
| 目录不可写 | `store_write_failed`，无半份记录 |
| 临时文件残留 | 失败时清理；成功路径无残留 |
| 文件被截断 | `evidence_corrupt` |
| JSON 损坏 | `evidence_corrupt` |
| 摘要不匹配 | `evidence_digest_mismatch` |
| 同 ID 不同内容 | `evidence_conflict`（不覆盖） |
| 同 ID 同内容 | `unchanged`（幂等） |
| 加载时已过期 | `expired` |
| 失效标记存在 | `blocked` / `evidence_invalidated` |
| 另一请求复用 | `blocked` / `evidence_request_mismatch` |
| 运行时变化后重载 | `blocked` / `identity_mismatch` |
| 存储损坏后验证 | `blocked` |

## 7. 测试与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 509/509，33.4s / 32.6s | 0 |
| 证据存储专项 | 17/17 | 0 |
| Mutation runner | 52 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

新增变异：`corrupt_evidence_accepted`、`evidence_conflict_overwritten`、`invalidation_ignored`，均被捕获。

修复过程中发现并修正一个真实缺陷：`mkstemp` 原先位于 `try` 之外，目录不可写时会抛出裸 `PermissionError` 而不是 `EvidenceStoreError`（现已包装）。

## 8. 已执行 / 静态 / 未执行

**已执行**：509 项测试、存储全故障矩阵、3 条变异。

**静态审查**：`os.replace`/`fsync` 的持久性语义（未做掉电测试）。

**未执行**：掉电持久性、并发多进程写入压力、真实运行时下的存储+执行联动（本轮未重新跑容器探针）。

## 9. 安全边界

真实模型 API 请求 0；未联网、未安装依赖；未启动/关闭 Docker Desktop；未读取 `.env`；`.idea/` 未修改未暂存；未删除 quarantine 或用户数据；未推送。

## 10. 残余风险

- **无来源认证**：同权限主体可同时篡改内容与摘要。
- 未做掉电/崩溃一致性实验；`fsync` 顺序是常规做法，不等于证明。
- 未实现并发多进程写同一 ID 的锁；当前依赖 `os.replace` 的原子性与冲突检测。
- 证据尚未与任务状态事务性绑定：任务状态更新失败时可能出现“任务记录存在但证据缺失”，当前以 fail-closed 处理（找不到证据即阻断）。
- 容器真实探针未在本轮重跑（3.26 的运行时证据仍是最近一次真实结果）。
