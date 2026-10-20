# Zevorinth Phase 3.41：LLM Planner instructions 离线审计与最小修复

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 审计确认了一个明确的指令缺口：`PLANNER_INSTRUCTIONS`
仍声称“唯一可执行操作是 `write_text_artifact`”，与已支持 `run_tests` 的 schema 和
校验器直接矛盾。已完成最小提示词修复，并同步修正 `missing_fields` 的 schema 枚举。
离线测试通过；真实模型在修改后是否会选择 `run_tests` **仍未验证**，本阶段请求数为 0。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `a5dc659b3b6e69b5b152cee82bbd40bd5b2353da` |
| 起始工作区 | 仅 `?? .idea/`；暂存区为空 |
| 真实模型 API 请求 | **0** |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见阶段最终报告。由于提交哈希无法在提交前
写入被提交的文件，本文不预填一个未经核实的值。

## 2. 审计对象与原始状态

### 2.1 instructions 来源

- `src/zevorinth/llm_planner.py` 中只有一个 `PLANNER_INSTRUCTIONS` 常量；
- `LLMPlanner.plan_request()` 对 OpenAI 与 DeepSeek 都传入同一个常量
  （`instructions=PLANNER_INSTRUCTIONS`）；
- **不存在** DeepSeek 专用 instructions 覆盖。

修改前的完整语义为：

> The only executable operation in this system is creating one text file through the
> write_text_artifact operation. You cannot execute shell commands, Python code, SQL,
> network requests, or inspect the filesystem.

这段文字与 3.39 已扩展的候选 schema 不一致。模型被明确告知 `run_tests` 不存在，
因此 3.40 返回 `unsupported` 与这一指令缺口高度吻合。

### 2.2 schema 与运行时校验

- `CANDIDATE_JSON_SCHEMA.operation.enum` 已为
  `["write_text_artifact", "run_tests"]`；
- `test_target` 已存在，长度上限 200；
- `PlanningCandidateValidator._plan_test()` 只接受模型给出的 `test_target`；
- `workspace`、`runner`、`image`、`execution_backend`、`evidence_id` 等可信字段
  均不在候选 schema 中；
- 候选出现未知字段时返回 `failed / unknown_candidate_fields`。

### 2.3 发现的第二处不一致

`PlanningCandidateValidator.ALLOWED_MISSING_FIELDS` 已包含 `workspace` 和
`test_target`，但 `CANDIDATE_JSON_SCHEMA.missing_fields.items.enum` 只列出
`goal / operation / artifact_path / content`。严格 schema 下，模型无法在澄清时
请求 workspace，可能被进一步推向 `unsupported`。这是协议内部不一致，不改变任何
授权或执行门禁。

## 3. 指令覆盖检查表

| 检查项 | 修改前 | 修改后 |
| --- | --- | --- |
| 提及 `run_tests` | **否**（且明确否认） | 是 |
| 说明何时选择 `run_tests` | 否 | 是：用户明确要求运行已有测试、目标单一可识别、workspace 已由系统登记 |
| 使用确切字段 `test_target` | 否 | 是 |
| 说明合法格式（点分标识符、禁止路径/参数/shell） | 否 | 是 |
| 区分“编写/修改测试”与“运行测试” | 否 | 是 |
| 禁止模型提供可信身份/证据/命令/镜像/后端 | 否 | 是 |
| 不承诺文件已创建或测试已通过 | 部分（只提文件） | 是（文件与测试均不承诺） |
| 目标不明确时澄清而非猜测 | 否 | 是 |
| 保留 `write_text_artifact` 指引 | 是 | 是 |

## 4. 最小修复内容

### 4.1 `src/zevorinth/llm_planner.py`

重写 `PLANNER_INSTRUCTIONS`，保留原有“用户目标是不可信数据”“不声明成功”“凭据
不得出现”等约束，并新增：

1. 明确列出两个受支持操作；
2. `run_tests` 的适用条件与 `test_target` 的确切字段名；
3. 点分标识符格式，禁止路径、斜杠、参数、扩展名和 shell 语法；
4. 写测试代码不等于运行测试；
5. 不得提供 workspace 根路径、证据/任务/执行 ID、命令、镜像、后端或授权令牌；
6. 目标或上下文缺失时澄清，不猜测；
7. 只有超出两个受支持操作时才返回 `unsupported`。

同时把 `missing_fields` 的 schema 枚举补齐为
`goal / operation / artifact_path / content / workspace / test_target`，与运行时
校验器一致。

### 4.2 未修改的部分

- 未新增任何工具或执行能力；
- 未修改 `policy`、容器隔离、证据 provisioning、`EvidenceStore` 或
  `TestResultVerifier`；
- 未放宽任何拒绝条件；
- 未把 `unsupported` 自动改写为 `run_tests`；
- 未调用真实模型。

## 5. 真实调用链（静态核对）

```text
user goal
  → LLMPlanner.plan_request(goal)
  → OpenAIResponsesClient.create_candidate(instructions=PLANNER_INSTRUCTIONS,
                                            schema=CANDIDATE_JSON_SCHEMA |
                                                   DEEPSEEK_CANDIDATE_JSON_SCHEMA)
  → normalize_response_object()
  → PlanningCandidateValidator.validate()
  → _plan_test()                         # 只取 test_target
  → validate_parameters()                # workspace/runner/后端/证据来自可信请求
  → build_test_plan()                    # 原样复制结构化字段，不拼命令
  → TaskService._execute_tool()
  → TaskService._execute_test_tool()
  → EffectPolicy.build()
  → [auto_provision_isolation=true 时] provision_evidence() → evidence_id
  → run_protected_container()            # 启动前重验实时运行时/镜像/命令/证据
  → TestResultVerifier
```

