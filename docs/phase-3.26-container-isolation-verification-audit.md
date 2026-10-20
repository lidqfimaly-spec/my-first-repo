# Zevorinth Phase 3.26：容器隔离验证与证据闭环

## 1. 结论与基线

**PASS WITH LIMITATIONS**（容器边界：**VERIFIED**，在本机授权运行时上真实执行）。

用户给出全面授权后，本机 Docker Desktop 被启动（此前禁止启动），拉取 `alpine:3.20`，并在硬化配置下完成七项真实探针。独立验证器判定 `verified`，`run_protected_container()` 实际启动了受保护进程；篡改证据则被拒绝且进程未启动。

| 项目 | 值 |
| --- | --- |
| 起始 HEAD | `2e66edb5c3966e93c4ef5b80ce35014e0ce3c896` |
| 阶段提交 | `3ee8afca69b3ee96b84582ebaad2c905705007d5`（当时 BLOCKED） |
| 本文档更新 | 本提交（真实运行时证据） |
| 平台 | macOS，Python 3.14.6 |
| 运行时 | Docker 29.2.1（Desktop，`/Applications/Docker.app`） |
| 镜像 | `alpine:3.20` = `alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc` |

## 2. 授权与操作记录

| 操作 | 说明 |
| --- | --- |
| `open -a Docker` | 用户全面授权后启动；daemon 4 秒内就绪 |
| `docker pull alpine:3.20` | 用户授权后联网拉取（13.7 MB），digest 已记录 |
| 容器运行 | 仅使用临时工作区与人工创建的诱饵文件 |

## 3. 硬化配置（实际使用）

```
docker run --rm --network none --read-only --user 65534:65534
  --cap-drop ALL --security-opt no-new-privileges
  --pids-limit 64 --memory 256m --cpus 0.5
  --tmpfs /tmp:rw,noexec,nosuid,size=16m
  --mount type=bind,src=<workspace>,dst=/workspace --workdir /workspace
  alpine:3.20 /bin/sh -c <probe>
```

配置摘要（命令规范化后的 SHA-256）记录在证据文件的 `container_config_digest`。

## 4. 七项真实探针结果

| 边界 | 期望 | 实测 | 判定 | 拒绝层 |
| --- | --- | --- | --- | --- |
| 工作区内读取 | allowed | allowed | PASS | 允许 |
| 工作区内写入 | allowed | allowed | PASS | 允许 |
| 工作区外读取 | denied | denied | PASS | 宿主路径在容器内不存在 |
| 工作区外写入 | denied | denied | PASS | **内核 EROFS**（read-only rootfs） |
| 网络 | denied | denied | PASS | **运行时拒绝**（`--network none`） |
| 凭据占位 | denied | denied | PASS | 路径不存在/非 root |
| 子进程 | allowed（受 pids-limit 约束） | allowed | PASS | 允许（有界） |

stderr 证据：`/bin/sh: can't create /outside.txt: Read-only file system`。

## 5. 独立验证器判定

`verify_evidence()` 用**实时** runtime 版本、镜像 digest 与即将执行的命令摘要重新计算：

| 项目 | 值 |
| --- | --- |
| 状态 | `verified` |
| 证据 digest | `975d54d82ad32f614cbf6de89153d9f421053398fa23de30c9c8a497035ddebf` |
| 拒绝原因 | 无 |

门禁行为：

| 场景 | 结果 |
| --- | --- |
| 完整且身份一致的证据 | `run_protected_container` **实际启动**，exit 0 |
| 篡改任一 probe 状态 | `failed`，`process_started=false` |
| 镜像/配置 digest 与实时环境不符 | `blocked` |
| `NOT_EXECUTED` / 缺 boundary | `blocked` |
| 缺证据 / 旧 verifier 版本 | `blocked` |

## 6. 回归与变异

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| 系统 Python 全量 unittest（连续两次） | 474/474，32.9s / 32.7s | 0 |
| 容器实测集成测试 | 1/1（真实容器） | 0 |
| 证据验证器专项 | 13/13 | 0 |
| Mutation runner | 46 killed / 0 survived / 0 invalid / 0 error | 0 |
| `compileall` / `git diff --check` | 无输出 | 0 |

Phase 3.21–3.25 的安全保证（意图门禁、quarantine 校验、Seatbelt `partial`、`run_tests` 非隔离类别）全部保持。

## 7. 产物

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/audits/phase-3.26/container-evidence.json` | `0436f0a14f68eca99755093802535f55ec0da2c703959e20d8720a0c8e08f322` |
| `artifacts/audits/phase-3.26/mutations.json` | `6116a8fb6e57450843dc0af582bf112667668dee381a93b8ff0c6fd8f06f9941` |

## 8. 安全边界

- 真实模型 API 请求：0；
- 联网仅用于 `docker pull alpine:3.20`（用户全面授权）；
- 未读取 `.env` 或任何凭据；
- 未挂载宿主目录（仅临时工作区）、未暴露 docker.sock、未继承宿主环境变量；
- `.idea/` 未修改未暂存；未删除 quarantine 或用户数据；未推送。

## 9. 残余风险

- 证据**有效期**仍未实现：验证器要求实时身份匹配，但尚无过期时间与自动失效；调用方必须在每次执行前重新验证。
- 容器逃逸/内核漏洞不在本阶段验证范围；`--cap-drop ALL` 与只读根不构成完整内核加固证明。
- 子进程边界是“pids 限制”而非“禁止创建”；已在证据中如实记录为 `allowed`。
- 镜像来自 Docker Hub，未做签名验证（仅记录 digest）。
- Docker Desktop 已被启动，属于对系统状态的变更；是否保持运行需用户决定。
