# Phase 3.90 — Exact Text Execution & Readback

## 结论

Phase 3.90 在离线范围内通过。

本阶段没有新增 action 或执行权限，而是把 Phase 3.89 的三种文本契约接到既有的
`write_text_artifact -> independent verification` 执行核，证明契约不仅约束 Planner
返回值，也约束真实创建的文件和最终持久化证据。

## 端到端链路

固定矩阵中的每个案例都使用：

```text
TaskRequest
  -> scripted LLM planner
  -> text precision preflight
  -> candidate schema/business validation
  -> text precision gate
  -> intent fidelity gate
  -> Policy
  -> real artifact publish in a temporary workspace
  -> independent byte readback
  -> task and event persistence
```

成功路径的实际事件顺序为：

```text
task_created
task_claimed
planning_started
planning_completed
task_planned
tool_started
tool_succeeded
verification_started
verification_completed
task_succeeded
```

拒绝路径在 planning 阶段结束，不包含 `task_planned`、`tool_started`、
`verification_started`、`verification_completed` 或 `task_succeeded`。

## 执行契约

Active contract：`planner-text-execution-v1`

SHA-256：

`7f306abeaf7bd5c800a06eeb0fe71dad6eb504f4f42c7f65ec70bb2c5e59fbfb`

契约绑定：

| 项目 | 值 |
| --- | --- |
| Precision contract | `planner-exact-text-v1` |
| Precision SHA-256 | `cf22a6adb91b03a03830c89c887a962e3d5835b7e7a2baa140e32eae15b8e8ec` |
| Retroactive | false |
| Cases | 10 |
| Succeeded tasks | 6 |
| Planning-failed tasks | 4 |
| Verified artifacts | 6 |
| Rejected artifacts | 4 |

## 覆盖矩阵

成功案例实际写入临时 workspace，并由独立 verifier 从磁盘重新读取：

| Case | 验证重点 |
| --- | --- |
| `exact_unicode_multiline` | 中文、重音字符、多行内容完整保留 |
| `exact_whitespace_preserved` | leading/trailing spaces 和换行不被 trim |
| `exact_utf8_bom_preserved` | U+FEFF 被保留为 UTF-8 `EF BB BF` 前缀 |
| `semantic_punctuation_preserved` | semantic 模式保留中文化句号 |
| `format_json_object_executed` | JSON object 通过确定性格式检查后写入 |
| `format_markdown_executed` | Markdown heading 通过格式检查后写入 |

拒绝案例：

| Case | 结果 |
| --- | --- |
| `exact_punctuation_mismatch_rejected` | `text_precision_mismatch` |
| `exact_trailing_newline_mismatch_rejected` | `text_precision_mismatch` |
| `format_json_mismatch_rejected` | `text_format_mismatch` |
| `empty_write_content_precondition_rejected` | `invalid_candidate_content` |

四个拒绝案例均满足：

- task status 为 `planning_failed`；
- `plan_created=false`；
- artifact file count 为 `0`；
- tool 与 verification 事件不存在；
- 重新打开 TaskService 后持久化状态仍是 `planning_failed`。

## 回读保证

成功案例不信任 Planner 或 writer 自报的成功状态。每个 artifact 都经过：

1. 读取实际文件 bytes；
2. 与实际 `expected_artifact_content.encode("utf-8")` 比较；
3. 重新计算 byte count 和 SHA-256；
4. 比较 tool evidence 中的路径、大小、SHA、attempt；
5. 校验 workspace effects 只包含声明 artifact；
6. 将 verification evidence 与 attempt ID 一起持久化；
7. 重新打开 TaskService 后再次确认 `succeeded` 状态。

例如 BOM 案例的实测 prefix 为：

```text
efbbbf68656c6c6f
```

这表示文件实际包含 UTF-8 BOM，然后才是 `hello`。该结果说明当前执行路径保留了精确
code point 内容，并按 UTF-8 固化到文件。它不是通用原始字节导入契约；当前文本任务仍以
UTF-8 编码作为产品边界。

## 门禁独立性

`exact_punctuation_mismatch_rejected` 的元数据为：

