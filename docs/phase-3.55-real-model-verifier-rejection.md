# Zevorinth Phase 3.55：真实模型验证器拒绝路径

## 1. 结论与基线

**PASS。** 唯一一次真实模型调用生成了合法 `run_tests` 候选；真实容器测试退出码为
0，但在独立验证前撤销 evidence，任务最终以 `evidence_invalidated` 失败，没有
`task_succeeded`。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `b2c871acc4d24e628a5addea8576f7117524bd1f` |
| Provider / 模型 | DeepSeek / `deepseek-flash` |
| 授权请求上限 | **1** |
| 实际请求 | **1** |
| 自动重试 | **0** |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 真实执行与拒绝结果

| 观察项 | 实际值 |
| --- | --- |
| 响应 ID | `e4ee91de-f811-40f4-8a57-7c6b5dcebdd0` |
| 输入 / 输出 / 总 tokens | 741 / 113 / 854 |
| 候选校验 | `passed` |
| 计划工具 / 目标 | `run_tests` / `test_ok` |
| 容器 | `zv-22883fdea736-68972669` |
| 容器退出码 | 0 |
| 测试退出码 | 0 |
| tool result success | true |
| evidence 撤销位置 | `after_test_before_verification` |
| verifier | `local-test-result` / `evidence_invalidated` |
| verifier passed | false |
| evidence 独立重验 | `blocked` / `evidence_invalidated` |
| 任务状态 | `failed` |
| 任务错误码 | `evidence_invalidated` |
| 计划/证据比对 | `None`（身份一致，但 evidence 已失效） |
| 任务内 workspace diff | 全零 |
| `task_succeeded` 事件 | 不存在 |
| 宿主机回退 | 0 |

事件序列：

```text
task_created → task_claimed → planning_started → planning_completed
→ task_planned → execution_plan_finalized → tool_started
→ tool_succeeded → verification_started
→ verification_completed → task_failed
```

## 3. 已证明与未证明

### 已证明

- 真实模型候选通过严格校验并进入真实容器；
- 容器和测试实际退出码为 0；
- 执行后重新验证 evidence 能识别撤销；
- 即使工具结果成功，独立验证器仍拒绝成功；
- 任务保持失败，没有 `task_succeeded`；
- 最终执行计划与 evidence 身份一致（`plan_mismatch=None`）；
- 没有宿主机回退。

### 未证明 / 限制

1. 本次拒绝是受控 evidence 撤销注入，不是模型或容器自然失败。
2. 真实模型下的容器启动失败仍未覆盖。
3. 单次样本不能证明所有验证器拒绝形态。
4. 本次未执行 provisioning 文件清理，`probe-written.txt` 留在临时工作区。
5. cleanup TOCTOU、历史迁移和自动清理仍未解决。

## 4. 测试与基线

| 检查 | 结果 |
| --- | --- |
| 预检专项 | 36/36 |
| Phase 3.53 系统 Python 全量 | 619/619 |
| Phase 3.53 `.venv` 全量 | 619/619 |
| Mutation runner | 71 killed / 0 survived |
| Phase 3.53 `compileall` / diff check | exit 0 |

本阶段没有修改产品代码，仅执行真实验证并记录审计。

## 5. 安全边界

- 真实模型 API 请求：1（授权上限 1）；
- 自动重试：0；
- 未改写模型输出；
- 未回退宿主机；
- 未读取 `.env`；密钥仅取自进程环境，未打印或落盘；
- 未联网安装依赖、未拉取镜像、未启停 Docker Desktop；
- `.idea/` 未修改、未暂存；
- 未推送远程仓库。

## 6. 产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.55/real-verifier-rejection-pilot.json` | `c2441f55f707e4ee187e81ef45f7f7a931d9e7f8814acb036e1ab7bf8959cece` |
| `artifacts/audits/phase-3.55/stable-summary.json` | `8709666305f4cb7edd761c05b8c88e61a50a796f414fe23eebcf2e55836f835a` |

## 7. 阶段判定

**PASS**：真实模型执行成功、测试退出码 0 的条件下，撤销 evidence 仍会触发独立
验证失败和任务失败。下一次真实模型调用仍需单独授权。
