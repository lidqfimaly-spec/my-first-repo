# S-D ⑤ 实施：从失败步骤续跑

**项目**：`/Users/lidequan/PythonProject/Zevorinth`
**日期**：2026-11-22
**范围**：S-D 阶段 C（实现）+ 阶段 D（测试）
**状态标记约定**：已实现 / 已测试 / 已实际体验 / 仅代码审查 / 未验证 / 当前无法验证

---

## 1. 一个必须先解释的设计选择：续跑是**新任务**

第一反应通常是"把失败的任务重新打开，从失败步骤继续"。这个方案在本项目里**不可行**，
原因不是实现麻烦，而是会破坏审计：

- `PARTIAL` 和 `FAILED` 是**终态**。
- `audit_task_lifecycle` 对 `duplicate_terminal_event` 报 issue，并且要求 `task_partial`
  事件出现时任务**正处于** `PARTIAL`。

所以"把 PARTIAL 的任务拉回 RUNNING 再跑一遍"会在父任务的事件流里制造
"终态 → 非终态 → 终态"的序列，审计链会被污染，而且污染的是**每一个被续跑过的计划**。

因此：**父任务保留它的判定和证据，续跑是一个持有"仍需要跑的步骤"的新计划。**

```python
continuation = self.enqueue(TaskRequest(...), metadata={"resume_of": parent.task_id}, ...)
self.store.record_event_and_update(parent.task_id, EventType.TASK_NOTE_ADDED, ...,
    payload={"kind": "plan_continuation", "continuation_task_id": ...})
```

父任务只增加一条 `TASK_NOTE_ADDED` 注记（`kind="plan_continuation"`），**状态不变**。
注记失败只记 `LOGGER.warning`，不回滚续跑任务——续跑任务本身就是权威记录，
父任务的注记只是给人看的索引。

## 2. 续跑的十个步骤（对应指令 5.1）

`TaskService.resume_failed_plan()` 的实际行为顺序：

| # | 步骤 | 失败时的拒绝码 |
| --- | --- | --- |
| 1 | 读父任务 | `TaskNotFound`（不吞） |
| 2 | 只接受 `PARTIAL` / `FAILED` | `task_not_resumable` |
| 3 | 有取消请求则不续跑 | `task_cancel_requested` |
| 4 | 仍持有执行租约则不续跑 | `task_claim_active` |
| 5 | 版本不一致（调用方给了期望版本） | `task_plan_version_changed` |
| 6 | 计划记录必须存在 | `plan_missing` |
| 7 | 算出 `carried`（已成功）与 `remaining`（其余） | `no_failed_steps`（防御性） |
| 8 | 对 `remaining` 逐条判定副作用不确定性 | `step_side_effect_uncertain` |
| 9 | 参数物化 + 依赖裁剪 + 建图校验 | `MultiStepPlanError.code` 直传 |
| 10 | 入队 + 父任务注记 + **立即执行** | — |

第 10 步的"立即执行"是本轮修出来的一个缺陷，见 §4.2。

### 2.1 已成功步骤不重复执行（对应 5.2）

```python
carried = [
    step.step_id for step in plan.steps
    if results.get(step.step_id) is not None
    and results[step.step_id].status == "succeeded"
]
remaining = [step for step in plan.steps if step.step_id not in carried]
```

`carried` 的步骤**整个从新计划里消失**，不是"保留但跳过"。也就是说续跑任务的计划
就是 `["b", "c"]`，从头到尾没有 `a`。

指令明确禁止"为了实现简单而把整个计划从第一步重新运行"——这里没有这么做，
而且测试用三种独立方式钉住：

- 续跑任务的 `plan.steps` 里没有 `a`；
- 续跑任务的 artifacts 目录下没有 `a.txt`；
- 父任务的 `a.txt` 内容与 `mtime` 在续跑前后**完全不变**；
- 全局 `tool_started` 计数：`write_text_artifact` 始终只有 1 次。

### 2.2 六种失败分类的落点（对应 5.3）

| 指令给的类型 | 本项目里的判定 | 可否续跑 |
| --- | --- | --- |
| 尚未开始 | `skipped`；或 `child_task_id is None` 且事件流里没有对应的 `step_started` | 可以 |
| 已开始但确定无副作用 | 子任务存在、`execution_mode == NOT_EXECUTED`、无 artifact、**无 `TOOL_STARTED`** | 可以 |
| 已产生部分副作用 | 子任务 `execution_mode != NOT_EXECUTED`，或有 artifact | **拒绝** |
| 结果未知 | 子任务有 `TOOL_STARTED` 但既无 artifact 也无成功记录 | **拒绝**（保守） |
| 已完成但验证失败 | 步骤成功但父任务聚合验证未通过 → 父任务不是 `SUCCEEDED`，该步骤仍算 carried | 不触发续跑（见 §5 的观察） |
| 因审批被拒 | 父任务停在 `AWAITING_APPROVAL`，**不是** `PARTIAL`/`FAILED` | 不走续跑，走批准 |
| 因权限被拒 | 子任务 `BLOCKED` + `not_executed`，无 `TOOL_STARTED` | 可以（但会再次被拒，见 §5） |

