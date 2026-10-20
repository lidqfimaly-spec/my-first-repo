# Zevorinth Phase 3.30：run_tests 容器执行链验收与默认后端切换

## 1. 结论与基线

**PASS WITH LIMITATIONS。** `run_tests` 的默认后端已切换为 **container**；在本机真实容器里跑通了 `python -B -m unittest` 的通过与失败两条路径，证据来自 `EvidenceStore` 并在每次启动前实时重验。宿主机路径仅作为**显式** `execution_backend="host"` 保留给离线夹具，不做静默回退。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `4a4d375b32f03b9d404d8d6082f0162beff4064f` |
| 分支 | `phase-2.6-response-hardening` |
| 镜像 | `python:3.13-alpine` = `python@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a` |
| 运行时 | Docker 29.2.1 |

## 2. 执行链

```
TaskService._execute_test_tool
  ├─ 前快照（workspace）
  ├─ execution_backend == "container"（默认）
  │    ├─ 解析工作区 + 构造 argv: python -B -m unittest -q <target>
  │    ├─ 执行器自行派生 config_digest（整条 docker 命令）与 command_digest（argv）
  │    ├─ EvidenceStore.verify_stored_evidence(runtime, image, config, request_id=task_id, command)
  │    ├─ 非 verified → FAILED isolation_unverified（不启动、不回退）
  │    └─ subprocess.run(docker run ...) + 输出上限/截断标记
  ├─ 后快照 + 差异（沿用 3.17 副作用审计）
  ├─ TOOL_SUCCEEDED → VERIFYING → TestResultVerifier
  └─ SUCCEEDED / FAILED
```

关键点：`config_digest` / `command_digest` 由执行器从**即将执行的命令**派生，调用方提供的值不被信任。

## 3. 真实容器执行结果

集成测试 `tests/test_run_tests_container_live.py`（真实 docker，非 mock）：

| 场景 | 结果 |
| --- | --- |
| 通过的测试目标 | SUCCEEDED，`exit_code=0`，证据含 stdout/stderr |
| 失败的测试目标 | FAILED `tests_failed`，`exit_code=1` |
| 边界探针（同一镜像） | 读内 allowed、写内 allowed、越界读写 denied、网络 denied、凭据 denied、子进程 allowed（pids 限制） |

`-B` 用于阻止 `__pycache__` 写入 bind mount，避免被副作用审计判为 unexpected change（这是实现过程中真实发现的失败原因）。

## 4. 默认切换与显式 host

| 入口 | 默认 |
| --- | --- |
| `validate_parameters` / planner / service | `container` |
| CLI `zevorinth test` | 跟随引擎默认；新增 `--backend {container,host}`、`--evidence-id`、`--image` |
| 离线夹具（用户验收 B2、审计矩阵、3.16 专项） | 显式 `execution_backend="host"` 或 `--backend host` |

## 5. 失败关闭

- 证据缺失 / 过期 / 失效 / 请求不符 / 命令或配置不符 / 运行时或镜像变化 → FAILED `isolation_unverified`，`test_evidence is None`（测试未执行）。
- 非法 backend → BLOCKED `invalid_execution_backend`。
- 容器分支任何失败都不转去宿主机执行（测试断言无 host 产物）。

## 6. 测试与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 515/515，34.0s / 34.5s | 0 |
| 真实容器 run_tests 集成测试 | 1/1（真实 docker） | 0 |
| run_tests 容器门禁专项 | 5/5 | 0 |
| Mutation runner | 54 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

## 7. 真实 vs 模拟

- **真实容器**：`python:3.13-alpine` 内的 unittest 通过与失败、边界探针、证据实时重验、默认后端切换后的端到端。
- **模拟/静态**：证据缺失、过期、篡改、跨请求复用等负向路径（单元测试构造）。
- **未执行**：容器超时与取消的真实终止验证、容器内大输出截断、测试产物（报告文件）回收。

## 8. 安全边界

真实模型 API 请求 0；未读取 `.env`；未修改系统安全策略；`.idea/` 未修改未暂存；未删除 quarantine 或用户数据；未推送。

**环境变更（已获你此前全面授权）**：Docker Desktop 运行中；新增镜像 `python:3.13-alpine`（83.5 MB）与 `alpine:3.20`（13.7 MB）。

## 9. 残余风险

- 容器超时/取消的**真实**终止证据尚缺（`--rm` + `subprocess timeout` 会杀客户端，但未验证容器是否立即消失）。
- 测试产物（报告文件、日志引用）尚未回收；当前只记录 stdout/stderr 摘要与哈希。
- `run_tests` 的容器内输出上限只在客户端侧截断，容器内仍可能产生大输出。
- host 后端仍存在（显式），需要调用方自律；policy 不阻止显式 host。
- 证据 TTL 为 900s，长测试可能跨过期窗口（执行前校验通过即可，执行中不复查）。
