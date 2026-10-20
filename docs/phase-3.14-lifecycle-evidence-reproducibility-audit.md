# Zevorinth Phase 3.14：生命周期证据与可复现审计产物

## 1. 基线

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `5dc83f9761f52013e21ac69031a6aae92609be37` |
| 分支 | `phase-2.6-response-hardening` |
| Schema 版本 | v4（未变更） |

开始时工作树除 `.idea/` 未跟踪文件外干净；没有 reset、rebase、切换分支或清理。

## 2. 真实执行顺序（以代码为准）

依据 `TaskService._execute_tool` 与 `_verify_and_finish`：

| 序号 | 动作 | 事务/文件系统 | 代码位置 |
| --- | --- | --- | --- |
| 1 | 领取任务、预算检查 | SQLite `BEGIN IMMEDIATE` | `event_store.claim_task` |
| 2 | 工具调用、staging 写入、原子发布 | 文件系统（`os.link` + fsync） | `RealArtifactWriter.write` |
| 3 | 记录 `tool_succeeded` 与 artifact evidence | 单个 SQLite 事务 | `service._execute_tool` |
| 4 | 转 `VERIFYING` | 单个 SQLite 事务 | `service._execute_tool` |
| 5 | 独立验证 | 只读文件 + 哈希 | `ArtifactVerifier.verify` |
| 6 | 记录 `verification_completed` | 单个 SQLite 事务 | `service._verify_and_finish` |
| 7 | 记录 `task_succeeded` | 单个 SQLite 事务 | `service._verify_and_finish` |

因此第 2 步与第 3 步之间**存在文件系统与 SQLite 的原子性缺口**：文件可能已经发布，而数据库尚无证据。这正是本阶段要覆盖的窗口。

`RealArtifactWriter` 内置 `checkpoint_hook`（默认 `None`），并在 `after_staging_write`、`after_publish_authority`、`after_publish` 三处回调；`TaskService.fault_hook` 默认也为 `None`。本阶段所有故障注入都通过测试内子类实现，生产默认路径没有任何变化。

## 3. 故障点与结果

新增 `tests/test_lifecycle_provenance.py`，8 个用例全部通过。

| 故障点 | 注入方式 | 最终状态 | 关键断言 |
| --- | --- | --- | --- |
| 发布前异常 | `CheckpointWriter` 在 `after_staging_write` 抛出 | FAILED `tool_exception` | 无产物、无 `.part` 残留、无 `tool_succeeded`、`effects` 为 none/0 写入 |
| 发布后、证据前异常 | `CheckpointWriter` 在 `after_publish` 抛出 | FAILED `tool_exception` | 文件存在且哈希可算，`task.artifact is None`，无 `tool_succeeded`，可复现为 `unbacked` |
| 证据事务失败（发布已完成） | `FaultStore` 在提交 `tool_succeeded` 前抛出，事务回滚 | RUNNING（持有租约）→ 恢复后 BLOCKED `artifact_exists` | 文件字节与哈希未变、`writer.calls == 0`、无 `task_succeeded`、`claim_generation == 2` |
| 证据已持久化、验证未开始 | `FaultStore` 在 `verification_started` 前抛出 | RUNNING → 恢复后 SUCCEEDED | 恢复只做校验，`tool_started == 1`、`task_succeeded == 1`、`effects` 为 recovered/0 写入 |
| 成功提交事务失败 | `FaultStore` 在 `task_succeeded` 前抛出 | VERIFYING（验证已通过）→ 恢复后 SUCCEEDED | 连续 `resume` 两次仍只有一条 `task_succeeded`，无额外写入 |
| 恢复时内容被修改 | 修改已发布文件后恢复 | FAILED `verification_failed` | 文件未被覆盖、无成功事件、`integrity` 为 mismatch 且两个哈希确实不同 |
| 恢复时文件缺失 | 删除已发布文件后恢复 | FAILED `verification_failed` | 原因为 `artifact_missing`、无成功事件、文件未重建 |
| 旧 generation 证据 | 用 generation 2 的证据验证 generation 1 的声明 | 验证失败 | 原因为 `tool_evidence_mismatch` |

第 4 项“成功提交期间进程被强制终止”由既有 SIGKILL 测试覆盖（`test_sigkill_inside_final_success_transaction_rolls_back_together`），本阶段补充的是同一窗口内的事务回滚路径。