| 维度 | 结果 |
| --- | --- |
| Text precision | `exact_mismatch`, gate blocked |
| Intent fidelity | `planned_faithful` |
| Final task | `planning_failed` |
| Artifact | absent |

因此 text precision 失败不会被 intent fidelity 的成功覆盖。

候选也不能自行声明预期答案。若 Planner 输出加入 trusted-only 的 `expected_text`
字段，既有 candidate validator 返回 `unknown_candidate_fields`，task 进入
`planning_failed`，不创建 artifact。

## 写后篡改验证

专项故障注入在 writer 发布正确 artifact 后，将文件改为其他内容，但保留原 tool evidence。

结果：

| 项目 | 结果 |
| --- | --- |
| Tool | `tool_succeeded` |
| Verification | `verification_completed` |
| Verification result | failed |
| Reason | `artifact_content_mismatch` |
| Task | `task_failed` |
| `task_succeeded` | absent |

这说明 Plan/Precision 通过后，最终成功仍取决于独立回读，而不是 writer 的自我声明。

## 隔离与副作用

| 项目 | 结果 |
| --- | --- |
| 真实 artifact writes | 6，全部在 `TemporaryDirectory` |
| 临时 TaskService submissions | 10 |
| Real API calls | 0 |
| Network requests | 0 |
| Dependency installs | 0 |
| Container executions | 0 |
| Project workspace writes | 0 |
| Live model quality measured | false |

本阶段没有发起第五次 live run，也没有修改 Phase 3.89 的原始契约。

## 自动化验证

| 项目 | 结果 |
| --- | --- |
| 系统 Python unittest | 867/867 |
| `.venv` unittest | 867/867 |
| Mutation testing | 223 killed / 0 survived / 0 invalid / 0 error |
| Execution unverified | 0 |
| Compileall | PASS |
| `git diff --check` | PASS |

新增变异覆盖：

- precision contract binding drift；
- 实际 writer trim 文本；
- 实际 writer 自动添加 BOM；
- verifier 忽略内容不匹配。

## Phase 3.89 完整性

| 文件 | SHA-256 | 状态 |
| --- | --- | --- |
| `artifacts/audits/phase-3.89/stable-summary.json` | `8380b2b10fdd1e473efd99d414a3f66be75cf0c6de12463e44645d38b07cb321` | match |
| `src/zevorinth/evals/planner_exact_text_v1.json` | `cf22a6adb91b03a03830c89c887a962e3d5835b7e7a2baa140e32eae15b8e8ec` | match |
| `src/zevorinth/planner_text_precision.py` | `82bbc11359595f5e9604a99566d1cb570f01d630145ebacd2112d0aff40da60b` | match |

Phase 3.89 baseline commit：`12b8588`。

## 本地产物

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.90/e2e-regression.json` | `eed4b5c86ae7a24b497cbf52c355600343ee1f29249779921f86e0bbbf35676d` |
| `artifacts/audits/phase-3.90/execution-contract.json` | `7bde28641e92e57ed3aec879b66d8da275f1f303e03efdb83aa87bd5a24fe44e` |
| `artifacts/audits/phase-3.90/post-write-tamper.json` | `5ade50776277f598ed7a78f69078b7bc0fdd1b61845db4fc85fe1c1683e86e0f` |
| `artifacts/audits/phase-3.90/phase-3.89-integrity.json` | `d051728d90c159d2b959646a68ceb6d3409ab522c6b65acca57c5b7939822c89` |
| `artifacts/audits/phase-3.90/mutations.json` | `dc0b0fee88f5502be54899c970e8fb751af3c294fa291fd1135a6bbc715d62d6` |

这些运行产物由 `.gitignore` 排除，只保留在本地审计目录。

## 限制

- 端到端矩阵使用脚本 Planner，不测量真实模型理解或参数准确率；
- artifact 只在临时 workspace 中创建，不证明外部持久存储可靠性；
- UTF-8 readback 保证不等同于任意原始字节、BOM 状态或编码协商契约；
- text precision 仍不是来源身份认证，trusted context 依赖当前任务调用方；
- 本阶段不新增自动恢复、后台重试、Shell、浏览器或 cleanup 能力；
- 真实模型在新契约下的表现仍需另行授权和独立评分。
