# Zevorinth Phase 3.12：可复现门禁与证据归属审计

## 1. 基线

| 项目 | 值 |
| --- | --- |
| 起始基线 | `8f7f21aff69becc1f9279bc35e9c401b81a4fb7d` 之上、Phase 3.11 提交 `22caeb80869ab1212bd5cd301fa3f3a3f10640bb` |
| 分支 | `phase-2.6-response-hardening` |
| Schema 版本 | v4（本阶段未变更） |

本阶段不新增案例类别，不改变安全策略、Lease fencing、执行预算、不可覆盖发布、协作式取消或独立验证语义。

## 2. P0-1：同解释器覆盖率

### 2.1 问题

Phase 3.11 的覆盖率由 Python 3.12.13 + coverage 7.16.1 测得，而完整测试运行在 Python 3.14.6。两者数字一致，但复核者需要额外信任“不同解释器给出相同结果”这一前提。

### 2.2 离线方案

`tests/coverage_runner.py` 从本机既有安装复制 `coverage` 包到临时 shim，删除 `*.so`、`*.pyd`、`*.dylib` 和 `__pycache__`，使 coverage 退回到自带的纯 Python tracer，然后在**当前解释器**下运行真实测试套件、生成 JSON 报告并调用 `tests/coverage_gate.py`。

整个过程不联网、不安装依赖。若本地没有可用 coverage，runner 以 `CoverageEnvironmentError` 失败关闭，而不是给出伪造数据。

### 2.3 结果

同一 Python 3.14.6 解释器同时完成测试与覆盖率测量，261 项测试通过，门禁 PASS。连续两次独立运行得到**完全一致**的模块覆盖率数据。

## 3. event_store 覆盖率波动的根因

Phase 3.11 观察到 `event_store.py` 覆盖率在两次运行间相差 1 行、1 个分支。定位结果：

- 波动点：`event_store.py` 第 2060 行，`apply_plan_revision` 的乐观锁冲突 `raise PlanRevisionConflict("task plan version changed before revision committed")`。
- 触发测试：`tests/test_context_continuity.py::test_concurrent_revisions_have_one_winner`。
- 原因：两个线程在同一道屏障后并发修订。落败线程既可能在 `TaskService.revise_plan` 的预检处抛错（未进入 store），也可能通过预检后在 store 事务内撞到乐观锁。该测试的**断言**始终成立，只是覆盖路径不确定。

修复方式是新增确定性测试，而不是提高阈值或削弱并发测试：

`tests/test_event_store.py::test_apply_plan_revision_rejects_stale_version_at_store_layer` 直接调用 `store.apply_plan_revision`，用过期 `expected_plan_version` 固定触发该分支，并断言 `plan_version` 未变、事件未追加。

修复后 `event_store.py` 稳定为 802/899 行、176/246 分支（89.21% / 71.54%），两次重复测量完全一致。

## 4. P0-2：Artifact / Evidence 配对审计

### 4.1 之前的问题

Phase 3.11 的 `artifact_count=11` 与 `file_write_count=10` 只能靠人工说明（恢复案例读取已有产物而不重新写入）。聚合值还依赖 `actual` 字典里的启发式默认值（例如缺省时把 `artifact_created` 折算成 1 次写入），结构上无法证明来源。

### 4.2 现在的模型

每个案例都带一条 `effects` 记录，由产生副作用的位置直接写入，而不是事后推断：

| 字段 | 含义 |
| --- | --- |
| `origin` | `written`（本案例写入了新产物）、`recovered`（从持久化状态读回）、`none` |
| `relative_path` | 相对产物路径 |
| `file_sha256` | 磁盘文件的实际哈希 |
| `evidence_sha256` | `ArtifactEvidence` 中记录的哈希 |
| `evidence_attempt_id` / `evidence_claim_generation` | 证据归属 |
| `evidence_count` | 证据记录数 |
| `file_writes` | 观察到的 writer 调用次数 |
| `tool_calls` | 观察到的工具启动次数 |

`validate_case_effects()` 的规则：

