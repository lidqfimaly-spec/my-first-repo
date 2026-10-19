# 第 2.6 阶段：响应诊断与规划契约加固

日期：2026-10-20

## 1. Phase 2.5 结果保留

原始在线试点结果未覆盖、未改写：

- `artifacts/evaluations/phase-2.5-online-v1.json`
- 大小：10187 字节
- SHA-256：`9379a1efa8484ddb03c8f8c6ed9da44447b6d0087e5c8b8f6fccf86e1f006476`
- 结果：3/5 通过
- 失败：`clarify_ambiguous_goal`、`unsupported_shell`

原始报告保留其历史字段，即使其中 `error_code_correct` 的旧计算口径后来被审计修正。

## 2. invalid_candidate_fields 代码路径

### 已确认的代码事实

旧 `PlanningCandidateValidator` 要求候选对象恰好包含固定的 7 个字段：

```text
classification
operation
artifact_path
content
missing_fields
clarification_questions
reason
```

只要缺少任一字段或出现任一额外字段，所有分类都会得到同一个错误：

```text
invalid_candidate_fields
```

这会错误地要求 `clarification` 和 `unsupported` 同时携带文件生成参数，也会掩盖具体是缺字段还是多字段。

### 离线复现

Fake Client 返回合法澄清语义、但只包含：

```text
classification
missing_fields
clarification_questions
```

旧契约会判为 `invalid_candidate_fields`；新契约进入 `awaiting_clarification`，且工具调用为 0。

缺少分类必需字段会得到 `missing_candidate_fields`；未知字段会得到 `unknown_candidate_fields`。

### 仍未知

原始在线响应没有保存字段名，因此无法确认线上失败究竟是缺少无关字段、缺少必要字段还是出现额外字段。不得声称已经确定具体线上根因。

## 3. invalid_structured_output 代码路径

### 已确认的代码事实

旧适配器只读取 `response.output_text` 并立即执行 `json.loads`。任何 JSON 解析异常都会在响应状态、输出项类型、内容片段和 Token 诊断进入内部结果之前被抛出，最终只留下：

```text
invalid_structured_output
```

旧路径没有安全记录：

- HTTP 状态；
- response.status；
- incomplete reason；
- output item types；
- content part types；
- text presence 和 length；
- JSON 错误类别。

### 新规范化层

新增 `response_normalization.py`，将 SDK 响应转换为内部 `ModelCallResult`：

- 从 `response.output` 的 message/output_text/refusal 片段提取文本；
- 只在没有结构化文本片段时回退到 `response.output_text`；
- 区分 refusal、failed、cancelled、incomplete、空输出和无效 JSON；
- 记录经过白名单筛选的诊断元数据；
- 不保存原始响应文本。

未知 response status、非 2xx HTTP 状态、response error、空文本和解析失败全部 fail-closed。

### 仍未知

原始线上响应没有保存结构信息，因此无法确认 `unsupported_shell` 当时是：

- 空输出；
- 非 JSON 文本；
- 被截断；
- 内容片段类型异常；
- SDK/Provider 结构差异；
- 其他响应结构问题。

必须通过未来单独批准的在线复测才能确认。

## 4. 新候选契约

`classification` 始终必需。之后按分类检查：

| 分类 | 必需业务字段 |
| --- | --- |
| planned | `operation`、`artifact_path`、`content` |
| clarification | `missing_fields`、`clarification_questions` |
| unsupported | `reason` |

与当前分类无关的字段可缺失，并规范化为 null/空列表。

安全门槛没有降低：

- 未知字段仍拒绝；
- planned 仍要求操作白名单；
- 文件名仍执行原路径安全检查；
- 内容仍有长度限制；
- 仍必须通过 Policy、租约、工具和 Verifier；
- 不安全候选仍不能因为字段诊断变化而获得工具权限。

DeepSeek Schema 只要求 `classification` 作为共同必需字段，业务必需字段由本地验证器按分类强制；OpenAI 的 strict 全字段 Schema 保持不变。

## 5. 安全诊断白名单

允许记录：

- HTTP 状态；
- response status；
- incomplete reason；
- output item type 列表；
- content part type 列表；
- 是否有文本、文本长度；
- JSON 错误类别；
- 缺失/未知字段名；
- 已识别错误类别；
- 延迟、Token、响应 ID。

禁止记录：

- 完整用户目标；
- 完整候选字段值；
- 完整模型文本；
- 拒答原文；
- 认证头、Key、包含 Key 的异常文本；
- 任意未筛选异常字符串。

未知字段名也必须匹配安全标识符格式，否则显示为 `<redacted>`。

## 6. 离线验证

新增或强化的测试覆盖：

1. 合法 planned；
2. 合法 clarification，无无关文件字段；
3. 合法 unsupported；
4. 拒答；
5. incomplete + reason；
6. 空输出；
7. 无效 JSON；
8. 缺少分类必需字段；
9. 未知字段；
10. 越权路径；
11. shell 操作被本地拒绝；
12. 异常 output item/content type；
13. 异常中含敏感值时只记录白名单字段；
14. 所有规划失败工具调用为 0；
15. 合法计划经 Policy、工具和 Verifier 完成真实闭环。

## 7. 本阶段边界

- 真实 API 请求：0
- 在线评测：未运行
- 新在线结果文件：未创建
- 原始 Phase 2.5 在线结果：保持不变
- Provider/Model 自动切换：未发生
- 文件工具调用：仅离线临时测试
- 旧项目：未修改

## 8. 后续线上复测建议

若后续单独批准，只运行两个历史失败案例：

```text
clarify_ambiguous_goal
unsupported_shell
```

建议命令使用两个显式 `--case-id`，而不是重新扩大为完整 5 案例或 28 案例。
