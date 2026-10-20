# Zevorinth Phase 3.69：证据可信来源与外部篡改边界

## 1. 结论与范围

**PASS。** Phase 3.69 没有新增 action、签名、密钥或外部服务。它明确了现有恢复证据
究竟能证明什么，以及不能证明什么。

当前审计可以证明：

- 持久化记录之间在定义的 schema 下相互一致；
- completion 证据与当前文件观察在审计时点匹配；
- 已知的损坏、冲突、崩溃残留和文件漂移能够被识别。

当前审计不能证明：

- 审批人身份；
- evidence 确实由不可伪造的可信执行环境产生；
- 同一权限主体没有同时重写全部记录和摘要；
- 代码、Python 运行时、操作系统或内核没有被攻击者控制。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `a8a48132ac00fd052f790cac96d7f35bd86fc00f` |
| 新增 action | **0** |
| 真实恢复 / 清理 / 重试 | **0** |
| 真实模型 / 网络 / 安装 | 0 / 0 / 0 |
| 签名 / 密钥基础设施 | **未实现** |

## 2. 威胁模型

| 威胁 | 当前是否防御 | 说明 |
| --- | --- | --- |
| 普通 JSON/文件损坏 | 是 | record/evidence digest 和 schema 检查 |
| 单字段意外修改 | 是 | 旧 record digest 不匹配 |
| 崩溃留下 `consuming` | 是 | 只读对账进入人工复核 |
| 文件内容、权限或 inode 改变 | 是 | 当前 no-follow 观察与历史证据交叉核对 |
| action/target/freshness 不匹配 | 是 | 授权与 preview 身份检查 |
| 多条记录局部矛盾 | 是 | evidence graph 交叉校验 |
| 同 UID 主体重写文件、记录和全部摘要 | **否** | 可形成内部自洽记录 |
| 已控制 Python 进程或代码 | **否** | 可绕过任何进程内检查 |
| root / kernel / 文件系统攻击 | **否** | 本地普通摘要不构成可信根 |
| 远程透明审计缺失或离线 | 当前无 | 未实现外部审计服务 |

受保护的主体是“不能写恢复审批目录和执行 workspace 的故障或外部观察者”。
不受保护的主体是“拥有同一 OS 账户、代码执行能力或更高权限的恶意主体”。

## 3. 证据来源分类

| 证据 | 来源 | 当前认证 |
| --- | --- | --- |
| approval record | 本地文件存储 | 否 |
| execution intent | 执行器持久化到本地审批记录 | 否 |
| execution evidence | 执行器报告的本地操作结果 | 否 |
| verification evidence | 独立运行时验证函数重新读取的文件观察 | 否 |
| completion record | 本地文件存储 | 否 |
| current observation | 审计进程重新读取的当前 workspace | 否 |

“独立验证”在这里表示职责和读取路径独立于 materialization 返回值，不表示可信身份
或密码学远程证明。

## 4. 审计输出中的信任边界

每次审计都包含固定字段：

```text
authenticity=not_authenticated
trust_root=none
same_privilege_rewrite_protected=false
scope=controlled_recovery_consistency_only
security_certification=false
```

证据声明被明确拆分为：

```text
integrity=digest_detects_corruption_only
consistency=audited
authenticity=not_authenticated
same_privilege_tamper=not_defended
```

因此 `consistent` 只表示当前记录和当前文件观察在审计 schema 下自洽，不表示证据来源
可信或系统已通过安全认证。

## 5. 隔离篡改实验

所有实验均在 `tempfile` 创建的独立 service root、workspace 和审批记录中进行。

| 实验 | 结果 |
| --- | --- |
| 单条记录修改但不更新 record digest | `approval_corrupt` |
| 单个审批目录之外的生产文件 | 未被访问或修改 |
| 协调修改 intent/result/evidence digest 和 record digest，但保留错误 workspace | `stored_evidence_inconsistent` |
| 文件内容相同但 inode 替换且未重写记录 | 当前身份漂移 |
| 同权限主体重写文件、execution/verification、全部 digest 和 record digest | 审计可为 `consistent`，但明确标记未认证 |
| approved 或 revoked 状态 | 同样返回 `trust_root=none` 和未认证边界 |

## 6. 可信根评估

本阶段只评估，不实现：

| 候选可信根 | 可抵御同 UID 篡改 | 主要代价 / 限制 |
| --- | --- | --- |
| 同一文件树内额外 SHA-256 | 否 | 攻击者可同时重写 |
| 独立文件中的 Ed25519 签名 | 取决于私钥隔离 | 私钥与记录同权限时仍可被复制 |
| OS Keychain / Secure Enclave | 可能 | 平台绑定、解锁、密钥恢复和可移植性 |
| 本地 append-only 日志 | 部分 | 有写权限的攻击者仍可删除或重建 |
| 远程 append-only / transparency log | 可能 | 网络、身份、可用性、隐私和运维 |
| TPM / measured boot / runtime attestation | 可能 | 平台限制和部署复杂度 |

在威胁模型没有明确“同 UID 恶意主体”是否在范围内之前，不建议直接引入签名或密钥
系统。若未来需要防御该主体，应同时评估密钥隔离、签名记录、外部追加式日志和密钥
丢失恢复，而不是继续增加普通摘要字段。

## 7. 测试与变异

| 检查 | 结果 |
| --- | --- |
| Phase 3.69 trust-boundary 矩阵 | 5/5 |
| 系统 Python 全量 unittest | 728/728，exit 0 |
| `.venv` 全量 unittest | 728/728，exit 0 |
| Mutation runner | **122 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 122/122 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异覆盖：把 `consistent` 误标为已认证、伪造本地 trust root、声称可防御同权限
重写，以及把本地证据源标为已认证。

## 8. 安全边界与限制

- 未访问网络、未安装依赖、未读取 `.env`；
- 未调用真实模型；
- 未启动 Docker、shell 或恢复执行；
- 未新增密钥、签名或外部审计服务；
- 所有篡改只作用于测试临时目录；
- `.idea/` 保持未跟踪且未修改；
- 当前仍只在 macOS/POSIX 环境下验证。

## 9. 阶段判定

**PASS**：系统已经明确区分证据完整性、内部一致性和来源真实性；单条与局部协调篡改
均可识别；同权限主体重写全部本地记录的能力边界被准确记录，且没有被宣称为安全认证。
