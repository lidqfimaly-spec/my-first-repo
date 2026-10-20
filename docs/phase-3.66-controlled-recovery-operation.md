# Zevorinth Phase 3.66：单项受控恢复操作

## 1. 结论与范围

**PASS。** Zevorinth 现在完成了一条真实文件副作用闭环：

```text
preview
→ exact approval
→ freshness revalidation
→ atomic claim
→ durable execution intent
→ fixed marker creation
→ independent no-follow readback
→ consumed / failed
→ read-only crash reconciliation
```

本阶段只允许一个操作：

```text
restore_controlled_marker
```

它只会在审批绑定的专用 workspace 中创建：

| 项目 | 值 |
| --- | --- |
| 相对路径 | `controlled-recovery/restored-marker.txt` |
| 内容 | `zevorinth-controlled-recovery-v1\n` |
| 权限 | `0600` |
| 内容 SHA-256 | `50b32a1b2d5766040b4f37cee2453cc6343e7b5d5dc344a667e258252bdaef92` |
| 目标前置条件 | target 不存在，父目录与 workspace 身份安全 |

没有实现任意路径写入、任意 shell、通用文件删除、自动恢复、自动清理、
历史任务迁移或多操作恢复引擎。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `9034bd285b577ee772d6ddcad855d2964d389358` |
| 真实模型 API 请求 | **0** |
| 网络访问 / 依赖安装 | 否 / 否 |
| Docker / 容器操作 | 0 / 0 |
| 任意 shell / 宿主机任务执行回退 | 0 / 0 |

该操作是专用本地 workspace 的受限文件原语，不通过 task executor、shell、
subprocess 或容器回退执行。

## 2. Preview 与审批绑定

`with_controlled_restore_action()` 在现有只读 recovery preview 上追加唯一
allowlisted action。审批绑定：

- 完整 preview 和 task state digest；
- action 名称和绝对目标 URI；
- operation、preconditions、expected result；
- verification 方法、risk、executable；
- 完整 action 摘要；
- execution attempt ID。

目标 URI 包含 workspace 的规范化绝对路径。执行器收到不同的 workspace 时，
在 claim 前拒绝。preview 为 stale、审批撤销、审批缺失或 action 不匹配时，
不会调用副作用函数。

## 3. 持久化执行意图

claim 成功后，执行器先读取真实目标状态。只有 `target_absent` 才构造 intent，
并通过 `RecoveryApprovalStore.record_intent()` 在文件副作用前持久化：

- attempt ID；
- action / target / action digest；
- workspace path；
- 固定相对路径；
- 预期大小、摘要和权限；
- workspace device/inode；
- 父目录前置状态与身份。

intent 写入失败时不会创建文件。intent 一旦写入不可替换；相同内容返回
`unchanged`，不同内容返回 `execution_intent_conflict`。

## 4. 真实操作与独立验证

文件创建使用已打开的 workspace 目录描述符和 `O_EXCL | O_NOFOLLOW`：

1. 验证 workspace device/inode 未变化；
2. 验证父目录身份和权限；
3. 以 `O_EXCL` 创建固定文件，不覆盖已有目标；
4. 写入固定内容，设置 `0600`，执行文件 `fsync` 和父目录 `fsync`。

执行结束后，独立验证器重新从磁盘打开目标：

- 不信任 materialization 返回值；
- 父目录和文件均使用 no-follow；
- 检查 regular file、inode 稳定性、权限、大小和 SHA-256；
- 只有实际内容完全匹配时才允许 `verification_passed=true`。

因此“写入函数返回成功”不等于操作成功；只有独立读取的真实状态通过验证，
审批才能进入 `consumed`。

## 5. 状态与恢复语义

| 状态 | 含义 |
| --- | --- |
| `approved` | action 已确认，但尚无 attempt 或 intent |
| `consuming` + intent | 唯一 attempt 与执行意图已持久化，结果未知 |
| `consumed` | 文件副作用和独立验证均已持久化 |
| `failed` | 前置条件失败、操作失败或独立验证拒绝 |

| 故障场景 | 结果 |
| --- | --- |
| claim 持久化失败 | `unverified`，无文件，禁止重试 |
| intent 持久化失败 | `unverified`，无文件，禁止重试 |
| intent 后、副作用前崩溃 | target absent；对账为 `unverified + manual_review` |
| 副作用后、completion 前崩溃 | target verified；仍为 `unverified + manual_review` |
| completion 持久化失败 | `consuming`，真实副作用存在但不得自动重试 |
| 独立验证拒绝 | `failed`，不得进入 `consumed` |
| 目标已存在 | 不覆盖，记录失败 |
| 目标或父目录是符号链接 | 不跟随、不写入 |
| 两个进程竞争 | 最多一个 claim 和一个真实文件创建 |
| 重复执行 | 拒绝重放，不产生第二次副作用 |

无法区分具体崩溃位置时，只读 reconcile 报告当前真实文件状态，但不自动完成、
不删除、不重跑。

## 6. 可逆性

副作用仅是创建一个固定、可识别、`0600` 的标记文件。人工补偿可在权限审计后
删除该精确文件；系统不会自动执行删除。人工补偿后：

- 原 `consumed` 执行记录保持不可变；
- reconcile 会报告终态与当前观察不一致；
- 仍要求人工复核且 `safe_to_retry=false`。

## 7. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 受控恢复专项矩阵 | 18/18 |
| 系统 Python 全量 unittest | 700/700，exit 0 |
| `.venv` 全量 unittest | 700/700，exit 0 |
| Mutation runner | **104 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 104/104 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异覆盖：target 绑定绕过、前置条件绕过、跳过 intent、claim 失败继续、
intent 未 claim、intent identity 绕过、信任 writer 返回值、摘要验证绕过、
跟随符号链接、completion 失败误报成功、崩溃后跳过人工复核和允许自动重试。

## 8. 未验证事项与残余风险

1. 文件原语和目录描述符语义只在当前 macOS/POSIX 环境验证；跨 OS、网络文件
   系统或容器共享卷行为未验证。
2. 验证与实际 completion 写入之间仍存在本机恶意竞争者可使用的 TOCTOU 窗口。
3. `record_digest` 仍不是审批或验证器身份的密码学认证。
4. 人工补偿没有自动化删除实现；这是刻意的安全限制。
5. 本阶段没有真实模型，也没有把该文件原语接入通用任务恢复或自动恢复队列。

## 9. 安全边界

- 未读取 `.env`；
- 未访问互联网、未安装依赖；
- 未调用真实模型；
- 未启动或关闭 Docker；
- 未执行任意 shell、task executor 或宿主机执行回退；
- 未操作生产数据、`.idea/` 或未授权 workspace；
- 未自动清理或删除文件；
- 未放宽 evidence、独立验证或 recovery approval 门禁。

## 10. 阶段判定

**PASS**：一个明确、低风险、可独立验证且有显式人工补偿方式的真实文件副作用
已在审批、并发 claim、持久化 intent、独立验证、崩溃对账和防重放约束下完成。
