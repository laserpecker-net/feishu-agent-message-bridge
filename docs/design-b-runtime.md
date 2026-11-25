# B 档第一段：消费者运行时解耦（设计稿，未实施）

> **定位**：B 档已落地的部分（出口唯一、任务卡与审批、命令表）以 [CONTRACTS.md](../CONTRACTS.md) 为唯一事实；
> 还没做的（S3a/S3b 干活端、S5 平台验收）见 [design-b-loop.md](design-b-loop.md)。
> 本文只展开它列为「非目标」、却会在接任务卡之前先撞墙的一段：**回话端消费者运行时**。
> 落地后并进 `CONTRACTS.md` 与 `docs/HISTORY.md`，本文件删除。

## 0. 结论：B 档的第一行代码不是任务卡

三条都能从现有代码/配置复跑：

1. **死线从消息创建时刻起算 600s**：`bridge/gateway.py:121-123`，`reply_timeout_seconds` 缺省 600。
2. **消费者串行，实测 245s/条**：见 [plan-s1-outbox-v8.md](plan-s1-outbox-v8.md) §7 的取舍表。
3. **事件租约只有 120s**：`bridge/store.py:950`，`claim_events(..., lease_seconds=120)`；领取即开始计时，
   续租目前只出现在 say 续命/超时判定路径（`store.py:1230`、`:1358`），不是生成期间的常态。

后果：三条消息依次到达，第三条要等约 490s，只剩 110s 生成时间——它一定死在 600s 上。
S1 的 `SAY_GRACE_SECONDS=3600` 只保住了「同一条消息的回话窗口」，没有解决「消息入口被长活堵住」。

所以顺序是 **B0（出槽并发）→ B1（thread + owner）→ S2（已完成）→ S3a → S3b**。
B0 是其中唯一「不改就一定出事」的一条（plan-s1 §7 的原话），S2 的卡片上线只会放大它：
人审要等主人回复，等待期越长，排队越深。

## 1. B0：领取与生成解耦

### 1.1 不变量

| # | 不变量 | 为什么 |
|---|---|---|
| I1 | 人的新消息不被长活饿死 | 队列里至少留一格给 `message`/`attention` |
| I2 | 租约 ≠ 生成时长 | 领取是短租约（120s）+ 心跳续租；生成可跑 30 分钟，事件不因生成中而 `dead` |
| I3 | 崩溃可回收且不重复 | 租约到期重派时，出站由 outbox `uuid` 幂等挡住（S1 已有） |
| I4 | 出槽可解释 | 能回答「这条为什么还没被领、被谁占着」 |

### 1.2 改动点

- **主循环并发槽**（`dsh-consumer.py`）：从「领一条 → 跑完 → 再领」改成「领一条 → 起一个槽 → 立刻回去领」，
  默认 `N=2`（1 个长生成 + 1 个短回话）。写库仍是单写者（SQLite WAL + 现有重试），并发只发生在生成侧。
- **心跳续租**：新增 `Store.renew_event_lease(event_id, consumer, claim_token, seconds)`，条件是
  `state='leased' AND claim_token=?`；每个槽每 30s 调一次。ack/fail 仍走现有 CAS，不新增状态值。
- **保底格**：`claim_events` 增加可选 `reserved_kind='message'`：领取时若队列里有可领的 message/attention，
  先发它。真正的 `role` 路由（replier/worker 分进程）在 S3 做，B0 只做「不饿死」的保底。
- **观测**：`events_view` 增 `lease_remaining`、`leased_age`；`feishu-bridge events stats` 打出
  「排队最久 / 在跑最久 / 各槽归属」。

### 1.3 验收（可复跑）

- 注入 3 条消息、每条生成人为拖 200s：600s 内三条都有 `reply`，无 `dead`；
- 生成中 `kill -9` 消费者：重启后未 ack 的事件在租约到期后被重派，outbox 不出现重复行（幂等 `uuid`）；
- 长活占满一个槽时，新消息领取延迟 < 5s；
- 单测：续租后租约不失效、`claim_token` 不符时续租被拒、并发槽下 `ack` 只成功一次。

## 2. B1：thread + owner

- **列**：`events.thread_key TEXT`、`events.owner TEXT`。建议与 S2 的 `tasks` 扩列合并成一次
  **v9 迁移**（S1 刚迁到 v8，两次迁移会把「一次窗口」变成「两次停服」）。
- **thread_key**：`chat_id + 根消息 id`。同一条 @ 消息的后续回复/追问落在同一 thread，
  payload 带 `thread: {key, root_mid, turn}`，后续消息不必重走全链路。
- **owner**：领取时写入 consumer；同一 thread 的后续消息优先给同一 owner，owner 租约过期且无心跳时释放。
  判断只读 `events`/`tasks` 表，**不许拿进程内存当缓存**（AGENTS §0「一条事实一个答案」）。
- **验收**：同一 thread 的三条消息由同一 owner 领到；第二条 payload 带前序摘要；换 owner 后能续上；
  owner 离线后 60s 内被接管，且不重复回话。

## 3. B1.5：上下文截断要诚实

`payload.context` 增 `truncated: boolean` + `omitted: {messages, bytes}`（缺省必须是 `false`，不许缺字段）。
补读入口：`feishu-bridge events context --event <id> --since <mid>`。

验收：超限时 `truncated=true` 且数字与实际省略量一致；未超限时 `false`。

## 4. 之后接 design-b-loop（不重复定义）

- **S2 任务卡与审批**（已落地）：三态解析、三档批准、`approval_mid/approval_kind/approved_at/executor`
  审计——以 [CONTRACTS.md](../CONTRACTS.md) 的「任务卡与审批」为唯一事实，本文不改口径。
- **S3a/S3b 干活端**：`Workbench(direct)` / `cmd-interop`、凭证 overlay、进程树收尾——
  以 design-b-loop §9、§10.1–10.4 为唯一事实。

## 5. 排期

| 切片 | 内容 | 估时 |
|---|---|---|
| B0 | 并发槽 + 心跳续租 + 保底格 + 观测 + 6 条用例 | 2–3 天 |
| B1 | `thread_key`/`owner`（v9 迁移，与 S2 合并）+ 载荷 + 接管 | 1–2 天 |
| B1.5 | 截断字段 + 补读入口 | 0.5 天 |
| S2 | 任务卡与审批（已落地，实际约 6 人日；偏差见 docs/HISTORY.md） | — |
| S3a/S3b | 干活端 mac / Windows | 2–2.5 天 |

## 6. 要主人拍板的三件事

1. **B0 槽位数**：2（建议，省 token）还是 3（更快消化突发）？
2. **v9 迁移是否与 S2 合并**：建议合并，一次停服窗口。
3. **第一件验收真活**：建议选只读任务（查资料/整理清单），先不碰写文件与对外发送。

## 7. 本轮未验证 / 风险

- **测试没跑**：本机 `python3.12`/`python3.14` 都没装 pytest，仓库也没有 `.venv`；本轮只做了代码与库结构核对，
  没有「pytest 全绿」这个结论。
- **库结构已到 v8**：本机生产库 `PRAGMA user_version=8`，`outbox` 已有 `row_id/inbox_id/seq/kind/retracted_at`，
  说明 S1 的结构已在线上库；但**线上进程跑的是哪个 release 没有核**（plist/unit 内容不算验证，AGENTS §2）。
- **并发槽的收尾**：Windows/WSL 侧的进程树必须按 design-b-loop §6.1 用 `taskkill /T` / `killpg` 收，
  否则孤儿 DSH 会继续烧 token；这块在 S3b 真机验，不在 B0 假装验过。
