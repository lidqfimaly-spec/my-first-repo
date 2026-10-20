# Zevorinth Phase 3.13：E2E 证据来源与复合变异审计

## 1. 基线

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `e5b5ba435e09437b017570be44cb3df06a78279d` |
| 父提交 | `22caeb80869ab1212bd5cd301fa3f3a3f10640bb` |
| 分支 | `phase-2.6-response-hardening` |
| Schema 版本 | v4（未变更） |

工作树在开始时除 `.idea/` 未跟踪文件外干净，未做 reset、rebase、checkout 或清理。

## 2. 统一的 Artifact / Evidence 契约

Phase 3.12 的 `effects` 结构与 `validate_case_effects()` 被提升为公共接口
（`build_effect` / `validate_case_effects`），Gate 与 E2E 共用同一实现，没有第二套近似语义。

契约在原有字段上增加 `integrity`，用于表达 E2E 必须覆盖的反例：

| integrity | 含义 |
| --- | --- |
| `verified` | 有产物，文件哈希与证据哈希一致 |
| `mismatch` | 有产物，文件哈希与证据哈希刻意不一致（反例） |
| `unbacked` | 磁盘上有文件，但系统没有登记任何证据（孤立文件） |
| `not_applicable` | 既无产物也无文件 |

`origin` 仍为 `written` / `recovered` / `none`。校验规则：

- `none` 只能配 `unbacked` 或 `not_applicable`，且写入数、证据数必须为 0、路径与哈希必须为空；
- `written` 必须有至少一次 writer 调用与工具调用；
- `recovered` 必须写入数为 0 且证据存在；
- `verified` 要求两个哈希相等，`mismatch` 要求两个哈希不等（声明不匹配但实际相等同样失败）；
- 声明为 `mismatch` 的场景不会被计入“证据完整”。

哈希在记录时从磁盘重新计算（`build_effect` 读取实际文件），不信任报告里的字段。E2E runner 在返回前还会重算
`artifact_count`、`file_write_count`、`evidence_count`、`duplicate_side_effects` 与
`evidence_integrity_rate` 的分子分母，与汇总不一致即抛 `AcceptanceContractError`。

## 3. E2E 案例来源

场景从 11 个增加到 12 个：新增 `E2E-12`，用真实 `ArtifactVerifier` 验证“磁盘文件哈希与已记录证据哈希不一致”。

| 案例 | origin | integrity | 写入 | 证据 |
| --- | --- | --- | --- | --- |
| E2E-1 正常成功 | written | verified | 1 | 1 |
| E2E-2 计划修订 | none | not_applicable | 0 | 0 |
| E2E-3 协作取消 | none | not_applicable | 0 | 0 |
| E2E-4 SIGKILL 后恢复 | recovered | verified | 0 | 1 |
| E2E-5 有文件无证据 | none | unbacked | 0 | 0 |
| E2E-6 验证器否决 | written | verified | 1 | 1 |
| E2E-7 幂等 | written | verified | 1 | 1 |
| E2E-8 预算与取消竞争 | none | not_applicable | 0 | 0 |
| E2E-9 修订与执行竞争 | written | verified | 1 | 1 |
| E2E-10 证据损坏后拒绝 | recovered | verified | 0 | 1 |
| E2E-11 验证器异常 | written | verified | 1 | 1 |
| E2E-12 哈希不匹配 | written | mismatch | 1 | 1 |

汇总：`artifact_count=8`、`file_write_count=6`、`evidence_count=8`、`duplicate_side_effects=0`、
`evidence_integrity_rate=7/8`（分母只含 8 个有产物的案例，排除 4 个无产物案例并注明原因）。

两点值得记录：

- `E2E-10` 的恢复流程会为损坏后的字节重新记录证据，因此证据与文件哈希仍然一致，拒绝来自“内容与计划不符”。
  因此它记为 `recovered/verified`，检测方式写在 `details.detection`。真正的哈希不匹配由 `E2E-12` 覆盖。
