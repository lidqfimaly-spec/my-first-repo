# Zevorinth Phase 3.15：离线端到端用户验收

## 1. 基线

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `45bd9e6a5402637a6b45cffe7ef53f1534615040` |
| 分支 | `phase-2.6-response-hardening` |
| Schema 版本 | v4（未变更） |

开始时工作树除 `.idea/` 外干净。所有文件与命令都在临时目录中执行。

## 2. 当前真实能力（以代码为准）

| 能力 | 状态 | 依据 |
| --- | --- | --- |
| 写文本产物 `write_text_artifact` | 支持 | `policy.SUPPORTED_OPERATIONS` 只含该操作 |
| 原子发布、不可覆盖、fsync | 支持 | `RealArtifactWriter` |
| 独立验证（内容 + 证据 + provenance） | 支持 | `ArtifactVerifier` |
| 协作式取消、预算、Lease、恢复 | 支持 | `TaskService` / `EventStore` |
| 多轮对话、澄清回填、计划修订 | 支持（确定性规则） | `ConversationService` / `IntentClassifier` |
| 通用 shell / 代码编辑 | **不支持** | 无对应工具，`plan()` 对未知操作抛 `unsupported_operation` |
| 运行测试并读取退出码 | **不支持** | 同上 |
| 网络检索 / 引用核实 | **不支持** | 无检索工具、无离线语料 |
| 真实模型 API | 未启用 | 本阶段为 0 次 |

验收场景只针对“支持”的能力；缺失能力以“诚实拒绝”场景验证，并在第 6 节列为产品待办。

## 3. 场景矩阵与结果

共 15 个场景，全部通过预期判定（`status = PASS`）。

| ID | 场景 | 入口 | 预期状态 | 实际状态 | 判定 |
| --- | --- | --- | --- | --- | --- |
| A1 | 创建文件、读回、独立验证 | 真实 CLI `run` | succeeded | succeeded | passed |
| A2 | 任务目录内已存在同名产物 | `TaskService` | blocked | blocked | blocked |
| A3 | 路径穿越 `../escaped.txt` | 真实 CLI `run` | blocked | blocked | blocked |
| A4 | 发布后篡改文件（负向对照） | 真实 CLI + 重新验证 | succeeded | succeeded | passed（验证必须失败） |
| B1 | 写出代码文件，由验收器运行测试 | 真实 CLI + 外部 `unittest` | succeeded | succeeded | passed |
| B2 | 要求 Agent 自己运行测试 | 真实 CLI `run --operation run_tests` | blocked | blocked | blocked |
| B3 | 缺少 content | 真实 CLI `run` | awaiting_clarification | awaiting_clarification | needs_clarification |
| C1 | 要求联网检索 | 真实 CLI `run --operation web_search` | blocked | blocked | blocked |
| D1 | 计划中使用 `shell` 工具 | `ArtifactPolicy` | blocked | blocked | blocked |
| D2 | 预算过期 | `TaskService` + 受控时钟 | failed | failed | failed |
| D3 | 工具执行前取消 | `TaskService.request_cancel` | cancelled | cancelled | cancelled |
| E1 | 四轮澄清（goal→operation→path→content） | `ConversationService` | succeeded | succeeded | passed |
| E2 | 新目标创建独立任务 | `ConversationService` | succeeded | succeeded | passed |
| E4 | 未执行计划先修订再执行 | `TaskService.revise_plan` | succeeded | succeeded | passed |
| E3 | 发布后中断、恢复不重写 | `TaskService` + fault hook | succeeded | succeeded | passed |

### 关键发现

1. **产物按任务隔离。** 最初把“同名文件已存在”设计成跨任务冲突，实际第二次 `run` 会成功——因为路径是 `artifacts/<task_id>/<name>`。A2 因此改为在任务目录内预置文件，复现真实的 `artifact_exists` 拒绝。这是设计属性，不是缺陷，已写入报告。
2. **未知操作先要求补全字段。** 直接 `--operation run_tests` 而不给 path/content，会先进入澄清而不是拒绝。补齐字段后才走 `unsupported_operation`。两种行为都是诚实的；场景固定为后者以验证明确的拒绝路径。
3. **内容损坏与哈希不匹配是两件事。** A4 篡改后重新验证失败且哈希不一致；如果恢复流程重新登记证据（E2E-10 的情况），哈希会一致但内容比对仍会失败。二者不可互相替代。

## 4. 独立验收方式

每个成功场景都不依赖 Planner 的自我结论：

- 从磁盘重新读取产物并计算 SHA-256；
- 用全新的 `ArtifactVerifier` 实例重新执行验证（内容 + 证据 + attempt/generation）；
- 对比 `effects.file_sha256`、`effects.evidence_sha256` 与案件记录；
- 检查事件计数（`tool_started`、`tool_succeeded`、`task_succeeded`）；
- 用统一 `build_effect` / `validate_case_effects` 校验每个场景。

## 5. 指标

分母均明确，负向对照（A4）从验证类比率中排除并单独计数。

