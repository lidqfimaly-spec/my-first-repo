# Zevorinth Phase 3.37：隔离证据自动准备

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 新增 `provision_evidence()` 与 CLI `verify-isolation`：先用同一镜像/工作区跑七项边界探针，记录内核观察结果，绑定**将要执行的真实命令**摘要，写入 `EvidenceStore`。执行器仍只消费已验证证据，不自行签发。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `71f8a2fb8b41b19fe4c7668da75daeac13a72aa9` |
| 分支 | `phase-2.6-response-hardening` |
| 镜像 | `python:3.13-alpine` |
| 平台 | macOS，Python 3.14.6，Docker 29.2.1 |

## 2. 流程

```
enqueue(task) ──► task_id
  │
  ├─ provision_evidence(root, image, workspace, target, request_id=task_id, evidence_id=E)
  │     ├─ 跑七项容器探针（读/写内、越界读写、网络、凭据、子进程）
  │     ├─ 断言 observed == expected（否则 ProvisioningError）
  │     ├─ 绑定 config_digest（整条 docker 命令）+ command_digest（python -B -m unittest -q <target>）
  │     └─ EvidenceStore.save_evidence(E)
  │
  └─ execute_task(task_id)  ──► 证据实时重验（TTL/请求/命令/运行时/镜像/配置）──► 容器内执行
```

CLI：`zevorinth verify-isolation --workspace demo --target test_ok --image python:3.13-alpine --request-id <task_id> --evidence-id <id>`

## 3. 真实测试

`tests/test_evidence_provisioning.py`（真实容器）：

| 用例 | 结果 |
| --- | --- |
| 准备证据 → 同任务容器内跑 unittest | SUCCEEDED，`exit_code=0` |
| 指定不存在的镜像 | 抛错，不产生证据 |

探针观测：`read_outside=denied`、`write_outside=denied`、`network=denied`、`credential=denied`、`write_inside=allowed`、`read_inside=allowed`、`subprocess=allowed`。

## 4. 与 3.26 的分工

| 阶段 | 谁产生证据 |
| --- | --- |
| 3.26/3.30 | 测试代码手工构造并保存 |
| 3.37 | `provision_evidence()` 自动执行探针并保存 |

两者写入的是同一 schema（`isolation_evidence` 1.1），执行器不区分来源——它只验证。

## 5. 测试与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest | 537/537，51.6s | 0 |
| 证据准备专项 | 2/2（真实容器） | 0 |
| Mutation runner | 58 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

## 6. 安全边界

真实模型 API 请求 0；未联网、未安装依赖；未启动/关闭 Docker Desktop；未读取 `.env`；`.idea/` 未修改未暂存；未删除用户数据或 quarantine；未推送。

## 7. 残余风险

- 证据仍由**调用方**触发生成；若攻击者能控制调用方，仍可自行造一份"探针都通过"的记录（摘要只防意外损坏）。
- 探针在 provisioning 时运行，与真正执行之间仍有时间窗（TTL + 实时身份重验缩小但未消除）。
- 未接入模型自动决定何时 provisioning。
- 探针的 `subprocess=allowed` 是设计选择（受 pids 限制），不是"禁止子进程"。
