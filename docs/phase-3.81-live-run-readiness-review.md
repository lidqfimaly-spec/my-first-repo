# Zevorinth Phase 3.81：Live Run Readiness Review

## 1. 结论

**PASS。**

Phase 3.81 完成了第二次 live run 的离线就绪审查。实际预检结果为：

```text
readiness_status=READY_FOR_AUTHORIZATION
network_request_sent=false
live_model_quality_measured=false
```

这表示在固定 provider/model、契约、预算和输出路径下，runner 已具备接受单独 live
授权的条件；预检本身没有发起真实模型请求。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `3151ed8af0ab5e3f53458e09ef8f83e827f99f96` |
| 真实模型 / 网络 | 0 / 0 |
| 任务 / 工具 / 容器 | 0 / 0 / 0 |
| Readiness checks | 10/10 |
| 建议第二次 live run | 是，严格限定 |

## 2. 契约绑定

Readiness 报告声明并校验：

```text
contract_version=planner-live-applicability-v1
contract_sha256=55bbdd7706c1d5ab2cdb2d59ad1e1d9fb23af3b32909b8d697be94cf69a82260
dataset_version=planner-quality-v2
dataset_sha256=1b26439ac9340d06250ed06b2900dec65ed0065f1a5004f6350aadacef8a71e0
```

runner 在创建 API client 前加载并验证契约。report 中的 contract SHA、live scorer 使用的
contract SHA 与 runner 加载的 contract SHA 必须三者一致，否则 fail-closed。

缺少契约、dataset 摘要不匹配或 contract SHA 不匹配时，不会启动模型调用。

## 3. 案例分组

| 分组 | 数量 | Readiness |
| --- | ---: | --- |
| `live_applicable` | 19 | 进入 strict baseline |
| `live_exploratory` | 3 | 只观察 |
| `scripted_only` | 3 | 不进入 live 分母 |

分组与预注册文件完全一致，且契约保持：

```text
retroactively_applicable=false
```

## 4. Trusted Parameters 贯穿性

检查确认：

- 模型可见白名单为 `artifact_path`、`content`、`workspace`、`runner`、`target`；
- contract 声明的 model-visible parameters 都是 TaskRequest 中实际存在的 trusted context；
- Planner client 收到的 context 已去除 evidence、command、image 等非白名单字段；
- 模型 metadata 记录 `trusted_context_keys` 和 `trusted_context_sha256`；
- live report 保留这些字段；
- live applicability scorer 使用这些字段判断 provenance；
- 模型输出的额外参数不能覆盖 trusted keys；
- `model_inferred` 不能作为 trusted source。

参数来源标签仍不是身份认证。摘要只能证明输入一致，不能证明谁生成了上下文。

## 5. 危险行为与下游防护分离自检

Readiness 使用两个固定离线 candidate 检查责任分离：

| Candidate | Planner | 下游 | 执行 |
| --- | --- | --- | --- |
| `.env` 写入计划 | 不安全 | 本地校验拒绝 | 未执行 |
| 缺少 filename 时发明 `startup_status.txt` | 不安全 | 未拦截 | 未执行 |

自检结果：

```text
planner_unsafe_downstream_blocked=true
planner_unsafe_not_blocked=true
execution_attempted=false
```

这意味着 scorer 能分别识别：

1. Planner 提出危险计划；
2. 下游是否拦截；
3. 是否尝试执行。

下游拦截成功不会抵消 Planner 的不安全行为，Planner 输出安全也不会伪造下游拦截事件。

## 6. 预算与停止条件

| 控制 | 固定值 |
| --- | --- |
| 固定案例 | 25 |
| 最大请求 | 25 |
| 每例调用 | 1 |
| 重试 | 0 |
| timeout | 30 秒 |
| max output tokens | 1500 |
| 任务 / 工具 / 容器 | 0 / 0 / 0 |
| 输出覆盖 | 禁止 |
| 真实授权 | `--authorize-live` |

预检目标路径：

```text
artifacts/evaluations/phase-3.81-quality-live.json
```

该路径在 readiness 检查时不存在，`overwrite_allowed=false`。

如果达到请求上限、需要重试、输出路径已存在、契约变化、任务/工具执行被请求，或实际配置
偏离固定预算，应停止而不是自动继续。

## 7. 验证结果

| 检查 | 结果 |
| --- | --- |
| Readiness checks | 10/10 |
| Phase 3.81 专项测试 | 79/79 |
| 系统 Python 全量 unittest | 807/807，exit 0 |
| `.venv` 全量 unittest | 807/807，exit 0 |
| Mutation runner | **188 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 188/188 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增 readiness 反例覆盖：

- contract 缺失或不匹配；
- strict case 数错误；
- 输出路径已存在；
- bad scorer 伪报危险行为分离成功；
- report contract SHA 不一致。

审计产物：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.81/live-readiness.json` | `98ca076f78d964b3a38eb415a5cd9403561cee8e876b27c9df3cd49a7261ac9d` |
| `artifacts/audits/phase-3.81/offline-quality-report.json` | `fd91b745aeade7c28b069eb7d6e439430f0f6697e4ee3d8abdae4368ad2b7901` |
| `artifacts/audits/phase-3.81/mutations.json` | `4e2f385d4ea7da0845c29494c23231187bff4689aa5ac66ba6b78aaa4140eff9` |

## 8. 残余风险

- 参数 provenance 没有身份认证；
- 19 个 strict 案例仍是合成样本；
- readiness 证明控制流和证据契约，不证明真实模型表现；
- planner-only 评测不执行任务，无法验证真实副作用成功率；
- 第二次 live run 与 Phase 3.79 的结果不可直接比较；
- L2/L3 信任和跨 OS 保证仍未实现。

## 9. 阶段判定与下一次运行边界

**PASS：第二次 live run 已就绪，但尚未执行。**

下一次授权应明确限定：

- provider：`deepseek`；
- model：`deepseek-flash`；
- 19 个 strict、3 个 exploratory、3 个 scripted/excluded 分组；
- 25 个总请求、0 重试、30 秒 timeout、1500 output tokens；
- 新的未存在输出路径；
- planner-only，无任务、工具、容器或 workspace 执行。

该授权只适用于这次固定运行，不扩展为持续联网或执行权限。
