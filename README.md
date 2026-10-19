# Zevorinth（衍序）

Zevorinth 是一个以**可验证执行**为核心的 AI Agent 系统。当前完成第 1 阶段：一个确定性规划、单步执行、真实受控工具、独立验证、SQLite 持久化和安全恢复的最小闭环。

当前不是通用 Agent：尚未接入 LLM，不能理解任意自然语言，也不能执行 shell、网络操作或多 Agent 协作。

## 当前真实能力

- 接收结构化 `write_text_artifact` 任务。
- 用 `deterministic-v1` Planner 生成唯一、有界的步骤。
- 在执行前检查操作类型、文件名、路径、内容大小和覆盖风险。
- 在 `artifacts/<task-id>/` 内真实创建文本文件。
- 独立重新打开文件，比较内容、字节数和 SHA-256。
- 用 SQLite 在同一事务中保存任务状态和有序事件。
- 区分 `real`、`simulated`、`not_executed` 三种执行模式。
- 识别重启前仍处于运行中的任务，并优先验证已写产物而不是盲目重写。

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

## 持久化与恢复

- 数据库：`<home>/data/zevorinth.sqlite3`
- 产物：`<home>/artifacts/<task-id>/`
- 状态与事件由 SQLite 事务共同提交。
- 新运行实例会把上次遗留的 `running`、`verifying` 任务标为 `interrupted`。
- 若已有真实工具证据，`resume` 先重新验证产物，不重复写入。
- 若现有文件缺失、内容不一致或证据冲突，任务进入显式失败，不覆盖原文件。

当前只支持单进程运行时。已测试的是“确定性进程重启”：在真实文件写入后、验证前让独立子进程直接异常退出，再由新实例识别和恢复；尚未证明 `kill -9`、断电或文件系统损坏后的全部边界。

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
- 无多 Agent、插件市场、自动修改源码、无限自主循环。
- 不使用外部数据库服务；SQLite 由 Python 标准库提供。

## 文档

- [第 0 阶段环境核查](docs/environment-audit.md)
- [旧项目复用矩阵](docs/reuse-matrix.md)
- [架构与设计决策](docs/architecture.md)
- [原始验收标准](docs/acceptance-criteria.md)
- [第 1 阶段实现说明](docs/phase-1-implementation.md)
- [第 1 阶段验收结果](docs/phase-1-results.md)
- [执行记录](docs/execution-log.md)
- [文件清单](docs/file-manifest.md)
