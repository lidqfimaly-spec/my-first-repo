# Zevorinth Phase 3.56：真实模型容器启动失败与生命周期一致性

## 1. 结论与基线

**PASS。** 唯一一次真实模型调用生成了合法 `run_tests` 候选；provisioning 正常完成，
容器启动原语被可控注入 `OSError`，任务明确失败为 `container_start_failed`。
没有测试证据、验证结果、容器或 `task_succeeded`；随后受控清理了 provisioning 产物。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `b6eb24803efb27e479914ff7c9fefc0fd3652753` |
| Provider / 模型 | DeepSeek / `deepseek-flash` |
| 授权请求上限 | **1** |
| 实际请求 | **1** |
| 自动重试 | **0** |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 真实执行与启动失败

| 观察项 | 实际值 |
| --- | --- |
| 响应 ID | `e94fdb10-f1eb-4573-90a9-c86b1755b0df` |
| 输入 / 输出 / 总 tokens | 741 / 145 / 886 |
| 候选校验 | `passed` |
| 计划工具 / 目标 | `run_tests` / `test_ok` |
| 任务状态 | `failed` |
| 任务阶段 | `tool_failed` |
| 错误码 | `container_start_failed` |
| 错误信息 | `OSError` |
| 工具名 | `run_tests` |
| `test_evidence` | null |
| `verification` | null |
| 容器记录 | 空 |
| `task_succeeded` 事件 | 不存在 |
| 宿主机回退 | 0 |

故障注入点是 `TaskService.container_popen`，因此这是**真实模型 + 可控启动失败**，
不是自然 Docker 故障。

## 3. 执行计划与证据

| 项目 | 值 |
| --- | --- |
| evidence ID | `auto-9511d4d2-6741-4489-9499-805e1512f689` |
| evidence digest | `2192bc6fed32c8fba0bba6dd3827725261c4d929a393f5cf59541111e065ba62` |
| config_digest | `2766b657e22f3445e1390d1f3df612f071275486fb9d9faf7c1fa30090289d87` |
| command_digest | `f53de1b7abdcd3d378eafa3f5a45cce37119df22ca69f312573ed061afc53024` |
| provisioning 探针 | 7 |
| 独立 evidence 复核 | `verified` |

最终执行计划在 `tool_started` 前持久化；容器未启动，因此没有测试证据或验证结果。

## 4. 清理与任务状态分离

| 观察项 | 结果 |
| --- | --- |
| `probe-written.txt` | `completed`，已 quarantine + unlink |
| reconcile | `reconciled_completed` |
| 重复清理 | `already_cleaned` |
| `inside.txt` | 保留 |
| 清理后任务状态 | 仍为 `failed` |
| cleanup events | `cleanup_intent → cleanup_completed → cleanup_rejected` |

清理不改变原任务失败状态，也不把启动失败误报为清理失败。

## 5. 事件序列

```text
task_created → task_claimed → planning_started → planning_completed
→ task_planned → execution_plan_finalized → tool_started
→ tool_failed → task_failed
```

再加上清理事件：

```text
cleanup_intent → cleanup_completed → cleanup_rejected（重复请求）
```

## 6. 已证明与未证明

### 已证明

- 真实模型候选通过严格校验；
- provisioning 正常完成，evidence 可独立复核；
- 容器启动失败有明确错误码，不产生虚假测试证据；
- 没有宿主机回退或 `task_succeeded`；
- provisioning 产物可在失败任务后受控清理；
- 清理不改变原任务失败状态。

### 未证明 / 限制

1. 启动失败是 `Popen` 注入的 `OSError`，不是自然 Docker 故障。
2. 单次样本不能证明所有启动失败形态。
3. 清理仍为显式调用。
4. `inside.txt` 所有权不明确，继续保留。
5. 历史任务迁移和自动清理仍未实现。
6. cleanup TOCTOU 窗口仍未证明彻底消除。

## 7. 测试与基线

| 检查 | 结果 |
| --- | --- |
| 启动失败专项（增强断言） | 1/1 |
| 预检专项 | 36/36 |
| Phase 3.53 系统 Python 全量 | 619/619 |
| Phase 3.53 `.venv` 全量 | 619/619 |
| Mutation runner | 71 killed / 0 survived |

本阶段仅增强了一个测试断言（启动失败时 `test_evidence` / `verification` 必须为
null，且必须出现 `task_failed`），没有修改产品代码。

## 8. 安全边界

- 真实模型 API 请求：1（授权上限 1）；
- 自动重试：0；
- 未改写模型输出；
- 未回退宿主机；
- 未读取 `.env`；密钥仅取自进程环境，未打印或落盘；
- 未联网安装依赖、未拉取镜像、未启停 Docker Desktop；
- `.idea/` 未修改、未暂存；
- 未推送远程仓库。

## 9. 产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.56/real-startup-failure-pilot.json` | `69e6f88764be0b70faabb52f22c822b2939d63fa2ec0ad0e5e3a3d8e988329ed` |
| `artifacts/audits/phase-3.56/stable-summary.json` | `1ba77228c89c8688883e6c5e0c8568fc00c08347a685c6fecabd70d3f151c315` |

## 10. 阶段判定

**PASS**：真实模型路径下的容器启动失败已明确失败关闭，没有虚假测试证据或验证结果；
provisioning 产物可在失败任务后受控清理，任务状态保持失败。
