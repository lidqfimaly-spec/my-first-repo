# Zevorinth Phase 3.17：工作区副作用审计与恢复

## 1. 基线与提交

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `11734c49563aad152f86c3e08eb84f9a176572ab` |
| 分支 | `phase-2.6-response-hardening` |
| Schema 版本 | v4（未变更） |

开始时工作树除 `.idea/` 外干净；未执行 reset/rebase/checkout/clean。

## 2. 设计

新增 `src/zevorinth/workspace_snapshot.py`：

- `scan_workspace()` 生成有界快照；
- `diff_snapshots()` 生成确定性差异；
- `EffectPolicy` 把变化分为 `expected` / `unexpected` / `unknown` / `not_applicable`；
- `validate_diff_record()` 校验差异明细与计数一致。

快照条目字段：相对路径、类型（file/dir/symlink/other）、大小、SHA-256、符号链接目标、mtime、快照标识（workspace/task/attempt/generation）、是否完整、未读取路径与原因、命中的上限。

快照不写文件内容，只写路径、元数据与哈希。

### 安全扫描规则

- 使用 `os.scandir(..., follow_symlinks=False)`，符号链接只记录目标字符串，**从不跟随**；
- 读取使用 `O_NOFOLLOW`，避免检查后被替换为链接；
- 默认包含隐藏文件；空目录记录为 `dir` 条目；
- 上限：2000 个条目、单文件 1 MiB、总读取 8 MiB、5 秒；
- 达到任一上限、无法读取、读取期间文件被修改（size/mtime 变化）→ `complete=False` 并记录原因；
- 取消检查 → `cancelled` + `complete=False`。

### 差异契约

每条差异：路径、变化类型（added/modified/deleted/type_changed/symlink_changed）、分类、判定依据、前后哈希与大小。计数由完整明细汇总；超过 200 条时保留前 200 条并标记 `changes_truncated`。

## 3. 生命周期集成

`TaskService._execute_test_tool` 的新顺序：

1. 取消检查与 authority 校验；
2. 构建 `EffectPolicy` 并解析工作区；
3. **前快照**；不完整 → FAILED `snapshot_incomplete_before`，进程不启动；
4. `TOOL_STARTED` 事件（含前快照摘要）；
5. 运行受控测试；
6. **后快照**；
7. 差异计算并写入 `TestExecutionEvidence`；
8. `TOOL_SUCCEEDED` → `VERIFYING`；
9. `TestResultVerifier` 复核。

测试任务不使用 Artifact 通道；证据挂在既有 `ToolResult.test_evidence` 上，没有新建平行的 Task/Event 模型。

## 4. 允许的副作用策略

参数扩展（仍为结构化字段）：

| 字段 | 含义 |
| --- | --- |
| `allowed_side_effects` | 允许写入的相对前缀数组，如 `["reports"]` |
| `protected_paths` | 禁止写入的相对路径数组，优先级高于允许列表 |

默认（两者皆空）= 只读策略：任何变化都是 `unexpected`。

校验拒绝绝对路径、含 `..` 的路径、反斜杠与非法列表类型。策略不依赖测试名称推断安全性。

## 5. Verifier 规则

`TestResultVerifier` 现在依次检查：

1. 进程状态（取消 / 超时 / 终止确认）；
2. 快照存在性；
3. 快照身份：前后快照与证据的 task/attempt/generation 一致，且 `evidence.claim_generation <= task.claim_generation`；
4. 快照完整性；
5. 差异记录自洽（`validate_diff_record`）；
6. **从快照独立重算差异**，与记录逐项比对；
7. `unexpected` / `unknown` 变化（任一存在即失败）；
8. 退出码与输出截断。

Verifier 不读取 Planner 或执行器的自我结论，只依赖持久化证据。

## 6. Recovery 行为

