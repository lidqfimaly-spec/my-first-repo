# Zevorinth Phase 3.48：真实模型下的最终执行计划一致性验证

## 1. 结论与基线

**PASS。** 唯一一次真实模型调用完成了：

```text
合法候选 → 最终执行计划持久化 → provisioning → 证据复核
→ 真实容器 → 独立验证 → task_succeeded
```

最终执行计划中的 `evidence_id`、`config_digest`、`command_digest` 与实际执行证据
完全一致，`plan_mismatch=None`。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `1806acb885c57e9f3540252386458046533b2d7b` |
| Provider / 模型 | DeepSeek / `deepseek-flash` |
| 授权请求上限 | **1** |
| 实际请求 | **1** |
| 自动重试 | **0** |
| 任务状态 | `succeeded` / `verified` |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 真实调用结果

| 观察项 | 实际值 |
| --- | --- |
| 响应 ID | `165c1d7f-95ba-41ea-bc5b-e771f254049f` |
| 输入 / 输出 / 总 tokens | 741 / 173 / 914 |
| 候选校验 | `passed` |
| 计划工具 / 目标 | `run_tests` / `test_ok` |
| 最终计划 evidence_id | `auto-3f4eedc5-3a78-4a82-9ebc-ba078c16d1b7` |
| 最终计划 config_digest | `13dbd3aefd06e3a4ef20469cecae7ae4a418662f51b7e11d0e6e2e147db1bc3a` |
| 最终计划 command_digest | `f53de1b7abdcd3d378eafa3f5a45cce37119df22ca69f312573ed061afc53024` |
| 计划/证据比对 | `None`（一致） |
| Provisioning 探针 | 7 |
| 容器 | `zv-3f4eedc53a78-cdb26278` |
| 容器退出码 | 0 |
| 终止确认 | true |
| 测试退出码 | 0 |
| 独立 evidence 复核 | `verified` |
| evidence digest | `10893c755a05f138fc49a98f999d429994425d70f771c6246cb27095a889dd17` |
| `TestResultVerifier` | `local-test-result` / `tests_passed` |
| 任务内 workspace diff | 全零 |
| 宿主机回退 | 0 |

## 3. 计划与执行记录的区分

`task.plan.steps[0].parameters.evidence_id` 仍为 `null`；这是**批准计划**的字段。
实际执行使用并持久化在 `execution_plan_snapshot` 中：

```text
kind=run_tests
task_id=3f4eedc5-3a78-4a82-9ebc-ba078c16d1b7
attempt_id=dd6dd185-67e7-448a-99e2-89a8b7640852
claim_generation=1
plan_version=0
evidence_id=auto-3f4eedc5-3a78-4a82-9ebc-ba078c16d1b7
config_digest=13dbd3ae...
command_digest=f53de1b7...
```

这符合 Phase 3.47 的设计：模型候选、批准计划和最终执行记录是三个不同对象。
最终执行记录才是“实际使用了哪份证据、哪条命令”的权威来源。

## 4. Provisioning 文件

最终执行计划记录：

| 文件 | 所有权 | 保留 | 清理 |
| --- | --- | --- | --- |
| `inside.txt` | workspace 输入或 provisioning | workspace baseline | 延迟到受控清理 |
| `probe-written.txt` | provisioning | workspace baseline | 延迟到受控清理 |

两个文件都在 provisioning 完成后、执行前快照之前存在，因此任务内 diff 为零。
本阶段没有删除任何 provisioning 文件。

## 5. 事件顺序

```text
task_created → task_claimed → planning_started → planning_completed
→ task_planned → execution_plan_finalized → tool_started
→ tool_succeeded → verification_started → verification_completed
→ task_succeeded
```

`execution_plan_finalized` 位于 `tool_started` 之前，证明最终执行计划在容器启动前
已经持久化。

## 6. 前置与回归

执行前 41 项针对性测试通过：

- 可信参数/最终执行计划契约；
- 候选诊断；
- 真实容器 `run_tests` 测试。

Phase 3.47 最终代码的全量基线为：

| 检查 | 结果 |
| --- | --- |
| 系统 Python 全量 unittest | 599/599 |
| `.venv` 全量 unittest | 599/599 |
| Mutation runner | 64 killed / 0 survived |

本阶段没有修改代码，仅执行真实调用并记录审计。

## 7. 已证明与未证明

### 已证明

- 真实模型能产出合法 `run_tests` 候选；
- 最终执行计划在容器启动前持久化；
- 计划中的 evidence_id、config_digest、command_digest 与真实证据一致；
- provisioning 的 7 项探针真实执行；
- evidence 独立复核为 `verified`；
- 真实容器启动并退出码 0；
- `TestResultVerifier` 返回 `tests_passed`；
- 没有宿主机回退、没有重试、没有 unexpected workspace change。

### 未证明 / 限制

1. 单次成功样本，不能证明模型在所有提示措辞下的稳定性；
2. provisioning 文件清理尚未执行，只记录了延迟清理策略；
3. `type` 字段语义仍未确定；
4. 本阶段未覆盖真实模型路径下的超时、取消、容器启动失败和验证器拒绝分支；
5. 历史任务没有新的最终执行计划结构时会 fail-closed，尚未提供迁移。

## 8. 安全边界

- 真实模型 API 请求：1（授权上限 1）；
- 自动重试：0；
- 未改写模型输出；
- 未回退宿主机；
- `protected_processes_started_without_verified_isolation = 0`；
- 未读取 `.env`；密钥仅取自进程环境，未打印或落盘；
- 未联网安装依赖、未拉取镜像、未启停 Docker Desktop；
- `.idea/` 未修改、未暂存；
- 未推送远程仓库。

## 9. 产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.48/pilot-result.json` | `66425574dfd473547e8a42d8c4134a9d22400cd8e87fbce4fd115bc6a8807f77` |
| `artifacts/audits/phase-3.48/stable-summary.json` | `964bba61d54450f75d8aa45e449f5fbb1fa3f0271058f6753acef0295389a784` |

## 10. 阶段判定

**PASS**：最终执行计划与真实证据在真实模型调用中完全一致，容器执行与独立验证
成功，安全边界保持不变。残余限制集中在单样本覆盖和 provisioning 文件清理策略，
不构成本次执行链失败。
