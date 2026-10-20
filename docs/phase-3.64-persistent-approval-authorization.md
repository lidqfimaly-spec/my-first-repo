# Zevorinth Phase 3.64：持久化审批与授权边界

## 1. 结论与基线

**PASS。** 审批记录现在可原子持久化、重启读取、撤销、拒绝和 stale 校验；审批记录
不能携带执行授权。两个独立进程在状态变化后都会拒绝旧审批。审批与执行能力仍然分离。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `d82f554f50967f7dacf9690400cfdb39d4f15f27` |
| 真实模型 API 请求 | **0**（本阶段） |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 持久化审批存储

新增 `RecoveryApprovalStore`：

- 目录：`recovery_approvals/`
- 目录权限：`0700`
- 文件权限：`0600`
- 使用临时文件 + `fsync` + `os.replace` 原子写入
- 每条记录包含 `record_digest`
- 重启后通过新 store 实例读取并验证

重复保存相同记录返回 `unchanged`；相同 approval ID、不同内容返回
`approval_conflict`。

## 3. 审批状态机

本阶段实际实现并测试：

| 状态 | 行为 |
| --- | --- |
| `approved` | 可绑定有效 preview，但不授权执行 |
| `rejected` | 不能通过授权校验 |
| `revoked` | 持久化撤销，不能通过授权校验 |
| `stale` | 状态摘要变化后派生，旧审批不能继续使用 |

`pending` 和 `consumed` 暂不纳入正式协议，避免只添加枚举而没有行为。

## 4. 新鲜度与授权分离

审批绑定：

- approval ID
- preview ID / version
- task ID
- state digest
- approver / time / scope

任务、事件、evidence 或 cleanup 状态变化后，`state_digest` 改变，旧审批返回
`stale`。两个独立进程读取同一持久化审批，也必须同时返回 stale。

`authorize_recovery_action()` 只做能力判定：

- 审批有效；
- 操作和目标属于当前 preview；
- scope 正确；
- 返回值仍为 `execution_supported=false`。

审批记录中的 `execution_authorized=true` 会被 store 拒绝；审批不能转化为执行许可。

## 5. 完整性

- 篡改 approval JSON/record digest → `approval_corrupt`
- 撤销后的记录重启仍为 revoked
- rejected approval 不能授权
- 重复提交不会产生重复授权
- 状态变化后旧审批不能重新生效

## 6. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 审批存储矩阵 | 8/8 |
| 系统 Python 全量 unittest | 660/660，exit 0 |
| `.venv` 全量 unittest | 660/660，exit 0 |
| Mutation runner | **77 killed / 0 survived / 0 invalid / 0 error** |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异 `approval_store_execution_authorization_allowed` 已被杀死。

## 7. 未验证事项与残余风险

1. 本阶段没有真实模型调用。
2. 审批 store 是本机文件存储；身份认证、多用户权限和外部审计系统未实现。
3. `pending`/`consumed` 状态和实际执行消费协议未实现。
4. 自动恢复、自动清理和历史迁移未实现。
5. cleanup TOCTOU、跨 OS 文件锁语义和数据库页级完整性仍未覆盖。

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
| `artifacts/audits/phase-3.64/approval-store-matrix.json` | `381a67cb7c50b804201cca093e4c4b58e90797b2be06bfa91b6ca9cfa204fb7f` |
| `artifacts/audits/phase-3.64/mutations.json` | `d7c50a50e10f2720e452286d0cca80ee41b4ad7b282d2c24bf105dfba88aaa39` |
| `artifacts/audits/phase-3.64/stable-summary.json` | `e938b743c08366961cd41336bc599295ddcaba36053b2c21869a2717e7d929e5` |

## 10. 阶段判定

**PASS**：审批记录持久化、重启可读、撤销/拒绝、stale 失效、重复提交幂等和
多进程 stale 重验均通过；审批与执行授权保持分离。
