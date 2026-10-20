# Zevorinth Phase 3.44 — 单次真实模型端到端验证任务书

## 0. 当前状态

**任务书状态：未执行，等待单独授权。**

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 基线 HEAD | `a9943c92fd5f1f5f6092e6411f681d4325816fbd` |
| 上一阶段 | Phase 3.43（PASS） |
| 真实模型 API 请求 | **0**（本任务书准备阶段） |
| 离线基线 | 578/578 全量测试；诊断专项 19/19；变异 60/60 |

本任务书只定义下一次受控验证，不代表 `run_tests` 已由真实模型完成端到端执行。

## 1. 目标

用**一次**真实模型调用，回答两个彼此独立的问题：

1. **候选协议问题**：模型能否产出通过当前严格校验的 `run_tests` 候选？
2. **执行链问题**：合法候选能否获得隔离证据、在已验证容器中运行测试，并由
   `TestResultVerifier` 独立确认结果？

不能把“候选通过”当作“执行成功”，也不能把“任务状态为 `SUCCEEDED`”当作整条链路
已经验证。

## 2. 固定范围

| 项目 | 固定值 |
| --- | --- |
| Provider / 模型 | DeepSeek / `deepseek-flash` |
| 真实请求上限 | **1** |
| 自动重试 | **0** |
| 目标 workspace | `demo`（临时隔离根目录中预置） |
| 测试目标 | `test_ok` |
| 执行后端 | `container` |
| 镜像 | 本地已有 `python:3.13-alpine`；禁止拉取 |
| 隔离证据 | `auto_provision_isolation=true`，由可信编排层准备 |
| 宿主机回退 | **禁止** |
| 允许的文件生成 | 仅测试 fixture 与运行产物 |

固定用户目标：

```text
Run the unit test module test_ok from the demo workspace.
```

固定可信参数：

```json
{
  "workspace": "demo",
  "auto_provision_isolation": true
}
```

模型只能提供 `test_target`；workspace、runner、镜像、后端、证据 ID、任务身份和
命令摘要都必须来自可信编排层或执行器。

## 3. 授权边界

只有用户明确回复类似以下内容后才可执行：

> 执行 Phase 3.44：授权 1 次真实模型请求，0 重试；仅允许 demo/test_ok；仅容器后端；
> 禁止宿主机回退。

执行期间禁止：

- 读取 `.env`、输出密钥或认证头；
- 联网、安装/升级依赖、拉取镜像；
- 启动或关闭 Docker Desktop；
- 修改、暂存或跟踪 `.idea/`；
- 执行 reset / clean / rebase / amend / 分支切换；
- 重试、改写模型输出、手工合成候选或自动删除未知字段；
- 在候选未通过严格校验时启动 provisioning 或容器；
- 在容器失败时回退宿主机执行；
- 扩大目标、workspace、镜像或执行后端范围；
- 把模拟、静态检查或未执行项目报告为真实结果。

## 4. 前置离线基线

在执行真实调用前，必须完成并记录：

1. `git rev-parse HEAD` 与工作区状态；基线不符则停止；
2. 系统 Python 全量 `unittest`；
3. `.venv` 全量 `unittest`；
4. 候选诊断专项（合法写文件、合法 `run_tests`、未知 `type`、缺失字段、非法操作）；
5. `tests.test_llm_planner`、`tests.test_llm_run_tests_capability`；
6. 证据 provisioning、容器生命周期、`TestResultVerifier` 相关专项；
7. `compileall`、`git diff --check`；
8. 确认本地镜像存在，且不执行拉取；
9. 确认 `DEEPSEEK_API_KEY` 在进程环境中存在（只打印布尔值，不打印内容）。

任何一项失败都停止，不进入真实调用。

## 5. 执行步骤

### Step 0：隔离环境

- 使用 `mktemp -d` 创建临时项目根目录；
- 创建 `workspaces/demo/test_ok.py`，内容为一个确定通过的 `unittest`；
- 预置 `inside.txt`，避免 provisioning 写入被误判为业务副作用；
- 记录调用前 workspace 文件清单与 SHA-256。

### Step 1：唯一一次模型调用

- 通过 `TaskService(planner=LLMPlanner.from_environment())` 提交固定目标；
- 客户端 `max_retries=0`；
- 记录请求数、响应 ID（安全字段）、token 数、候选安全形状；
- **不得重试**。

### Step 2：候选校验门禁

只有同时满足以下条件才继续：

- `classification == "plan"`；
- `operation == "run_tests"`；
- `test_target == "test_ok"`；
- 候选没有未知字段或伪造可信字段；
- 本地校验结果为 `planned`。

如果候选被拒绝：

- 记录 `candidate_fields`、`candidate_classification`、`candidate_operation`、
  错误码和未知字段；
- 不删除 `type` 或任何未知字段；
- 不 provisioning、不启动容器、不重试；
- 结论停在协议阶段。

### Step 3：隔离证据 provisioning

候选通过后，由可信编排层执行：

- 运行七项边界探针；
- 生成并持久化 evidence；
- 记录 evidence ID、digest、TTL、镜像 digest、运行时身份；
- provisioning 失败则停止；不得用宿主机执行替代。

### Step 4：容器执行