- `none`：`file_writes`、`evidence_count` 必须为 0，路径与哈希必须为空；
- `written`：至少 1 次 writer 调用和 1 次工具调用，路径与两个哈希齐全；
- `recovered`：`file_writes` 必须为 0，证据必须存在；
- 任何有产物的案例，`file_sha256` 必须等于 `evidence_sha256`。

聚合指标改为从这些记录求和，因此“恢复读取被计成写入”在结构上不再可能。

### 4.3 实际分布

| origin | 案例数 |
| --- | --- |
| `written` | 10（每个 1 次写入、1 条证据） |
| `recovered` | 1（`local_recovery_readback_no_retry`，0 次写入） |
| `none` | 29 |

`artifact_count=11`、`file_write_count=10`、`evidence_count=11`、`duplicate_side_effects=0`，11 个产物的文件哈希与证据哈希全部一致。

指标定义版本因此从 `1.0.0` 提升到 `1.1.0`，Gate 版本提升到 `1.1.0`：统计口径确实变了，旧报告不再可直接比较。

## 5. P1：变异集扩展

新增三类安全性质覆盖，总数从 9 增至 12：

| Mutation ID | 性质 | 捕获测试 |
| --- | --- | --- |
| `policy_path_rejection` | 策略拒绝不安全路径（不允许异常被当成放行） | `test_safety_gate...test_gate_passes_with_layered_cases_and_bounded_metrics` |
| `execution_budget_enforced` | 过期执行预算在 claim 阶段即失败 | `test_execution_budget...test_claim_layer_rejects_expired_budget_on_its_own` |
| `recovery_verify_existing` | 已有产物恢复走校验而非重写 | `test_crash_consistency...test_sigkill_inside_final_success_transaction_rolls_back_together` |

预算变异最初存活，原因是有两层独立防护（`claim_task` 与 `_run_with_renewal`），服务级测试无法隔离。新增的 claim 层测试直接调用 `store.claim_task`，使该变异可被单独捕获。这一过程记录了“冗余防护会掩盖单点变异”的事实，而不是通过放宽断言解决。

结果：`killed=12 survived=0 invalid=0 error=0`。

## 6. P1：runner 抗误判

`unittest` 对“断言失败”和“测试选择失败（模块不存在、导入错误）”都返回退出码 1。旧 runner 会把后者计为 `killed`，即把 runner 自身缺陷当成成功捕获。

现在的分类规则：

- 退出码 0 → `survived`；
- 退出码 1 **且**输出包含 `FAILED (` **且**每个声明的测试 ID 都出现 → `killed`；
- 输出包含 `_FailedTest` 或 `unittest.loader` → `error`；
- 其它非零退出码 → `error`。

自检 `--self-test` 现在复用同一 `_classify_result()`，不再复制分类逻辑，因此自检确实验证生产分类器。新增测试覆盖：缺失测试模块 → `error`；声明为 killed 但实际 survived → 门禁 FAIL 且 `unexpected_results` 列出该 ID。

## 7. P1：覆盖率门禁加固

`tests/coverage_gate.py` 原先在 `num_branches == 0` 时把分支覆盖率记为 100%，一个只用行覆盖生成的报告也能通过分支门禁。现在：

- `meta.branch_coverage` 必须为 `true`，否则直接拒绝报告；
- 任一模块分支分母为 0 → `no_branch_data` 失败；
- 门禁结果记录覆盖率报告路径与 SHA-256。

## 8. P2：版本间评测比较

`tests/eval_compare.py` 比较两份 Gate 报告，规则：

- 缺少必需字段（含每个指标的 numerator/denominator/contributing_case_ids）→ `NOT_COMPARABLE`，不做猜测填补；
- `metric_definition_version` 变化 → `NOT_COMPARABLE`；
- 案例被删除 → `case_removed:<id>` 回归项；
- 基线通过、新版本失败的案例 → `case_regressed:<id>`；
- 新增案例单独列在 `added_cases`，失败的记为 `new_case_failing`；
- 分母缩小 → `denominator_shrunk:<metric>`；
- 高优指标下降、重复副作用上升 → `metric_regressed:<metric>`；
- `skipped_count` / `unexecuted_count` 增加 → 独立回归项。

