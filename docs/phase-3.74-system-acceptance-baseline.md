# Zevorinth Phase 3.74：系统级验收基线与关键缺口

## 1. 总结论

**PASS WITH LIMITATIONS。**

Zevorinth 当前可以被接受为：

> 单机、本地、受控执行切片的 Alpha 基线。

它已经具备计划、策略、执行、独立验证、持久化、取消、故障恢复、证据审计和
受控资源清理的基础闭环。但它还不是通用自主 Agent，也不是生产级多用户平台。

本阶段只做只读评估，没有新增执行能力、调用真实模型、联网、安装依赖或修改代码。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 基线 HEAD | `aa5da6a799f72119bdf12fe4d0f97a35264f2ac2` |
| Phase 3.74 代码变更 | **0** |
| 本阶段真实模型/网络/安装 | 0 / 0 / 0 |

## 2. 证据来源

本基线没有重新执行测试，而是汇总已验证阶段和当前代码事实：

| 证据 | 结果 |
| --- | --- |
| Phase 3.69 | 728/728，122 mutations |
| Phase 3.70 | 730/730，129 mutations |
| Phase 3.71 | 735/735，133 mutations |
| Phase 3.72 | 742/742，141 mutations |
| Phase 3.73 | 751/751，148 mutations |
| Phase 3.7 E2E acceptance | 11/11，连续 10 轮通过 |
| 离线 wheel/sdist 安装 | 已通过 |
| 当前 worktree | 仅 `.idea/` 未跟踪 |

真实模型证据来自 Phase 3.46、3.48、3.51、3.54、3.55、3.56 等固定单样本路径。
这些证明特定闭环可运行，不代表通用规划质量或所有模型候选都安全。

## 3. 当前能力清单

状态定义：

- `implemented_tested`：已实现，有自动化测试和可复核证据。
- `implemented_partial`：已实现但覆盖范围或运行环境有限。
- `not_implemented`：未实现。
- `unverified`：没有足够证据证明。
- `documentation_drift`：实现与用户可见说明不一致。

| 能力 | 状态 | 主要证据 / 限制 |
| --- | --- | --- |
| 确定性 Planner | implemented_tested | Phase 3.7、acceptance tests |
| LLM Planner 协议与本地校验 | implemented_tested | 离线 fake/scripted + 多次受控真实请求 |
| 通用 LLM 规划质量 | unverified | 只有固定案例和少量真实样本 |
| `write_text_artifact` | implemented_tested | A1/A2/A5/A6、独立 Verifier |
| `run_tests` | implemented_tested | 容器、超时、取消、启动失败、Verifier 拒绝 |
| SQLite 原子状态/事件 | implemented_tested | Schema v4、迁移、故障注入 |
| Lease/fencing/budget | implemented_tested | 并发与 SIGKILL 测试 |
| 目标澄清与恢复执行 | implemented_tested | conversation/clarification/resume |
| 独立验证 | implemented_tested | 文件读回、测试证据重新校验 |
| 显式取消 | implemented_tested | 真实容器取消路径 |
| 受控 provisioning 清理 | implemented_partial | 显式调用、quarantine、reconcile；无自动队列 |
| Recovery preview/approval | implemented_tested | exact action、freshness、persistent records |
| 固定 marker 恢复操作 | implemented_tested | 单一 allowlisted action，不是通用恢复 |
| 生命周期/事件完整性审计 | implemented_tested | L0/L1、attempt 因果绑定 |
| CLI 审计入口 | implemented_tested | `status`、`task-audit`、`recovery-audit` |
| Web/Desktop UI | not_implemented | 当前只有 CLI/API |
| 自动重试/自动恢复 | not_implemented | 明确 fail-closed |
| 自动清理队列 | not_implemented | 显式操作 |
| L2/L3 可信证据 | not_implemented | `trust_root=none` |
| 多机/分布式执行 | not_implemented | 单机 SQLite + 本地文件系统 |
| 跨 OS 文件/锁语义 | unverified | 当前仅 macOS/POSIX |
| 通用 shell 或浏览器工具 | not_implemented | 权限边界外 |
| README 与能力声明 | documentation_drift | 仍写“只支持一种真实操作” |
| `doctor.task_types` | documentation_drift | 只报告 write，不报告 run_tests |

## 4. 端到端任务闭环

