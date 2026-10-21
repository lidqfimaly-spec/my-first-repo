# Zevorinth Phase 3.76：真实失败场景驱动的受控恢复

## 1. 结论

**PASS。** 本阶段只实现一种真实、可复现的恢复路径：

```text
write_text_artifact + publish_lock_timeout
```

故障发生在 artifact 最终发布之前：staging 已写入但发布锁超时，writer 清理 staging，
任务记录 `tool_failed/task_failed`，最终 artifact 不存在。

恢复不修改原失败任务，也不自动重试；它要求显式授权，并创建一个确定性 replacement
task。replacement 使用新的 attempt、正常 Policy/执行/独立验证链。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `0ef5257e12cbbe9277feac56b85771ec290ce8a8` |
| 新任务操作 | **0** |
| 新恢复路径 | **1（prepublish publish-lock timeout）** |
| 真实模型 / 网络 / 安装 | 0 / 0 / 0 |

## 2. 为什么选择该失败

`publish_lock_timeout` 具备清晰的安全属性：

- 失败发生在最终 `os.link` 发布之前；
- 原 staging 文件在 writer `finally` 中清理；
- 原任务无 artifact、无 verification；
- tool result 明确为真实执行失败；
- 目标 artifact 不存在；
- 不涉及容器、测试副作用或用户文件覆盖。

因此可以在不猜测“副作用是否已发生”的情况下进行一次有限恢复。

## 3. 恢复前置条件

`recover_prepublish_publish_lock_failure()` 仅在以下条件全部满足时继续：

- 显式 `authorized=True`；
- 原任务状态为 `failed`；
- 操作是 `write_text_artifact`；
- 错误码严格等于 `publish_lock_timeout`；
- tool result 为 real、failure，并匹配原 attempt；
- 存在匹配 `tool_failed` 事件；
- 不存在 `tool_succeeded` 或 verification 事件；
- 原任务无 artifact 和 verification；
- artifact target 不存在且不是符号链接；
- task artifact directory/staging 路径安全；
- staging 目录为空；
- 没有由同一 source task 创建的第二个 replacement。

任何条件不满足都返回结构化 `rejected`，不创建 replacement。

## 4. 确定性 replacement

replacement task ID：

```text
uuid5(namespace=URL, name="zevorinth:<source_task_id>:prepublish_publish_lock_timeout")
```

因此同一 source task 最多对应一个 replacement：

- 两个线程同时请求，复用同一 replacement；
- 两个独立 Python 进程同时请求，只产生一个 replacement；
- replacement 创建成功但后续调用发生，返回 `already_recovered` 或当前状态；
- replacement 再次失败，不创建第三个任务，不自动重试。

原失败任务保持 `failed`，其 task/event 记录不被修改。

replacement 的 `task_created` payload 保存：

- recovery kind；
- source task ID；
- source error code；
- source attempt ID。

replacement 使用新的 task ID 和 attempt ID，并重新执行普通 Policy、真实文件写入和
独立文件读回验证。恢复动作返回成功不等于成功；只有 replacement 的独立 Verifier
通过后，报告才返回 `recovered`。

## 5. CLI

```text
zevorinth recover-prepublish <task-id> --confirm [--json]
```

没有 `--confirm` 时返回 `rejected/recovery_unauthorized`，退出码 4。

| 报告状态 | 退出码 |
| --- | --- |
| `recovered` / `already_recovered` | 0 |
| `recovery_failed` | 1 |
| `rejected` | 4 |
| `recovery_in_progress` | 5 |

报告同时携带 L0/L1 assurance 与未认证边界。

## 6. 拒绝矩阵

| 场景 | 结果 |
| --- | --- |
| 未授权 | `recovery_unauthorized` |
| 其他错误码 | `recovery_error_not_eligible` |
| source 有 artifact/verification | `recovery_side_effect_present` |
| target 已存在 | `recovery_target_exists` |
| staging 不干净 | `recovery_staging_not_clean` |
| 路径不安全 | `recovery_target_not_eligible` / `recovery_staging_unsafe` |
| replacement 创建后再失败 | `recovery_failed`，不再创建第三个任务 |
| 并发重复请求 | 同一 replacement |

## 7. 测试与验证

| 检查 | 结果 |
| --- | --- |
| Phase 3.76 恢复矩阵 | 13/13 |
| 系统 Python 全量 unittest | 772/772，exit 0 |
| `.venv` 全量 unittest | 772/772，exit 0 |
| Mutation runner | **163 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 163/163 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

真实故障测试通过独立进程持有 publication lock，使首个任务实际得到
`publish_lock_timeout`；随后释放锁并执行恢复。另有两个独立进程竞争恢复的测试。

## 8. 能力注册

`prepublish_write_recovery` 被登记为独立恢复能力：

- 不是第三个 task operation；
- 只接受 `publish_lock_timeout`；
- 只能用于无 artifact/verification 的失败；
- 每 source task 最多一个 replacement；
- 不是通用恢复，不自动执行。

## 9. 限制

- 只覆盖 `write_text_artifact` 的一个发布前故障；
- 不恢复测试执行失败、取消、超时或未知副作用；
- 不证明锁超时的所有底层原因；
- replacement 是新任务，不是把原 `failed` task 改写为成功；
- 不提供自动恢复队列；
- L2/L3 与同 UID/更高权限防护仍未实现。

## 10. 阶段判定

**PASS**：系统能够对一种真实、可复现、副作用尚未发生的失败进行显式授权、最多一次、
独立验证的受控恢复；所有不安全或不确定状态继续 fail-closed。
