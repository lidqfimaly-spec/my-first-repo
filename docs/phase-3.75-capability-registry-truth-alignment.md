# Zevorinth Phase 3.75：能力注册与产品能力真相对齐

## 1. 结论

**PASS。** 本阶段没有新增执行权限或任务操作。它修复了已知的能力真相漂移：

- README 曾声称只支持一种真实操作；
- `doctor.task_types` 曾只报告 `write_text_artifact`；
- 产品阶段字段停留在早期 Phase 2。

现在 `write_text_artifact` 与 `run_tests` 由同一个机器可读 capability registry 描述，
Policy 和 `doctor` 使用同一事实源，README 也明确列出条件能力与未实现边界。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `c3f7b46d02a8c9d047c66678866104a27ae37f62` |
| 新增 action | **0** |
| 新增执行权限 | **0** |
| 真实模型 / 网络 / 安装 | 0 / 0 / 0 |

## 2. 权威能力注册

新增 `zevorinth.capabilities`：

```text
capability-registry-v1
```

状态集合：

| 状态 | 含义 |
| --- | --- |
| `available` | 当前可用且已验证 |
| `conditional` | 已实现，但需要明确环境或配置前置条件 |
| `internal_only` | 只通过内部 Python API 提供 |
| `not_implemented` | 未实现 |
| `unverified` | 没有足够证据证明 |

当前任务操作只有：

### `write_text_artifact`

- status: `available`
- real / simulated 两种模式；
- 禁止覆盖、空内容和超限内容；
- 独立文件读回、大小和 SHA-256 验证。

### `run_tests`

- status: `conditional`
- 需要注册 workspace、bounded unittest target、isolation evidence 和容器环境；
- host backend 只允许显式 `local-maintenance` 授权，且不视为受信隔离；
- 已覆盖成功、超时、取消、启动失败、验证器拒绝和真实模型单样本路径。

## 3. 未实现能力明确排除

registry 与 README 明确标记为不可用：

- 任意 shell；
- 浏览器；
- 通用任务恢复；
- 自动恢复/自动重试；
- 自动清理；
- Web/Desktop UI；
- 多机执行；
- L2/L3 可信证据。

固定 `restore_controlled_marker` 标记为 `internal_only`，并明确不是通用任务恢复。

## 4. 跨模块一致性

### Policy

`ArtifactPolicy.SUPPORTED_OPERATIONS` 直接使用 registry 的 task operation 集合，避免
出现“能力清单声称可用但 Policy 拒绝”或反向漂移。

### Planner Schema

测试要求 LLM JSON Schema 的 operation enum 与 registry 操作集合完全一致。

### Doctor

`doctor --json` 现在包含：

- `phase=3.75`；
- `legacy_phase=2` 和 `legacy_hardening_phase=1.5`；
- `capability_registry_version`；
- 完整 `capability_manifest`；
- `task_types` 包含两个实际操作；
- 未实现能力布尔值为 false。

`phase` 从整数变为字符串是显式的机器可读契约调整；旧值保留在 legacy 字段中。

### README

README 已更新为两个真实任务操作、条件要求、固定 marker 的内部边界，以及未实现的
浏览器、shell、自动恢复、自动清理和多机能力。

## 5. 验证

| 检查 | 结果 |
| --- | --- |
| Phase 3.75 capability 矩阵 | 7/7 |
| 系统 Python 全量 unittest | 758/758，exit 0 |
| `.venv` 全量 unittest | 758/758，exit 0 |
| Mutation runner | **155 killed / 0 survived / 0 invalid / 0 error** |
| Mutation 执行证据 | 155/155 in-process，0 unverified |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

新增变异覆盖 Policy registry 漂移、doctor task types 漂移、run_tests 条件状态、
未实现 shell 能力误升、manifest 非隔离副本和产品阶段回退。

## 6. 残余限制

- `run_tests` 的实际可用性仍依赖容器环境，registry 只能声明条件，不能保证本机 Docker
  始终可用；
- LLM Planner 需要 provider、模型和 Key，通用模型质量未被本阶段重新验证；
- registry 是代码常量，不是远程能力发现服务；
- 能力真相对齐不代表新增执行权限；
- L2/L3、同 UID 全量重写和跨 OS 仍不在保证范围内。

## 7. 阶段判定

**PASS**：README、Policy、Planner schema、doctor 和机器可读 manifest 现在对“两个真实
任务操作及其前置条件”保持一致；未实现能力不再出现在可用任务清单中，产品阶段和能力
保证得到准确表达。
