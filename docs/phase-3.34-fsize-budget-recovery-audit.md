# Zevorinth Phase 3.34：内核文件上限、工作区预算与恢复幂等

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 容器新增内核级单文件上限（`--ulimit fsize`，默认 64 MiB）与工作区产物总量预算；中断的容器测试任务恢复时**不重跑**。三类缺口（磁盘、终止失败、恢复幂等）都已有可执行证据。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `89fb81f87577fca1582ea94f5b8041a1164081d3` |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS，Python 3.14.6，Docker 29.2.1 |

## 2. 资源边界

| 机制 | 语义 | 证据 |
| --- | --- | --- |
| `--ulimit fsize=64MiB`（可配） | 内核级单文件上限，超限写失败 | 真实测试：写 90 MB → 任务 FAILED，文件未落地 |
| `disk_limit_bytes` | tmpfs 工作区硬配额 + 只读输入 | 3.33 真实测试：1 MiB 配额写 5 MB 失败 |
| `workspace_budget_bytes` | 回收产物总量超过预算 → `workspace_over_budget` | 代码路径；**未加端到端用例**（见风险） |
| `max_output_bytes` | 客户端保存量与截断标记 | 3.32/3.33 覆盖 |

`fsize` 限制的是单个文件，**不是**总量；多小文件仍可增长，这是明确记录的残余风险（总量由 `disk_limit_bytes` 的 tmpfs 方案覆盖）。

## 3. 恢复幂等

`test_interrupted_container_task_is_not_rerun_on_resume`：容器任务处于 `tool_started`、无结果时中断 → 恢复得到 BLOCKED `test_execution_unknown`，`tool_started` 计数保持 1（没有第二次启动）。

这复用了 3.14 引入的"未知状态不重跑"守卫，并首次在容器后端上验证。

## 4. 测试与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 535/535，44.3s / 44.1s | 0 |
| 容器生命周期专项 | 11/11（真实容器） | 0 |
| run_tests 门禁专项 | 6/6 | 0 |
| Mutation runner | 58 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

## 5. 真实 vs 模拟

- **真实容器**：fsize 上限、磁盘配额（3.33）、超时/取消、产物缺失。
- **单元**：恢复幂等、host 授权、产物状态。
- **未执行**：`workspace_budget_bytes` 的端到端用例（需要构造超预算产物）；容器内 stdout 上限。

## 6. 安全边界

真实模型 API 请求 0；未联网、未安装依赖；未启动/关闭 Docker Desktop；未读取 `.env`；`.idea/` 未修改未暂存；未删除用户数据或 quarantine；未推送。

## 7. 残余风险

- `fsize` 只管单文件；多小文件仍可能增长（需 `disk_limit_bytes`）。
- `workspace_budget_bytes` 只校验"已收集产物"总量，不是运行期配额，也没有端到端用例。
- 默认容器路径仍是 bind mount（无总配额）。
- `host_authorization` 仍是固定字符串。
- 恢复幂等只在"无结果"场景验证，未覆盖"有结果但未验证"的容器任务。