所有用例都检查：任务状态、`claim_generation`、事件类型与计数、文件存在性与实际哈希、证据归属，以及通过 `build_effect` / `validate_case_effects` 记录的 `origin` / `integrity` / 写入次数。

## 4. 稳定结果与运行元数据

`tests/audit_report.py` 定义拆分与序列化契约：

- 对象键排序（混合键类型直接报错）；
- 紧凑分隔符 `,` `:`，无多余空白；
- UTF-8、`ensure_ascii=False`；
- `allow_nan=False`，`NaN` / `Infinity` 报错而不是写出非标准 JSON；
- 浮点数不取整、不改写，整数/布尔/`null` 保持标准表示；
- 数组顺序由稳定的案例 ID 决定，顺序变化会改变哈希；
- 文件内容为规范形式加**恰好一个换行**，`stable.sha256` 记录的正是该文件字节的哈希，可用 `shasum -a 256` 直接核对。

稳定投影移除的字段（运行时报告仍保留，便于人工排查）：

| 类型 | 字段 |
| --- | --- |
| Gate | `metrics.total_duration_ms`、`metrics.case_duration_ms`、`cases[].duration_ms`、`python_version`、`git_head`、`sdk_status` |
| E2E | `generated_at` |
| 本质随机 | `effects.evidence_attempt_id`（每次运行的新 UUID） |
| 随机临时目录 | `scenarios[].artifact.path`（保留 `exists` / `bytes` / `claimed` 与 `effects.relative_path`） |

元数据记录 UTC 生成时间、起止时间、耗时、命令、退出码、Python/平台信息、`stable_sha256` 与 `not_run` 列表，不采集环境变量值。

## 5. 双次复现与受控变化

两次独立进程运行（各自生成原始报告）后投影比较：

| 报告 | 第一次稳定哈希 | 第二次稳定哈希 | 结果 |
| --- | --- | --- | --- |
| E2E acceptance | `1c6e1531…0dd4c` | `1c6e1531…0dd4c` | identical |
| Safety gate | `b0a1bef1…accc4` | `b0a1bef1…accc4` | identical |

受控变化：把 E2E 稳定结果中的 `metrics.passed` 减 1，哈希从
`1c6e1531…` 变为 `25da5615…`，差异键为 `metrics`。变化是可见且可解释的，没有靠忽略案例取得复现。

## 6. effects 与报告级一致性

沿用 Phase 3.13 的契约，并对 E2E-10 / E2E-12 的语义分别固定：

- `E2E-10`：内容被改写，恢复流程重新读取文件并登记新证据，因此**哈希一致**，拒绝来自内容与计划不符（`details.detection = content_mismatch`）。
- `E2E-12`：改写后沿用原始证据，**文件哈希与证据哈希不一致**，`integrity = mismatch`，原因是 `tool_evidence_mismatch`。

本阶段新增的恢复用例中，`test_recovery_with_modified_content_fails_closed` 也采用“原始证据 + 被篡改文件”的组合，与 E2E-12 同一语义，但发生在真实任务生命周期内。

## 7. 变异执行证据边界

每条变异现在输出：

```json
"execution_evidence": {
  "scope": "in_process",
  "executed_in_process": true,
  "executed_in_child_process": false,
  "child_trace_supported": false,
  "trace_missing": false
}
```

聚合结果增加 `execution_evidence_counts`、`execution_unverified` 与 `unverified_survivals`。规则：

- `killed` 只表示选定的测试确实失败（且输出匹配测试 ID、无 `_FailedTest`）；
- `survived` 只有在 `scope == in_process` 时才算有效结论；
- 存活但未追踪到执行 → 记入 `unverified_survivals` 并使门禁 FAIL；
- 子进程执行不被推断为已证明，`executed_in_child_process` 恒为 `false`，`child_trace_supported` 恒为 `false`。

新增测试：`test_untraced_run_is_reported_as_unverified`（无 trace 时 scope 为 unverified）、`test_unverified_survival_fails_the_gate`（cli.py 的变异未被任何测试导入，测试通过但门禁 FAIL）。

正式变异集：14 条全部 `killed`，`in_process = 14`、`unverified = 0`、`child_process = 0`。