判定函数的三层证据：

```python
if result.status == "skipped":
    return True
if result.child_task_id is not None:
    child = self.store.get_task(result.child_task_id)
    if child is None:
        return False          # 记录过、现在不见了 → 不是"没跑过"的证明
else:
    announced = self._announced_child_id(parent_task_id, plan_step.step_id)
    if announced is None:
        return True           # 连 id 都没宣布过
    child = self.store.get_task(announced)
    if child is None:
        return True           # 宣布过但从未落库 → 死在参数解析阶段
if child.execution_mode is not ExecutionMode.NOT_EXECUTED:
    return False
if child.artifact is not None:
    return False
return not any(e.event_type == EventType.TOOL_STARTED.value
               for e in self.store.list_events(child.task_id))
```

`child_task_id` 为空的两种情形被**分开**处理，因为它们的证据强度不同：
"从没宣布过"和"宣布过但没落库"都能证明没跑过；"记录过某个 id 但查不到"不能。

### 2.3 参数物化（顺带消掉 R-24 在续跑路径上的影响）

```python
outputs = {step_id: dict(results[step_id].output or {}) for step_id in carried}
for step in remaining:
    parameters = resolve_step_parameters(step.parameters, outputs)
```

`${a.artifact_sha256}` 在续跑时被替换成**具体值**，写进新计划。于是待批准步骤的审批
覆盖的是真实内容，而不是"取值规则"。实测：`c.txt` 的内容正是 `a` 的真实 sha256
（`06f961b8…`），而不是字符串 `${a.artifact_sha256}`。

这一条同时决定了"上游输出变了怎么办"：续跑绑定的是**记录里的输出**，不是磁盘上现在的
文件。测试里故意在父任务完成后篡改 `a.txt`，续跑仍然用记录的 sha256——否则等于批准了
一份没人看过的内容。

### 2.4 依赖裁剪（对应 5.4）

```python
depends_on=[d for d in step.depends_on if d not in carried]
```

被 carried 的依赖已经"烘"进参数里了，留着它会让 `validate_plan_graph` 因为
`unknown_dependency` 拒绝整个计划（新计划里没有 `a`）。裁剪后 `c` 只依赖 `b`。

未被 carried 的依赖**不动**：`c` 依赖失败的 `b`，续跑时 `b` 仍然失败 → `c` 不启动。
`fail_fast` 策略下 `c` 连 `StepResult` 都不会产生；`continue` 策略下 `c` 被记为
`skipped` 并带 `dependency_not_satisfied` 原因。两种都不会真的跑。

### 2.5 恢复时的重新验证（对应 5.5）

续跑任务是一个**普通任务**，走的是普通执行路径，所以它在恢复时重新经过：

| # | 重新验证项 | 在哪里 |
| --- | --- | --- |
| 1 | 工具白名单 | `validate_plan_graph` → `SUPPORTED_STEP_OPERATIONS` |
| 2 | 计划图形（阶数上限、无环、依赖存在） | `validate_plan_graph` |
| 3 | 步骤级审批 | `_execute_multi_step_plan` 的门禁（重算摘要） |
| 4 | 每个子任务的 policy 决策 | 子任务是独立任务，走 `policy.evaluate` |
| 5 | 取消请求 | 每步前的 `_cancel_if_requested` |
| 6 | 暂停请求 | 每步前后边界 |
| 7 | 执行租约与权威 | `_assert_execution_authority` |
| 8 | 最终产物验证 | `_finalize_multi_step_plan` → `_publish_multi_step_delivery` |

关键点：**续跑不继承任何审批**（`approved_step_ids == []`），所以第 3 项不是"重新检查",
而是"重新要"。恢复操作因此不构成绕过正常门禁的第二条通道。

## 3. 拒绝码一览

所有 `PlanResumeRefused` 都表示**没有创建任何东西、没有运行任何东西**。测试里对每一条
都断言了任务数不变。