对 Phase 3.11 与 3.12 报告的实际比较结果为 `NOT_COMPARABLE`，原因 `metric_definition_version_changed:1.0.0->1.1.0`，这是预期行为。

## 9. 回归与验证

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests` | 261 项通过，约 11s | 0 |
| `python3 -m tests.coverage_runner --coverage-home <local-venv> --output <dir>` | 261 项通过，7/7 模块 PASS | 0 |
| `python3 -m tests.mutation_runner` | 12/12 killed，PASS | 0 |
| `python3 -m tests.mutation_runner --self-test` | killed/survived/invalid/error 各 1 | 0 |
| `python3 -m tests.e2e_acceptance_runner` | 11/11 场景通过 | 0 |
| `python3 -m tests.eval_compare <3.11> <3.12>` | NOT_COMPARABLE（预期） | 1 |
| `python3 -m compileall -q src tests` | 无输出 | 0 |
| `git diff --check` | 无输出 | 0 |

覆盖率门禁（Python 3.14.6，coverage 7.16.1，纯 Python tracer）：

| 模块 | 行 | 分支 | 阈值（行/分支） |
| --- | --- | --- | --- |
| `artifact_tool.py` | 82.78% | 65.48% | 70 / 60 |
| `conversation_service.py` | 69.47% | 57.14% | 60 / 55 |
| `event_store.py` | 89.21% | 71.54% | 80 / 65 |
| `policy.py` | 81.03% | 75.00% | 70 / 65 |
| `safety_gate.py` | 86.89% | 67.61% | 75 / 60 |
| `service.py` | 89.48% | 77.71% | 80 / 70 |
| `verifier.py` | 80.56% | 75.00% | 70 / 70 |

## 10. 运行产物

```text
artifacts/audits/phase-3.12-safety-gate.json
sha256=55fe34adbf4d341abf875598f7a9bef7ca48834d5147a2100437576edf4702f3

artifacts/audits/phase-3.12-coverage.json
sha256=32ef6554f87d8e71300a1cb69352dd2f4d6576a98f2dd61afb996d0025cdfd29

artifacts/audits/phase-3.12-coverage-gate.json
sha256=2ffd5d920493230c513dc343afbc34e64f614451320ac770a900781ff8e83b4b

artifacts/audits/phase-3.12-coverage-run.json
sha256=2472a44d6bd881441c44b78405c85947bd2f99c7dd30070a9a1ce17f90724b88

artifacts/audits/phase-3.12-mutations.json
sha256=9064d2a0f1b9f5441d08cb69738fd5b140d9fa3488dbad049a16ba1d5e5cda86

artifacts/audits/phase-3.12-mutation-self-test.json
sha256=b6dfc965d7f120270846c57842a8d76267f995c6a3f53784fcf8c948930344c7

artifacts/audits/phase-3.12-e2e.json
sha256=69df5d0eb41019badd57ab0261e317b826e1a654491a007a20b3ed0605d8cecf

artifacts/audits/phase-3.12-compare-3.11.json
sha256=acf88ba009a8e040b59099a093f0ceb5057b6e9a71c4a363cbd24af4939663dd
```

`artifacts/` 由 `.gitignore` 排除，仅本地保留；哈希记录于此以便追溯。

## 11. 限制与风险

- coverage 以纯 Python tracer 运行在 3.14 上，与编译 tracer 口径一致但性能较低；尚未在 CI 之外的其他解释器组合上重复验证。
- 变异集是 12 条手写补丁，不构成完整变异测试；未安装自动变异框架（NOT RUN，未联网安装）。
- `execution_budget_enforced` 证明两层防护中的 claim 层可被单独捕获，但“同时禁用两层”的复合变异未纳入，冗余防护的交叉覆盖仍是已知盲区。
- `effects` 只覆盖 Gate 自己的案例；E2E runner 使用独立的 1.0.0 指标契约，尚未接入同一套来源校验。
- `python3 -m zevorinth.cli` 不会执行 `main()`（模块缺少 `__main__` 守卫），命令行请使用安装后的 `zevorinth` 入口或显式调用 `main()`。
- 真实模型未参与本阶段任何评测。
