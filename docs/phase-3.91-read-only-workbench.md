# Phase 3.91 — Read-Only Local Workbench v1

## 结论

Phase 3.91 在离线范围内通过。

本阶段交付一个 loopback-only 的本地只读工作台。它只投影现有 SQLite 任务记录、事件、
verification、lifecycle audit 和 recovery preview，不创建第二套任务状态，也不提供提交、
重试、取消、恢复、覆盖或删除入口。

设计事实源：[`DESIGN.md`](../DESIGN.md)。

## 启动

```bash
PYTHONPATH=src python3 -m zevorinth \
  --home RUNTIME_ROOT \
  workbench --port 8765
```

打开：

```text
http://127.0.0.1:8765/
```

服务启动不初始化工作台自己的数据库。任务数据库以 SQLite `mode=ro` 和
`PRAGMA query_only=ON` 打开。

## 信息架构

工作台首页包含：

- 全部任务、需要关注、in progress 和 succeeded 数量；
- 可过滤、分页的任务索引；
- Attention Inbox；
- 任务详情：目标、当前状态、phase、错误和 attempt 信息；
- 当前/registered artifact evidence；
- 独立 verifier 结果和 assurance 边界；
- 受控 artifact preview；
- 只读 recovery preview；
- 按持久化 sequence 展示的 event timeline；
- lifecycle audit。

Attention 分类：

| 状态 | 工作台语义 |
| --- | --- |
| `awaiting_clarification` | Needs your answer |
| `interrupted` | Review required |
| `blocked` | Blocked; review the reason |
| `failed` / `planning_failed` | Failed; view the evidence |
| stale/consuming approval | Review required |

首版中所有 attention 项都是只读的。回答澄清、resume、cancel 和 recovery 仍通过既有
CLI/Python 入口完成。

## 只读后端

新增 `ReadOnlyWorkbench`：

- 直接读取现有 `tasks`、`events` 表；
- 不实例化会迁移或恢复 lease 的 `TaskService`；
- 查询有界：任务列表默认 40，最多 100；
- status 使用枚举参数化查询；
- task detail 保持事件 sequence 顺序；
- recovery preview 调用既有只读 `build_recovery_preview()`；
- artifact preview 只使用 `task.artifact.relative_path` 和既有安全 reader；
- 任务详情不返回 lease token；
- event payload 仅展示最多 12 个有界字段；
- 敏感键、Bearer token、`sk-*` 和常见 key/token/password 赋值会从摘要中移除或脱敏。

工作台无法从浏览器输入任意 SQL 路径或文件路径。

## HTTP 边界

支持的路由：

| Method | Route | 作用 |
| --- | --- | --- |
| GET/HEAD | `/` | 工作台静态页面 |
| GET/HEAD | `/api/tasks` | 有界任务列表和状态计数 |
| GET/HEAD | `/api/attention` | attention inbox |
| GET/HEAD | `/api/tasks/{task_id}` | 任务详情、事件、审计和 recovery preview |
| GET/HEAD | `/api/tasks/{task_id}/artifact` | 已登记 artifact 的只读文本预览 |

其他 method 返回 `405 read_only_workbench`。

网络与浏览器防护：

- server 只允许绑定 `127.0.0.1` / `localhost`；
- 非 loopback `Host` header 返回 `421`；
- `Cache-Control: no-store`；
- `X-Content-Type-Options: nosniff`；
- `X-Frame-Options: DENY`；
- `Cross-Origin-Resource-Policy: same-origin`；
- CSP 禁止外部脚本、样式、图片和表单；
- 静态前端不引用外部字体、CDN 或分析服务。

工作台没有认证，因此不应暴露到非本机网络。

## Artifact 预览

预览不接受文件路径。读取流程：

1. 从持久化 task 取得 registered artifact evidence；
2. 使用 `task_id + relative_path` 调用既有安全 artifact reader；
3. 计算当前 byte count 和 SHA-256；
4. 与 registered evidence 比较；
5. 展示 persisted verifier 结果和当前 observation 两个维度。

若文件在验证后被修改：

- 历史 verification 记录保持原样；
- workbench 显示 `modified_since_registration`；
- UI 显示 “not currently verified”；
- 文件不会被恢复或覆盖；
- 任务状态不会被工作台修改。

## Attention 与审批

Attention Inbox 从任务状态和既有 approval records 派生。

审批只在以下情况进入 attention：

- approved approval 的 `state_digest` 与当前 recovery preview 不一致；
- approval 处于 consuming；
- recovery attempt 已失败；
- approval store 或关联任务不可读。

