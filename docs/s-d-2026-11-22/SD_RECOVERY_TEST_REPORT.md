# S-D 续跑测试报告

**项目**：`/Users/lidequan/PythonProject/Zevorinth`
**日期**：2026-11-22
**解释器**：`./.venv/bin/python`（CPython 3.14.6）
**环境**：macOS / POSIX，全部在临时目录内运行，未写仓库 `data/`
**状态标记约定**：已实现 / 已测试 / 已实际体验 / 仅代码审查 / 未验证 / 当前无法验证

---

## 0. 结论先行

| 项 | 结果 |
| --- | --- |
| 新增续跑测试 | **31 项，全绿** |
| 受影响模块批次（9 文件） | **222 passed / 51 subtests，全绿** |
| 静态检查 `compileall` | 退出码 0 |
| 变异验证（6 个变异体） | **6/6 全部转红**，每个钉住一条不同守卫 |
| 官方证据通道 `_run-tests` | **未通过**（退出码 2，未产出结果文件）。对照实验证明与本阶段改动无关 |
| 全量 `pytest tests/` | **8 failed / 1759 passed / 15 skipped / 9 errors / 567 subtests**，12 分 19 秒 |
| 全量里的 8 红 9 错 | **全部落在本阶段改动面之外**；其中 5 类可由错误文本直接认定为本机环境或测试自身缺陷，3 项未直接归因（见 §5） |

**没有把部分通过写成全量通过。** 官方通道是"未通过"，不是"通过"。

---

## 1. 新增测试清单

文件：`tests/test_failed_step_resume.py`（31 项）

测试分两层，刻意分开：

- **端到端行为测试**：用真实 service 跑，断言工具实际启动次数（跨全部任务统计
  `tool_started` 事件）与磁盘上真实落盘。
- **决策函数分支测试**：直接调判定函数，用手工构造的 `StepResult` 钉住每个分支的
  分类结果。这一层明确标注"验证的是规则分支，不是用户路径"。

| 类 | 数量 | 覆盖指令第八节的哪一条 |
| --- | --- | --- |
| `ResumeDoesNotRedoFinishedWorkTests` | 3 | 1（不重复执行第一步）、9（历史正确） |
| `ResumeCarriesRecordedOutputTests` | 3 | 2（保留上游）、6（上游输出变化下游重查） |
| `SideEffectDecisionTests` | 6 | 3、4、5（六类失败分类） |
| `SideEffectRefusalEndToEndTests` | 2 | 3、4、5（真实工具失败 → 拒绝） |
| `ApprovalAcrossContinuationTests` | 3 | 7（下游审批失效 / 重新审批） |
| `DownstreamDoesNotStartTests` | 2 | 8（前置未成功下游不启动） |
| `RepeatedFailureTests` | 1 | 9（续跑再失败） |
| `RefusalTests` | 5 | 10（取消 / 暂停不被绕过）、版本冲突 |
| `BoundaryTests` | 4 | 11（权限边界不变） |
| `FinalArtifactTests` | 2 | 12（产物重新验证） |

### 1.1 「验证实际执行次数与副作用」是怎么做的

不是查状态字符串，而是四类硬证据：

```python
def _tool_starts(self, tool_name, *tasks) -> int:
    """How many times a tool was actually invoked, across every task."""
```

- **执行次数**：跨所有相关任务（父 + 续跑 + 全部子任务）统计 `tool_started` 事件，
  按 `tool_name` 计数。断言形如 `assertEqual(self._tool_starts("write_text_artifact"), 1)`。
- **副作用**：直接看 `artifacts/<child_task_id>/<name>` 是否存在，以及内容是不是期望值。
- **没被重跑的上游**：父任务产物在续跑前后 `read_bytes()` 相等 **且 `st_mtime_ns` 相等**。
- **拒绝时什么都没发生**：`_task_count()` 前后相等（任务总数不变）。

---

## 2. 变异验证（守卫是否真实）

对每条守卫注入一个缺陷，确认对应测试转红，再恢复。全部在临时副本上做，脚本
`/tmp/mutate_resume.py`，源码恢复后复跑全绿。