- `E2E-3` 在旧契约里被标为 `evidence_complete=True`（无产物时无意义）；新契约改为
  `integrity=not_applicable`，不再污染证据完整率。

## 4. 预算防护的多层变异

定位到两层独立防护：

1. `event_store.claim_task`：claim 时若 `execution_deadline_at` 已过期，直接转 FAILED；
2. `TaskService._run_with_renewal`：拿锁后重新计算剩余预算，`remaining_budget <= 0` 时失败。

为让第二层可被单独命中，新增
`tests/test_execution_budget.py::test_runtime_layer_rejects_budget_expired_after_claim`：
包装 `_claim` 让它在成功领取后把时钟推到截止时间之后，模拟“领取后时钟跳变”，从而只经过运行时检查。

| Mutation ID | 补丁数 | 结果 | 变异行被执行 |
| --- | --- | --- | --- |
| `budget_claim_layer_only` | 1 | killed | 是 |
| `budget_runtime_layer_only` | 1 | killed | 是 |
| `budget_compound_layers` | 2 | killed | 是 |

复合变异确实同时替换了两个文件中的两处守卫（`patch_count=2`），并且两个测试都失败。它没有被第三层
防护掩盖，说明当前只有这两层。续租器内部的 `max_duration_seconds` 是同一预算的传递参数，不构成独立检查。

## 5. 变异 runner 的执行证明

runner 现在会在临时副本中写入一个 trace 入口 `_mutation_probe.py`，用 `sys.settrace` 记录测试实际执行过的
行，再与每个补丁改写的行区间比对，输出 `mutated_lines_executed`。

- 14 条变异全部 `mutated_lines_executed.executed = true`，即变异代码确实被执行，而不是“测试没跑到所以存活”；
- 进程内线程通过 `threading.settrace` 覆盖；子进程（SIGKILL worker 等）不在追踪范围内，已在限制中说明；
- 分类仍要求输出含 `FAILED (`、包含声明的测试 ID、且不含 `_FailedTest`，测试选择失败计为 `error`。

结果：`killed=14 survived=0 invalid=0 error=0`。

## 6. CLI 入口

契约核查结果：

- 打包入口是 console script `zevorinth = zevorinth.cli:main`；
- `src/zevorinth/__main__.py` 提供 `python -m zevorinth`；
- `src/zevorinth/cli.py` 缺少模块级 `__main__` 守卫，`python -m zevorinth.cli` 会静默退出 0 且不执行任何逻辑。

修复：在 `cli.py` 末尾加入

```python
if __name__ == "__main__":
    raise SystemExit(main())
```

该块只在直接执行模块时触发，导入与 console script 路径不受影响（有测试断言导入无副作用）。

验证：

| 检查 | 结果 |
| --- | --- |
| `python -m zevorinth --version` | `0.3.0`，一行，exit 0 |
| `python -m zevorinth.cli --version` | 与上一行完全相同，exit 0 |
| `import zevorinth.cli` | 不打印、不执行主流程 |
| `python -m zevorinth.cli --home <tmp> run ...` | status `succeeded`，execution_mode `real`，产物 `entry.txt` |

## 7. 契约版本与跨版本比较

E2E 指标口径发生实质变化（新增来源指标、证据完整率改为只统计有产物案例），因此
`metric_definition_version` 从 `1.0.0` 升至 `1.1.0`。Gate 契约保持 `1.1.0` 不变。

`tests/eval_compare.py` 增加报告类型识别与 `compare_acceptance_reports`：

| 比较 | 结果 | 退出码 |
| --- | --- | --- |
| 3.12 Gate ↔ 3.13 Gate | `PASS`（同口径，40 案例，无回归） | 0 |
| 3.12 E2E ↔ 3.13 E2E | `NOT_COMPARABLE`（1.0.0 → 1.1.0） | 1 |

`NOT_COMPARABLE` 是预期结果：不允许把旧报告按新口径静默补齐。

