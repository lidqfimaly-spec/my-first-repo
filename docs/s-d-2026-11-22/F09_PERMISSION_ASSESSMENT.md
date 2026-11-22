# F-09 权限评估：`execute_plan` 产品入口 与 `run_tests` operation 放行

**项目**：`/Users/lidequan/PythonProject/Zevorinth`
**日期**：2026-11-22
**性质**：**只评估、不实施**。本文档不修改任何白名单、令牌、审批或控制面逻辑。
**状态标记约定**：已实现 / 已测试 / 已实际体验 / 仅代码审查 / 未验证 / 当前无法验证

---

## 0. 一句话结论

**这两个决定不能一起做，因为它们的现状根本不同：**

- `execute_plan` 的"不开放"**已经不成立**——它今天就能通过 `pause_task` + `resume_task`
  被真实执行（连跑两次工具、两次落盘，已实测）。拦住它的是**一个可以被绕过的门**，
  门后**没有第二道锁**。
- `run_tests` 的"不开放"**成立且有多重冗余**——同一个旁路走进去，会被
  `isolation_unverified` 挡住；即使过了隔离证据，还要求一次性 host grant。

所以：`execute_plan` 的关键问题不是"要不要开放"，而是**先把已经存在的旁路关掉，再谈开放**；
`run_tests` 的关键问题是"如果开放，grant 怎么跟着请求走"。

---

## 1. 现状事实（可复现）

### 1.1 创建入口：已经存在

- 控制面把"复杂任务"意图引导到 `execute_plan`，并返回
  `operation: "execute_plan"`、`required_fields: ["steps"]`（`workbench_control.py:1613-1630`）。
- 前端有对应的创建入口：`workbench_static/index.html:133` 的
  `<option value="execute_plan">执行有界多步计划</option>`，`app.js` 里
  `state.createJsonMode = operation.value === "execute_plan"`。
- 结论：**"不开放用户创建入口"这一条，在当前产品中已经处于"已开放"状态。**
  本阶段没有新增它，也不会关闭它（关闭属于产品形态变更，需单独决定）。

### 1.2 启动门禁：声明上是关的

```python
# workbench_control.py:52-54
CONTROL_STARTABLE_OPERATIONS = frozenset(
    set(SUPPORTED_TASK_OPERATIONS) - {"run_tests"}
)
# 8 个 operation 里，execute_plan 根本不在 SUPPORTED_TASK_OPERATIONS 中
```

`start_queued_task` 的校验：

```python
or task.request.operation not in CONTROL_STARTABLE_OPERATIONS
→ ControlActionError("task_start_not_allowed", ...)
```

实测：`execute_plan` 任务调 `start_queued_task` → `task_start_not_allowed`。**符合声明。**

### 1.3 启动门禁：实际可绕过（本阶段最重要的发现）

`PAUSE_TASK` 只检查状态，不检查 operation：

```python
elif action == PAUSE_TASK:
    if task.status not in {QUEUED, RUNNING, AWAITING_APPROVAL}:
        raise ControlActionError("task_pause_not_allowed", ...)
elif action == RESUME_TASK:
    if task.status is not TaskStatus.PAUSED:
        raise ControlActionError("task_resume_not_allowed", ...)
```

`resume_task` 映射到 `service.resume(task_id)`，而 `resume` 只在终态/BLOCKED 时早退，
其余情况一律 claim → 执行。

**控制面实测（`TaskControlService.execute`，非 service 直调）**：

```
created: queued
start_queued_task refused: task_start_not_allowed     ← 门是关的
pause_task accepted
resume_task accepted
artifacts: [.../8f3d325d-..../s2.txt, .../d7f19823-..../s1.txt]
```

即：**两次真实工具执行、两个文件真实落盘**，而 `CONTROL_STARTABLE_OPERATIONS`
自始至终没有包含 `execute_plan`。

这不是本阶段引入的，是既有的。本阶段没有修，因为修它要改控制面的权限判定条件
（等于改"operation 白名单"的实际效力），属于指令保留事项。

**严重性判断**：门禁被绕过，但门后的保护还在——每一步仍然是独立子任务，
仍然过 `policy.evaluate`、仍然过审批门禁。所以这不是"任意执行"，
而是**"多步任务可以在没有显式启动确认的情况下被启动"**。对用户而言，这意味着
"创建了一个复杂任务但没启动"之后，一个暂停再恢复就会让它跑起来。

