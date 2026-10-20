# Zevorinth Phase 3.43：诊断契约与测试隔离审计

## 1. 结论与基线

**PASS。** 诊断字段的准确性、安全边界和异常输入行为已通过离线测试锁定；并行全量
测试的容器名称冲突已修复为按任务前缀隔离。两套全量测试在并行和单独运行下均稳定
通过。本阶段没有真实模型调用，也没有改变执行策略。

| 项目 | 值 |
| --- | --- |
| 仓库 | `/Users/lidequan/PythonProject/Zevorinth` |
| 分支 | `phase-2.6-response-hardening` |
| 起始 HEAD | `a1a9785a2fb2646056364a8163d6bfb0123d94bc` |
| 起始工作区 | 仅 `?? .idea/` |
| 真实模型 API 请求 | **0** |
| 联网 / 安装依赖 | 否 / 否 |
| Docker 启停 | 未启动、未关闭 |

最终 HEAD 为本审计文档所在提交，具体哈希见阶段最终报告。

## 2. 诊断字段准确性

`candidate_field_count` 在 `_candidate_shape()` 中对排序后的完整键列表调用
`len(keys)`，发生在 `_safe_field_names()` 截断之前。因此：

- `candidate_fields` 最多展示 20 个字段；
- `candidate_field_count` 始终表示完整候选对象的字段总数；
- 不会把截断后的 20 误报为实际字段数。

新增测试 `test_field_count_reflects_the_full_candidate` 用 38 个字段验证了
“展示 20 个、计数 38 个”的行为。

## 3. 诊断安全性

失败路径只记录：

- `candidate_fields`：安全字段名，最多 20 个；
- `candidate_field_count`：完整字段数；
- `candidate_classification`：安全枚举或 `invalid`；
- `candidate_operation`：安全枚举或 `invalid`。

不记录任意字段值、完整候选文本、文件内容或凭据。新增测试覆盖：

- 包含换行的字段名、超长字段名、`__proto__` 等名称被替换为 `<redacted>`；
- 合法字段名 `ok.field-1` 正常保留；
- 未知字段值、`content` 值不会出现在诊断中；
- 非法 operation 值只记录 `invalid` 哨兵，不记录原始字符串；
- 不可迭代/不可读的 Mapping 降级为
  `candidate_fields=["<unavailable>"]`、`candidate_field_count=-1`，仍按
  `invalid_candidate` 拒绝；
- 诊断生成失败不会导致候选被接受。

## 4. 边界行为

| 输入 | 结果 |
| --- | --- |
| 合法 `write_text_artifact` | `planned`，成功元数据不含诊断形状 |
| 合法 `run_tests` | `planned`，目标与后端不变 |
| 未知 `type` 字段 | `failed / unknown_candidate_fields`，仍拒绝 |
| 缺少 `operation` | `failed / missing_candidate_fields` |
| 非法 operation 类型/未知 operation | `unsupported` 或失败，记录 `invalid` |
| 非法 `test_target` | `failed / invalid_test_target`，记录 operation 形状 |
| 缺少 workspace | `clarification`，不是失败 |
| 非对象 JSON | `failed / invalid_candidate` |
| 不可读 Mapping | `failed / invalid_candidate`，诊断安全降级 |
| `unsupported` 候选 | 保持 `unsupported`，不转换为可执行操作 |
| 伪造 `evidence_id` 等可信字段 | 仍被 `unknown_candidate_fields` 拒绝 |

## 5. 并行测试隔离修复

### 问题

`tests/test_container_timeout_cancel.py` 原先在 `setUp`/`tearDown` 和断言中使用：

```text
docker ps --filter name=zv-
```

这会看到、甚至删除另一套并行全量测试正在使用的容器。3.42 的并行运行已复现该问题：

```text
FAIL: test_cancel_during_container_run_stops_it
AssertionError: 'c30e97c9147f' != ''
```

### 修复

仅修改测试代码，不改产品容器命名或终止逻辑：

- 使用任务专属前缀 `zv-<task_id 去横线后 12 位>-`；
- `_container_names(task_id)` 只返回匹配该前缀且以 `prefix + "-"` 开头的容器；
- 每个测试在 `_run()` 或手动建任务时登记 `task_id`；
- `tearDown()` 只清理本测试登记任务对应的容器；
- 删除全局 `_cleanup_containers()`，避免误删其他测试或用户的容器。

超时与取消断言现在只检查本次任务的容器：

```text
self.assertEqual(_container_names(result.task_id), [])
```

## 6. 测试与回归

| 检查 | 结果 |
| --- | --- |
| 候选诊断专项 | 19/19 通过 |
| 容器超时/取消专项 | 11/11 通过 |
| Planner / run_tests / 证据 / 执行器 / 验证器专项 | 136/136 通过 |
| 系统 Python + `.venv` 全量**并行** | 578/578 + 578/578，均 exit 0 |
| 系统 Python 全量**单独** | 578/578，51.0s，exit 0 |
| `.venv` 全量**单独** | 578/578，48.5s，exit 0 |
| Mutation runner | 60 killed / 0 survived / 0 invalid / 0 error |
| `compileall` | exit 0 |
| `git diff --check` | exit 0 |

并行运行日志：

- 系统 Python：578/578，52.0s，exit 0；
- `.venv`：578/578，51.4s，exit 0。

## 7. 未验证事项与残余风险

1. `type` 的实际值和语义仍未确定；本阶段没有增加兼容规则；
2. 诊断只保存字段名与安全枚举，仍不能离线复现完整候选；
3. 测试隔离依赖服务当前的容器命名约定
   `zv-<task_id12>-<random8>`；若命名规则变化，测试辅助函数需要同步；
4. 其他未来测试仍可能重新引入全局 `zv-` 过滤，目前没有仓库级静态检查阻止；
5. 未验证真实模型在下一次调用中是否会返回无 `type` 的候选；
6. 未触达 provisioning、容器执行或 `TestResultVerifier`（本阶段无真实调用）。

## 8. 安全边界

- 真实模型 API 请求：0；
- 未读取 `.env` 或输出密钥；
- 未访问互联网、未安装依赖；
- 未启动或关闭 Docker Desktop；
- 未修改、暂存或跟踪 `.idea/`；
- 未执行 reset / clean / rebase / amend / 分支切换；
- 未放宽 policy、隔离证据或独立验证门禁。

## 9. 审计产物

以下文件位于 `.gitignore` 排除的 `artifacts/` 下，**仅本地存在**：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.43/stable-summary.json` | `0045cb18e75dfa1f0bdac97a1b90d267eb7be967e312b0e39f6dd6131aba9fe9` |
| `artifacts/audits/phase-3.43/mutations.json` | `e99635ef99b96e98f3185a9a6368fe9de2ba9256eec21585190506c473bff08b` |

## 10. 阶段判定

**PASS**：诊断字段准确性、安全性和边界行为已通过离线测试；并行测试隔离已修复并
通过并行与单独两种运行方式的验收；没有真实模型调用，也没有改变执行策略。

下一次真实调用仍需用户单独授权。即使届时诊断显示
`candidate_operation="run_tests"`，也只证明候选表达了该操作，不代表它已经通过
严格校验、获得隔离证据或实际执行成功。
