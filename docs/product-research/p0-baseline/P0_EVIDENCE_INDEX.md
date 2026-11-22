# P0 · 证据索引

`[P0_EVIDENCE_INDEX · 2026-11-22]`

> 每条证据给出：编号 / 证明什么 / 位置 / 复现方式 / 时长。
> 复现命令假设 `cd /Users/lidequan/PythonProject/Zevorinth` 且服务已按 §0 启动。

---

## 0. 环境与启动（复现前置）

```bash
cd /Users/lidequan/PythonProject/Zevorinth
rm -rf /tmp/zv-audit-home && mkdir -p /tmp/zv-audit-home
PYTHONPATH=src ./.venv/bin/python -m zevorinth --home /tmp/zv-audit-home control-center --port 8791
# 另开一个终端：
TOKEN=$(curl -s http://127.0.0.1:8791/control/api/session | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
```

> 所有写操作都落在 `/tmp/zv-audit-home`。**不要**把 `--home` 指向仓库根目录，否则会污染 `data/` 与 `artifacts/`。

---

## 1. 实跑证据（本轮真实产生）

| ID | 证明什么 | 位置 | 复现 | 时长 |
| --- | --- | --- | --- | --- |
| E-01 | 服务可启动、loopback、能力声明 | `GET /control/api/session` | `curl -s $B/control/api/session` | 5 s |
| E-02 | 首跑空库任务列表返回空 `counts` | `GET /api/tasks` | `curl -s "$B/api/tasks?limit=5"` | 2 s |
| E-03 | J1：自然语言目标被要求 3 个内部字段 | `POST /control/api/conversations` → `POST .../messages` | 见 `P0_USER_JOURNEY_AUDIT.md` J1 脚本 | 10 s |
| E-04 | J1：非法澄清答案被原样写入 `operation="写文件"` | `GET /api/tasks/{id}` 的 `operation` 字段 | 同上，答 `写文件` 后取详情 | 5 s |
| E-05 | J1：4 轮对话可完成写文件任务 | 同上，答 `write_text_artifact`/`note2.txt`/正文 | 同上 | 15 s |
| E-06 | 会话只有 user 消息 | `GET .../messages` | `curl -s "$B/control/api/conversations/<cid>/messages?limit=50"` | 3 s |
| E-07 | 写文件任务成功 + 独立验证 | `GET /api/tasks/{id}` 的 `verification`/`artifact_observation` | 高级表单路径，见 J1 附 | 15 s |
| E-08 | 产物预览（64 KiB 上限） | `GET /api/tasks/{id}/artifact` | `curl -s "$B/api/tasks/<tid>/artifact"` | 3 s |
| E-09 | 代码修改成功 + 备份 + 重验 | `workspaces/demo/calc.py` + `artifacts/<tid>/workspace-backups/*.bak` | `POST /control/api/tasks`(edit_text_file) → start | 15 s |
| E-10 | 策略拒绝（旧文本不存在）且文件未动 | `error.code=text_match_count`，事件止于 `policy_rejected` | 同 E-09 但 `old_text` 写错 | 10 s |
| E-11 | 策略拒绝（摘要过期） | `error.code=source_file_changed` | 同 E-09 但 `expected_sha256` 用 64 个 0 | 10 s |
| E-12 | J2：`execute_plan` 创建成功但不可启动 | `GET .../actions` 无 `start_queued_task`；`POST` 返回 `task_start_not_allowed` | 见 J2 | 10 s |
| E-13 | J9：状态询问的精确匹配边界 | `intent.kind`：`进度`→`status_query`；`现在任务进行到哪了？`→`clarification_answer` | 见 J9 | 8 s |
| E-14 | J3：改口被判 `modify_pending` / `requires_confirmation` | `intent.kind` 字段 | 见 J3 | 5 s |
| E-15 | 多步引擎可审批 + 部分成功（**产品不可达**） | `TaskService` 直测：`awaiting_approval` → `partial`，`step_results` 三态 | 见 J2 引擎段脚本 | 20 s |
| E-16 | 只读模式首跑 503 无引导 | `workbench` 子命令 + 空库 | `PYTHONPATH=src ./.venv/bin/python -m zevorinth --home /tmp/zv-empty workbench --port 8792` 后 `curl /api/tasks` | 8 s |

---

## 2. 代码证据（静态，可逐条打开核对）