| code | 含义 | 用户该做什么 |
| --- | --- | --- |
| `task_not_resumable` | 状态不是 PARTIAL/FAILED | 无（任务没失败） |
| `task_cancel_requested` | 有取消请求 | 先处理取消意图 |
| `task_claim_active` | 仍持有执行租约 | 等上一次执行结束 |
| `task_plan_version_changed` | 计划在你决定之后变了 | 重新看一遍计划 |
| `plan_missing` | 计划记录缺失 | 无法续跑，需重提 |
| `no_failed_steps` | 全部成功 | 无（防御性分支） |
| `step_side_effect_uncertain` | 步骤可能已产生副作用 | 人工确认后重提 |
| `step_output_unavailable` / `step_output_field_missing` | 参数引用的上游输出不存在 | 计划本身不可解析，需重提 |
| `unknown_step_reference` / `undeclared_step_reference` | 引用关系非法 | 同上 |
| `cyclic_plan` | 依赖成环 | 同上 |

## 4. 本轮发现并修掉的两个缺陷

### 4.1 过度拒绝：`child_task_id is None` 被归为"副作用不确定"

原实现：

```python
if result.child_task_id is None:
    return result.status == "skipped"     # failed + 无子任务 → False
```

注释写的是"从未创建子任务"，代码却返回 `False`。结果是：**参数解析失败的步骤**
（`${a.no_such_field}`）被报成"可能已产生副作用，续跑有重复执行风险"。

按指令 5.3 的分类，这类步骤属于"尚未开始"，应当可续跑。但直接改成 `return True`
会让"旧版本/损坏记录声称没有子任务、实际跑过"的情形被当成干净——那才是真正的危险。

改成查证据：从父任务的 `step_started` 事件里拿回被宣布的 child id，再查这个 id
在库里是否存在。

```python
announced = self._announced_child_id(parent_task_id, plan_step.step_id)
if announced is None:
    return True
child = self.store.get_task(announced)
if child is None:
    return True     # 宣布过但从未落库 → 死在参数解析阶段
```

行为上的可见变化：同一个计划现在拒绝时给出的码是 `step_output_field_missing`
（真实原因），而不是 `step_side_effect_uncertain`（误导）。**注意**：这类计划
本身不可解析，所以下一层 `resolve_step_parameters` 仍然会拒绝——**结论不变，
变的是理由**。这一点不夸大。

### 4.2 死路：续跑任务入队后没人能启动

原实现 `return continuation`——只入队，不执行。而 `execute_plan` **不在**
`CONTROL_STARTABLE_OPERATIONS` 里（这是本阶段明令保持的边界）。两个事实相乘的结果是：
**续跑任务会永远停在 `QUEUED`，没有任何合法入口能启动它。**

改成和 `submit_plan` 一致：

```python
return self.execute_task(continuation.task_id)
```

这不是扩权：`execute_plan` 仍然不可由控制面创建；这里启动的是**服务内部已经创建的**
任务，和 `submit_plan` 走同一条路径。

## 5. 必须说清的三个限制

1. **任何走到工具阶段的失败都不能续跑。** 因为判据是"有 `TOOL_STARTED` 就拒绝"。
   最典型的受害者是 `run_tests` 步骤：测试失败（退出码非零）意味着工具真的跑过，
   于是整条链都不能续跑，只能人工决定重提。这是**保守到影响可用性**的取舍，
   不是遗漏。要么接受它，要么为"幂等工具"开白名单——后者需要单独论证，本阶段没做。
2. **权限被拒的步骤"可续跑"，但续跑会再被拒。** policy 决策是确定性的，同样的请求
   得到同样的拒绝。所以这不是安全洞，只是白跑一次。拒绝码会如实反映
   （`blocked` + 同样的 `error.code`）。
3. **暂停请求不会阻止续跑。** 理由是：`resume_failed_plan` 只接受终态任务，
   而终态任务没有"正在运行、需要暂停"的东西。真正的"暂停不能绕过"由
   `test_a_paused_plan_is_not_continued_through_this_path` 覆盖——PAUSED 任务走续跑
   路径会被 `task_not_resumable` 拒绝，必须走 `resume()`（重新过审批门禁）。
   取消则相反：取消是"放弃这件事"，所以有取消请求时**拒绝续跑**。

## 6. 尚未接入产品面（这是本阶段最大的缺口）

`resume_failed_plan` 目前只有 `TaskService` 层入口，**没有控制面 action、没有前端入口**。

原因是它需要给 `workbench_control.SUPPORTED_ACTIONS` 增加一个 action 名
（例如 `continue_failed_plan`），并让它进入控制面的权限模型。这属于
**新增用户可用能力**，指令要求"若某项实现必须突破边界，立即停止该项，提交具体方案等待批准"。

所以这里停住，方案写在 `F09_PERMISSION_ASSESSMENT.md` §6，没有擅自加。

因此对用户而言，当前能力等级是：**机制已可用（服务层 + 测试），产品面不可用**。
按状态标记，"已实现 / 已测试"，但**不是"已实际体验"**——没人能通过界面点出来。