审批展示不授权执行。Recovery preview 中即使 action 标记 `executable=false`，也不会出现
执行按钮。

## 视觉与响应式

设计采用“证据账本 / 运维控制台”视觉语言：

- 桌面双栏：任务索引 + 任务详情；
- 移动端单列，详情包含 “Back to tasks”；
- 状态使用文字和颜色双重表达；
- monospace 用于 task ID、event type、时间戳和 SHA-256；
- 支持 dark color scheme；
- 支持 keyboard focus 和 reduced-motion。

实际浏览器 smoke check：

| 场景 | 尺寸 | 结果 |
| --- | --- | --- |
| Desktop full page | 1265×2767 | 双栏、Attention Inbox、详情和 timeline 正常 |
| Mobile full page | 375×5038 | 单列布局，`grid-template-columns=351px`，task pane 非 sticky |

截图：

- `artifacts/audits/phase-3.91/workbench-desktop.jpg`
- `artifacts/audits/phase-3.91/workbench-mobile.jpg`

## 验收矩阵

| 项目 | 结果 |
| --- | --- |
| 无需 task ID 列任务 | PASS |
| status filter 和 bounded pagination | PASS |
| task detail 状态/事件/错误/verification/artifact 关联 | PASS |
| Attention Inbox | PASS |
| stale recovery approval 识别 | PASS |
| registered artifact preview | PASS |
| artifact drift 显示且不覆盖 | PASS |
| recovery preview 只读展示 | PASS |
| 不存在任务 404 / missing DB 503 | PASS |
| POST/PUT/PATCH/DELETE 405 | PASS |
| 任意 query path 400 | PASS |
| 非 loopback Host 421 | PASS |
| DB/artifact/event read-only hash | PASS |
| CSP/静态资产无外部依赖 | PASS |
| Desktop/mobile visual smoke | PASS |

审计矩阵中的 DB、artifact 和事件在请求前后保持字节级不变。

## 自动化验证

| 项目 | 结果 |
| --- | --- |
| 系统 Python unittest | 879/879 |
| `.venv` unittest | 879/879 |
| Mutation testing | 231 killed / 0 survived / 0 invalid / 0 error |
| Execution unverified | 0 |
| Node syntax check | PASS |
| Compileall | PASS |
| `git diff --check` | PASS |

新增变异覆盖：

- SQLite query-only 边界被移除；
- 非 loopback Host 被接受；
- POST mutation 被接受；
- artifact route 接受任意 query path；
- 敏感 event key 泄漏；
- Bearer token 未脱敏；
- artifact drift 被隐藏；
- stale approval 被隐藏。

## Phase 3.90 完整性

以下文件保持字节级一致：

| 文件 | SHA-256 | 状态 |
| --- | --- | --- |
| `artifacts/audits/phase-3.90/stable-summary.json` | `ac3abacb5645c710e58f0a2ded32777b7b85960ae65e2938c1d0d183ecc2f1ff` | match |
| `src/zevorinth/evals/planner_text_execution_v1.json` | `7f306abeaf7bd5c800a06eeb0fe71dad6eb504f4f42c7f65ec70bb2c5e59fbfb` | match |

Phase 3.90 baseline commit：`709b82d`。

## 本地产物

| 文件 | SHA-256 |
| --- | --- |
| `workbench-matrix.json` | `86514ff0ec5963f72f153ac2549110e1c112b2a94d6f795767dfd0bed3bfa65e` |
| `phase-3.90-integrity.json` | `bdb59751f7f77cc11af80611de2be33e1f6921377fca9f77289d48b73bd25299` |
| `mutations.json` | `a74d3cf94a5e79e35b4514d4c9ace00a8e212caa812312c27717c4a6876e4325` |
| `workbench-desktop.jpg` | `8500b2bb51d94cab8aa267957334fb24c2165f1f5819d73f9a350c3590ec50ba` |
| `workbench-mobile.jpg` | `df0c6fcc7f44081805b333beef1f9e3e6e03392b94f82e60bcb55b842ea67802` |

## 限制

- 工作台没有认证、远程访问或多用户隔离；
- 没有自动刷新、进度订阅或通知；
- event payload 只显示有界摘要，不提供完整原始 JSON；
- artifact preview 仅支持已登记 UTF-8 文本 artifact；
- 工作台不提供任何 mutation action；
- recovery preview 是现有后端协议的展示，不是恢复执行授权；
- 当前设计优先级来自架构缺口判断，尚未经过用户研究；
- 没有真实模型请求，也没有新增外部网络访问；
- 数据库与 artifact 的来源真实性仍是 L0/L1 内部一致性边界。