| ID | 结论 | 位置 |
| --- | --- | --- |
| C-01 | 零运行时依赖 | `pyproject.toml:11` |
| C-02 | 服务只绑 loopback，CLI 不暴露 host | `workbench_server.py:47,1604-1614,1630-1640` |
| C-03 | 路由是手写正则 + if/elif，共 29 个组合 | `workbench_server.py:64-115,168-231,989-1189` |
| C-04 | 写路由需 Origin + 进程内 token；`/session` 不校验且下发 token | `workbench_server.py:1265-1299,1346-1366` |
| C-05 | 前端 SSE 只当触发器；无 `onerror` | `workbench_static/app.js:1860-1877,1916-1918` |
| C-06 | 事件 payload 被投影成形状（list → count/type） | `workbench.py:1048-1056,1145-1182` |
| C-07 | 14 个状态 + 迁移表；SUCCEEDED 只能从 VERIFYING | `models.py:14-28`、`state_machine.py:24-113` |
| C-08 | `record_event_and_update` 不校验迁移 | `event_store.py:2307-2364` |
| C-09 | 默认确定性规划器；控制中心显式传入 | `service.py:361`、`workbench_control.py:586,1157` |
| C-10 | LLM 规划器只可选 2 个操作，不可产多步 | `llm_planner.py:78-81,107-179` |
| C-11 | 缺字段即澄清，文案 `请提供 {field}` | `planner.py:473-490,556-586,625-630` |
| C-12 | 必填字段表在 `policy.py` 重复一份 | `policy.py:54-115` |
| C-13 | 执行同步；控制面全局锁 | `service.py:431-443`、`workbench_control.py:201,1154` |
| C-14 | 租约 60 s / 执行预算 600 s | `service.py:323-324` |
| C-15 | 暂停只在多步步边界 | `service.py:3278-3294,3439-3455` |
| C-16 | 运行中取消被拒 | `event_store.py:2141,2163-2166` |
| C-17 | 只有 `run_tests` 可进程级中断 | `service.py:6346-6351`、`local_test_runner.py:444-447` |
| C-18 | 恢复：有证据只重验，无证据整步重跑 | `service.py:3163-3213` |
| C-19 | 工具 `TOOL_STARTED` 后不重放 | `service.py:3406-3416` |
| C-20 | 多步上限 8 步、3 次尝试、fail_fast/continue | `multi_step.py:16-29` |
| C-21 | 部分成功 = PARTIAL 且终态 | `service.py:3893-3914`、`retry_policy.py:147-153` |
| C-22 | 8 个操作；`execute_plan` 不在其中 | `capabilities.py:35-322,937` |
| C-23 | 控制面可启动集 = 支持集 - run_tests | `workbench_control.py:52-54` |
| C-24 | policy 只作用于单步；多步父任务先分支 | `service.py:3063-3080` |
| C-25 | 审批绑 step_id + plan_version + lease，无 digest | `models.py:180-219`、`service.py:867-956` |
| C-26 | `approved_step_ids` 不清空；replan 只清 pending | `service.py:935-945,1026-1055,3340` |
| C-27 | 写/读/改/inspect/edit/research 均重读重算验证 | `verifier.py:109-137,216-224`、`workspace_inspection.py:358-372`、`workspace_edit.py:244-254`、`research_workflow.py:402-405` |
| C-28 | shell 验证只看形状 | `service.py:6166-6175` |
| C-29 | 产物路径与硬链接发布 | `artifact_tool.py:382-440` |
| C-30 | 无下载端点、无 `Content-Disposition` | 全仓 grep |
| C-31 | 会话历史不进规划；只取当前消息 | `conversation_service.py:308-320` |
| C-32 | 历史上限 200 且取最早 | `event_store.py:126,1237` |
| C-33 | 记忆只被 UI 接口读写 | `workbench_server.py:643,647,735` |
| C-34 | 意图 = 5 个精确字符串 | `intent.py:15-16,59-62` |
| C-35 | `isolation.py` profile 允许全局读，且无调用方 | `isolation.py:42-47,61-76` |
| C-36 | `safety_gate` 只在 CLI | `cli.py:1258-1264` |
| C-37 | 测试规模与构成 | `tests/` 111 个 `test_*.py`、1 687 个 test 函数、0 真实模型 |

---

## 3. 文档证据（项目自有文档，作为"设计意图"而非"已实现"）

| ID | 用途 | 位置 |
| --- | --- | --- |
| D-01 | 设计总纲 | `DESIGN.md` |
| D-02 | 产品能力总表（66 KB） | `README.md` |
| D-03 | 产品路线图与各格状态 | `docs/product-implementation-roadmap.md` |
| D-04 | P0-2 质量基线历史（v2/v3/V3.1） | `docs/p0-2-*.md` |

`FACT`：`README.md` 与 `docs/product-implementation-roadmap.md` 描述的**状态**比产品实际可做的多
（示例：README 记录多步与审批能力，但产品面无法启动 `execute_plan`）。引用文档前必须先用 E-12 对一下。

---

## 4. 外部证据（竞品）

| ID | 产品 | 等级 | 链接 |
| --- | --- | --- | --- |
| X-01 | Claude Artifacts | `OFFICIAL_SOURCE` | https://support.anthropic.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them |
| X-02 | Claude 核心概念（权限卡/计划审批） | `OFFICIAL_SOURCE` | https://claude.com/docs/claude-science/core-concepts |
| X-03 | Claude 记忆与项目记忆 | `OFFICIAL_SOURCE` | https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context |
| X-04 | Claude Artifact 版本与溯源 | `OFFICIAL_SOURCE` | https://claude.com/docs/claude-science/artifacts |
| X-05 | Manus 快速上手（Plan Mode/Branch/Projects） | `OFFICIAL_SOURCE` | https://manus.im/docs/zh-tw/introduction/quickstart |
| X-06 | Manus Sandbox 机制 | `OFFICIAL_SOURCE` | https://media@manus.im/blog/manus-sandbox |
| X-07 | Cursor Cloud Agents changelog（steering） | `OFFICIAL_SOURCE` | https://www.cursor.com/changelog/08-19-26 |
| X-08 | Dify Human Input 节点 | `OFFICIAL_SOURCE` | https://docs.dify.ai/zh/cloud/use-dify/nodes/human-input |
| X-09 | Manus 真实使用体验 | `THIRD_PARTY_SOURCE` | https://www.iaifeed.com/how-to-delegate-a-research-task-to-manus-im |
| X-10 | Cursor 后台代理综述 | `THIRD_PARTY_SOURCE` | https://www.simular.ai/articles/cursor-background-agents |

---

## 5. 证据缺口（本报告不能证明什么）

| 缺什么 | 影响 |
| --- | --- |
| 真实用户样本 | H-1/H-6 无法验证；"是否只是内部字段名问题"无法定论 |
| 语义级正确性 ground truth | M-01/M-08 不可测 |
| 并发/故障注入实测 | R-03 的改造风险只能估算 |
| 7 个竞品的官方来源 | 竞品结论只能用于方向，不可对外 |
| 逐像素 UI 评审 | 视觉/可访问性结论仅来自代码，未做人眼验证 |