## 8. 回归与验证

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| `python3 -m unittest discover -s tests` | 309 项通过，约 18s | 0 |
| `python3 -m tests.e2e_acceptance_runner` | 12/12 场景通过 | 0 |
| 生命周期故障注入 | 8/8 通过 | 0 |
| 稳定结果双次复现 | E2E / Gate 均 identical | 0 |
| `python3 -m tests.mutation_runner` | 14 killed，全部 in_process | 0 |
| `python3 -m tests.mutation_runner --self-test` | killed/survived/invalid/error 各 1 | 0 |
| `python3 -m tests.coverage_runner --coverage-home <local-venv>` | 309 项通过，7/7 PASS | 0 |
| `python3 -m compileall -q src tests` | 无输出 | 0 |
| `git diff --check` | 无输出 | 0 |

覆盖率（Python 3.14.6，coverage 7.16.1 纯 Python tracer）：

| 模块 | 行 | 分支 |
| --- | --- | --- |
| `artifact_tool.py` | 83.15% | 69.05% |
| `conversation_service.py` | 69.47% | 57.14% |
| `event_store.py` | 89.43% | 71.95% |
| `policy.py` | 81.03% | 75.00% |
| `safety_gate.py` | 86.42% | 68.59% |
| `service.py` | 89.90% | 78.31% |
| `verifier.py` | 80.56% | 75.00% |

## 9. 审计产物

全部位于 `artifacts/audits/phase-3.14/`，被 `.gitignore` 排除，**未进入 Git 提交**。复现方式：

```bash
python3 -m tests.e2e_acceptance_runner --output <run1>
python3 -m tests.e2e_acceptance_runner --output <run2>
python3 -m tests.audit_report --kind acceptance <run1> <run2> --output-dir <stable>

python3 -m zevorinth.cli eval-safety-gate --offline --output <gate1> --json
python3 -m zevorinth.cli eval-safety-gate --offline --output <gate2> --json
python3 -m tests.audit_report --kind safety_gate <gate1> <gate2> --output-dir <stable>
```

| 文件 | SHA-256 |
| --- | --- |
| `e2e-stable/stable.json` | `1c6e1531cab7dbee8351987ea87d2c6b7ba09919fe537bece91f19467a50dd4c` |
| `e2e-stable/metadata.json` | `a4f85178a3241324979f9932578416174d5017874d0a3f4de12b5d067d85df9f` |
| `gate-stable/stable.json` | `b0a1bef15633ef7d88b03918a0dcf6f1559d5bda2873c1f343a2895a4d3accc4` |
| `gate-stable/metadata.json` | `fc36d0c7f6971d91110d92049b79aa5471803fefd1a2d518e3c690f35c2554ca` |
| `e2e-compare.json` | `8ceeb255751c1ef11b4ea735d6a28daaac48441cfd3a02b211df9090430f2ae3` |
| `gate-compare.json` | `844a6b08d13467acb3148dd71344606f36b69961e421ecd9f87737f76ca419da` |
| `controlled-change.json` | `889639407b33c543b460a485a6615b3d0c45ff9c4cd9fca128182f91081c2a83` |
| `mutations.json` | `7c5dcaeee04c8164330c1d970d032a4afd6a46961283141da1889d815017e9d6` |
| `mutation-self-test.json` | `b6dfc965d7f120270846c57842a8d76267f995c6a3f53784fcf8c948930344c7` |
| `coverage/coverage-gate.json` | `d888f19a6867568c424244097644f8265985fdf7cfcb7e235a1acd916606d08a` |

## 10. 限制与未解决事项

- 变异追踪只覆盖当前进程与线程；子进程路径明确标记为不支持，不推断为已证明。
- `survived` 与 `unverified` 的区分依赖 trace 文件是否落盘；trace 缺失时按 unverified 处理。
- 稳定哈希不覆盖 `evidence_attempt_id` 与 `artifact.path`，二者是构造上随机的；它们仍保留在运行时报告中用于排查。
- 生命周期用例使用测试专用 `EventStore` 子类在事务内抛错，等价于“进程在提交前退出”的数据库结果，但没有真的杀掉进程；进程级 SIGKILL 场景仍由既有崩溃一致性测试覆盖。
- `E2E-12` 仍在 verifier 层构造，未走完整生命周期；本阶段新增的 `test_recovery_with_modified_content_fails_closed` 在真实任务上覆盖同一语义，但它是单元级用例，不进入 E2E 报告指标。
- 覆盖率仍只在 Python 3.14.6 + coverage 7.16.1 组合上验证。
- 未安装自动变异框架（NOT RUN，未联网）。
