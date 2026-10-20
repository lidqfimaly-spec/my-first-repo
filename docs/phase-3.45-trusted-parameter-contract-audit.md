# Zevorinth Phase 3.45：可信参数契约与离线执行闭环审计

## 1. 结论与基线

**PASS。** 已修复 Phase 3.44 暴露的两个契约问题：

1. 可信测试字段在规范化/计划构建过程中丢失；
2. `run_tests` 失败结果被错误记录为 `write_text_artifact`。

同时修正了 provisioning 与执行前快照的顺序，避免探针产物被误判为测试副作用。
离线伪候选 + 真实 provisioning + 真实容器 + 独立验证的闭环已经通过；本阶段没有
调用真实模型 API。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `18f2b1d363a0747161605f9a0778e41e7a20ba20` |
| 真实模型 API 请求 | **0** |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见最终报告。

## 2. 可信字段契约

`src/zevorinth/local_test_runner.py` 的 `validate_parameters()` 现在保留并校验：

| 字段 | 规则 | 权威来源 |
| --- | --- | --- |
| `auto_provision_isolation` | bool，默认 false；true 仅允许 container | 可信任务参数 |
| `evidence_id` | 1–128 位 `[A-Za-z0-9_-]` | 可信编排层 |
| `image` | 固定 tag/digest，安全字符集 | 可信任务参数 |
| `config_digest` / `command_digest` | 小写 64 位 hex；执行器仍自行派生 | 计划元数据 |
| `host_authorization` | 有界 token | 可信任务参数 |
| `required_artifacts` | ≤64 个相对路径，拒绝绝对路径、`..` 和空值 | 可信任务参数 |
| `disk_limit_bytes` | int ≥ 1 MiB | 可信任务参数 |
| `workspace_budget_bytes` | int ≥ 0 | 可信任务参数 |
| `execution_backend` | `container` 或 `host`；host 仍受 policy 授权 | 可信任务参数 |

模型候选仍然只能提供 `test_target`。`workspace`、镜像、后端、证据、预算和授权
字段不在候选 schema 中，模型无法覆盖。

`src/zevorinth/planning_validation.py` 在计划构建后逐个比较规范化字段与计划步骤
参数；任何字段丢失或被改写都会返回 `trusted_parameter_loss`，不会静默继续。

## 3. 失败工具名修复

`TaskService._finish_tool_failure()` 现在接收显式 `tool_name`：

- `run_tests` 的所有失败路径传入 `RUN_TESTS_TOOL`；
- 写文件路径传入 `self.writer.tool_name`；
- 未提供工具上下文时使用明确的安全哨兵 `unknown_tool`，不再默认冒充写文件工具。

回归测试覆盖：

- run_tests 失败记录 `tool_name=run_tests`；
- write_text_artifact 失败记录 `tool_name=write_text_artifact`；
- 缺失工具上下文记录 `unknown_tool`；
- 失败任务状态与工具结果保持一致。

## 4. Provisioning 顺序修复

首次离线闭环暴露了一个真实集成问题：

```text
provisioning 写入 probe-written.txt
→ 执行前快照在 provisioning 之前
→ verifier 将 probe-written.txt 判为 unexpected_workspace_change
```

修复方式：把自动 provisioning 移到执行前 workspace 快照之前。这样：

- 探针产物成为任务执行前基线的一部分；
- 测试本身产生的差异仍会被审计和验证；
- provisioning 失败仍在快照/容器启动之前失败关闭；
- 测试容器仍只使用已验证的证据。

`probe-written.txt` 是 provisioning 探针产物，不是测试业务副作用；任务内
before/after 快照的 diff 为全零。

## 5. 离线完整闭环

使用伪候选（不调用模型 API）+ 真实 provisioning + 真实容器执行：