| 变异 | 注入方式 | 结果 | 转红的测试（样例） |
| --- | --- | --- | --- |
| M1 副作用分类失效 | 判定函数开头无条件 `return True` | **3 failed / 28 passed** | `test_a_child_that_reached_the_tool_is_not_clean`、`test_a_failed_step_whose_tool_ran_is_refused_not_retried` |
| M2 续跑保留已成功步骤 | `remaining = list(plan.steps)` | **18 failed / 13 passed** | `test_the_step_that_succeeded_is_not_run_again` |
| M3 参数不物化 | 跳过 `resolve_step_parameters` | **17 failed / 14 passed** | `test_upstream_output_is_baked_into_the_resumed_parameters` |
| M4 不裁剪已满足依赖 | `depends_on=list(step.depends_on)` | **17 failed / 14 passed** | `test_dependencies_on_carried_steps_are_dropped` |
| M5 摘要忽略计划身份 | payload 里 `"plan_id": "constant"` | **1 failed / 30 passed** | `test_an_approval_written_for_the_parent_plan_does_not_satisfy_the_step` |
| M6 续跑只入队不执行 | `return continuation` | **14 failed / 17 passed** | `test_the_continuation_has_its_own_plan_identity` |

### 2.1 M5 的一次失败尝试（记录，不隐瞒）

第一次设计的 M5 是"把 `resume_plan.plan_id` 改成父计划 id"，**结果 0 失败**。
排查后发现原因是一个值得记录的事实：**`resume_failed_plan` 里构造的 `Plan` 对象只用于
图校验**——真正持久化的计划是规划器在 `execute_task` 阶段从请求参数重建的，
`plan_id` 在那里重新生成。所以改那个变量等于没改。

改成"摘要 payload 里把 `plan_id` 写死为常量"之后，测试才正确转红（1 failed）。
这说明：**验收一条守卫，必须确认变异真的改到了判定输入上，否则"绿"是假的。**

---

## 3. 受影响模块批次

```bash
PYTHONPATH= .venv/bin/python -m pytest \
  tests/test_failed_step_resume.py tests/test_multi_step_tasks.py \
  tests/test_approval_binding.py tests/test_approval_form.py \
  tests/test_approval_gate_execution.py tests/test_recovery_approval_store.py \
  tests/test_workbench_control.py tests/test_workbench.py \
  tests/test_artifact_continuation.py -q
```

```
222 passed, 51 subtests passed in 71.73s (0:01:11)
```

对照：上一轮（阶段 B）同批次去掉 `test_failed_step_resume.py` 是
`191 passed, 51 subtests passed in 68.97s`。`222 - 191 = 31`，正好等于新增测试数。

`PYTHONPATH=` 前缀是必须的：本机 WorkBuddy 沙箱会注入 `sitecustomize.py`，
导致 `mkdir`/`.locks` 类操作抛 `FileExistsError`（栈顶 `sitecustomize.py:617`）。
清空后复现干净环境。

---

## 4. 官方证据通道：**未通过**，且与本次改动无关

按 README 的正式命令：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH= .venv/bin/python \
  tests/task_control_center_evidence.py _run-tests --root "$PWD" \
  --result /tmp/sd-official-result.json --log /tmp/sd-official-tests.log --cwd-root
