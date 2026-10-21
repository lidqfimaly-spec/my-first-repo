# Zevorinth Phase 3.84：Intent Fidelity Enforcement Integration

## 1. 结论

**PASS（离线强制集成）。**

Intent fidelity 已从审计字段升级为正式 LLM planning 门禁。`LLMPlanner.plan_request()` 在
validator 返回后、计划进入 TaskService 接受与执行之前执行独立检查。

本阶段没有调用真实模型、联网、提交任务、启动工具或容器。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `041eacaec403b980d3ff0cb2974610c92fd7e07a` |
| 真实模型 / 网络 | 0 / 0 |
| 新合同 | `planner-intent-fidelity-v2` |
| 核心失败码 | `intent_fidelity_failed` |
| checker failure code | `intent_fidelity_check_error` |

## 2. 门禁语义

### planned + fidelity pass

保持 `planned`，并在 metadata 中记录：

```text
intent_fidelity_enforced=true
intent_fidelity_gate=passed
intent_fidelity=<assessment>
```

### planned + lossy_plan

不返回 executable plan，转为：

```text
classification=failed
error_code=intent_fidelity_failed
intent_fidelity_gate=blocked
plan=null
```

TaskService 因此进入 `planning_failed`，不会产生 `task_planned`、`tool_started` 或执行结果。

### non-plan outcome + raw Planner fidelity failure

若 validator 已把危险 candidate 拒绝为 unsupported/failed/clarification，则保留原来的
non-execution terminal outcome，同时记录：

```text
intent_fidelity_gate=terminal_non_execution
intent_fidelity.fidelity_pass=false
intent_fidelity.downstream_blocked=true
```

这样下游拦截成功不会抹去 Planner 的意图保真失败。

### checker error / inconsistent assessment

检查器异常、字段缺失、状态冲突或错误声明执行尝试时，统一返回：

```text
error_code=intent_fidelity_check_error
intent_fidelity_gate=blocked
```

不默认通过。

## 3. Contract v2

Phase 3.83 的 v1 保持原样，未回溯修改。

Phase 3.84 新增 v2，原因是强制集成时发现：

- 合法 unittest target 不只形如 `tests.*`；
- Unicode 文件名也需要被识别；
- 英文 `content` 需要被识别为内容声明。

v2 只在新报告中启用。v1 的历史证据和 SHA 保持有效。

## 4. 核心回归：run_tests_shell_target

使用 Phase 3.82 保存的原始 candidate 做离线 replay，不调用模型。

结果：

```text
task_status=planning_failed
task_error_code=intent_fidelity_failed
plan_created=false
gate_result=blocked
no_task_planned_event=true
no_tool_started_event=true
execution_attempted=false
reason=unsafe_goal_component_omitted
```

规划失真现在被阻止在计划接受边界，而不是只停留在审计报告中。

## 5. 状态与审计一致性

测试覆盖：

- 忠实计划继续执行；
- lossy executable plan 转为 planning failure；
- TaskService 没有 `task_planned` 与 tool events；
- 下游已拒绝的危险 candidate 仍保留 fidelity failure metadata；
- checker 异常 fail-closed；
- checker 返回不一致字段 fail-closed；
- executed 字段不能由 checker 伪造为 true；
- 核心 shell target 回归不产生 plan；
- 旧策略/执行测试在显式 goal 或 trusted context 下继续工作。

## 6. 验证结果

| 检查 | 结果 |
| --- | --- |
| Intent enforcement tests | 6/6 |
| Intent contract tests | 13/13 |
| 相关定向测试 | 90/90 |
| 系统 Python 全量 unittest | 828/828，exit 0 |
| `.venv` 全量 unittest | 828/828，exit 0 |
| Mutation runner | **202 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 202/202 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

审计产物：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.84/offline-quality-report.json` | `33d3afc05a8d310e0d7cc2c2ce8c49f4a67608c38f59835eaf617ebbc248592e` |
| `artifacts/audits/phase-3.84/core-regression.json` | `4997450bfd944f7c78c7d5b8bf845817263a9710edd223c3f9ca675722ef9efc` |
| `artifacts/audits/phase-3.84/enforcement-summary.json` | `3ade7421cbfcfa63382d59f9ddad688c7d5b543ebb782ff90107ce3e314fd0dd` |
| `artifacts/audits/phase-3.84/mutations.json` | `313d39357a330d664d56484950fe10e4d6905269f613e02ac8b503a918aee7a9` |

Intent contract v2 SHA-256：

```text
98ec443f533dc3a16e11f8e79cf0ec34a3d74af871b2a37bb08abe4a8f3bd1e0
```

Phase 3.83 的 v1 SHA-256 保持：

```text
9a83cc21c029ef0a6f992c234f081252e06b717949921b35e2e09c4d9bf4d97b
```

## 7. 限制

- v2 仍是确定性模式检查，不是通用自然语言 entailment；
- 不能认证 goal 或 trusted context 来源；
- 忠实计划不等于执行已获授权；
- 本次只验证 LLMPlanner 正式路径，不把 deterministic planner 纳入同一文本检查；
- 未执行第三次 live run；
- 下游成功/失败均不能抵消 Planner fidelity failure。

## 8. 阶段判定

**PASS：意图保真检查已接入正式计划接受门禁，并完成失败关闭验证。**

下一次 live run 应同时报告 applicability strict、intent fidelity、危险 candidate、被接受危险
计划和执行尝试，且各项不得合并为单一分数。
