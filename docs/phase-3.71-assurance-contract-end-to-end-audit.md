# Zevorinth Phase 3.71：安全保证契约的端到端一致性

## 1. 结论与范围

**PASS。** Phase 3.71 没有新增恢复 action、签名、密钥、网络服务或自动恢复能力。
本阶段把 assurance 从单个恢复审计函数提升为跨模块产品契约。

共享模块：`zevorinth.assurance`

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `2cbaa7f26f71484f8e850127b01a6d7bbaf7f9e9` |
| 新增 action | **0** |
| 真实恢复 / 清理 / 重试 | **0** |
| 真实模型 / 网络 / 安装 | 0 / 0 / 0 |

## 2. 跨模块契约盘点

| 输出边界 | 当前行为 |
| --- | --- |
| `audit_task_lifecycle()` | 一致时 L1；有问题或存储不可读时 L0 |
| `RecoveryPreview.to_dict()` | 携带 audit assurance、未认证、无 trust root、无安全认证 |
| controlled execution result | `succeeded/failed/denied` 为 L1；`unverified/unknown` 为 L0 |
| `audit_controlled_recovery()` | 按原有状态与优先级返回 L0/L1 |
| `reconcile_controlled_restore()` | 复用 controlled audit 契约 |
| CLI `recovery-audit` | 只读 JSON/人类输出，显式展示 assurance 与未认证边界 |
| CLI `doctor` | 只报告已实现的 L0/L1，不宣称 L2/L3 |
| task events / task status | 表达任务执行状态，不冒充恢复证据认证 |

没有前端模块；未来 UI 必须把这些字段直接呈现，不得只显示绿色成功。

## 3. 状态到 assurance 的映射

| 状态 | 等级 | 允许行为 |
| --- | --- | --- |
| `consistent` | L1 | 显示内部一致，注明来源未认证 |
| `not_claimed` | L1 | 显示未认领，不得显示完成 |
| `denied` | L1 | 显示拒绝 |
| `verified_failure` | L1 | 显示失败和人工复核 |
| `succeeded` / `failed` | L1 | 显示受控执行结果，不等于来源认证 |
| `completed` | L1 | 保留既有完成记录，注明未认证 |
| `claimed` / `consuming` | L0 | 未验证，人工复核 |
| `unverified` / `unknown` | L0 | 只读检查，人工复核 |

L2/L3 仍未实现，不会由任何输出路径返回。

## 4. 只读 CLI

新增命令：

```text
zevorinth recovery-audit <approval-id> --workspace <workspace> [--checked-at ISO] [--json]
```

命令只读取审批记录、workspace 文件并返回审计结果，不启动 TaskService 执行流程，不
修改审批或 workspace。

退出码：

| 状态 | 退出码 |
| --- | --- |
| `consistent` / `completed` | 0 |
| `verified_failure` | 1 |
| `not_claimed` | 3 |
| `denied` | 4 |
| `unverified` / unknown | 5 |

人类输出必须同时显示：

```text
Assurance: L1 (...)
Authenticity: not_authenticated
Trust root: none
Security certification: False
Manual review: ...
Safe to retry: ...
```

JSON 输出保留完整 evidence/trust/provenance 字段。

## 5. 行为约束

- L0 不得触发自动恢复、自动重试或自动清理。
- L1 不得自动重试、自动清理或扩大执行权限。
- `succeeded` 只表示受控操作内部结果，不表示 evidence 来源认证。
- `security_certification=false` 必须贯穿所有输出。
- L2/L3 未实现，CLI doctor 只报告 L0/L1。
- 文件存在、记录一致都不能绕过 task/evidence 契约。

## 6. 测试覆盖

| 检查 | 结果 |
| --- | --- |
| Phase 3.71 端到端契约矩阵 | 5/5 |
| 系统 Python 全量 unittest | 735/735，exit 0 |
| `.venv` 全量 unittest | 735/735，exit 0 |
| Mutation runner | **133 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 133/133 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

测试覆盖 lifecycle、preview、controlled result、controlled audit、reconcile、CLI
JSON/人类输出、损坏记录 L0、doctor capability、状态映射和共享 trust 字段。

## 7. 未验证事项

1. 当前仍只在 macOS/POSIX 环境下验证。
2. 没有真实前端，因此前端必须遵守的契约只在文档和 CLI/API 层验证。
3. L2/L3、签名和外部可信根未实现。
4. 同 UID 全量重写仍明确不在保护范围。
5. 未调用真实模型、未联网、未安装依赖、未自动恢复或清理。

## 8. 阶段判定

**PASS**：L0/L1 assurance、未认证边界、行为限制和 trust 字段已经跨 lifecycle、
preview、controlled execution、audit、reconcile、CLI 与 doctor 一致传播；没有发现
被错误映射为来源可信或安全认证的出口。
