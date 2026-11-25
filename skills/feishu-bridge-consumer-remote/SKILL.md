---
name: feishu-bridge-consumer-remote
description: 在另一台机器（尤其 Windows）上远程消费 feishu-bridge 事件：走 HTTP 事件网关（Authorization Bearer token）或 SSH 调 Mac 上的 `feishu-bridge events` CLI，完成 claim → 处理 → reply/ack/fail。Use when the consumer session runs on Windows or any non-Mac host and must answer Feishu personal messages through a bridge running on the Mac, or when setting up/checking the remote HTTP gateway.
whenToUse: 当消费者会话不在 Mac 本机（Windows / Linux / 另一台 Mac），需要通过 HTTP 网关或 SSH 接入 feishu-bridge 事件通道时使用。消费者与 bridge 同机时用 `feishu-bridge-consumer` 本地版，不要用本 skill。
---

# 远程消费 feishu-bridge 事件（Windows / 跨机）

**Windows 拓扑见仓库根 [`WINDOWS-DECISION.md`](../../WINDOWS-DECISION.md)（唯一权威）**：bridge 装在哪、Windows 走形态 A 还是 B、以及「不要做的 6 件事」都在那里；本 skill 只管**消费**，不提供安装路径、也不定义拓扑。本 skill 让**另一台机器上的 DSH / Codex 会话**通过网络消费同一条事件队列；语义不变：竞争消费、租约、claim_token、至少一次 + event_id 幂等、回复由 Mac 上的 sender 线程发出。

原生 Windows 不能运行仓库的 Python 消费者运行时（依赖 POSIX `flock`/进程组，`consumer_runtime.require_posix_runtime()` 会在 claim 前拒绝），所以 Windows 侧的消费路径就是本 skill：HTTP 网关 + 会话消费者，或 SSH 调 Mac/WSL2 的 CLI。

## 两种传输，选一种

| 方式 | 适用 | 安全边界 |
|---|---|---|
| **HTTP 网关**（推荐 Windows） | Windows 会话直接发 HTTP | 默认只绑 `127.0.0.1:8766`；Windows 通过 SSH 隧道访问，或 Mac 上 `--allow-remote` + 防火墙白名单 |
| **SSH 远程 CLI** | 任何有 ssh 的机器 | 复用 Mac 的 SSH 认证；正文走 ssh stdin，不落 argv |

两条路都**不直连飞书**、不调 lark-cli。

## Mac 侧准备（一次性）

```bash
# 1) 生成/查看共享 token（0600 文件；--rotate 会轮换，旧 token 在重启 events serve 后才失效）
feishu-bridge events token
# 2) 启动网关（前台；默认只绑回环）
feishu-bridge events serve --host 127.0.0.1 --port 8766
# 3) 确认 consumer 已注册（Windows 侧用 win-main；名字可换）
feishu-bridge events register --consumer win-main --kind other
```

Windows 连 Mac 的两种网络方式：

```powershell
# A. 推荐：SSH 隧道，把 Mac 的回环端口映射到 Windows 本机回环
ssh -N -L 8766:127.0.0.1:8766 <mac-user>@<mac-host>
# 之后 Windows 访问 http://127.0.0.1:8766 即可，无需 Mac 暴露端口

# B. 局域网直连：Mac 上 events serve --host 0.0.0.0 --allow-remote
#    必须配合防火墙只放行可信网段，不要把 8766 暴露到公网
```

把 token 放进 Windows 会话环境变量（不要写进仓库/日志）：

```powershell
$env:BRIDGE_TOKEN = '<token>'
$env:BRIDGE_BASE  = 'http://127.0.0.1:8766'
```

## 消费循环（HTTP）

所有 `/v1/*` 都要 `Authorization: Bearer <token>`；`/health` 不需要。

```powershell
# 1) 领事件（空数组 = 本轮结束，别空转）
$claim = Invoke-RestMethod -Method Post -Uri "$env:BRIDGE_BASE/v1/events/claim" `
  -Headers @{ Authorization = "Bearer $env:BRIDGE_TOKEN" } `
  -ContentType 'application/json' `
  -Body (@{ consumer='win-main'; limit=5; lease_seconds=600; kind='message' } | ConvertTo-Json)
$claim.events

# 2) 处理 payload.text / payload.context，写回复正文（纯文本，≤1500 字，不要 @ 人）
# 3) 提交回复（reply 成功即事件 done，不要再 ack）
Invoke-RestMethod -Method Post -Uri "$env:BRIDGE_BASE/v1/events/$($ev.id)/reply" `
  -Headers @{ Authorization = "Bearer $env:BRIDGE_TOKEN" } `
  -ContentType 'application/json' `
  -Body (@{ consumer='win-main'; claim_token=$ev.claim_token; body=$replyText } | ConvertTo-Json)

# 4) 处理失败（可恢复 → 延迟重试；不可恢复 → permanent 进死信并转人工）
Invoke-RestMethod -Method Post -Uri "$env:BRIDGE_BASE/v1/events/$($ev.id)/fail" `
  -Headers @{ Authorization = "Bearer $env:BRIDGE_TOKEN" } `
  -ContentType 'application/json' `
  -Body (@{ consumer='win-main'; claim_token=$ev.claim_token; error='model timeout'; retry_delay=30 } | ConvertTo-Json)