| 中断位置 | 恢复行为 |
| --- | --- |
| 前快照完成、进程未启动 | 正常重新执行（`tool_started` 计数为 0） |
| 已记录 `tool_started`、无结果 | BLOCKED `test_execution_unknown`，**不重跑** |
| 结果已持久化、验证未完成 | 只读验证（重新计算差异），**不重跑** |
| 验证完成、最终状态未提交 | 既有逻辑：重新验证并提交 |
| Lease 接管后旧 generation 证据 | 允许只读复核（generation ≤ 当前），但快照身份必须自洽 |

## 7. 场景矩阵

### 快照

| 场景 | 结果 |
| --- | --- |
| 空工作区 | 完整，0 条目 |
| 嵌套目录 + 隐藏文件 | 排序稳定，隐藏文件被收录 |
| 符号链接指向工作区外 | 只记录 `symlink`，不遍历目标 |
| 目录不可读 | `unreadable` + 不完整 |
| 文件数超限 / 总字节超限 / 单文件超限 | 不完整并记录具体上限 |
| 扫描取消 | 不完整 |
| 扫描期间文件被修改 | `unstable:<path>` + 不完整 |
| 两次扫描同一状态 | 摘要一致，顺序由路径决定 |

### 差异与策略

| 场景 | 结果 |
| --- | --- |
| 只读策略 + 新增文件 | `unexpected` / `read_only_policy` |
| 允许前缀写入 | `expected` |
| 受保护路径落在允许前缀内 | `unexpected` / `protected_path` |
| 修改 + 删除 | 分别识别 |
| 文件→目录、链接目标变化 | `type_changed` / `symlink_changed` |
| 伪造明细列表 | `validate_diff_record` 抛错 |
| 计数与明细不符 | 抛错 |
| 重复计算同一对快照 | 字节级一致 |

### 生命周期

| 场景 | 结果 |
| --- | --- |
| 干净测试 | SUCCEEDED，0 变化 |
| 允许的报告写入 | SUCCEEDED，2 个 expected |
| 测试通过但修改源码 | FAILED，`unexpected_workspace_change` |
| 修改受保护文件 | FAILED |
| 前快照不完整 | FAILED `snapshot_incomplete_before`，`tool_started=0` |
| 超时 | FAILED `test_timeout`，后快照完整且存在 |
| 中断后未知状态 | BLOCKED `test_execution_unknown`，`tool_started=1`（未重跑） |
| 结果已持久化后恢复 | SUCCEEDED，`tool_started=1`（未重跑） |
| 旧 generation 快照 | `snapshot_identity_mismatch` |
| 篡改差异 | `diff_recompute_mismatch` |

## 8. 指标（独立复算）

`tests/side_effect_audit_runner.py`，8 个场景：

| 指标 | 值 |
| --- | --- |
| scenario_count / started / audited / complete_audit | 8 / 7 / 6 / 6 |
| before_snapshot_rate | 0.75（6/8；未启动与未知恢复无前快照） |
| after_snapshot_rate | 1.0（6/6） |
| complete_snapshot_rate | 1.0（6/6） |
| diff_consistency_rate | 1.0（6/6） |
| side_effect_verification_rate | 0.667（4/6；两个负向场景刻意产生 unexpected） |
| expected / unexpected / unknown 变化 | 3 / 2 / 0 |
| added / modified / deleted | 4 / 1 / 0 |
| insufficient_evidence | 2 |
| recovery_reruns | 0 |
| duplicate_side_effects | 0 |

门禁内置检查：succeeded 必须有完整审计且 0 个 unexpected；blocked 不得启动新进程（恢复场景除外，改用 `recovery_reruns == 0` 判定）；重复场景 ID 直接失败。

