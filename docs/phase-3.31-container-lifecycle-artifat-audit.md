# Zevorinth Phase 3.31：容器生命周期、产物回收与 host 授权

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 三项优先问题均已落地并有真实/单元证据：容器终止有"不在运行"的确认、测试产物按路径与哈希回收、显式 host 后端需授权令牌否则被 policy 拒绝。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `73d8a628ff8314cf2b6f26dfeca248a9013b20af` |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS，Python 3.14.6，Docker 29.2.1 |

## 2. 容器生命周期

`_run_container_test` 现在：

1. 为每次执行生成唯一容器名 `zv-<task12>-<rand8>` 并 `--name` 固定；
2. 超时时调用 `_terminate_container(name)`：`docker kill` → 轮询 `docker ps --filter name=`（**只列运行中**）最多 5s；
3. 只有"不再运行"才返回 `terminated_confirmed=true`；无法确认 → `TestExecutionError("container_termination_unverified")`，任务失败关闭；
4. 正常路径同样记录 `container_name` 与产物。

**真实测试**（`tests/test_run_tests_lifecycle.py`，无 mock）：启动一个 `sleep 60` 容器，调用终止逻辑，断言 `docker ps --filter name=` 输出为空；随后 `docker rm -f` 清理测试容器。

注意：`docker ps -a` 仍可能短暂列出已停止未删除的容器——"终止"的判据是**不在运行**，这一点在文档与测试中明确。

## 3. 测试产物回收

`_collect_test_artifacts(workspace, limit)`：

- 仅扫描 `<workspace>/artifacts`；
- 跳过符号链接，且要求 `resolve()` 后仍在 root 之内（双重防护）；
- 单文件与总量均受 `max_output_bytes` 限制；
- 记录 `relative_path`、`bytes`、`sha256`；
- 结果写入测试证据的 `artifacts` 字段。

单元测试覆盖：正常收集、越界符号链接被跳过、超限不收集。变异 `artifact_symlink_escape_collected` 最初被"containment 检查"掩盖（survived），改为同时移除两处防护的复合变异后被杀。

## 4. host 后端授权

policy 新增：`execution_backend == "host"` 时必须携带 `host_authorization == "local-maintenance"`，否则 BLOCKED `host_execution_not_authorized`，**进程不启动**。

> **已被取代（S1，2026-11-21）**：`local-maintenance` 常量已删除。宿主机执行改用一次性人工签发凭据，缺凭据的拒绝码变为 `host_authorization_required`。见 `docs/product-implementation-roadmap.md` 的 S1 段。

CLI 同步新增 `--allow-host`（映射到该令牌）；离线夹具显式使用 `--allow-host`。

测试：无令牌 → BLOCKED 且无 `tool_result`；错误令牌 → BLOCKED；正确令牌 → 正常执行。

## 5. 测试与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 523/523，35.4s / 36.9s | 0 |
| 生命周期/授权/产物专项 | 8/8 | 0 |
| 真实容器终止测试 | 1/1（真实 docker） | 0 |
| Mutation runner | 56 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

新增变异：`host_without_authorization_accepted`、`artifact_symlink_escape_collected`（复合）。

## 6. 真实 vs 模拟

- **真实**：容器启动/终止确认、`docker ps` 残留检查、`python:3.13-alpine` 内的 unittest 通过/失败（3.30 起沿用）。
- **单元**：host 授权拒绝、产物收集边界、超限、符号链接。
- **未执行**：容器内超时（长时间测试触发 `TimeoutExpired`）的端到端路径——终止原语已真实验证，但"超时→杀容器"的组合尚未跑一次完整任务。

## 7. 安全边界

真实模型 API 请求 0；未联网、未安装依赖；未启动/关闭 Docker Desktop；未读取 `.env`；`.idea/` 未修改未暂存；未删除 quarantine 或用户数据；未推送。

## 8. 残余风险

- 容器内超时组合路径未端到端验证（仅验证了终止原语）。
- 容器内输出仍不受限（客户端截断只限保存量）。
- 产物回收不含"部分失败"语义：目前是"收集到多少算多少"，未对缺失期望产物报错。
- `host_authorization` 是固定字符串，不是密钥；它阻止的是误用而非恶意调用者。
- 证据 TTL 执行期间不复查。