```

- `claim` 返回的每个事件含 `claim_token`（**只在 claim 响应里出现**）；`reply/ack/fail` 必须带同一个 token。
- 空结果是正常的：`{"count":0,"events":[]}` → 本轮结束，等下一次 `/loop`。
- `message` 事件只能 reply（reply 自动 done）；`attention` 事件只能 ack 或 fail。
- 重复 reply（同 token + 同正文）返回 `duplicate:true`，不会重发；正文不同会被拒（不可变 outbox）。
- 超时（Mac 侧 `external.json.reply_timeout_seconds`，默认 600s）未回复 → 事件 dead + inbox `needs_attention`（转人工，不丢消息）。

## API 速查

```text
GET  /health                                         # 公开
GET  /v1/events/status                               # external/events/consumers 摘要
GET  /v1/events?consumer=win-main&state=pending&limit=50
GET  /v1/events/{id}                                 # 不含 claim_token
GET  /v1/events/stats | GET /v1/events/consumers
POST /v1/events/claim          {consumer, limit, lease_seconds, kind}
POST /v1/events/{id}/ack       {consumer, claim_token}
POST /v1/events/{id}/fail      {consumer, claim_token, error, retry_delay, permanent}
POST /v1/events/{id}/reply     {consumer, claim_token, body}
```

错误统一为 `{"ok":false,"error":{"code":"...","message":"..."}}`；`401` token 错/缺失，`409` 租约/状态冲突或 consumer 未注册，`400` 参数错，`404` 路由/事件不存在，`413` 正文过大（>64KB）。

## 替代方案：SSH 远程 CLI

不想开网关时，直接远程执行 Mac 的 CLI（语义与本地版 skill 完全一致，只是加 ssh 前缀）：

```powershell
ssh mac 'feishu-bridge events claim --consumer win-main --limit 5 --lease 600 --kind message'
# 正文通过 ssh stdin 传，不要放 argv：
$body | ssh mac 'feishu-bridge events reply --consumer win-main --event 42 --claim-token <token> --body-file -'
```

缺点：每条命令一次 SSH、claim_token 要手工传递；优点：零新增端口。

## 能力模型（与同机消费者一致）

远程会话消费者和 Mac 上的常驻入口共用一套能力模型：**回复 + 检索（读文件/搜索/联网）+ 执行并完成（跑命令、改/建文件）**允许；**删除/破坏性操作、读取或外传密码密钥令牌 `.env` 私钥、委派子代理、MCP** 禁止。

- 这台机器上的会话可以实际检索和动手，不需要也不应该在回复里假装做过。
- 没有本机适配器兜底：没有 `workspace-write` 沙箱、没有流式工具事件检测、没有 `finalize_answer` 出站脱敏。回复正文要自查，不能带密钥原文；做不到就说清缺什么。
- 汇报只写实际看到和做过的事；不要把没执行的步骤说成已完成。
- 不要声称本路径与 Cursor/DSH/Codex 常驻入口的沙箱等价。

## 红线

- **不**直接调用 lark-cli / cursor-agent，不自己发飞书消息，不写 bridge 状态文件。
- **能力边界**：不得删除文件或目录、不得做破坏性操作（`rm`、清空、删库、格式化、强制推送等）、不得读取或打印 `.env`/私钥/密码/令牌并写进回复、不得委派子代理或使用 MCP。
- token 不写进仓库、不打印到日志、不通过聊天明文长期保存；泄露后 `feishu-bridge events token --rotate` 轮换。
- 同一 event_id 只处理一次；未回复的 message 事件不要 ack。
- 不要把 `--allow-remote` 网关直接暴露公网；跨公网请用 SSH 隧道/VPN。
- 不执行 Mac 上的 `install.py` / `launchctl` / `feishu-bridge start|stop|restart`。

## 安装（Windows 无 symlink）

```text
把整个 skills\feishu-bridge-consumer-remote 目录复制到：
  %USERPROFILE%\.dsh\skills\feishu-bridge-consumer-remote\
以及（用 Codex 时）：
  %USERPROFILE%\.dsh\skills 或 %USERPROFILE%\.agents\skills（DSH 不扫 .codex）\feishu-bridge-consumer-remote\
```

DSH Windows 会话用 `/loop 30s` + `SESSION-PROMPT-WINDOWS.txt` 的 prompt；Codex 用 Automation 定时跑同一段 prompt。

## 排障

| 现象 | 处理 |
|---|---|
| `401 unauthorized` | 检查 `$env:BRIDGE_TOKEN` 与 Mac 上 `events token` 是否一致；轮换过要同步 |
| 连接被拒 | Mac 上 `events serve` 是否在跑；`ssh -L` 隧道是否建立；端口是否被占用 |
| `409 consumer_not_registered` | Mac 上 `events register --consumer win-main --kind other` |
| `409 ... not leased` | 租约过期或被其他 consumer 接管：重新 claim 拿新 token；慢会话 `lease_seconds=900` |
| `409 ... only message events can be replied` | attention 事件用 ack/fail，不要 reply |
| `413 payload_too_large` | 回复正文超 64KB：缩短正文（bridge 最终也会按 6000 字截断） |
| 事件一直 pending | 没有 consumer claim，或 fail 退避未到；Mac 上 `events status` 查看 |