## 9. 回归结果

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| `python3 -m unittest discover -s tests` | 389 项通过，约 32s | 0 |
| 新增 `tests/test_workspace_side_effects.py` | 28 项通过 | 0 |
| `tests/side_effect_audit_runner.py` | 8/8 场景符合预期 | 0 |
| 稳定结果双次运行 | `582a10e9…` 一致 | 0 |
| Phase 3.16 专项 | 26/26 | 0 |
| 用户验收（16 场景） | PASS | 0 |
| E2E acceptance | 12/12 | 0 |
| Mutation runner | 22 killed / 0 survived / 0 invalid / 0 error | 0 |
| 覆盖率门禁 | 389 项通过，9/9 PASS | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

覆盖率：

| 模块 | 行 | 分支 |
| --- | --- | --- |
| `workspace_snapshot.py` | 85.77% | 78.75% |
| `local_test_runner.py` | 91.49% | 90.48% |
| `service.py` | 89.13% | 78.80% |
| `verifier.py` | 85.54% | 85.29% |

新增 5 条变异全部被捕获且证明改写行被执行：

| Mutation | 性质 |
| --- | --- |
| `side_effect_ignores_deletion` | 删除变化被忽略 |
| `side_effect_before_snapshot_skip` | 前快照不完整仍继续 |
| `side_effect_unexpected_as_expected` | 只读策略下的变化被当作预期 |
| `side_effect_count_forgery` | 差异校验被整体跳过 |
| `side_effect_recovery_rerun` | 未知状态被重跑 |

其中两条最初存活：`unexpected_as_expected` 因为变异行被更早的只读分支遮蔽（未执行），`count_forgery` 因为后续逐项校验仍然拦截。分别改为命中只读分支、以及让校验函数整体短路后被杀。

## 10. 稳定报告与产物

`artifacts/audits/phase-3.17/`（`.gitignore` 排除，未进入 Git）：

| 文件 | SHA-256 |
| --- | --- |
| `side-effect-stable/stable.json` | `582a10e963aa551ff2422fbfd6dd2c7e6f7c9ca64dbae5a5dc9dd911a7967ddb` |
| `side-effect-stable/metadata.json` | `b3d4c9af32a49df633bb1d57fba01d9492b3adeb4ad96803904e12ffcde3e648` |
| `mutations.json` | `6e7f6b742ec18926cc551a10ca0317c6c80c3928fd993bb943aff46a2c725710` |
| `execution-stable/stable.json` | `0c5b6ba6cc1cf72e0c02a86ea0190b975d6e831437db93c870def8a9a197ec86` |
| `e2e.json` | `074423805ae7a49186ebb094a0b308008cd756a706d01d9cb89d6f3f0173cc5d` |
| `user-acceptance/stable.json` | `8914e936beebbcfeab45132cd2c67e1f859eddd082d018e051b2f730577ed3f3` |
| `coverage/coverage-gate.json` | `1e793b7783d2908eefafe33076133bb8eb1e161c2f3dd5ef538c4af87bb5153d` |

## 11. 安全边界

- 真实模型 API 请求：0；
- 未联网、未安装依赖；
- 未读取 `.env`；子进程环境变量仍是白名单；
- `.idea/` 未修改、未暂存；
- 所有快照与测试都在 `tempfile.TemporaryDirectory()` 内；
- 快照不读取工作区之外的文件，也不跟随符号链接。

## 12. 限制与残余风险

- **不是操作系统级隔离。** 只能观察工作区；测试对工作区之外的访问只能检测其“未声明”，无法阻止或证明未发生。
- 快照是逐文件抽样：扫描期间发生的变化会标记 `unstable` 并使快照不完整，但不保证捕获到所有瞬时状态。
- 不自动回滚任何变化（避免覆盖用户并发修改）；发现 `unexpected` 时只做失败与报告。
- 差异明细上限 200 条；超出时保留计数但不保留全部明细。
- 空目录会参与差异（目录新增/删除会被报告），可能与某些工具产生的临时目录相冲突，需要策略显式放行。
- 快照时间与 mtime 不进入差异判定（只比较类型/大小/哈希/链接目标），因此“同一内容被重写”不会产生差异。
- 未验证非 macOS 平台的文件系统语义（大小写敏感性、符号链接权限）。
