# Zevorinth（衍序）

Zevorinth 是一个以**可验证执行**为核心的 AI Agent 系统。当前完成第 1.5 阶段：在 Phase 1 最小闭环上增加单机跨进程执行租约、围栏令牌、真实 SIGKILL 恢复和并发竞争测试。

当前不是通用 Agent：尚未接入 LLM，不能理解任意自然语言，也不能执行 shell、网络操作或多 Agent 协作。

## 当前真实能力

- 接收结构化 `write_text_artifact` 任务。
- 用 `deterministic-v1` Planner 生成唯一、有界的步骤。
- 在执行前检查操作类型、文件名、路径、内容大小和覆盖风险。
- 在 `artifacts/<task-id>/` 内真实创建文本文件。
- 独立重新打开文件，比较内容、字节数和 SHA-256。
- 用 SQLite 在同一事务中保存任务状态和有序事件。
- 用 SQLite 原子认领同一任务的执行权，活动租约存在时拒绝第二执行者。
- 用 lease token 围栏阻止过期执行者覆盖较新的任务状态。
- 区分 `real`、`simulated`、`not_executed` 三种执行模式。
- 只把租约已经过期的运行中任务视为中断，不误伤仍由其他进程持有的任务。
- 真实 SIGKILL 后优先验证已写产物而不是盲目重写。

## 快速开始

```bash
cd /Users/lidequan/PythonProject/Zevorinth
PYTHONPATH=src python3 -m zevorinth doctor

DEMO_ROOT="$(mktemp -d)"
PYTHONPATH=src python3 -m zevorinth \
  --home "$DEMO_ROOT" \
  run \
  --goal "创建一份可验证的证明" \
  --artifact-path proof.txt \
  --content "Zevorinth wrote this file." \
  --json
```

成功时退出码为 `0`。输出包含真实产物路径、字节数、SHA-256 和独立验证结果。复制输出中的 `task_id` 后可查询：

```bash
PYTHONPATH=src python3 -m zevorinth --home "$DEMO_ROOT" status TASK_ID
PYTHONPATH=src python3 -m zevorinth --home "$DEMO_ROOT" events TASK_ID --json
```

## 模拟与未执行

模拟只用于明确标注的演示或测试，不创建文件：

```bash
PYTHONPATH=src python3 -m zevorinth --home "$DEMO_ROOT" run \
  --goal "模拟写入" \
  --artifact-path simulated.txt \
  --content "not a real file" \
  --simulate
```

结果是 `status=simulated`、`execution_mode=simulated`，且没有 artifact 或真实验证证据。

策略阻断的任务会显示 `status=blocked`、`execution_mode=not_executed`。例如：

```bash
PYTHONPATH=src python3 -m zevorinth --home "$DEMO_ROOT" run \
  --goal "非法路径" \
  --artifact-path ../escape.txt \
  --content "must be rejected"
```

缺少必要信息时任务进入 `awaiting_clarification`。用 `resume` 补齐：

```bash
PYTHONPATH=src python3 -m zevorinth --home "$DEMO_ROOT" resume TASK_ID \
  --content "completed after clarification"
```

## 测试

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q src tests
```

全部测试只使用 Python 标准库、临时目录和临时 SQLite 数据库，不需要网络、模型或 API Key。

## 安装

在不覆盖现有环境的前提下创建隔离虚拟环境：

```bash
python3 -m venv /tmp/zevorinth-venv
/tmp/zevorinth-venv/bin/python -m pip install --no-deps -e .
/tmp/zevorinth-venv/bin/zevorinth doctor
```

## CLI 退出码

| 退出码 | 含义 |
| ---: | --- |
| `0` | 真实执行并通过独立验证 |
| `1` | 工具或验证失败 |
| `2` | 参数解析失败或任务不存在 |
| `3` | 等待澄清、模拟完成或取消 |
| `4` | 计划/策略阻断 |
| `5` | 中断或非终态 |
| `6` | SQLite 忙、存储错误或执行权围栏失效 |

## 持久化与恢复

- 数据库：`<home>/data/zevorinth.sqlite3`
- 产物：`<home>/artifacts/<task-id>/`
- 状态与事件由 SQLite 事务共同提交。
- 新运行实例不会把活动租约任务标为中断；只有租约过期后才允许恢复。
- 默认执行租约为 60 秒，可通过全局 `--lease-seconds` 调整。
- 同一任务通过 SQLite `BEGIN IMMEDIATE` 原子认领；竞争者会收到 `claim_rejected` 事件，不能执行工具。
- 每次认领会增加 `claim_generation`，所有执行写回都必须携带当前 lease token。
- 若已有真实工具证据，`resume` 先重新验证产物，不重复写入。
- 若现有文件缺失、内容不一致或证据冲突，任务进入显式失败，不覆盖原文件。

已执行真实 `SIGKILL` 测试：子进程在文件完成、工具证据和 `verifying` 检查点落库后等待父进程强制终止；新运行时在租约到期后恢复并重新验证文件。仍不声称覆盖断电、物理磁盘故障或文件系统损坏。

当前支持**同机、多进程、共享本地 SQLite 数据库**的单任务互斥。不支持跨机器分布式锁或分布式文件系统围栏。任务执行不得超过租约；当前绑定工具为 64 KiB 文本，未实现长任务租约心跳。

## 安全边界

- 产物名只允许单个安全文件名，不接受目录或多级路径。
- 拒绝绝对路径、`..`、隐藏文件、控制字符、链接逃逸和覆盖已有产物。
- 使用目录文件描述符、`O_NOFOLLOW`、临时文件、`fsync` 和不覆盖的硬链接提交。
- 不提供任意 shell、网络、凭据或动态代码执行工具。
- 历史事件不原地修改；任务状态转换集中在 `state_machine.py`。

## 当前限制

- Planner 是确定性的，不理解任意自然语言；用户必须提供结构化参数。
- 只支持一种工具和一种任务类型。
- 不调用模型 API，不需要 API Key。
- 相同请求重复 `run` 会创建不同任务；同一 task ID 的 `execute`/`resume` 在终态后保持幂等。
- 无多 Agent、插件市场、自动修改源码、无限自主循环。
- 不使用外部数据库服务；SQLite 由 Python 标准库提供。
- 多实例只支持单机共享本地文件系统，不支持分布式部署。

## 文档

- [第 0 阶段环境核查](docs/environment-audit.md)
- [旧项目复用矩阵](docs/reuse-matrix.md)
- [架构与设计决策](docs/architecture.md)
- [原始验收标准](docs/acceptance-criteria.md)
- [第 1 阶段实现说明](docs/phase-1-implementation.md)
- [第 1 阶段验收结果](docs/phase-1-results.md)
- [第 1.5 阶段审计](docs/phase-1.5-audit.md)
- [第 1.5 阶段结果](docs/phase-1.5-results.md)
- [执行记录](docs/execution-log.md)
- [文件清单](docs/file-manifest.md)
