# P0 Agent 能力矩阵

> 审查日期：2026-11-03（Asia/Shanghai）  
> 审查对象：`/Users/lidequan/PythonProject/Zevorinth`  
> 产品阶段（registry）：`3.92` / `single_host_local_alpha`  
> 标签约定：**[事实]** 可复核证据；**[推断]** 基于事实的合理解释；**[建议]** 下一阶段行动；**[无法确认]** 本轮未验证。  
> 约束：未改业务代码；旧仓与 Origin 云端另线仅作背景，不纳入本仓能力断言。


说明：状态枚举 = 已实现且已验证 / 已实现但未经充分验证 / 部分实现 / 仅有设计或文档 / 尚未实现 / 存在缺陷 / 无法确认。  
**禁止把文档当实现**；下列「registry 自报」须与代码/实测交叉。

## A. 任务操作

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| `write_text_artifact` | 已实现且已验证 | registry `available`；CLI `run` 成功写文件+SHA；`task-audit` ok |
| `run_tests` | 已实现但未经充分验证（本轮） | registry `conditional`；本轮未跑容器 live；代码+大量单测存在 |
| `general_shell` | 尚未实现 | registry `not_implemented`；`not_available_task_types` |
| `browser` | 尚未实现 | 同上 |

## B. 规划

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| deterministic_planner | 已实现且已验证 | `planner.py`；缺参澄清、完整执行实测 |
| llm_planner | 部分实现 / 条件 | 实现存在；无 Key → `planning_failed` exit 7；有 Key 路径本轮未跑 |
| 多步计划 | 尚未实现 | `policy.py` 强制单步 |
| intent fidelity gate | 已实现但未经充分验证（live） | registry+单测；live 依赖授权评测 |
| text precision gate | 已实现但未经充分验证（live） | 同上 |
| general_llm_planning_quality | 无法确认 / registry=`unverified` | 固定 pilot ≠ 通用质量 |

## C. 执行控制与生命周期

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| 任务持久化与事件 | 已实现且已验证 | SQLite + events 实测 |
| lease/fencing/budget | 已实现且已验证 | 代码+大量测试；doctor 报告 |
| clarification + resume | 已实现且已验证 | 澄清任务 resume 成功实测；clarification 单测 OK |
| revise 未执行计划 | 已实现且已验证 | CLI revise 实测 plan_version++ |
| cancel 无活跃 lease | 已实现且已验证 | CLI cancel → cancelled |
| request-cancel | 已实现但未经充分验证（运行中） | API 存在；本轮未构造长跑中断 |
| pause/resume 语义暂停 | 尚未实现 | 无 pause 命令 |
| 执行中追加要求 | 部分实现 / 缺口 | 仅未执行可 revise；运行中无「追加」API |
| automatic_retry/recovery/cleanup | 尚未实现 | registry |
| general_task_recovery | 尚未实现 | registry；仅有 prepublish 有界恢复与 marker |
| prepublish_write_recovery | 已实现但未经充分验证（本轮未触发故障） | CLI+registry |
| controlled_cleanup | 条件已实现 | 需显式调用 |

## D. 验证与信任

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| independent_verification | 已实现且已验证 | 成功任务 phase=verified |
| L1 lifecycle audit | 已实现且已验证 | task-audit ok；assurance L1 |
| L2/L3 trust | 尚未实现 | registry |
| safety_gate 离线 | 已实现且已验证（测试面） | CLI eval-safety-gate + 测试文件 |

## E. 对话 / 产品表面

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| conversation 消息幂等 | 已实现且已验证 | 服务单测+本轮 process |
| 对话驱动自然语言多目标 | 部分实现 | 缺结构化参数则依赖 LLM 或澄清；deterministic 默认写 artifact |
| local_read_only_workbench | 已实现且已验证 | HTTP 实测 + 测试 |
| task_control_center_clarification | 已实现但未经充分验证（浏览器） | session/actions 实测；Origin 保护；单测丰富 |
| web_ui 远程多用户 | 尚未实现 / conditional 只读本地 | registry limitations |
| 中文 i18n 工作台 | 已实现且已验证 | i18n JSON + HTML lang=zh-CN |

## F. 记忆 / 知识 / 个性化

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| 长期记忆 | 尚未实现 | 无模块 |
| 知识库 / RAG | 尚未实现 | 无模块 |
| 用户偏好设置面 | 仅有设计或文档 / 弱 | `.env.example` 配置；无产品设置 UI |

## G. 可观测与评测

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| events / attention | 已实现且已验证 | CLI+workbench API |
| planner_quality_offline | 已实现且已验证 | CLI+registry |
| live baseline protocol | 条件已实现 | default-deny；本轮未授权 live |
| doctor 能力报告 | 存在缺陷（版本漂移） | doctor phase 3.75 vs registry 3.92 |

## H. Drift 清单（registry/文档/代码）

| 项 | 观察 |
| --- | --- |
| 阶段号 | README/registry=`3.92`；`cli.doctor_report` 字符串=`3.75`；pyproject version=`0.3.0` |
| doctor `model_provider: not implemented` vs `llm_planner_available: implemented` | 表述维度不同，易误导操作员 |
| 「通用 AI Agent」愿景 vs 双工具单步 Alpha | 产品叙事需与 registry 对齐，否则用户预期失控 |
