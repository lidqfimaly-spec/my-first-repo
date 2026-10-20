# Zevorinth Phase 3.51：真实模型执行后的受控清理验证

## 1. 结论与基线

**PASS。** 唯一一次真实模型调用完成了合法候选、最终执行计划、provisioning、
真实容器和独立验证；随后通过受控 API 清理了真实 provisioning 产物
`probe-written.txt`。`inside.txt` 因所有权不明确被保留，原任务状态保持
`succeeded`，重复清理幂等。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `c4092986f972cc24f433f7544fa50f9c5b13481d` |
| Provider / 模型 | DeepSeek / `deepseek-flash` |
| 授权请求上限 | **1** |
| 实际请求 | **1** |
| 自动重试 | **0** |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 真实执行结果

| 观察项 | 实际值 |
| --- | --- |
| 响应 ID | `2de0d815-e977-4030-8c3d-355b035b42a6` |
| 输入 / 输出 / 总 tokens | 741 / 134 / 875 |
| 候选校验 | `passed` |
| 计划工具 / 目标 | `run_tests` / `test_ok` |
| 最终计划 evidence_id | `auto-f107b7fc-b71d-42c1-93f2-534b35d1d432` |
| 最终计划 config_digest | `f6c9a66625f369c6fbac5b73b82e145086a91e739ff33b1f6617a6db2cb6a515` |
| 最终计划 command_digest | `f53de1b7abdcd3d378eafa3f5a45cce37119df22ca69f312573ed061afc53024` |
| 计划/证据比对 | `None`（一致） |
| 容器 | `zv-f107b7fcb71d-b94c99f0` |
| 容器退出码 | 0 |
| 测试退出码 | 0 |
| evidence 独立复核 | `verified` |
| `TestResultVerifier` | `tests_passed` |
| 任务内 workspace diff | 全零 |
| 任务状态（清理前/后） | `succeeded` / `succeeded` |

## 3. 受控清理结果

| 观察项 | 实际值 |
| --- | --- |
| 清理目标 | `probe-written.txt` |
| 所有权 | `provisioning` |
| cleanup intent | 已持久化 |
| 清理状态 | `completed` |
| quarantine | `.quarantine/<operation>/probe-written.txt` |
| 文件已删除 | true |
| 恢复核对 | `reconciled_completed` |
| 重复请求 | `already_cleaned`，removed=false |
| `inside.txt` | 保留 |
| 任务状态变化 | 无 |

事件：

```text
cleanup_intent → cleanup_completed → cleanup_rejected（重复请求）
```

这证明清理不会修改原任务最终状态，也不会把重复请求报告为新的删除。

## 4. 前置与回归

执行前 44 项针对性测试通过：

- 可信参数/最终执行计划；
- provisioning 清理；
- 真实容器 `run_tests`；
- 容器超时/取消/终止确认。

Phase 3.50 最终代码的全量基线为：

| 检查 | 结果 |
| --- | --- |
| 系统 Python 全量 unittest | 610/610 |
| `.venv` 全量 unittest | 610/610 |
| Mutation runner | 69 killed / 0 survived |

本阶段没有修改代码，仅执行真实调用与清理并记录审计。

## 5. 已证明与未证明

### 已证明

- 真实模型能产出合法 `run_tests` 候选；
- 最终执行计划在容器启动前持久化；
- provisioning、证据复核、真实容器和独立验证成功；
- 真实 provisioning 产物可在任务成功后按受控流程清理；
- `inside.txt` 因所有权不明确被保留；
- 重复清理幂等；
- 清理后原任务仍为 `succeeded`；
- 无宿主机回退、无重试、无 unexpected workspace change。

### 未证明 / 限制

1. 单次成功样本，不能证明所有提示措辞下的模型稳定性；
2. 清理 API 是显式调用，不会自动在任务结束时执行；
3. `inside.txt` 仍保留，需要后续明确所有权策略；
4. 未在真实模型路径下覆盖超时、取消、容器启动失败和验证器拒绝；
5. cleanup 原语的 TOCTOU 风险被缩小但未证明完全消除；
6. 历史任务没有最终执行计划时仍 fail-closed，未提供迁移。

## 6. 安全边界

- 真实模型 API 请求：1（授权上限 1）；
- 自动重试：0；
- 未改写模型输出；
- 未回退宿主机；
- 未读取 `.env`；密钥仅取自进程环境，未打印或落盘；
- 未联网安装依赖、未拉取镜像、未启停 Docker Desktop；
- `.idea/` 未修改、未暂存；
- 未推送远程仓库。

## 7. 产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.51/pilot-result.json` | `9c46eb58ec912a2a9b033f1e91b8b46479ed0eb161b3c955c45d1de41edbd13e` |
| `artifacts/audits/phase-3.51/stable-summary.json` | `62613003a2b51ac3b15ae7e8cbe7467685a2fe585fcbcd9430ae4502cd961496` |

## 8. 阶段判定

**PASS**：真实模型执行后的 provisioning 文件清理已通过真实容器、真实清理和
恢复核对；任务状态、幂等性和 `inside.txt` 保留策略符合 Phase 3.50 契约。
下一次真实调用仍需单独授权。
