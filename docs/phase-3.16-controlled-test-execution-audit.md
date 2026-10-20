# Zevorinth Phase 3.16：受控本地测试执行审计

## 1. 基线与提交

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `2f84b55ddfbd1861b2a479961f3e1cc9b6a847dc` |
| 分支 | `phase-2.6-response-hardening` |
| Schema 版本 | v4（未变更） |

开始时工作树除 `.idea/` 外干净，未执行 reset/rebase/checkout/clean。

## 2. 设计与实现边界

只新增一个工具 `run_tests`，参数全部结构化，不接受命令字符串：

| 字段 | 规则 |
| --- | --- |
| `runner` | 只允许 `unittest`（`SUPPORTED_RUNNERS`） |
| `target` | 点分标识符 `^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$` |
| `workspace` | `^[A-Za-z0-9_-]{1,64}$`，解析到 `<project_root>/workspaces/<name>` |
| `timeout_seconds` | 1–60 |
| `max_output_bytes` | 256–65536 |

可执行文件固定为当前解释器 `sys.executable`，参数固定为 `-m unittest -q <target>`；调用方不能指定解释器、参数、环境变量或可执行路径。

实现文件：

- `src/zevorinth/local_test_runner.py`：参数校验、工作区解析、子进程、有界读取、超时/取消终止；
- `src/zevorinth/policy.py`：`_evaluate_tests` 在执行前完成全部校验；
- `src/zevorinth/planner.py`：`build_test_plan` / `plan_request` 分支；
- `src/zevorinth/service.py`：`_execute_test_tool` / `_verify_test_and_finish`；
- `src/zevorinth/verifier.py`：`TestResultVerifier`；
- `src/zevorinth/models.py`：`TestExecutionEvidence`（挂在既有 `ToolResult.test_evidence` 上，未新建平行 Task/Event）；
- `src/zevorinth/cli.py`：`zevorinth test --workspace --target`。

## 3. 调用链

```
CLI test / TaskService.submit
  → DeterministicPlanner.plan_request      （只复制结构化字段）
  → ArtifactPolicy.evaluate                （校验 runner/target/workspace/timeout/output）
  → TaskService._execute_test_tool
       ├─ 取消检查点 before_tool
       ├─ TOOL_STARTED 事件（runner/target/workspace/attempt/generation）
       ├─ local_test_runner.run_unittest
       │    ├─ 固定 argv：sys.executable -m unittest -q <target>
       │    ├─ cwd = workspaces/<name>，最小环境变量
       │    ├─ start_new_session=True（进程组）
       │    ├─ 两个线程按 4 KiB 读取，内存上限 = max_output_bytes
       │    └─ 超时/取消 → killpg + wait 确认
       ├─ TOOL_SUCCEEDED（含 TestExecutionEvidence）
       └─ VERIFYING → TestResultVerifier → SUCCEEDED / FAILED / CANCELLED
```

恢复与重复执行沿用既有 Lease/claim generation 机制，没有新增状态机状态。

## 4. 状态映射

| 结果 | Task 状态 | 错误码 / 原因 |
| --- | --- | --- |
| 测试通过 | SUCCEEDED | verifier `tests_passed` |
| 测试失败（退出码非 0） | FAILED | `tests_failed` |
| 超时且已确认终止 | FAILED | `test_timeout` |
| 超时但无法确认终止 | FAILED | `test_execution_failed`（verifier：`test_timeout_unconfirmed`） |
| 执行中取消 | CANCELLED | `cancelled`（证据 `cancelled=true`） |
| 策略拒绝 | BLOCKED | `unsupported_runner` / `invalid_test_target` / `unsafe_workspace` 等 |
| 输出截断 | FAILED | `test_execution_failed`（verifier：`test_output_truncated`） |
| 未启动 | BLOCKED / CANCELLED | 无 `tool_started` 事件 |

## 5. 场景矩阵

### 正向

| 场景 | 结果 |
| --- | --- |
| 通过目标 → 真实退出码 0、verifier 通过、单次 `task_succeeded` | PASS |
| 失败目标 → FAILED `tests_failed`，无成功事件 | PASS |
| 输出在限制内 → 未截断、摘要与哈希齐全 | PASS |
| 输出超限（50 KB > 256 B）→ 截断、FAILED，内存有界 | PASS |
| 真实 CLI `test` → exit 0、证据完整 | PASS |

### 安全拒绝

| 场景 | 拒绝码 | 是否启动进程 |
| --- | --- | --- |
| 未知 runner `bash` | `unsupported_runner` | 否 |
| 未知参数 `command` | `unsupported_parameter` | 否 |
| `test_demo; rm -rf /`、`&&`、`\|`、`$(whoami)`、`-m http.server` | `invalid_test_target` | 否 |
| `/etc/passwd`、`../outside`、`a/b` | `invalid_test_target` | 否 |
| `../etc`、`/tmp`、`a/b`、`..`、空 workspace | `invalid_workspace` | 否 |
| 符号链接工作区 | `unsafe_workspace` | 否 |
| 符号链接目标模块 | `unsafe_test_target` | 否 |
| 不存在的 workspace / target | `workspace_missing` / `test_target_missing` | 否 |
| timeout 0 / 120、output 10 / 131072 | `invalid_timeout` / `invalid_output_limit` | 否 |
| 计划含 `shell` 工具 | `unsupported_tool` | 否 |

所有拒绝场景都断言 `tool_started` 计数为 0。

### 超时 / 取消 / 证据

