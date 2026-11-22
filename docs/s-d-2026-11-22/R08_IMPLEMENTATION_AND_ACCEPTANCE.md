# R-08 实施与验收：审批绑定到具体步骤内容

**项目**：`/Users/lidequan/PythonProject/Zevorinth`
**日期**：2026-11-22
**范围**：S-D 阶段 B（审批闭环）+ 阶段 C 中与审批相关的部分
**状态标记约定**：已实现 / 已测试 / 已实际体验 / 仅代码审查 / 未验证 / 当前无法验证

---

## 1. 修复前的事实（阶段 A 只读检查结论）

阶段 A 的九个检查点，逐条给出代码事实，而不是结论。

| 检查点 | 修复前的实际状态 | 证据位置 |
| --- | --- | --- |
| 审批记录如何关联计划 / 步骤 / 内容 | 只关联 `step_id`，**不关联内容，也不关联计划** | `Task.approved_step_ids`（`list[str]`） |
| 摘要覆盖哪些字段 | 无摘要。批准就是「这个 id 被批准了」 | — |
| 参数规范化是否稳定 | 无规范化，因为没有摘要 | — |
| 工具身份与完整参数是否纳入绑定 | **没有** | — |
| 执行期是否重算摘要 | **不重算**，只查 id 是否在列表里 | `_execute_multi_step_plan` |
| 校验是否在副作用之前 | 是，位置正确（在 `_execute_multi_step_child` 之前） | 同上 |
| 重规划后旧审批是否失效 | **会失效**，但只因为重规划显式清空 `approved_step_ids`，不是因为内容比对 | `replan_pending_steps` |
| 步骤替换 / 参数变化 / 工具变化 / 计划版本变化是否可复用旧审批 | **可以复用**。只要 id 相同，内容随便换 | 同上 |
| 恢复执行时是否重新检查审批有效性 | 检查，但检查的是同一个弱谓词 | `resume()` → `_execute_multi_step_plan` |

一句话概括修复前的风险：**审批是"同意叫 s2 的那件事"，不是"同意 s2 现在要做的这件事"。**

## 2. 改了什麼

### 2.1 摘要

`src/zevorinth/multi_step.py`：

```python
APPROVAL_DIGEST_VERSION = "step-approval-v2"

def step_approval_digest(step: PlanStep, plan_id: str) -> str:
    payload = {
        "version": APPROVAL_DIGEST_VERSION,
        "plan_id": plan_id,
        "step_id": step.step_id,
        "tool_name": step.tool_name,
        "parameters": step.parameters,
        "expected_result": step.expected_result,
        "depends_on": sorted(step.depends_on),
        "max_attempts": step.max_attempts,
        "retry_on_not_executed": step.retry_on_not_executed,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

规范化方式：`sort_keys=True` 让字段顺序无关，`separators=(",", ":")` 去掉多余空白，
`ensure_ascii=False` 让中文按原字符参与哈希（避免 `\uXXXX` 转义带来的等价性歧义）。

**为什么不含 `plan_version`**：重规划会 bump 版本但保留未变步骤。绑版本会导致
"用户已经看过且内容没变"的步骤被再问一次，而安全上毫无增加。内容绑定是更强的承诺，
所以这里绑内容 + 计划身份，不绑版本。这个取舍写在函数 docstring 里。

**为什么含 `plan_id`**：否则一条批准可以被搬到另一个内容完全相同的计划里。加了之后，
"批准"这句话的主语变成"这个步骤，在这个计划里"。

### 2.2 谓词

```python
def step_is_approved(step: PlanStep, task) -> bool:
    if task.plan is None:
        return False
    if step.step_id not in task.approved_step_ids:
        return False
    recorded = (task.approved_step_digests or {}).get(step.step_id)
    if not isinstance(recorded, str) or not recorded:
        return False
    try:
        expected = step_approval_digest(step, task.plan.plan_id)
    except (TypeError, ValueError):
        return False     # 摘要算不出来的步骤，永不继承批准
    return recorded == expected
```

四个失败分支全部返回 `False`：无计划、无 id、无摘要、摘要算不出来。这是 fail-closed 的
形状——新形态的记录（缺摘要）在旧代码路径上不会被当成批准。

### 2.3 执行期门禁

`src/zevorinth/service.py::_execute_multi_step_plan`：

```python
for step in ordered:
    latest = self.store.require_task(task.task_id)   # ← 关键
    ...
    if step.requires_approval and not step_is_approved(step, latest):
        ...
        "reason": (
            "approval_content_changed"
            if step.step_id in latest.approved_step_ids
            else "approval_required"
        ),