### 1.4 `run_tests` 在同一旁路下的表现

同一个"pause → resume"路径：

```
created: queued
paused: paused
resumed: failed  err: isolation_unverified
```

也就是说 `resume` 确实进到了执行链，但被**隔离证据**挡住。再往后还有
一次性 host grant（`capabilities.py` 中 `run_tests.requirements` 明列）。

**所以两个 operation 的差别不在"门"，在"门后面有几把锁"。**

---

## 2. 为什么必须分开评估（证据）

| 维度 | `execute_plan` | `run_tests` |
| --- | --- | --- |
| 在 `SUPPORTED_TASK_OPERATIONS` | **不在** | 在 |
| 在 `CONTROL_STARTABLE_OPERATIONS` | 不在 | **不在**（被显式排除） |
| 走 `policy.evaluate` | **不走**（`_is_multi_step_plan` 分支在 policy 之前返回） | 走 |
| 第二步锁 | 无 | `isolation_evidence` + 一次性 host grant |
| 真实副作用范围 | 最多 8 步 × 每步 1 个子任务，每步受各自政策约束 | 在自己的 workspace 里跑任意测试代码 |
| 现有产品状态 | 可创建、可启动（旁路） | 可创建、**不可执行**（多重门） |
| 失败代价 | 写错文件、跑错工具 | 在宿主机执行任意代码 |

一句话：`execute_plan` 是**编排层**的开关，`run_tests` 是**执行任意代码**的开关。
把它们合并成一个"多步能力开放"决定，会用编排层的风险评估去掩盖代码执行的风险。

---

## 3. 十个问题

### Q1　`execute_plan` 当前能不能被用户创建？

**能。** 见 §1.1。引导流程 + 前端选项都在。

### Q2　`execute_plan` 当前能不能被启动？

**声明上不能，实际上能。** `start_queued_task` 拒绝；`pause_task` + `resume_task` 成功执行。
证据见 §1.3。

### Q3　放开显式启动，父任务会不会经过 policy？

**不会。** `service.py:3425`：

```python
if self._is_multi_step_plan(running):
    return self._execute_multi_step_plan(running, lease_token)
decision = self.policy.evaluate(...)      # ← 多步任务在这里之前就返回了
```

所以"放开启动"并不改变父任务不过 policy 这件事。**这项检查不能通过"放开启动"获得，
只能单独补。** 缺的是什么：整份计划作为一个整体的形状（步骤数已有 8 步上限、
依赖已有图校验、工具已逐个在子任务过 policy），真正缺的是"这份计划合起来要做什么"
这一层的判断——目前没有任何地方做。

### Q4　放开启动后，每步子任务是否仍受 policy 约束？

**是。** 每个子任务是独立任务，走 `execute_task` → `policy.evaluate`。
`test_failed_step_resume.py` 的 `test_a_dependent_step_is_skipped_with_a_reason`
与既有的 `inspect_workspace`/`write_text_artifact` 路径测试都覆盖了子任务级拒绝。

### Q5　审批门禁是否足以约束"一次启动 N 个工具"？

**部分是。** 审批是**按步骤**的（`requires_approval` 逐步骤配置），而现在
创建面板允许提交 `steps` JSON——默认 `requires_approval` 是 `false`。
也就是说：一个 8 步计划可以一次启动、8 次真实执行、**零次人工确认**，
只要创建者没在 JSON 里写 `requires_approval: true`。

这是 Q5 的实质答案：**审批机制能约束"被标记为需要审批的步骤"，不能约束"启动这个动作本身"。**
如果开放启动，值得考虑的是"多步任务整体启动需要一次确认"（`requires_confirmation: true`
已经存在于 action 元数据里，见 `workbench_control.py:250-259`），而不是逐步骤加审批。

### Q6　放开后最坏情况的量级？

- 步骤数上限 8（`MAX_PLAN_STEPS`），每步 `max_attempts` ≤ 3（`MAX_STEP_ATTEMPTS`）。
- 单产物上限 64 KiB（`workbench_control` 的创建限制文案，
  `write_text_artifact` 自身在 `artifact_tool` 里有对应上限）。
- 最坏：8 步 × 3 次尝试 = 24 次工具调用，产物全部落在**该任务自己的** `artifacts/<task_id>/`
  下，不回写工作区（`modify_text_artifact`、`edit_text_file` 例外，它们写工作区并有备份）。
