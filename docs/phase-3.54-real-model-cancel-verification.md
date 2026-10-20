# Zevorinth Phase 3.54：真实模型取消路径验证

## 1. 结论与基线

**PASS。** 唯一一次真实模型调用生成了合法 `run_tests` 候选；容器实际启动后由
操作者取消，容器终止确认、任务进入 `cancelled`，没有 `task_succeeded`，最终计划
与证据身份保持一致。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `58e841460a64fa15dbcf6dc4c5026798d389faf1` |
| Provider / 模型 | DeepSeek / `deepseek-flash` |
| 授权请求上限 | **1** |
| 实际请求 | **1** |
| 自动重试 | **0** |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 真实取消结果

| 观察项 | 实际值 |
| --- | --- |
| 响应 ID | `72fd86b3-a2ca-4e11-b8c5-9541a0f8c5a5` |
| 输入 / 输出 / 总 tokens | 741 / 124 / 865 |
| 候选校验 | `passed` |
| 计划工具 / 目标 | `run_tests` / `test_ok` |
| 取消前观察到的容器 | `zv-f7133c4ff957-3859a707` |
| 容器取消 | true |
| 容器超时 | false |
| 终止确认 | true |
| 容器退出码 | null（被终止） |
| 任务状态 | `cancelled` |
| 任务错误 | `cancelled / test execution was cancelled` |
| `TestResultVerifier` | `test_cancelled` |
| evidence 独立复核 | `verified` |
| 计划/证据比对 | `None`（一致） |
| 任务内 workspace diff | 全零 |
| `task_succeeded` 事件 | 不存在 |
| 宿主机回退 | 0 |

事件序列：

```text
task_created → task_claimed → planning_started → planning_completed
→ task_planned → execution_plan_finalized → tool_started
→ cancel_requested → tool_succeeded → verification_started
→ verification_completed → task_cancelled
```

## 3. 已证明与未证明

### 已证明

- 真实模型候选可以通过严格校验并进入容器执行；
- 容器实际启动后才发出取消；
- 取消检测触发真实容器终止，终止确认 true；
- 任务进入 `cancelled`，没有虚假成功；
- 最终执行计划与 evidence 身份一致；
- 未发生宿主机回退。

### 未证明 / 限制

1. 真实模型只验证了取消路径；容器启动失败和验证器拒绝仍未在真实模型下执行。
2. 本取消样本未执行 provisioning 文件清理，`probe-written.txt` 仍留在临时工作区。
3. 单样本不能证明所有取消时序。
4. cleanup TOCTOU、历史任务迁移和自动化清理仍未解决。
5. 一次预检组合测试首次出现 1 个未复现失败；随后同一命令连续 3 次 38/38 通过。
   该瞬时失败未用于通过证据。

## 4. 测试与基线

| 检查 | 结果 |
| --- | --- |
| 预检专项（连续复跑） | 38/38，3 次 |
| Phase 3.53 系统 Python 全量 | 619/619 |
| Phase 3.53 `.venv` 全量 | 619/619 |
| Mutation runner | 71 killed / 0 survived |
| Phase 3.53 `compileall` / diff check | exit 0 |

本阶段没有修改产品代码，仅执行真实取消验证并记录审计。

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
| `artifacts/audits/phase-3.54/real-cancel-pilot.json` | `701b16acbbd80aa4fe46cff94eeda00ffd0e16f0edf88ac3ba229e04968b66ae` |
| `artifacts/audits/phase-3.54/stable-summary.json` | `171229e6661e7a5621cbccf8cbccab87b2393c51ca9adacf5be3c1feac83bae1` |

## 7. 阶段判定

**PASS**：真实模型驱动的取消路径已完整闭环，容器终止、任务状态、独立验证和证据
身份一致。下一次真实模型调用仍需单独授权。