| 观察项 | 结果 |
| --- | --- |
| Planner 客户端调用 | 1（本地伪客户端） |
| 真实模型 API 请求 | 0 |
| plan tool / target | `run_tests` / `test_ok` |
| `auto_provision_isolation` | true |
| Provisioning 探针 | 7 个 |
| 容器名称 | `zv-f5677fffc44b-189a94a0` |
| 容器退出码 | 0 |
| 终止确认 | true |
| 测试退出码 | 0 |
| `TestResultVerifier` | `local-test-result` / `tests_passed` |
| 独立 evidence 复核 | `verified` |
| evidence digest | `f5be820f5643d754fe59f5f641d2d324fae8935fa6abdc497bcbd08e3292106a` |
| `container_config_digest` | `f9ef13923840813ee0b65c142b86557f6b6ebda00b2ce7318fd44eb66a245c1b` |
| `command_digest` | `f53de1b7abdcd3d378eafa3f5a45cce37119df22ca69f312573ed061afc53024` |
| 任务状态 | `succeeded` |
| 任务内 workspace diff | added/deleted/modified/unexpected/unknown 全为 0 |
| 宿主机回退 | 0 |

容器使用 `--rm`，运行结束后不再出现在 `docker ps -a`；容器名称和退出码来自
执行器真实返回的 raw result。

## 6. 失败分支

| 分支 | 结果 |
| --- | --- |
| provisioning 失败 | 任务 `failed` / `provisioning_failed`；容器未启动；工具名为 `run_tests` |
| run_tests 执行失败 | 任务 `failed`；工具名为 `run_tests` |
| write_text_artifact 失败 | 任务 `failed`；工具名为 `write_text_artifact` |
| 缺失工具上下文 | 工具名为 `unknown_tool` |
| 缺失/无效 evidence | 既有测试仍拒绝执行，未回退宿主机 |
| 非布尔 auto 标志 | `invalid_auto_provision_isolation` |
| host + auto true | `invalid_auto_provision_isolation` |
| 非固定镜像/非法证据 ID/非法 artifact 路径 | 对应参数错误，拒绝进入执行 |

## 7. 测试与变异

| 检查 | 结果 |
| --- | --- |
| 新增可信参数/执行契约测试 | 19/19 |
| 相关执行、策略、证据、容器专项 | 162/162 |
| 系统 Python 全量 unittest | 597/597，exit 0 |
| `.venv` 全量 unittest | 597/597，exit 0 |
| Mutation runner | **62 killed / 0 survived / 0 invalid / 0 error** |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异：

- `auto_provision_isolation_forced_false` — killed；
- `run_tests_failure_tool_name_reverted` — killed。

## 8. 未验证事项与残余风险

1. 没有真实模型调用；伪候选只证明协议与执行链接口，不证明下一次真实模型行为；
2. `type` 字段的实际值和语义仍未确定；
3. provisioning 会在 workspace 留下 `probe-written.txt`；它已纳入任务基线，但
   仍是需要记录的 provisioning 产物；
4. 真实模型 + 真实容器的同一任务闭环仍需下一次单独授权验证；
5. 未覆盖所有供应商响应形态和所有 Docker 异常。

## 9. 安全边界

- 真实模型 API 请求：0；
- 未读取 `.env` 或输出密钥；
- 未访问互联网、未安装依赖；
- 未启动或关闭 Docker Desktop；
- 未修改、暂存或跟踪 `.idea/`；
- 未执行 reset / clean / rebase / amend / 分支切换；
- 未放宽 policy、隔离证据或独立验证门禁；
- 未发生宿主机回退。

## 10. 产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.45/offline-e2e.json` | `04b08f35cc4fbc63aa9d6a5790071a62aa1e4fc843c60e58bc92cfed86f12a1f` |
| `artifacts/audits/phase-3.45/mutations.json` | `3f0f7f52e6606e0fa8be74717fd39ed1ad20435369b77e41489b453673d6a715` |
| `artifacts/audits/phase-3.45/stable-summary.json` | `01965d959843cd6eff8440d23ed49296c1c5d1fe7d2b50da7996ba31445c4f43` |

## 11. 阶段判定

**PASS**：可信参数契约、失败工具名和 provisioning 顺序均已修复；离线真实容器
闭环通过；完整回归与 62 个变异全部通过；没有真实模型请求，也没有放宽任何安全门禁。

下一次真实调用仍需单独授权。届时如果候选通过，应分别记录候选校验、provisioning、
证据复核、容器执行和 `TestResultVerifier`，不能只凭任务状态报告成功。