- 结论：量级可控，且有界；真正的风险不是"量"，是 Q5 的"零确认"。

### Q7　除了 `start_queued_task`，还有哪些路径能触发执行？

已核对的触发面：

| 路径 | 对 `execute_plan` 是否生效 | 说明 |
| --- | --- | --- |
| `start_queued_task` | 否 | 显式拒绝 |
| `pause_task` → `resume_task` | **是** | §1.3 实证 |
| `answer_clarification` | 否 | 只对 `AWAITING_CLARIFICATION`/`BLOCKED` 生效 |
| `approve_step` | 否 | 只对 `AWAITING_APPROVAL` 生效，且该状态说明已启动过 |
| `replan_pending_steps` | 否（但它会**创建**新的多步任务） | replan 之后任务仍停在 PAUSED/AWAITING，需要再走 resume 才执行 → 依然经过 §1.3 那条路 |
| `revise_unstarted_task` | 否 | 只改请求，不改状态 |
| `add_task_note` | 否 | 只追加事件 |
| HTTP / CLI 直接调 `service.execute_task` | 不可达 | 没有对外路由暴露 |

结论：**唯一的旁路是 pause → resume**，而 `replan_pending_steps` 会产生新的
待执行任务，也会落到这条路上。修一个点即可覆盖全部。

### Q8　`run_tests` 为什么被排除？

因为它需要**外部环境授权**，而不是任务级授权：

`capabilities.py` 里 `run_tests.requirements`：

- registered workspace
- bounded unittest target
- valid isolation evidence
- container runtime for trusted isolation
- **one-time host execution grant issued out of band（`zevorinth authorize-host-test`），
  绑定 workspace 与 target、一次性、短有效期**

这些条件**不能由控制面在"启动一个任务"这个动作里满足**。启动请求里没有 grant，
而有 grant 的那个人（在终端里跑 `authorize-host-test` 的人）和点按钮的人
可能不是同一个人。这就是它必须被排除的机制原因——不是"暂时不想开"，是**权限模型不同构**。

### Q9　两个决定是否互相依赖？

**不依赖。** 三条独立证据：

1. `execute_plan` 不在 `SUPPORTED_TASK_OPERATIONS` 里，`run_tests` 在——它们的白名单
   位置本来就不同（一个缺条目，一个被显式减掉）。
2. `run_tests` 有 `isolation_evidence` + host grant 两道后置门，`execute_plan` 没有。
   放开前者不影响后者，反之亦然。
3. 实测：同一个旁路能跑 `execute_plan`，跑不动 `run_tests`（§1.4）。

唯一的相关性是**语义上的**：一个多步计划里可以包含 `run_tests` 步骤
（`SUPPORTED_STEP_OPERATIONS` 含 `run_tests`）。所以"开放 `execute_plan`"会
**间接**给 `run_tests` 一个入口——绕过 `CONTROL_STARTABLE_OPERATIONS` 那个排除项。

这是本评估里第二个必须点明的事实：**开放 `execute_plan` 等于部分开放 `run_tests`。**
如果要分开决定，就必须在处理 `execute_plan` 时同时决定"多步计划里是否允许 `run_tests` 步骤"。

顺带一个不一致：`capabilities.py` 中 `run_tests.access` 不含
`multi_step_task_engine`，但 `multi_step.py` 的 `SUPPORTED_STEP_OPERATIONS` 里
**有** `run_tests`。能力注册表与实现不一致，属于已有缺陷（记录为 R-28）。

### Q10　续跑能力（S-D ⑤）接入产品面需要什么？

`resume_failed_plan` 目前只有服务层入口。要让它成为用户可用能力，需要：

1. 在 `workbench_control.SUPPORTED_ACTIONS` 增加一个 action（例如
   `continue_failed_plan`），并在 `_validate` 里加门槛：
   状态 ∈ {PARTIAL, FAILED}、`cancel_requested_at is None`、无租约。
2. 该 action 会**创建并启动**一个新的 `execute_plan` 任务——所以它**继承 Q2/Q7 的问题**：
   如果 §1.3 的旁路不修，"续跑"只是又一个"能启动多步任务"的入口。
3. 因此顺序应该是：**先关旁路 → 再决定显式启动 → 最后加续跑 action**。
   反过来做会放大已有的洞。

本阶段**没有**实施第 1 条：它属于"新增用户可用能力"，指令要求停止并提交方案。