| 指标 | 分子 / 分母 | 值 |
| --- | --- | --- |
| scenario_count | — | 15 |
| real_execution_count | origin ≠ none | 7 |
| passed | SUCCEEDED 且全部检查通过 | 7 |
| blocked | 策略/能力拒绝 | 5 |
| needs_clarification | 等待澄清 | 1 |
| cancelled | 用户取消 | 1 |
| failed | 执行失败 | 1 |
| not_executed | — | 0 |
| simulated | — | 0 |
| artifact_count（正向） | — | 6 |
| negative_control_count | — | 1 |
| verifier_pass_rate | 6/6 | 1.0 |
| artifact_integrity_rate | 6/6 | 1.0 |
| recovery_success_rate | 1/1 | 1.0 |
| conversation_constraint_rate | 3/3 | 1.0 |
| duplicate_side_effects | — | 0 |

`artifact_count = 6` 与 `file_write_count = 6` 相等：本阶段没有纯恢复读回的产物进入正向分母（E3 的恢复读取计入其自身用例）。

## 6. 产品能力缺口

| 缺口 | 影响 | 当前行为 |
| --- | --- | --- |
| 无通用 shell / 代码编辑工具 | 无法自行修改现有代码库或运行测试 | 以 `unsupported_operation` 明确拒绝；B1 中由验收器代为运行测试 |
| 无测试运行器 | 无法读取测试退出码并据此决策 | 同上 |
| 无网络检索 / 引用核实 | 无法完成研究类任务 | C1 明确拒绝，不生成任何引用或产物 |
| 无多步骤计划 | 一次只允许一个计划步骤 | `policy` 以 `unbounded_plan` 拒绝多步计划；本阶段未验证依赖链 |
| 无并行执行 | — | 未实现，未在验收中假设 |
| 无自动变异框架 | 变异覆盖有限 | NOT RUN，未联网安装 |

这些都记录为待办，没有用 mock 成功代替。

## 7. 回归验证

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| `python3 -m unittest discover -s tests` | 318 项通过，约 19.4s | 0 |
| `python3 -m tests.user_acceptance_runner` | 15/15 场景符合预期 | 0 |
| 同命令第二次运行（稳定哈希比较） | 哈希一致 | 0 |
| `python3 -m tests.e2e_acceptance_runner` | 12/12 场景通过 | 0 |
| `python3 -m tests.mutation_runner` | 14 killed | 0 |
| `python3 -m tests.coverage_runner` | 318 项通过，7/7 PASS | 0 |
| `python3 -m compileall -q src tests` | 无输出 | 0 |
| `git diff --check` | 无输出 | 0 |

覆盖率与 Phase 3.14 相同（`artifact_tool` 83.15%/69.05%，`event_store` 89.43%/71.95%，`service` 89.90%/78.31% 等），新增验收用例不改变模块覆盖率口径。

## 8. 稳定报告与哈希

稳定结果两次独立运行哈希一致：`fc941714b14299121a3097f7f89b42ae042e0b9d32968ad456fdfdde52f19d12`。

产物位于 `artifacts/audits/phase-3.15/`，被 `.gitignore` 排除，**未进入 Git 提交**：

| 文件 | SHA-256 |
| --- | --- |
| `acceptance-stable/stable.json` | `fc941714b14299121a3097f7f89b42ae042e0b9d32968ad456fdfdde52f19d12` |
| `acceptance-stable/metadata.json` | `32b4d7106051b822b6a738f1d4927af8be434246e7d77a3bc12d38d3c6541020` |
| `acceptance-summary.json` | `2a03d107dde42f5fa6a32a0fae45dad35cb29fdc4ca8435b535f1daffe0b9d3a` |
| `mutations.json` | `d51826d1dfc598e9a0c296290a040528adb97224e2485c717f646d67b181efa7` |
| `e2e.json` | `b3aa9099b7a6ddd27fa197f99282918da05b555d46e33d6fc36a0e7471a852e4` |
| `coverage/coverage-gate.json` | `2946c6022ec1c583ed0ff68884f16c563c01c770c7dc8b73bd6359f7ab056c29` |

复现命令：

```bash
python3 -m tests.user_acceptance_runner --output-dir <dir1>
python3 -m tests.user_acceptance_runner --output-dir <dir2>
diff <dir1>/stable.sha256 <dir2>/stable.sha256
```

## 9. 安全边界

- 真实模型 API 请求：0；
- 未联网、未安装依赖；
- 未读取 `.env` 或任何凭据；
- `.idea/` 未修改、未暂存；
- 所有写入限制在 `tempfile.TemporaryDirectory()` 内；
- 拒绝类场景未执行任何真实危险操作（shell、路径穿越都只触发拒绝路径）。

## 10. 限制与结论

**结论：PASS WITH LIMITATIONS。** 已支持的能力（单步文本产物、独立验证、取消、预算、澄清、计划修订、恢复）在真实入口与真实文件系统上通过验收；不支持的能力被如实拒绝并列为产品待办。

限制：

- B1 的测试执行由验收器完成，不是 Agent 自身能力；
- 只验证单步计划，多步骤依赖链与并行执行未实现；
- 研究场景只能证明“拒绝”，不能证明“检索质量”；
- 真实模型规划质量完全未评估（0 次调用）；
- 覆盖率仍只在一个 Python/coverage 组合上验证。