## 8. 回归与验证

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| `python3 -m unittest discover -s tests` | 282 项通过，约 13s | 0 |
| `python3 -m tests.e2e_acceptance_runner` | 12/12 场景通过 | 0 |
| `python3 -m tests.mutation_runner` | 14 killed | 0 |
| `python3 -m tests.mutation_runner --self-test` | killed/survived/invalid/error 各 1 | 0 |
| `python3 -m tests.coverage_runner --coverage-home <local-venv>` | 282 项通过，7/7 模块 PASS | 0 |
| `python3 -m compileall -q src tests` | 无输出 | 0 |
| `git diff --check` | 无输出 | 0 |

覆盖率（Python 3.14.6，coverage 7.16.1 纯 Python tracer）：

| 模块 | 行 | 分支 |
| --- | --- | --- |
| `artifact_tool.py` | 82.78% | 65.48% |
| `conversation_service.py` | 69.47% | 57.14% |
| `event_store.py` | 89.21% | 71.54% |
| `policy.py` | 81.03% | 75.00% |
| `safety_gate.py` | 86.42% | 68.59% |
| `service.py` | 89.90% | 78.31% |
| `verifier.py` | 80.56% | 75.00% |

## 9. 运行产物

`artifacts/` 仍由 `.gitignore` 排除，以下文件只在本地保存，未进入 Git 提交。Gate 与 E2E 报告包含耗时与
生成时间字段，因此字节哈希每次运行都会变化；可变字段之外的指标值稳定。

```text
artifacts/audits/phase-3.13-safety-gate.json
sha256=6a1f5b1fe486ce4b3a331be448acab918fcaf986fd7757e2f64d371015950a8e

artifacts/audits/phase-3.13-e2e.json
sha256=96594356fb87ebf71b2b5994e4305f7b7c035bdd22c11a0c1a14c6b2e0d8e8a3

artifacts/audits/phase-3.13-mutations.json
sha256=911607d8bf2b9ec4a1946689ba383c9910b977934c17be5610732b7d4c9d2198

artifacts/audits/phase-3.13-mutation-self-test.json
sha256=b6dfc965d7f120270846c57842a8d76267f995c6a3f53784fcf8c948930344c7

artifacts/audits/phase-3.13-coverage.json
sha256=2eb9cb5dc6d9dce3c806c55c6aa34ad19113f6788201978c86c7e3c7e05737ab

artifacts/audits/phase-3.13-coverage-gate.json
sha256=f14318929cfe415c016857758867417a2e403ebaafeb90945e2e0392d185dc78

artifacts/audits/phase-3.13-coverage-run.json
sha256=436a31b31eb921da3f5f2c31cd262056f518c1d7f239d9199c491cf03e0cd5ab

artifacts/audits/phase-3.13-compare-gate.json
sha256=b2b52d5d1f6d285dd84102a2f45a5b221f237eda50d0b2333082046060dac2e4

artifacts/audits/phase-3.13-compare-e2e.json
sha256=e88107bfe40aba4667b908cdc181fc4a960dc6694954aabaf33fa91da01b664d
```

## 10. 限制与未解决事项

- 变异追踪只覆盖当前进程与线程；SIGKILL worker 等子进程中的执行不在 trace 范围内，其“被执行”证据来自测试失败本身。
- 变异集仍是 14 条手写补丁，未安装自动变异框架（NOT RUN，未联网安装）。
- E2E 的 `evidence_integrity_rate` 只衡量“文件与证据是否一致”，不衡量证据内容能否证明任务目标；那由独立 Verifier 负责。
- `E2E-12` 在 verifier 层构造哈希不匹配，未经过完整任务生命周期，因为现有 API 没有在发布前替换文件内容的钩子。
- Gate 与 E2E 报告含耗时字段，字节哈希不可复现；长期应把可变字段与指标字段分离存储。
- 覆盖率仍只在一个 Python/coverage 组合上验证。
- 真实模型未参与本阶段任何评测。