---

## 4. 工具组合绕过路径

即使不开放任何新入口，现有能力组合也值得记一笔：

| 组合 | 效果 | 是否有额外门 |
| --- | --- | --- |
| `execute_plan` + `write_text_artifact` × 8 | 一次启动写 8 个产物 | 无（每步 policy 允许） |
| `execute_plan` + `modify_text_artifact` | 改工作区文件（有 backup） | policy + 工作区注册 |
| `execute_plan` + `run_tests` | **在宿主机跑测试代码** | isolation evidence + host grant（仍在） |
| `replan_pending_steps` → `resume` | 把任意新步骤塞进一个已暂停的任务再跑起来 | 同 §1.3 |
| `execute_plan` + `read_text_artifact`（`source_task_id`） | 读**别的任务**的产物 | 见下 |

最后一条需要说明：`read_text_artifact` 的 `source_task_id` 是一个跨任务读取面。
`_step_attempt_left_no_side_effect` 的实现过程中曾因为指向不存在的 `source_task_id`
撞到 `TaskNotFound` 未导入的缺陷（已修）。这个跨任务读取面本次**没有**评估其授权模型，
标注为**未验证**。

---

## 5. 建议（本阶段不实施）

### 5.1 `execute_plan`：先关旁路，再谈开放

**P0（必须，且与 F-09 是否开放无关）**
给 `resume_task` 加一条：任务是 `PAUSED` 且**从未启动过**
（`claim_generation == 0` 且 `execution_mode is NOT_EXECUTED`）时，
`request.operation` 必须 ∈ `CONTROL_STARTABLE_OPERATIONS`。
理由：`pause → resume` 对"未启动任务"在语义上就是 `start`，不应有两套门槛。
影响面：需要复跑 `test_workbench_control.py` 中所有 pause/resume 组合。

**P1（开放的前置条件）**
1. 决定多步计划里是否允许 `run_tests` 步骤（Q9）。
2. 给多步任务的启动加一次整体确认（`requires_confirmation` 已有机制可用）。
3. 补"计划整体形状"校验——当前父任务不过 policy（Q3），这一步不是可选项。

**P2（可选）**
把 `execute_plan` 加进 `CAPABILITY_REGISTRY`（目前完全缺失，导致能力矩阵无法描述它，
这也是 P0 R-02 的根因之一）。

### 5.2 `run_tests`：不要跟着 `execute_plan` 一起放

如果将来要开放，需要解决的是"grant 怎么跟着请求走"：启动请求必须携带一个
绑定了 workspace + target 的一次性 grant，且控制面要能验证它、消费它、并把失败原因
（`host_authorization_*` 那 9 个码）如实返回给点按钮的人。这是**新的权限传递协议**，
不是加一个白名单条目。

在当前阶段，`run_tests` 维持不可启动是**正确且已生效**的，且比其他 operation 更安全
（旁路也打不穿）。

### 5.3 续跑：等 P0 修完再加 action

见 Q10。

---

## 6. 本阶段的实际动作

| 项 | 动作 |
| --- | --- |
| `execute_plan` 加入可启动集 | **未做** |
| `run_tests` 加入可启动集 | **未做** |
| 修改 `CONTROL_STARTABLE_OPERATIONS` | **未做**（前后对比一致：7 个） |
| 修改 `SUPPORTED_TASK_OPERATIONS` | **未做**（前后一致：8 个） |
| 修复 §1.3 旁路 | **未做**，理由：属于控制面权限判定变更，需批准 |
| 续跑接入控制面 | **未做**，理由同上 |

---

## 7. 状态汇总

| 结论 | 状态 |
| --- | --- |
| `execute_plan` 可创建 | **已实际体验**（读代码 + 前端入口 + 创建流程文案） |
| `execute_plan` 经 `start_queued_task` 不可启动 | **已测试**（`task_start_not_allowed`） |
| `execute_plan` 经 pause→resume 可启动并产生真实副作用 | **已实际体验**（控制面调用，两个文件落盘） |
| `run_tests` 经同一旁路仍被挡 | **已实际体验**（`isolation_unverified`） |
| 父任务不过 policy | **仅代码审查**（`service.py:3425` 分支顺序） |
| `read_text_artifact` 跨任务读取的授权模型 | **未验证** |
| 修 §1.3 旁路后的连带影响 | **当前无法验证**（未实施） |
