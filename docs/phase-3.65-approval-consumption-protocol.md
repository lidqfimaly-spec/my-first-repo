# Zevorinth Phase 3.65：审批消费协议与并发授权边界

## 1. 结论与基线

**PASS。** 系统现在可以为一个已持久化、仍新鲜且精确绑定到 preview action 的审批
申请唯一 execution attempt；两个独立进程竞争时最多一个取得 claim。重放、过期、
撤销、拒绝、范围扩大和 action 漂移均被拒绝。`consuming` 只表示“执行尝试已预留”，
不表示真实恢复执行已被启动，也不授予执行能力。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `2bc54b50fd7a2375084eb1521d69839bb2ab8ce9` |
| 真实模型 API 请求 | **0** |
| 真实恢复 / 清理 / 容器操作 | **0** |
| 联网 / 安装依赖 | 否 / 否 |

## 2. 审批与 action 绑定

`approve_recovery_preview()` 现在必须明确传入 `action` 和 `target`。审批记录持久化：

- preview ID、version、task ID、state digest；
- action、target；
- operation、current state、preconditions、expected result；
- verification 方法、risk、executable；
- 完整 action 的 SHA-256 摘要。

claim 前会重新核对 preview 身份、state digest、scope、action/target 和完整 action
摘要。Phase 3.64 遗留的未绑定 action 审批按 fail-closed 处理，必须重新审批，不会
被自动迁移或推测目标。

## 3. 消费状态机

| 状态 | 语义 | 重放 / 恢复规则 |
| --- | --- | --- |
| `approved` | 已确认具体 preview action，但尚未预留执行尝试 | 可在状态仍新鲜时 claim |
| `consuming` | 已获得唯一 attempt ID，结果尚未验证 | `unverified`、人工复核、禁止自动重试 |
| `consumed` | attempt 结果与独立验证均已持久化，验证通过 | 已完成，禁止重放 |
| `failed` | attempt 结果与独立验证均已持久化，验证拒绝 | 已验证失败，人工处理，禁止自动重试 |
| `revoked` | 审批已被撤销 | 拒绝 |
| `rejected` | 审批被拒绝 | 拒绝 |

`consuming` 记录包含：

- 唯一 `execution_attempt_id`；
- `claimed_at`；
- 审批时绑定的完整 action 身份。

`consumed` / `failed` 还必须包含：

- `completed_at`；
- JSON object 形式的 result；
- `verification_passed`；
- 非空的独立验证摘要。

终态与验证结论不一致、缺少 result、缺少 attempt 或缺少验证信息的记录按
`approval_state_invalid` 拒绝加载和保存。

## 4. 并发与原子性

每个 approval ID 使用独立文件锁：

```text
recovery_approvals/.locks/<approval_id>.lock
```

- 目录权限 `0700`，锁文件权限 `0600`；
- `os.O_NOFOLLOW` 与 regular-file 检查阻止锁文件符号链接；
- claim 在 `flock(LOCK_EX)` 内完成 reload、digest 校验、preview 重验、action
  精配、状态转换和原子记录替换；
- 两个独立 Python 进程竞争同一审批时，实际结果为
  `{"claimed", "denied"}`，持久化记录只有一个 attempt ID；
- 第二次 claim、撤销后 claim 和 `consumed` 后 claim 均拒绝。

审批记录继续使用临时文件、`fsync`、`os.replace` 和目录 `fsync`。claim 或 completion
写入失败时返回错误，不返回成功完成结果。

## 5. 失败与崩溃语义

| 场景 | 结果 |
| --- | --- |
| claim 持久化失败 | 仍为 `approved`，调用方收到写入错误 |
| completion 写入失败 | 仍为 `consuming`，对账为 `unverified` + 人工复核 |
| result 无法 JSON 序列化 | completion 拒绝，状态保持 `consuming` |
| claim 后进程消失 | `consuming`，无 completion，禁止盲目前滚或重试 |
| verification 为 `false` | `failed`，不能成为 `consumed` |
| attempt ID 不匹配 | completion 拒绝 |
| 对账 | 纯只读；重复对账不改变 approval 文件 |

## 6. 授权边界

本阶段没有实现任何恢复操作执行器，也没有接入自动恢复、自动清理或历史迁移：

- claim、completion 和 reconcile 都返回或保持 `execution_supported=false`；
- approval 继续拒绝 `execution_authorized=true`；
- `approved` 和 `consuming` 都不是执行许可证；
- 测试确认任务状态、事件、workspace 文件和工作区探针在消费前后不变；
- 本阶段没有启动容器、Docker 或真实模型请求。

## 7. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 消费专项矩阵 | 21/21 |
| 系统 Python 全量 unittest | 681/681，exit 0 |
| `.venv` 全量 unittest | 681/681，exit 0 |
| Mutation runner | **91 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 91/91 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异覆盖：新鲜度绕过、scope 扩大、action/target 漂移、完整 action 摘要绕过、
claim 写入失败误报、无独立验证完成、验证失败误记为 consumed、completion 写入失败、
claim 授予执行能力、崩溃后自动重试和终态验证矛盾。

## 8. 未验证事项与残余风险

1. 文件锁仅在本机 macOS/POSIX 文件系统语义下验证；跨 OS、网络文件系统和容器共享
   文件系统上的 `flock` 行为未验证。
2. `record_digest` 检测内部损坏，但不是审批人或调用方的密码学身份认证；具有同等
   本机写权限的主体仍可重写记录及摘要。
3. 本阶段没有真实恢复操作，因此“操作已产生副作用但 completion 尚未落盘”只验证了
   状态协议，没有验证真实外部副作用。
4. 锁目录与文件路径仍存在本机恶意竞争者造成的 TOCTOU 窗口；当前只缩小了窗口，
   没有通过能力型文件 API 或形式化模型证明消除。
5. 没有实现审批过期 TTL、自动恢复队列、审批身份服务或跨主机授权。

## 9. 安全边界

- 未读取 `.env` 或输出凭据；
- 未访问互联网、未安装依赖；
- 未触发真实模型请求；
- 未启动或关闭 Docker；
- 未执行真实恢复、自动清理或历史迁移；
- 未修改、暂存或跟踪 `.idea/`；
- 未执行 reset / clean / rebase / amend / 分支切换 / push；
- 未放宽 evidence、隔离执行、取消或独立验证门禁；
- 未发生宿主机回退。

## 10. 阶段判定

**PASS**：审批精确绑定 action；claim 在同一跨进程锁内重验状态并分配唯一 attempt；
重放与 stale 被拒绝；写入失败和 claim 后崩溃 fail-closed；`consumed` 需要持久化结果
与独立验证；真实恢复执行能力仍未启用。
