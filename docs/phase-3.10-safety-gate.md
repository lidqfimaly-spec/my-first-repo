# Zevorinth Phase 3.10：离线模型评测与安全回归门禁

状态：**PASS WITH LIMITATIONS**

本阶段没有真实模型 API 请求、没有读取 `.env`、没有联网安装依赖、没有推送远程。

## 1. 基线

- 基线：`8c06d207a89b81a69457a44e0e6922bbea9ca06f`
- 分支：`phase-2.6-response-hardening`
- Schema：v4。

## 2. 现状与差距

已有：

- 28 个脚本化 LLM planner 案例；
- Candidate Validator、Policy、TaskService、Verifier、恢复和 E2E 测试；
- 离线 `eval-planner`；
- 离线 wheel 安装检查。

缺口：

- 评测器没有统一输出 Git、Python、fake client、失败分类、副作用计数和耗时；
- planner 案例与本地策略/取消/预算/恢复/验证案例没有同一版本化安全门禁；
- 没有对无效评测数据集和“不一致结果仍 PASS”进行 fail-closed 检查。

## 3. 版本化安全门禁

新增：

- `src/zevorinth/evals/safety_gate_v1.json`
- `src/zevorinth/safety_gate.py`
- CLI `eval-safety-gate --offline --output ...`

总案例：39

- 28 个既有离线 planner 案例；
- 11 个本地边界案例。

本地案例类别：

- policy path traversal；
- clarification；
- status query；
- cooperative cancellation；
- execution budget；
- message idempotency；
- duplicate execution；
- recovery readback；
- evidence attempt mismatch；
- verifier exception；
- execution plan snapshot mismatch。

责任层在数据集中显式标注为 Planner/CandidateValidator/Policy、ConversationService、
TaskService、ArtifactVerifier 等，不把所有判断错误地归因于模型。

## 4. 数据集校验

Gate 在任何执行前 fail closed：

- 数据集必须包含非空 cases 和 required_categories；
- case ID 唯一；
- 必须包含 id/category/kind/component/input/expected/forbidden_actions/design_reason；
- kind 必须有已注册本地 handler；
- planner dataset version 必须匹配；
- required categories 缺失时 FAIL。

无效数据集不会输出 PASS。

## 5. 报告

机器报告：

```text
artifacts/audits/phase-3.10-safety-gate-v3.json
SHA-256=e501170040b7fcbed126df1e887361e93b1ed6c5b4e5b1901670dd31d3adef03
```

报告包含：

- gate/evaluation/dataset 版本；
- Git HEAD；
- Python 版本和 SDK 状态；
- fake client 标识：`ScriptedPlannerClient`；
- 每个案例输入、预期、实际、责任层、禁止动作、耗时；
- classification/status/candidate validation；
- normal plan 与 safety block 分离统计；
- tool/artifact/duplicate/tool side effects；
- timeout、skipped、unexecuted；
- failure category counts；
- total 和 per-case duration；
- final PASS/FAIL 和 failure reasons；
- real_api_calls=0、network_request_sent=false。

## 6. 指标定义

| 指标 | 分母 |
| --- | --- |
| classification_accuracy | 全部 planner 案例 |
| status_accuracy | 所有定义了 expected.status 的案例 |
| candidate_validation_pass_rate | 实际收到候选的 planner 案例 |
| normal_plan_success_rate | expected status=succeeded 的 planner 案例 |
| safety_block_pass_rate | expected blocked/planning_failed 的案例 |
| duplicate_side_effects | 所有 idempotency 本地案例；目标必须为 0 |
| tool_call_count | planner tool-executed 案例 + 本地 tool_started 次数 |
| artifact_side_effect_count | 实际产生 artifact/evidence 的案例数，不等于成功数 |
| unexecuted_count | execution_mode=not_executed 的案例 |

模拟、澄清、阻断和未执行从不计作真实执行成功。

## 7. 实际结果

```text
status=PASS
case_count=39
planner_case_count=28
local_case_count=11
classification_accuracy=1.0
status_accuracy=1.0
candidate_validation_pass_rate=0.6666666666666666
normal_plan_success_rate=1.0
safety_block_pass_rate=1.0
tool_call_count=10
artifact_side_effect_count=11
duplicate_side_effects=0
timeout_count=1
skipped_count=0
unexecuted_count=22
failure_counts={}
total_duration_ms=693.4492499567568
```

`candidate_validation_pass_rate=0.666666...` 的 1.0 分母只包含实际收到候选的案例，
拒答/不完整/超时等没有候选的案例不会虚增通过率。

## 8. 安全回归

Gate 测试覆盖：

- 有效 gate PASS；
- 缺失字段、重复 ID、空数据集、未知 kind 均配置失败；
- 不一致 planner 报告导致 FAIL；
- 报告路径存在时拒绝覆盖。

定向隔离副本变异：

- 绕过 artifact name/path 校验 → gate FAIL；
- 忽略 claim 取消 → gate FAIL；
- 忽略 evidence attempt 匹配 → gate FAIL。

三项均被捕获。自动 mutation framework 仍未建立。

## 9. 回归与打包

| 项目 | 结果 |
| --- | --- |
| 系统完整 unittest | 223/223，exit 0 |
| 隔离环境完整 unittest | 223/223，exit 0 |
| safety gate 重复 | 20/20 轮 0 失败 |
| E2E acceptance | 10/10 轮 0 失败 |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增 safety gate JSON 已进入 wheel/sdist。Phase 3.10 重新构建的干净 Python 3.14
安装检查通过：

```text
wheel sha256=1fd47290f7947986c84d6da4d2e9308f4c1fdbd4b97567eb73789b512099eb20
sdist sha256=821866108123d828489568d411e974533206a1ee4ebdfb85c7fe18aaa649c4cc
release-check sha256=315084d881c990f0dd09cc81e95bf683587e258eed26af4ddc038c7335693a66
```

## 10. 未执行与风险

- coverage：NOT RUN，环境未安装。
- 自动 mutation framework：NOT RUN。
- Gate 使用 scripted/deterministic fake client，不代表真实模型质量。
- 真实模型行为仍只有 Phase 3.9 两个固定案例证据。
- 多机、共享文件系统和网络文件系统未验证。
