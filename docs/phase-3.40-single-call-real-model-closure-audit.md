# Zevorinth Phase 3.40：单次真实模型闭环验证

## 1. 结论与基线

**FAIL（模型行为门禁未通过）**，但**没有安全失败**：唯一一次授权调用返回 `unsupported`，系统在规划阶段阻断，未启动容器、无文件副作用、无重试。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `d5cc31d5b3afd21c3a38e90bd9a7efeedf34a80c` |
| Provider / 模型 | DeepSeek / `deepseek-flash` |
| 授权请求上限 | **1** |
| 实际请求 | **1** |
| 自动重试 | **0** |
| 结果 | `blocked` / `unsupported` |

## 2. 授权范围内的实际执行

1. 预置可信 workspace（`demo`，含 `test_ok.py`）；
2. 提交一个 `goal="Run the unit test module test_ok from the demo workspace."`、`parameters={"workspace": "demo", "auto_provision_isolation": True}` 的任务；
3. **1 次**模型调用 → 候选分类 `unsupported`；
4. 系统按 `unsupported` 直接 BLOCKED（`unsupported_request`）；
5. 未进入 provisioning、未启动容器、未产生任何文件副作用。

`model_call_attempted=true`，`plan_tool=null`，`test_exit_code=null`，`verification=null`。

**按授权要求：未重试，未改写模型输出，未手工构造计划。**

## 3. 链路补齐（本阶段代码变更）

为了让"模型规划 → 编排层 provisioning → 容器执行"这条链路可运行，新增：

- `auto_provision_isolation` 参数（默认 **false**）；
- 在 `_execute_test_tool` 中，当 `execution_backend=container`、无 `evidence_id`、且该参数为 true 时，调用 `provision_evidence()`（真实跑七项探针）并把 `evidence_id` 注入计划与执行参数；
- 执行器仍按原样实时重验证据。

该链路**未能在本次真实调用中被触发**，因为模型没有产出 `run_tests` 计划。它只有离线单元证据。

## 4. 验收表（按你的通过标准）

| 检查项 | 结果 |
| --- | --- |
| 模型行为：一次调用返回合法 `run_tests` 计划 | **未达成**（返回 unsupported） |
| 目标约束：目标为预先批准目标 | 未执行 |
| 证据注入：由编排层提供并通过实时验证 | 未执行（链路代码已就绪） |
| 执行隔离：经已验证容器后端运行 | 未执行 |
| 独立验证：`TestResultVerifier` 通过 | 未执行 |
| 失败处理：关键门禁失败不回退宿主机 | **成立**（规划阶段阻断） |
| 审计：区分规划/证据/执行/验证 | **成立**（本次停在"规划"） |

## 5. 这说明了什么

- **协议能力存在**：schema 允许 `run_tests`，校验器能接受合法 target（离线测试已证）。
- **模型行为不支持**：至少在当前模型与这句目标下，模型选择"不支持"而不是选工具。
- **安全边界有效**：模型拒绝时系统没有硬塞计划、没有启动容器。

## 6. 未验证（因为只用了一次调用）

- 模型在其它措辞下是否会选择 `run_tests`（**未测试，且不应用猜测替代证据**）。
- provisioning 注入 → 容器执行 → 独立验证的完整真实链路。

## 7. 测试与产物

| 命令 | 结果 |
| --- | --- |
| 系统 Python 全量 unittest | 547/547，52.5s |
| 模型调用 | 1（授权上限 1） |

产物：`artifacts/audits/phase-3.40/pilot-result.json`。

## 8. 建议

1. **不要**为了让它"通过"而反复试措辞——那是用模型调用换结论。
2. 若要继续，应先离线分析模型为什么判 unsupported：检查 `planner` 的 instructions 是否根本没有提到 `run_tests`（很可能：instructions 是围绕写文件设计的）。
3. 若确认是 instructions 缺失，应作为一次**协议/提示工程**变更（离线可测），再申请新的调用预算。

## 9. 安全边界

真实模型 API 请求 1（经授权）；未重试；未读取 `.env`；未安装依赖；未启动/关闭 Docker Desktop；`.idea/` 未修改未暂存；未推送。
