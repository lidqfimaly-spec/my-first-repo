# Zevorinth Phase 3.46：真实模型端到端验证审计

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 唯一一次真实模型调用产出了合法的 `run_tests` 候选，
随后完成 provisioning、证据持久化与复核、真实容器执行和 `TestResultVerifier`
验证。主链路成立；仍有非关键的审计持久化限制。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `824b982e512b47cd1d8fb541c335def012088cac` |
| Provider / 模型 | DeepSeek / `deepseek-flash` |
| 授权请求上限 | **1** |
| 实际请求 | **1** |
| 自动重试 | **0** |
| 任务状态 | `succeeded` / `verified` |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 真实调用结果

| 观察项 | 实际值 |
| --- | --- |
| 响应 ID | `5975b5d0-b87b-4dea-bcda-d7b06c2799b1` |
| 输入 / 输出 / 总 tokens | 741 / 188 / 929 |
| 延迟 | 约 2.66s（含执行） |
| 候选校验 | `passed` |
| 计划工具 | `run_tests` |
| 测试目标 | `test_ok` |
| `auto_provision_isolation` | true |
| 容器名称 | `zv-049b3b52ef37-02542e1d` |
| 容器退出码 | 0 |
| 终止确认 | true |
| 测试退出码 | 0 |
| 独立 evidence 复核 | `verified` |
| `TestResultVerifier` | `local-test-result` / `tests_passed` |
| 任务内 workspace diff | added/deleted/modified/unexpected/unknown 全为 0 |

模型最终选择并表达了 `run_tests`，目标与预批准的 `test_ok` 一致。原始候选文本
没有落盘，因此这里以计划工具和目标作为模型行为的证据，而不是原始 JSON。

## 3. Provisioning 与证据

| 项目 | 值 |
| --- | --- |
| 探针数量 | 7 |
| evidence ID | `auto-049b3b52-ef37-4866-8216-7a9d3c8b9464` |
| evidence digest | `8b949733e2e3754fedd6f8b3763970d860e74720d088a5d969c5a5b2f573eafb` |
| `container_config_digest` | `5cd37e3c4b7257be1cffbd9d5097aae0ffcb6b95c8b71dab9d6f614774f24574` |
| `command_digest` | `f53de1b7abdcd3d378eafa3f5a45cce37119df22ca69f312573ed061afc53024` |
| image digest | `python@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a` |
| runtime identity | `29.2.1` |

Provisioning 在执行前快照之前完成；`probe-written.txt` 因而成为任务基线的一部分，
没有被误判为测试副作用。任务内 before/after diff 全零。

## 4. 执行链

```text
真实模型调用 1 次
  → LLM 候选校验通过（run_tests / test_ok）
  → provisioning（7 项边界探针）
  → evidence 持久化 + 执行器实时复核
  → 真实容器 zv-049b3b52ef37-02542e1d
  → 测试退出码 0
  → TestResultVerifier: tests_passed
  → task succeeded
```

事件序列：

```text
task_created → task_claimed → planning_started → planning_completed
→ task_planned → tool_started → tool_succeeded
→ verification_started → verification_completed → task_succeeded
```

## 5. 已证明与未证明

### 已证明

- 模型能产出当前严格协议接受的 `run_tests` 候选；
- 目标与预批准目标一致；
- `auto_provision_isolation` 从可信请求一路传递到执行器；
- provisioning 真实执行了 7 项探针；
- evidence 真实持久化，并能被独立复核为 `verified`；
- 容器真实启动并以退出码 0 结束；
- `TestResultVerifier` 独立返回 `tests_passed`；
- 没有宿主机回退、没有重试、没有 unexpected workspace change。

### 未证明 / 限制

1. **持久化计划未保留注入的 evidence_id**：执行器在 provisioning 后把
   `auto-<task_id>` 注入运行时参数，但持久化 plan 快照仍显示 `evidence_id=null`。
   证据通过 evidence store 和任务 ID 约定关联，审计可复核，但计划快照本身不完整。
2. **原始模型候选未持久化**：只能从计划工具/目标和诊断元数据证明模型行为。
3. **provisioning 留下 `probe-written.txt`**：它是 provisioning 探针产物，已纳入
   任务基线；任务 diff 为零，但 workspace 会多出该文件。
4. **单样本**：一次成功不能证明模型稳定性或所有提示措辞下的行为。
5. 未覆盖超时、取消、容器启动失败等本次未触发的分支。

## 6. 安全边界

- 真实模型 API 请求：1（授权上限 1）；
- 自动重试：0；
- 未改写模型输出；
- 未回退宿主机；
- `protected_processes_started_without_verified_isolation = 0`；
- 未读取 `.env`；密钥仅取自进程环境，未打印或落盘；
- 未联网安装依赖、未拉取镜像、未启停 Docker Desktop；
- `.idea/` 未修改、未暂存；
- 未推送远程仓库。

## 7. 建议的后续改进

1. 将 `evidence_id`、`container_name` 持久化到 `TestExecutionEvidence` 或计划快照，
   使审计不依赖 `auto-<task_id>` 命名约定；
2. 持久化候选的安全摘要（operation/classification/字段名），但不要保存完整原始
   候选；
3. 决定 provisioning 探针文件是否应在任务结束后清理或放入独立临时 workspace；
4. 在获得新授权后，再覆盖超时、取消、容器启动失败等真实分支。

这些改进不改变本次主链路的成功结论，但会提高后续审计的完整性。

## 8. 产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.46/pilot-result.json` | `177a53263e99c68a2c23dcd32c9f563edb33b137d845d8cbefc781f9d56a5c68` |
| `artifacts/audits/phase-3.46/stable-summary.json` | `f0c064e44ab7c81ea552674c2cc2ef6c127afa8002e860bbffb2ca67e511c5a4` |

## 9. 阶段判定

**PASS WITH LIMITATIONS**：真实模型 → 严格候选校验 → provisioning → 证据复核 →
真实容器 → 独立验证的主链路首次完整成立。限制集中在审计持久化与单样本覆盖，
不是安全边界或执行链失败。
