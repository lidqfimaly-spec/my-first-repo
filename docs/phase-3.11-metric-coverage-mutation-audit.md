# Zevorinth Phase 3.11：指标、覆盖率与自动化变异审计

状态：**PASS WITH LIMITATIONS**

本阶段没有真实模型 API 请求、没有读取 `.env`、没有联网安装依赖、没有推送远程。

## 1. 基线

- 基线：`8f7f21aff69becc1f9279bc35e9c401b81a4fb7d`
- 分支：`phase-2.6-response-hardening`
- Schema：v4。

## 2. 指标契约

新增 `metric_definition_version=1.0.0`。

每个指标均输出：

- value；
- numerator；
- denominator；
- excluded_count；
- excluded_reason_counts；
- contributing_case_ids；
- metric_definition_version；
- definition；
- rate/count 类型。

安全 gate 共 17 个指标。关键实际值：

| 指标 | value | 分子/分母 | 排除 |
| --- | ---: | ---: | ---: |
| classification_accuracy | 1.0 | 28/28 | 12 |
| status_accuracy | 1.0 | 37/37 | 3 |
| candidate_validation_pass_rate | 0.6666666666666666 | 14/21 | 19 |
| normal_plan_success_rate | 1.0 | 6/6 | 34 |
| safety_block_pass_rate | 1.0 | 19/19 | 21 |
| tool_call_count | 10 | 10/40 observed cases | 30 |
| file_write_count | 10 | 10/40 | 30 |
| artifact_count | 11 | 11/40 | 29 |
| evidence_count | 11 | 11/40 | 29 |
| duplicate_side_effects | 0 | 0/40 | 40 |
| simulated_count | 0 | 0/40 | 40 |
| clarification_count | 6 | 6/40 | 34 |
| blocked_count | 10 | 10/40 | 30 |
| failed_count | 12 | 12/40 | 28 |
| timeout_count | 1 | 1/40 | 39 |
| skipped_count | 0 | 0/40 | 40 |
| unexecuted_count | 22 | 22/40 | 18 |

计数的 denominator 表示观察案例范围，不表示 count/denominator 的比率。

## 3. 指标定义

- 分类正确率：planner 观察分类等于预期 / 全部 planner 案例。
- 状态正确率：观察状态等于预期 / 声明 expected status 的案例。
- 候选验证通过率：candidate_valid=true / 实际收到候选的案例。
- 正常计划成功率：通过 / expected status=succeeded 的 planner 案例。
- 安全阻断正确率：通过 / expected blocked 或 planning_failed 的案例。
- 工具调用：planner tool-executed 案例与本地 tool_started 次数。
- 文件写入：planner artifact 成功写入与本地真实 writer 调用数。
- Artifact：存在 artifact evidence 或实际 artifact 的案例。
- Evidence：存在持久化 artifact/evidence 的案例。
- 重复副作用：幂等案例中超出一次的工具/文件副作用。
- 模拟、澄清、阻断、失败、超时、跳过和未执行均单独计数。

Artifact 数可以大于文件写入数：恢复案例只读取已有 artifact，不再次写文件。
报告保留逐案例明细，禁止仅凭聚合差推断错误。

## 4. 指标一致性门禁

报告验证会拒绝：

- 重复 case ID；
- case_count 与案例数组不一致；
- 未知任务状态；
- rate 分母为 0；
- rate 分子大于分母；
- metric value 与分子/分母不一致；
- summary 与 metric audit 不一致；
- metric 引用不存在的案例；
- evidence 数大于 artifact 数；
- 负 count。

新增 corrupted-report 测试，篡改 numerator 后 `validate_gate_report()` 抛错。

## 5. 覆盖率

使用本机已有 Python 3.12 环境中的 coverage 7.16.1；无网络安装。

命令：

```bash
coverage erase
coverage run --branch --source=src/zevorinth -m unittest discover -s tests
coverage json -o artifacts/audits/phase-3.11-coverage-final.json
python3 tests/coverage_gate.py \
  --coverage-json artifacts/audits/phase-3.11-coverage-final.json \
  --output artifacts/audits/phase-3.11-coverage-gate-final.json
```