```

**结果**：`EXIT=2`，未生成 `/tmp/sd-official-result.json`，最后一行是

```
evidence error: test runner start order/set does not match the fixed collection
```

这个错误来自 `tests/task_control_center_evidence.py:6554`：
`tracker.started_ids != selected_order`，即实际启动序列与固定收集序列不一致。

### 4.1 对照实验（关键）

为了判断是不是我新增的测试文件导致，把 `tests/test_failed_step_resume.py`
临时移出仓库，**用完全相同的命令**再跑一次（12 分 22 秒）：

```
evidence error: test runner start order/set does not match the fixed collection
EXIT=2
```

**同样的错误，同样的退出码，同样没有结果文件。**

结论：官方证据通道在本机/本仓库当前状态下**无论是否有本阶段改动都不能通过**。
这是既有条件（内存/工具链层面），不是本阶段引入的。

### 4.2 因此

- 我不声称"官方测试通道通过"。
- 也不把官方通道的失败算成"本阶段造成的失败"。
- 全量 `pytest tests/` 只作为**补充证据**，不是批准证据。

---

## 5. 全量 `pytest tests/`

```bash
PYTHONPATH= .venv/bin/python -m pytest tests/ -q -p no:randomly
```

```
8 failed, 1759 passed, 15 skipped, 2 warnings, 9 errors, 567 subtests passed in 739.93s (0:12:19)
```

未中断、未超时，跑完了。

### 5.1 逐个归因

| 失败/错误 | 数量 | 原因 | 是否本阶段改动面 |
| --- | --- | --- | --- |
| `test_task_control_center_evidence`（macOS ACL 两条） | 2 failed | `chmod +a` 在本机不可用（`bad mode '+a'`），测试要求 ACL 被拒但环境造不出 ACL | 否 |
| `test_read_confinement_and_lifecycle` | 1 failed | `sandbox-exec: sandbox_apply: Operation not permitted` —— 嵌套沙箱被拒 | 否 |
| `test_task_lifecycle_container_process` | 1 failed | 本机无容器后端 | 否 |
| `test_code_edit_integrity` | 1 failed | `SameFileError: ... are the same file` —— `/var` 与 `/private/var` 路径别名导致"复制到自身" | 否 |
| `test_user_acceptance`（9 个 ERROR） | 9 errors | `StopIteration` / `RuntimeError: generator raised StopIteration` —— 测试自身 helper 在 PEP 479 下的缺陷 | 否 |
| `test_crash_consistency` | 3 failed | `_count(root, "messages") == 2` 期望 1：SIGKILL 后首次写入未回滚，重放多插一条 | **未直接归因**（见 5.2） |

### 5.2 未能直接归因的那 3 项，说明白

`test_crash_consistency` 的失败发生在 `conversation_service` / `event_store` 的
消息持久化路径。**本次会话没有修改这两个文件**（本会话改的是 `service.py`、
`multi_step.py`、`approval_preview.py` 和测试）。测试的 worker 子进程确实到达了
`message_before_commit` 检查点（否则会走 `self.fail("worker ... did not reach its
checkpoint")`），所以不是"进程没起来"，而是"SIGKILL 之后那次插入居然留下了行"。

这更像本机 SQLite 在 3.14 下的 journal/WAL 行为差异，但我**没有证据**这么说，
所以标注为**未验证 / 无法归因到本阶段改动**，列入待查（`SD_PHASE_SUMMARY.md`
的风险项 R-29）。

### 5.3 我没有给出的东西

**改动前的基线失败数。** 本阶段被禁止使用 git（stage/commit/reset 等全部不许可），
所以无法回溯到改动前状态重跑。可替代的证据是：

- 续跑与审批相关批次 222 项全绿，且新增 31 项正好是增量；
- 全量里每一条失败的错误文本都指向解释器/沙箱/容器/测试自身，无一指向
  `service.py` / `multi_step.py` / `approval_preview.py` 的改动逻辑；
- 变异验证证明这些守卫在代码被改坏时会红。

这三条不能等价于"基线对比"，所以我不把它写成基线对比。

### 5.4 与上一轮的对照

上一轮文档 `docs/p0-3-v1-implementation.md` §5.5 记录的口径是
**1300 passed / 410 failed**，失败集中在 `os.supports_dir_fd`（332）与
`sitecustomize` 注入（55）。本次是 **1759 passed / 8 failed**，差异来自两个执行条件：

1. 本次全程用 `PYTHONPATH=`（清掉注入的 `sitecustomize`）；
2. 用 `./.venv/bin/python` 而不是系统解释器。

这也反过来证明上一轮那 410 条里绝大多数确实是环境噪音。

---

## 6. 静态检查与边界核对

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 语法/字节码编译 | `python -m compileall -q src tests` | 退出码 0 |
| 工作区未被写入 | 全部测试用 `tempfile` | 仓库 `data/` 无新增 |
| `SUPPORTED_TASK_OPERATIONS` | 断言在 `test_the_operation_whitelists_are_unchanged` | 8 个，未变 |
| `CONTROL_STARTABLE_OPERATIONS` | 同上 | 7 个，仍排除 `run_tests` 与 `execute_plan` |
| 续跑入口参数 | `inspect.signature` | 只有 `task_id` / 版本 / 会话 / 轮次，**无法注入步骤** |
| git | — | 零 stage、零 commit |

---

## 7. 未执行项（明确列出）

| 项 | 原因 |
| --- | --- |
| 官方证据通道全绿 | 本仓库当前状态下无法通过（§4），不是本阶段能修的 |
| 「审批被撤销」并发时序测试 | 需要同一循环内撤销批准，测试体系无构造手段 |
| 容器后端相关用例 | 本机无容器运行时 |
| 改动前基线对比 | 禁止使用 git |
| 真实用户界面走一遍续跑 | 续跑尚未接入控制面（见 F-09 §6） |
