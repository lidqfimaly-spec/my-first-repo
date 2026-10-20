# Zevorinth Phase 3.32：容器超时/取消闭环与产物清单

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 超时与取消现在在真实容器上端到端跑通：容器被终止且 `docker ps` 无残留，任务状态与事件一致；产物清单支持"必需产物缺失 → `collection_failed`"。过程中发现并修复了一个**真实的管道死锁**。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `5e812f5`（3.31 最终提交） |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS，Python 3.14.6，Docker 29.2.1 |
| 镜像 | `python:3.13-alpine` |

## 2. 超时与取消（真实容器）

`_run_container_test` 改为 Popen + 轮询：

- 每 50ms 检查取消标志与截止时间；
- 读线程并发 `communicate()` 排空 stdout/stderr；
- 超时/取消 → `docker kill` → 轮询 `docker ps --filter name=` 确认不在运行；
- 无法确认 → `container_termination_unverified`，失败关闭；
- 返回 `timed_out` / `cancelled` 与 `terminated_confirmed`。

**发现并修复的死锁**：首版在轮询期间不读管道，测试输出超过 64KiB 管道缓冲时容器阻塞，直到超时才被杀（该用例耗时 60s）。改为并发读线程后，同一用例 4.5s 完成，全部 6 个测试 5.1s。

### 真实测试结果

| 场景 | 任务状态 | 事件 | 容器残留 |
| --- | --- | --- | --- |
| 正常通过 | SUCCEEDED | `task_succeeded` | 无 |
| 超时（sleep 30 / timeout 2s） | FAILED `test_timeout` | `task_failed`，无 `task_succeeded` | 无 |
| 主动取消（1s 后） | CANCELLED | `task_cancelled` | 无 |
| 大输出（200k 字符） | 完成，记录截断 | — | 无 |
| 必需产物缺失 | FAILED `collection_failed` | 无成功事件 | 无 |

## 3. 产物清单

- `required_artifacts` 参数（经 policy/planner 透传）；
- 执行后收集 `<workspace>/artifacts`（跳过符号链接与越界路径、受输出上限约束、记录 SHA-256）；
- 缺失任一必需产物 → `collection_failed`，任务 FAILED，不进入成功路径。

## 4. 测试与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 529/529，40.5s / 40.1s | 0 |
| 超时/取消/产物专项 | 6/6（真实容器） | 0 |
| Mutation runner | 56 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

## 5. 真实 vs 模拟

- **真实容器**：正常、超时、取消、大输出、必需产物缺失（5 条端到端）。
- **单元**：产物边界（符号链接、超限）。
- **未执行**：容器内磁盘增长超限（bind mount 无配额，见风险）；Docker 客户端异常中断；重复取消幂等。

## 6. 安全边界

真实模型 API 请求 0；未联网、未安装依赖；未启动/关闭 Docker Desktop；未读取 `.env`；`.idea/` 未修改未暂存；未删除用户数据或 quarantine；未推送。

## 7. 残余风险

- **容器内磁盘无配额**：workspace 是 bind mount，容器可以写满宿主磁盘；当前只限制客户端保存量。
- 未验证"终止确认失败"的构造路径（需要制造 docker 不可用），该分支只有代码与单元级保护。
- `collection_failed` 的变异 `collection_failure_ignored` 被现有断言掩盖（未纳入变异集），作为已知缺口记录。
- 重复取消与恢复后的幂等未单独测试。
- `host_authorization` 仍是固定字符串，属于策略约束而非认证。