| 场景 | 结果 |
| --- | --- |
| 超时 1s（测试 sleep 30s） | FAILED `test_timeout`、`timed_out=true`、`exit_code=null`、`terminated_confirmed=true` |
| 执行前取消 | CANCELLED，无 `tool_started` |
| 执行中取消 | CANCELLED、`cancelled=true`、进程组已终止、verifier `test_cancelled` |
| 旧 generation 证据 | verifier `test_evidence_mismatch` |
| 错误 attempt / 参数漂移 | verifier `test_evidence_mismatch` |
| 退出码 0 但输出截断 | 不通过 |
| 缺失证据 | `test_evidence_missing` |

## 6. 指标与交叉校验

`tests/execution_audit_runner.py` 运行 9 个场景并独立复算：

| 指标 | 值 |
| --- | --- |
| requested / started / completed | 9 / 6 / 4 |
| passed / failed / blocked | 2 / 3 / 2 |
| cancelled（执行中）/ cancelled_before_start | 1 / 1 |
| timed_out | 1 |
| not_executed | 1 |
| duplicate_side_effects | 0 |
| positive_verifier_pass_rate | 1.0（2/2） |
| verifier_agreement_rate | 1.0（6/6） |
| timeout_confirmation_rate | 1.0（1/1） |
| cancel_confirmation_rate | 1.0（1/1） |

门禁内置的不一致检查（任一触发即抛错）：

- `succeeded` 但 verifier 未通过；
- `succeeded` 但退出码非 0；
- 超时却保留了退出码；
- `blocked` 却启动了进程；
- 重复启动（`runner_invoked > 1`）；
- 重复场景 ID。

负向场景（失败、超时、截断）不计入 `positive_verifier_pass_rate`，而是计入 `verifier_agreement_rate`。

## 7. 测试与回归

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| `python3 -m unittest discover -s tests` | 352 项通过，约 26s | 0 |
| 新增 `tests/test_local_test_execution.py` | 26 项通过 | 0 |
| `tests/execution_audit_runner.py` | 9/9 场景符合预期 | 0 |
| 稳定结果双次运行 | `0c5b6ba6…` 一致 | 0 |
| `tests/user_acceptance_runner.py`（16 场景） | PASS | 0 |
| `tests/e2e_acceptance_runner.py` | 12/12 | 0 |
| `tests/mutation_runner.py` | 17 killed / 0 survived / 0 invalid / 0 error | 0 |
| 覆盖率门禁 | 352 项通过，8/8 模块 PASS | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

覆盖率（Python 3.14.6）：

| 模块 | 行 | 分支 |
| --- | --- | --- |
| `local_test_runner.py` | 93.13% | 92.11% |
| `policy.py` | 82.35% | 73.91% |
| `service.py` | 89.43% | 78.74% |
| `verifier.py` | 87.93% | 90.91% |
| 其余模块 | 与 3.14 相当 | — |

新增变异（3 条，均被捕获且证明改写行被执行）：

| Mutation | 性质 |
| --- | --- |
| `test_target_validation` | 目标正则被移除后 shell 语法可通过 |
| `test_evidence_generation` | verifier 结构校验被短路 |
| `test_workspace_symlink` | 复合变异：两处工作区越界防护同时移除 |

`test_workspace_symlink` 最初只变异 `is_symlink()` 时存活——第二道 `resolved != root/workspace` 检查仍然拦截，属“冗余防护掩盖”。按 3.13 的做法改为复合变异后被杀。

## 8. 稳定报告与产物

`artifacts/audits/phase-3.16/`（被 `.gitignore` 排除，未进入 Git）：

| 文件 | SHA-256 |
| --- | --- |
| `execution-stable/stable.json` | `0c5b6ba6cc1cf72e0c02a86ea0190b975d6e831437db93c870def8a9a197ec86` |
| `execution-stable/metadata.json` | `d6b885fa77c68df216b1853d948843f898df5fea5e0ff73a78ccf7868c79bb17` |
| `execution-summary.json` | `c08b5940d863cb9fa4e6ee2f1cbb49b2a6f39609fb22cddfaac596fc2f2bfe06` |
| `mutations.json` | `fcf2a17534c7dfcc4f6f40f263cf565fb13de9130f3326bd7eb31a7acd737a55` |
| `e2e.json` | `7850c9c9616a29bc6accffd6ad4e0704def23ae3d5cde230f2a8d709b06cf2a7` |
| `user-acceptance/stable.json` | `8914e936beebbcfeab45132cd2c67e1f859eddd082d018e051b2f730577ed3f3` |
| `coverage/coverage-gate.json` | `0002d664d8aa01040db22ccb3a2e8ca39eab9c842298c41a5dc21cc2040e19ad` |

## 9. 安全边界

- 真实模型 API 请求：0；
- 未联网、未安装依赖；
- 未读取 `.env`；子进程环境变量为白名单，不含任何凭据；
- `.idea/` 未修改、未暂存；
- 所有测试在 `tempfile.TemporaryDirectory()` 内执行；
- 危险拒绝场景只触发拒绝路径，未执行任何真实危险命令。

## 10. 限制与未解决风险

- **不是操作系统级沙箱。** 子进程仍以当前用户身份运行，可读取该用户可访问的文件；限制来自固定 argv、工作区约束与资源上限，而非内核隔离。
- 超时/取消依赖 `killpg(SIGKILL)` 与 `wait`；若平台不允许进程组操作，会记录 `terminated_confirmed=false` 并使验证失败，但未在其他操作系统上验证。
- 只支持 `unittest` 与点分目标；`pytest`、参数化过滤、覆盖率参数均不支持。
- 测试可能修改工作区文件，当前只记录风险，未做写前/写后目录快照比对（任务书 §9.12 的“识别非预期副作用”尚未实现）。
- 依赖缺失时表现为 unittest 导入错误（退出码非 0）→ FAILED，而不是独立的 `not_executed` 细分。
- 覆盖率仍只在一个 Python/coverage 组合上验证。
