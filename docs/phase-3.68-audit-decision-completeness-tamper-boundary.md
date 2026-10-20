# Zevorinth Phase 3.68：恢复审计判定完备性与证据篡改边界

## 1. 结论与范围

**PASS。** Phase 3.68 没有新增 action，没有扩大执行权限，也没有触发真实恢复。
本阶段只强化现有 `restore_controlled_marker` 的只读审计判定。

审计现在会检查：

- 审批记录可读性和内部生命周期形状；
- approved 审批的可选 preview freshness；
- action / target 身份；
- execution intent 的完整 schema 和身份；
- execution、verification 与 completion 的交叉一致性；
- 当前真实文件与历史 completion 证据的一致性。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `138ed767ba09716efc5f1ea9b8f429bb37437869` |
| 新增 action | **0** |
| 新恢复 / 清理 / 重试 | **0** |
| 真实模型 / 网络 / 安装 | 0 / 0 / 0 |

## 2. 判定优先级

审计按固定顺序处理关键矛盾，避免因为某个正常字段而提前判定成功：

| 优先级 | 检查 | 主要结果 |
| --- | --- | --- |
| 1 | 审批缺失、JSON 损坏或 digest 不匹配 | `unverified / approval_store_unreadable` |
| 2 | 审批生命周期字段矛盾 | `unverified / approval_state_inconsistent` |
| 3 | revoked / rejected | `denied` |
| 4 | action 或 target 与当前 workspace 不匹配 | `unverified / controlled_action_binding_mismatch` |
| 5 | approved 但 preview 已 stale | `unverified / approval_freshness_invalid` |
| 6 | consuming intent 缺失或 schema 不一致 | `unverified / execution_intent_inconsistent` |
| 7 | completion / failed evidence 缺失或不一致 | `unverified / stored_*_evidence_inconsistent` |
| 8 | 历史成功但当前文件内容或身份漂移 | `unverified / historical_success_current_observation_diverged` 或 inode 不一致 |
| 9 | 全部检查一致 | `consistent` |

任何关键证据缺失、损坏、冲突或不可读取都不会返回 `consistent`。

## 3. 覆盖策略

没有穷举所有状态的笛卡尔积，而是覆盖七类独立边界，并要求每类至少存在一个反例：

1. 审批存储损坏；
2. 审批生命周期形状矛盾；
3. 授权身份或 freshness 失效；
4. execution intent 缺失或 schema 错误；
5. execution / verification / result envelope 交叉矛盾；
6. completion evidence digest 或 verification binding 矛盾；
7. 当前真实文件内容、权限或 inode 漂移。

多异常测试固定了优先级：

- action 绑定错误优先于 evidence/current-file 错误；
- stored evidence 损坏优先于当前文件漂移；
- revoked / rejected 即使文件存在也不能成为成功；
- 记录 shape 矛盾优先于普通状态解释。

## 4. 关键状态矩阵

| 组合 | 审计结果 |
| --- | --- |
| approved + 有效 preview | `not_claimed / approval_freshness=valid` |
| approved + stale preview | `unverified / manual_review` |
| approved 携带 attempt 或 result | `unverified / approval_state_inconsistent` |
| rejected / revoked | `denied` |
| consuming 无 intent | `unverified / manual_review` |
| consuming intent schema 不匹配 | `unverified / execution_intent_inconsistent` |
| consuming + target absent/verified/mismatch | `unverified / manual_review` |
| consumed 缺 intent/execution/verification | `unverified / stored_evidence_inconsistent` |
| consumed 的 result operation 或 target 不匹配 | `unverified / stored_evidence_inconsistent` |
| consumed 的 evidence digest 或 binding 不匹配 | `unverified / stored_evidence_inconsistent` |
| consumed + 当前内容/inode 不匹配 | `unverified / manual_review` |
| failed 且 evidence 图损坏 | `unverified / stored_failure_evidence_inconsistent` |
| failed 且证据记录一致 | `verified_failure / manual_review` |
| 重复 completion | `denied / approval_status_consumed` |
| completed evidence 与当前文件一致 | `consistent` |

所有未知、损坏和冲突状态均保持 `safe_to_retry=false`。

## 5. 混淆矩阵与摘要能力边界

| 场景 | 系统能力 |
| --- | --- |
| 普通 JSON 损坏或字段意外修改但未重算 record digest | 检测为 `approval_corrupt` |
| 字段修改并重算 record digest，但 evidence 交叉不一致 | 检测为 `stored_evidence_inconsistent` |
| 文件内容、权限或 inode 在 completion 后变化 | 检测为当前观察漂移 |
| 同一权限主体同时重写文件、completion evidence、所有 digest 和审批 record digest | 可形成新的内部自洽记录；审计返回 `consistent` |

最后一项不是尚未修复的普通 bug，而是当前本地摘要机制的能力边界。系统没有密钥、
审批人身份或外部可信时间戳，因此不能把 `consistent` 解释为安全认证。审计结果始终
包含：

```text
scope=controlled_recovery_consistency_only
security_certification=false
```

要抵御同权限恶意主体，需要后续单独引入密钥管理、签名审批、外部审计或可信执行环境。

## 6. 测试与变异

| 检查 | 结果 |
| --- | --- |
| Phase 3.68 判定矩阵 | 12/12 |
| 系统 Python 全量 unittest | 723/723，exit 0 |
| `.venv` 全量 unittest | 723/723，exit 0 |
| Mutation runner | **117 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 117/117 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异覆盖审批生命周期绕过、freshness 绕过、intent schema 绕过、failed evidence
digest 绕过、missing execution evidence 绕过和 result envelope operation 绕过。

## 7. 未验证事项

1. 当前仍只在 macOS/POSIX 文件和进程语义下验证。
2. 文件当前状态检查与 completion 写入之间仍有已知 TOCTOU 窗口。
3. record digest、evidence digest 和 observation digest 都不是密码学身份认证。
4. 没有真实模型、网络、依赖安装、Docker、自动恢复或自动清理。
5. 没有增加第二种恢复 action。

## 8. 阶段判定

**PASS**：关键证据缺失、损坏、freshness 失效、intent 不一致、completion 矛盾、
当前文件漂移和同权限重写边界均得到明确、稳定、保守且可复现的判定；任何未知或
矛盾状态不会被错误升级为可靠成功。