229 个测试通过，覆盖率门禁 PASS。

| 模块 | 行覆盖 | 分支覆盖 | 门禁 |
| --- | ---: | ---: | --- |
| artifact_tool.py | 82.78% | 65.48% | PASS |
| conversation_service.py | 69.47% | 57.14% | PASS |
| event_store.py | 89.10% | 71.14% | PASS |
| policy.py | 81.03% | 75.00% | PASS |
| safety_gate.py | 88.69% | 63.41% | PASS |
| service.py | 89.48% | 77.71% | PASS |
| verifier.py | 80.56% | 75.00% | PASS |

## 6. 自动化变异

Runner：`tests/mutation_runner.py`

Runner 自身四类结果：

```text
killed=1 survived=1 invalid=1 error=1
```

正式 mutation：9/9 killed，0 survived，0 invalid，0 error。

- artifact path validation；
- lease token fencing；
- cooperative cancellation；
- evidence attempt match；
- unsupported tool allowlist；
- verifier failure cannot become success；
- duplicate file side effect / overwrite protection；
- invalid candidate capability；
- status query classification。

每次 mutation 在临时源码副本中执行，原工作树不保留变异。测试退出 0 记为 survived；
退出 1 记为 killed；补丁目标缺失或编译失败记为 invalid；其他执行错误记为 error。

## 7. E2E 指标

E2E runner 也增加 metric audit：

- success_rate；
- executable_success_rate；
- recovery_success_rate；
- duplicate_side_effects；
- cancellation_handled；
- evidence_complete_rate。

11/11 场景通过：

```text
duplicate_side_effects=0
cancellation_handled=true
executable_success_rate=1.0
recovery_success_rate=1.0
```

## 8. 回归和打包

| 项目 | 结果 |
| --- | --- |
| 系统完整 unittest | 229/229，exit 0 |
| 隔离环境完整 unittest | 229/229，exit 0 |
| E2E acceptance | 连续 10 轮 0 失败 |
| 指标/覆盖率/变异专项 | 连续 10 轮 0 失败 |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |
| 干净 Python 3.14 wheel 安装 | PASS |

最终打包：

```text
wheel sha256=04e7cc040a18a60b672882988a494977d96cd652065fd5529069d01baf3d6e1b
sdist sha256=c7ef1e49232c3b3f6781209f2974bd0146f8f749c663456d6b8fce3fbbee589e
release-check sha256=50c0484f28adf87661014fa7f26fa3db450f84e0b7577f90a9031b1c1c7c664c
```

## 9. 运行产物

```text
artifacts/audits/phase-3.11-safety-gate-final.json
sha256=acc0b58bdf669de63fddd28149c6a96841ac3738ceeb0fd05caf342acb8c2a40

artifacts/audits/phase-3.11-coverage-final.json
sha256=33c840857c98d30fe5a6e5b0655aa8ba799644e4562fd74f3ca70ca51a5140a6

artifacts/audits/phase-3.11-coverage-gate-final.json
sha256=f37c382b35f2cacfdce5ea71b1ebd4d4c1474dc9748dac5224dc642e05a8d0d9

artifacts/audits/phase-3.11-mutations.json
sha256=91f5566743b9c34b3f03e5f74f6324c245af4eb57e6c520678a4809e02099b25

artifacts/audits/phase-3.11-mutation-self-test.json
sha256=b6dfc965d7f120270846c57842a8d76267f995c6a3f53784fcf8c948930344c7

artifacts/audits/phase-3.11-e2e.json
sha256=7e020c1d2ec9657d7070c828e298d427410391b3ff0f1a1acdda5b66f5a4359c
```

## 10. 限制与风险

- Coverage 通过 Python 3.12 工具运行；主测试同时验证 Python 3.14。
- Mutation runner 是固定补丁集合，不是完整 mutation framework。
- 真实模型行为未在本阶段重新测试。
- 多机、共享文件系统和网络文件系统仍未验证。
- Gate 的 ScriptedPlannerClient 不代表模型质量。