- 执行器重新计算 `config_digest`、`command_digest`；
- 从 `EvidenceStore` 读取并实时验证证据；
- 启动带任务专属名称的容器；
- 记录容器名称、启动时间、退出码、stdout/stderr 字节数与 SHA-256；
- 超时或取消时确认容器终止；无法确认则失败关闭；
- **不得回退宿主机**。

### Step 5：独立验证

- 由 `TestResultVerifier` 独立复核；
- 记录 `verifier_name`、`passed`、`reason`；
- 只有测试退出码为 0 且 verifier 通过，才可报告测试成功；
- 验证失败必须保持失败状态，不能被任务状态覆盖。

### Step 6：独立审计

分别记录，不得合并成一个“成功”：

- 模型候选是否有效；
- 候选是否绑定到预批准目标；
- evidence 是否由可信层准备并通过实时验证；
- 容器是否实际启动；
- 测试退出码；
- `TestResultVerifier` 结果；
- 产物清单、哈希、大小；
- workspace 副作用与预期差异；
- 容器是否终止、是否有残留。

## 6. 状态映射

| 结果 | 含义 |
| --- | --- |
| `planned` | 候选通过本地校验，尚未执行 |
| `provisioning_failed` | 证据准备失败，未启动容器 |
| `isolation_unverified` | 证据缺失/过期/不匹配，拒绝启动 |
| `container_start_failed` | 容器未启动 |
| `tests_failed` | 容器运行但测试退出码非 0 |
| `verification_failed` | 测试结果未通过独立验证 |
| `succeeded` | 容器实际运行、退出码 0、verifier 通过、无意外副作用 |
| `blocked` | 策略、授权或依赖阻止继续 |
| `cancelled` | 用户取消 |
| `not_executed` | 未实际执行目标操作 |
| `simulated` | 仅模拟，不得计入真实成功 |

## 7. 必须记录的证据

- `task_id`、`attempt_id`、`claim_generation`、`request_id`；
- provider、model、请求数、重试数、响应 ID、token 数；
- 候选 `classification`、`operation`、`test_target`、字段名列表；
- 校验结果与错误码；
- `evidence_id`、evidence digest、TTL、`image_digest`、runtime identity；
- `config_digest`、`command_digest`；
- 容器名称、启动/终止确认；
- 测试退出码、stdout/stderr 字节数与 SHA-256；
- artifact 相对路径、大小、SHA-256；
- `TestResultVerifier` 结果；
- workspace before/after 清单与副作用计数；
- 事件序列（`planning_*`、`test_*`、`verification_*`）。

## 8. 验收标准

### PASS

一次调用返回合法 `run_tests` 候选，目标为 `test_ok`；候选通过严格校验；provisioning
成功；容器实际启动；测试退出码 0；`TestResultVerifier` 通过；无宿主机回退；无意外
副作用；离线基线全部通过。

### PASS WITH LIMITATIONS

主链路成立，但存在已披露的非关键限制，例如原始候选未持久化、仅单样本、未覆盖
失败/取消路径。不得把未验证能力描述成已验证。

### BLOCKED

模型未产出合法候选、候选被未知字段拒绝、provisioning 不可用、容器无法启动，或
关键证据校验失败。保留现状，不为通过验收而放宽门禁。

### FAIL

任何安全门禁被绕过、发生宿主机回退、把失败/未知报告为成功、出现未授权副作用，
或审计记录与实际结果不一致。

## 9. 指标

- `real_model_requests`（必须 ≤ 1）
- `retries`（必须为 0）
- `candidate_received`
- `candidate_valid`
- `candidate_operation`
- `provisioning_attempted`
- `provisioning_passed`
- `container_started`
- `container_exit_code`
- `test_verifier_passed`
- `host_fallback_attempts`（必须为 0）
- `protected_processes_started_without_verified_isolation`（必须为 0）
- `unexpected_workspace_changes`（必须为 0）
- `evidence_digest_matches`
- `artifacts_collected`

指标必须从任务、事件、证据和磁盘结果独立复算，不能只读取运行器摘要。

## 10. 停止条件

遇到以下任一情况立即停止：

- 基线不一致或工作区有未预期改动；
- 需要联网、安装依赖、读取密钥或修改系统配置；
- 模型未返回合法候选，或候选含未知/伪造字段；
- 目标与预批准目标不一致；
- provisioning 失败；
- evidence 缺失、过期、被篡改或身份不匹配；
- 容器无法启动；
- 超时/取消后无法确认容器终止；
- `TestResultVerifier` 失败；
- 出现任何宿主机回退迹象；
- 出现未预期文件副作用。

停止后不得自动重试，不得进入下一阶段。

## 11. 交付物

执行后新增：

- `docs/phase-3.44-end-to-end-verification-audit.md`
- `artifacts/audits/phase-3.44/pilot-result.json`（本地，`.gitignore` 排除）
- `artifacts/audits/phase-3.44/stable-summary.json`（本地，SHA-256）

建议提交信息：

```text
feat: verify one model-driven container test end to end
```

若候选在协议阶段被拒绝，只提交审计文档与诊断结果，不伪造执行成功。

## 12. 明确不做

- 不规范化或删除 `type` 字段；
- 不放宽 `unknown_candidate_fields`；
- 不迁移 `run_tests` 之外的执行路径；
- 不扩大模型试点范围；
- 不增加第二个测试目标；
- 不修改 `.idea/`；
- 不推送远程仓库。