| 环节 | 当前状态 | 评价 |
| --- | --- | --- |
| 目标澄清 | 已实现 | 缺失字段进入 clarification，可显式恢复 |
| 计划生成 | 已实现 | 确定性成熟；LLM 限于严格协议和固定样本 |
| Policy/Lease/预算 | 已实现 | 模型不能绕过 |
| 工具执行 | 部分实现 | 两个真实操作：write artifact、run tests |
| 独立验证 | 已实现 | 不信任工具 success |
| 状态/事件持久化 | 已实现 | SQLite 事务、事件完整性审计 |
| 失败诊断 | 已实现 | 结构化错误、审计和生命周期约束 |
| 任务恢复 | 部分实现 | 有只读验证和固定 marker；无通用副作用恢复 |
| 资源清理 | 部分实现 | 显式受控清理；无自动生命周期钩子 |
| 用户交付 | 部分实现 | CLI/JSON；无 UI，README 能力声明落后 |

## 5. 实际任务能力判断

当前适合：

- 本地创建受控文本产物；
- 在注册 workspace 中运行有界测试；
- 容器证据验证；
- 单机、单用户、可人工监督的任务执行；
- 失败、取消、重启和证据不一致时保守停止。

当前不适合：

- 通用多工具自主任务；
- 任意 shell、网络浏览、代码修改；
- 无人工监督的高风险副作用；
- 多机/多用户；
- 自动修复和自动重试未知任务；
- 同 UID 或更高权限攻击者存在的高信任场景。

## 6. 缺口优先级

### P0：扩大自主执行前必须解决

1. **能力真相漂移**：README 和 `doctor` 未反映 `run_tests` 等现有能力。
2. **通用任务恢复缺失**：除固定 marker 外，没有经过审批的通用副作用恢复。
3. **A2/A3 信任边界未覆盖**：当前最高为 L1，不支持高信任场景。

### P1：核心产品能力缺口

1. 通用 LLM 规划质量缺少多样本评测。
2. 清理仍依赖人工显式调用。
3. 无 UI，用户状态和 assurance 必须通过 CLI/JSON 消费。
4. 跨 OS、共享文件系统和 Docker 环境未验证。
5. 事件历史只有内部一致性，不是外部认证。

### P2：增强项

1. 第二类受控工具或恢复 action。
2. 自动清理队列与生命周期钩子。
3. 多机调度。
4. 外部签名/透明审计。
5. 更丰富的记忆和会话能力。

## 7. 重复审计与边际收益

已经足够稳定的领域：

- L0/L1 assurance 语义；
- 生命周期/事件因果审计；
- 固定 marker 的原子 claim、intent、验证和崩溃对账；
- 文件内容、权限、inode 检查；
- SQLite lease/fencing 与事件事务。

继续增加普通 SHA-256 字段或相似审计规则的边际收益已经很低。除非出现：

- 新 action；
- 新持久化后端；
- 新平台；
- 新威胁等级；
- 新的真实任务失败案例；

否则不应继续扩张安全审计阶段。

## 8. 推荐后续顺序

1. **Phase 3.75 候选：能力真相与任务闭环**
   更新 README、`doctor` 和机器可读 capability manifest，使真实支持的操作、
   操作环境、保证等级和限制一致。

2. 基于真实用户任务选择第二个能力缺口：
   通用任务恢复、受控编辑、清理自动化或真实模型质量，按影响排序。

3. 如果产品确需 A2 防护：
   单独设计 L2 可信根，再进行签名和密钥生命周期验证。

4. 如果产品确需多机/多用户：
   重新设计持久化、锁、身份和分布式执行边界。

## 9. 停止条件

满足以下条件时应停止继续增加“审计阶段”：

- 当前没有新的 action 或数据边界；
- 没有新的真实失败样本；
- 现有 L0/L1、事件因果、恢复对账规则没有发现漏洞；
- 新增规则只是在重复验证相同不变量；
- 更重要的缺口已转向产品能力而非内部一致性。

只有在以下情况才启动新阶段：

- 发现可复现的状态/证据错误；
- 新增用户可见能力；
- 新增平台或威胁模型；
- 现有能力无法完成明确任务；
- 有需要验证的新风险假设。

## 10. 最终判断

Zevorinth 当前是一个有真实验证、故障关闭和审计基础的受控本地 Agent 内核，而不是
“已经完成通用安全认证的自主 Agent 平台”。

下一阶段最有价值的工作不是继续堆叠同类审计，而是修正能力真相、选择真实任务缺口，
并用端到端用户任务衡量交付成功率。
