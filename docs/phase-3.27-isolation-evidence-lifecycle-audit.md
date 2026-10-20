# Zevorinth Phase 3.27：隔离证据生命周期与执行前重验

## 1. 结论与基线

**PASS WITH LIMITATIONS。** 证据现在有签发时间、TTL、请求绑定与命令摘要，并在**每次**受保护执行前用实时运行时、镜像 digest、配置摘要与命令摘要重新核验；过期、缺失、篡改、跨请求复用、版本不兼容一律阻断，不回退宿主机执行。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `d46dda7eac5c9c1a8e34d21b0c50822c1a00a224` |
| 分支 | `phase-2.6-response-hardening` |
| 平台 | macOS，Python 3.14.6 |

## 2. 生命周期与 TTL

| 项 | 值 |
| --- | --- |
| `EVIDENCE_TTL_SECONDS` | 900（15 分钟，模块级常量，可注入） |
| `CLOCK_SKEW_SECONDS` | 5 |
| 证据字段 | `evidence_id`、`issued_at`、`expires_at`、`request_id`、`command_digest`、`container_config_digest`、`image_digest`、`runtime_identity`、`probes`、`digest` |
| schema | 1.1 |

语义：`issued → valid → expired`（超时）/ `invalidated`（摘要或身份不符）。过期只能**重新验证**，刷新时间戳会使 `digest` 失效并被判 `failed`。

TTL 与时钟：
- 时间用 `time.time()`（不可由任务提交者指定）；
- `issued_at` 晚于当前时间 + 5s 容差 → `blocked`（`evidence_issued_in_the_future`）；
- 缺字段、非数值、`expires_at <= issued_at` → `blocked`。

## 3. 执行前重验调用链

`run_protected_container(..., evidence, request_id)`：

1. 用**即将执行的真实命令**重建 `live_command`；
2. 计算 `expected_config` 与 `expected_command_digest`（同一份规范化命令）；
3. `_live_runtime_identity()`（`docker version`）；
4. `_live_image_digest(image)`（`docker image inspect`）；
5. `verify_evidence(...)`：摘要 → TTL → 请求绑定 → 命令摘要 → 运行时/镜像/配置 → 探针完整性；
6. 仅当 `state == "verified"` 才 `subprocess.run`。

因此“证据自报 verified”不足以放行：期望值全部来自现场。

## 4. 失效矩阵

| 变化 | 结果 |
| --- | --- |
| 运行时版本变化 | `blocked`（`identity_mismatch:runtime_identity`） |
| 镜像 digest 变化 | `blocked` |
| 配置摘要变化 | `blocked` |
| 命令摘要变化 | `blocked`（`command_digest_mismatch`） |
| 跨任务/请求复用 | `blocked`（`evidence_request_mismatch`） |
| verifier 版本变化 | `blocked` |
| 证据被篡改 | `failed`（`evidence_digest_mismatch`） |
| 过期 | `expired` |
| 仅刷新时间戳 | `failed`（摘要不含旧时间） |
| 探针缺项 / `NOT_EXECUTED` | `blocked` |
| 探针 `FAIL` | `failed` |

## 5. 竞态与残余风险

验证与启动使用同一份 `live_command` 计算摘要，但 `docker run` 与摘要计算之间仍有窗口：

- 镜像可能在验证后被删除或替换（`docker run` 会按本地 tag 解析）；
- 运行时可被重启或升级；
- 镜像内部内容不受 digest 之外的运行时校验。

因此：**重算摘要不等于消除启动竞态**。当前采取保守策略——验证失败即阻断、不重试、不回退；若启动时镜像缺失，`docker run` 会失败并返回非零退出码，不会被报告为成功。

## 6. 测试与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 492/492，33.1s / 33.3s | 0 |
| 证据生命周期专项 | 18/18 | 0 |
| 真实容器集成测试 | 1/1（真实执行） | 0 |
| Mutation runner | 49 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

新增变异：`expired_evidence_accepted`、`command_digest_change_ignored`、`evidence_replay_allowed`，均被捕获。

负向测试覆盖：TTL 内、刚好过期、明显过期、未来签发、时间格式错误、缺 `expires_at`、刷新时间戳、运行时/镜像/配置/命令/verifier 变化、篡改、跨请求复用、过期证据不启动进程、重验失败不启动进程、并发复用同一证据仍逐次重验。

## 7. 审计状态

可区分：`verified`、`expired`、`failed`（篡改/探针失败）、`blocked`（身份/版本/请求不符、缺项）、`unverified`（探针 `INCONCLUSIVE`）、以及 `process_started` 与否。

所有拒绝都带结构化 `reasons`。

`protected_processes_started_without_verified_isolation = 0`：由 `run_protected_container` 的单一入口保证——两条后端路径都必须在 `verified` 后才调用 `subprocess.run`；统计来源是该函数的返回记录，而非硬编码。

## 8. 安全边界

真实模型 API 请求 0；未联网、未安装依赖；未启动/关闭 Docker Desktop（沿用 3.26 已运行的实例）；未读取 `.env`；`.idea/` 未修改未暂存；未删除 quarantine 或用户数据；未推送。

## 9. 未执行

- 未在容器内验证“镜像在验证后被替换”的竞态（需要并发操纵镜像，超出本阶段授权）。
- 未验证运行时重启后旧证据的行为（`expired` 已覆盖时间维度，未覆盖重启维度）。
- `run_tests` 仍未迁移到容器后端。

## 10. 残余风险

- 证据没有持久化到磁盘的强制路径：当前由调用方传入内存对象；落盘与加载需在后续阶段定义（含完整性保护）。
- TTL 依据的是本机时钟，时钟被大幅回拨时无法检测（仅检测向前偏移）。
- 镜像 tag 解析与 digest 校验之间仍存在窗口。
- 容器逃逸/内核漏洞不在验证范围。