```

两条改动值得单独说：

1. **门禁读的是每轮重新读取的 `latest`，不是循环开始时的局部 `task`。** 原实现用 `task`，
   它是一个局部副本；同一循环内如果有批准被撤销，副本仍然是旧的。这是一个真实的绕过面。
   **这条改动没有对应的自动化测试能单独证明它**——要证明它需要"同一循环内撤销批准"的
   并发时序，测试里没有构造。它靠代码注释和这里的说明存在，不假装被覆盖。
2. **拒绝原因区分两种情况。** `approval_content_changed` 表示"你批准过这个 id，但现在内容
   不是那个内容"；`approval_required` 表示"从来没批准过"。对用户来说这是两句不同的话，
   所以不能合并成一个。

### 2.4 三处调用点补 `plan_id`

`approve_step`（用 `claim.task.plan.plan_id`）、`replan_pending_steps`（用 `revised.plan_id`）、
执行期事件（用 `plan.plan_id`）。`approval_preview.py` 的 `_approval_state` 同步适配：
计划为空时摘要为 `None`，不再抛异常。

### 2.5 顺手修掉的两个既有缺陷

1. **`TaskNotFound` 在两处 `except` 里被引用但从未导入。** 异常真的抛出时，
   `except TaskNotFound` 会变成 `NameError`，把"任务不存在"伪装成"代码崩了"。
   这个缺陷是在写探针脚本时现场撞到的（第一次探针就 `<class 'NameError'>`）。
   已补进 `from .event_store import (...)`。
2. **`service.py` 没有模块级 `LOGGER`。** 新增的注记失败分支需要它。已加。

## 3. 五项分项验收结论

**验收方式说明**：以下每一条都指向能跑的测试或能复现的观察，不用"表单展示了摘要"
或"单测数量"代替机制证明。

| # | 结论 | 判定依据 | 状态 |
| --- | --- | --- | --- |
| 1 | 内容绑定 | 摘要覆盖 8 个字段（含工具身份、完整参数、expected_result、depends_on、max_attempts、retry_on_not_executed、plan_id）；`test_approval_binding.py` 8 项证明参数/工具/语义字段任一变化都会改变摘要 | **PASS**（已测试） |
| 2 | 执行期一致性校验 | 门禁在 `_execute_multi_step_child` 之前、用每轮重读的 `latest` 重算摘要并比对；`test_approval_gate_execution.py::GateRefusesChangedContentTests` 3 项 | **PASS**（已测试） |
| 3 | 重规划审批失效 | `_replanning_the_pending_step_drops_its_approval`：replan 后 `approved_step_ids == []`，再 resume 得到 `approval_required`，且未写文件 | **PASS**（已测试，真实可达路径） |
| 4 | 旧审批复用防护 | 计划身份进入摘要；`test_a_record_from_another_plan_is_refused`（执行期）+ `test_an_approval_written_for_the_parent_plan_does_not_satisfy_the_step`（续跑期）；变异「摘要忽略 plan_id」使后者转红 | **PASS**（已测试 + 变异验证） |
| 5 | 副作用前拒绝 | 拒绝后：目标文件不存在、无该步骤的 `StepResult`、任务总数不变（`_task_ids()` 前后相等） | **PASS**（已测试） |

### 3.1 五项之外的一条：不可达状态没有被伪装成可达

`test_approval_gate_execution.py` 的文件头写明：

> "approved, then the same step id is redefined" is **not reachable** through the product.
> `approve_step` executes the step immediately, and a completed step cannot be replanned.
> The window only exists if a persisted record was written by an older build, or if the
> process died between the approval transition and the execution (the crash-recovery path).

也就是说：**"已批准但内容被换掉"这个状态，真实用户路径构造不出来。** 测试夹具是刻意写的，
它验证的是"门禁在执行期会重新计算并比对、并在工具运行前拒绝"，**不是**"有人真的看到了旧内容"。
后者没有自动化测试能证明，只能靠审批表单测试 + 人工走查。这一点在报告里保留，不粉饰。

## 4. 真机 / 实测记录

全部在临时目录里跑，不写仓库 `data/`。

| 观察 | 结果 |
| --- | --- |
| 正常批准后执行 | `SUCCEEDED`，`s2.txt` 落盘 |
| 批准后改参数 | `AWAITING_APPROVAL`，`reason=approval_content_changed`，`s2.txt` 不存在 |
| 换工具 | 摘要变化 → 同上 |
| 另一计划的摘要 | `AWAITING_APPROVAL`，文件不存在，任务数不变 |
| 重规划后 resume | `approved_step_ids=[]` → `reason=approval_required` |
| 执行期摘要读取异常（参数不可序列化） | `step_is_approved` 返回 `False`，不继承批准 |
| 无计划 | 返回 `False` |

## 5. 仍未做

| 项 | 状态 |
| --- | --- |
| 「审批被撤销」的并发时序测试 | **未验证**。需要同一循环内撤销批准，当前测试体系没有构造手段 |
| 审批表单是否真的展示了与摘要一致的字段 | **仅代码审查**（`approval_preview.py` 已适配签名），无端到端截图证据 |
| 旧 v1 摘要历史记录的迁移 | 设计上不迁移：v2 摘要与 v1 永不相等，历史记录自动失效重问。这是 fail-closed 的预期行为，**未在真实历史数据上验证**（本地无 v1 历史库） |

## 6. 新风险

| 编号 | 描述 | 影响 | 状态 |
| --- | --- | --- | --- |
| R-24 | 审批绑定的是**计划里的参数**，而参数里的 `${step.field}` 在批准时可能还是模板。批准覆盖的是"取值规则"而不是"最终内容" | 续跑路径已通过参数物化消除（见 `SD_FAILED_STEP_RESUME_IMPLEMENTATION.md` §2.3）；**首次执行路径仍存在** | 已识别，部分缓解 |
| R-25 | 摘要依赖 `json.dumps`，参数里出现不可序列化对象时 `step_is_approved` 返回 `False`（拒绝）而不是抛错 | 方向正确（fail-closed），但会表现为"莫名不能执行" | 已识别 |
| R-26 | 门禁读 `latest` 的改动无自动化测试覆盖 | 若未来有人把它改回局部副本，测试不会红 | 已识别，需后续补并发测试 |