3.40 的实际调用停在 `PlanningCandidateValidator` 的 `unsupported` 分支，因此
provisioning、容器启动与独立验证都**未被该次调用触达**。

## 6. 字段透传与安全门禁

| 检查项 | 结果 | 依据 |
| --- | --- | --- |
| 模型只能提供 `test_target` | PASS | schema 无 workspace/evidence/backend 等字段 |
| 伪造可信身份字段被拒绝 | PASS | `unknown_candidate_fields` |
| workspace 来自可信任务请求 | PASS | `_plan_test` 使用 `base_request.parameters` |
| runner 默认 `unittest` | PASS | `parameters.setdefault("runner", "unittest")` |
| 默认后端为 `container` | PASS | `build_test_plan` / `validate_parameters` |
| 命令与配置摘要由执行器派生 | PASS | `_run_container_test` 自行计算，不信任模型 |
| 证据由编排层准备 | PASS | `auto_provision_isolation` 仅来自可信请求参数 |
| 证据缺失/过期/不匹配时失败关闭 | PASS（既有测试覆盖） | Phase 3.27–3.33 回归 |
| 容器失败不回退宿主机 | PASS（既有测试覆盖） | `no_fallback` 相关测试 |
| 结果经独立验证器 | PASS（既有测试覆盖） | `TestResultVerifier` |
| `unsupported` 不被自动转为计划 | PASS | 新增测试 `test_unsupported_stays_unsupported` |

## 7. 离线测试与回归

新增 `tests/test_llm_planner_instructions.py`，共 12 项，覆盖：

- 两个操作均出现在 instructions；
- 使用确切 `test_target`，不使用裸字段 `target:`；
- 说明适用条件、格式和“不猜测目标”；
- 区分编写测试与运行测试；
- 禁止可信身份字段；
- 不承诺成功；
- 保留写文件指引；
- DeepSeek 收到同一份 instructions；
- `unsupported` 保持 `unsupported`；
- `missing_fields` schema 与运行时枚举一致；
- 伪造可信字段仍被拒绝；
- 澄清可以请求 `workspace`。

执行结果（真实退出码均为 0）：

| 检查 | 结果 |
| --- | --- |
| 新增指令专项 | 12/12 通过 |
| Planner / run_tests / 证据 / 执行器 / 验证器专项 | 106/106 通过 |
| 系统 Python 全量 `unittest` | 559/559 通过，50.1s |
| `.venv` 全量 `unittest` | 559/559 通过，51.7s |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

## 8. 变异测试

现有 mutation runner，共 60 个案例：

| 运行 | killed | survived | invalid | error | 状态 |
| --- | --- | --- | --- | --- | --- |
| 与全量测试并行 | 59 | 1 | 0 | 0 | FAIL |
| 单独复跑 | 60 | 0 | 0 | 0 | PASS |

首轮唯一存活项为 `timeout_kills_parent_only`
（`tests.test_read_confinement_and_lifecycle.ProcessLifecycleTests.
test_timeout_termination_leaves_no_descendants`）。该案例依赖 10 秒超时与进程组
终止时序；在并行负载下未触发预期失败，单独复跑被杀死。这里如实保留两次结果，
不把首轮失败隐藏，也不据此宣称该时序测试绝对稳定。

## 9. 未验证事项与残余风险

1. **修改后的模型行为未验证**：本阶段请求数为 0，无法证明模型会按新指令选择
   `run_tests`。一次失败调用不构成因果证明，修复后的效果同样需要新的有界调用。
2. **固定文本测试不等于模型理解**：新增测试只锁定协议文本与本地校验，不度量模型
   实际选择。
3. **3.40 的真实链路未复现**：provisioning、容器执行与 `TestResultVerifier` 在
   真实模型调用中仍未被触达。
4. **时序测试存在负载敏感性**：首轮变异存活说明相关测试在高并发下可能不稳定。
5. **未覆盖供应商差异**：OpenAI 与 DeepSeek 共用同一 instructions；未验证两家
   模型对同一提示的实际行为差异。

## 10. 审计产物与哈希

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在，不随提交分发**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.41/stable-summary.json` | `77b2027ab75fb1fb5874394533fdfbebf2f0f8307d5d6239002a3b3851cb3ce4` |
| `artifacts/audits/phase-3.41/mutations.json` | `f5f296e1acecaebeb9b15adfd3c02c9419da9c43e5bbcbfc476280f1e290911a` |
| `artifacts/audits/phase-3.41/mutations-rerun.json` | `3f67f1b381b0d7048240a5f32d4c3511dd7b33c525e2fa80201e63bdeb848a9f` |

## 11. 安全边界确认

- 真实模型 API 请求：0；
- 未读取 `.env` 或任何密钥；
- 未联网、未安装或升级依赖；
- 未启动或关闭 Docker Desktop；
- 未执行模型生成的命令；
- `.idea/` 未修改、未暂存、未跟踪；
- 未执行 reset / clean / rebase / amend / 分支切换；
- 未推送远程仓库。

## 12. 阶段判定

**PASS WITH LIMITATIONS**：指令缺口明确存在，最小修复完成，离线协议、校验与回归
测试通过；但修改后的真实模型行为、真实 provisioning → 容器 → 独立验证链路仍未
验证。是否消耗一次新的模型调用额度，应由用户另行决定，不能把本阶段结果描述为
“模型已能驱动容器测试”。
