# S1 详细设计：出口唯一（outbox v8 + `events say` / `events retract`）

> 状态：**已实现并上线（2026-11-17，release `e4e291dbc446a099`）**。本文保留为 outbox v8 表结构的唯一事实来源；行为契约见 [CONTRACTS.md](../CONTRACTS.md) 的「出站唯一出口」。
> 实施计划：[plan-s1-outbox-v8.md](plan-s1-outbox-v8.md)（三个 PR、9–11 人日、完成判据）
> **本文档是 outbox v8 表结构的唯一事实来源**（两份 DDL 曾互相矛盾，按仓库「一条事实一个答案」规则收敛到这里）。
> 版本轨迹：v1.1 修状态词汇 → v1.2 补迁移实测 → v1.3/v1.4/v1.5 吸收三轮 HY4 架构评审（20 条阻断）
> → **v1.6 吸收实施就绪度评审：2 处会照抄出错的硬矛盾（`complete_external_*` 的判重键、`events say`
> 参数互斥）+ 6 条必须预先定掉的决策 + 工时从 3.5 天修正为 9–11 人日**。

---

## 1. 为什么现在必须做

今天的 outbox 结构上只能「一条入站消息 → 一条回复」：

```sql
CREATE TABLE outbox (
    id TEXT PRIMARY KEY REFERENCES inbox(id),   -- PK 就是 inbox.id
    original_id TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL, ...
);
CREATE TRIGGER outbox_keep_rows BEFORE DELETE ...
CREATE TRIGGER outbox_immutable BEFORE UPDATE OF id,original_id,uuid,body,body_hash,created_at ...
```

三个后果，B 档会立刻遇到：**说不了第二句**（先答一句、干完再报做不到）、
**说错了收不回**（没有撤回通道）、**出口会变成三个**（Mac 桥 / HTTP 网关 / Windows 干活端）。

S1 不是加功能，是**把出口收成一个**，然后才允许 B 档接线。

---

## 2. 目标与非目标

**目标**

- 一条入站消息可产生 **N 条出站记录**，按 `seq` 顺序发出（say 在前、reply 在后）。
- 新增两种出站动作：`say`（补充说明/主动消息，不改任务状态）、`retract`（撤回已发出的那条）。
- 迁移一次性、可回滚；迁移后旧代码**拒绝启动**。
- 出站入口只有 `EventStore.append_outbox()` 一个。

**非目标（明确划出去，避免范围膨胀）**

- 不改 inbox 状态机，不改 `verify_reply` 判定语义。
- 不做「编辑已发消息」（飞书没这能力）。
- **`notify`（`cli.py:1362` 的 `Transport.send_dm`）本次不收编**，作为唯一例外写进
  `CONTRACTS.md`，收编挂到 S2；门禁用「文件+函数白名单」把它显式圈出来（见 §6）。
- 不做多机器人账号。

---

## 3. 数据模型 v8

```sql
CREATE TABLE outbox (
    row_id      INTEGER PRIMARY KEY AUTOINCREMENT,  -- outbox 行自己的身份
    inbox_id    TEXT REFERENCES inbox(id),          -- 来源消息；主动消息为 NULL
    chat_id     TEXT NOT NULL,                      -- 发到哪个会话（主动消息必需）
    seq         INTEGER NOT NULL DEFAULT 1,         -- 同一 inbox 内的发送顺序，从 1 开始
    kind        TEXT NOT NULL CHECK (kind IN ('reply','say','retract')),
    body        TEXT NOT NULL DEFAULT '',
    body_hash   TEXT NOT NULL DEFAULT '',
    target_mid  TEXT,                               -- retract: 要撤回的飞书 message_id
    uuid        TEXT NOT NULL UNIQUE,               -- 幂等键
    created_at  REAL NOT NULL,
    state       TEXT NOT NULL DEFAULT 'queued',
    next_due    REAL NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    verify_attempts INTEGER NOT NULL DEFAULT 0,
    attempt_id  TEXT, lease_until REAL, first_send_at REAL,
    reply_id    TEXT, sent_at REAL, verified_at REAL,
    retracted_at REAL,                              -- 被撤回的时刻（写在原行上，便于审计）
    error       TEXT
);
-- 只对"有来源消息"的行做 seq 唯一；主动消息（inbox_id IS NULL）不参与顺序域
CREATE UNIQUE INDEX outbox_seq ON outbox(inbox_id, seq) WHERE inbox_id IS NOT NULL;
CREATE INDEX outbox_due ON outbox(state, next_due, created_at);
```

设计取舍（每条都是 HY4 评审逼出来的）：

| 决定 | 理由 |
|---|---|
| `chat_id NOT NULL` + `inbox_id` 可空 | 上位文档要「主动消息」能力（不是回复某条消息，而是往会话里说一句）。`chat_id` 从 inbox 回填/由调用方给；`inbox_id IS NULL` 表示主动消息 |
| 部分唯一索引而非普通唯一索引 | SQLite 里 NULL 不参与唯一比较，普通 `UNIQUE(inbox_id,seq)` 会让所有主动消息都 seq=1 而不冲突——**看起来能用，语义是坏的**；用部分索引把意图写死（已实测：NULL 行可重复，同一 `(m1,1)` 被拦） |
| `row_id INTEGER PK`（不沿用 `id` 派生字符串） | 类型明确、AUTOINCREMENT 溯源清楚；代价是 12 处 `WHERE id=?` 必须一起改（§5.3 清单） |
| `retract` 是**独立行**，不改原行 `state` | 原行的 `reply_verified` 是审计事实，改成 `retracted` 会丢信息；撤回成功后在原行写 `retracted_at`（`state` 不在不可变触发器列表里，写它合法） |

### 3.1 状态机（严格照抄 `store.py` 现有词汇）

`store.py:350` 的未完成集合：`queued / send_started / verifying / verify_pending / retry_wait /
needs_attention / outcome_unknown`；成功终态是 **`reply_verified`**（`outbox_verified()` 里写死，
不是 `verified`）。**代码里没有 `sending` 这个状态**——`sending` 只存在于 `reminders`（`store.py:1062`），
是另一条状态机，别为了"对齐"把 outbox 也改成 `sending`。

两个冷知识（第 3 轮评审指出，v1.4 写错了）：

- **`dead` 目前在 outbox 里没有生产者**：`dead` 只在 `store.py:437`、`1426` 被**读**，
  cleanup 实际写的是 `dropped`（386/450）。所以 §4 的放行集里写 `dead` 是"防御性放行"，
  不是"现状会出现的状态"；将来谁引入 `dead` 生产逻辑，必须同时确认它在放行集里。
- `stats()` 的 `oldest_pending_at`（`store.py:1426`）硬编码
  `NOT IN ('needs_attention','outcome_unknown','dead','dropped')`——新终态 `sent` 必须补进去，
  否则"只有 say 行、reply 还没发"的消息会被算成最老待处理，把一个已推进的任务误报成堆积。

v8 唯一的词汇新增是 **`sent`**（say/retract 的成功终态）：

| kind | 路径 | 成功终态 | 失败去向 |
|---|---|---|---|
| `reply` | `queued` → `send_started` → `verify_pending` → `verifying` → **`reply_verified`** | `reply_verified`（并置 inbox `completed`） | `retry_wait` / `needs_attention` / `outcome_unknown` |
| `say` | `queued` → `send_started` → **`sent`** | `sent`（不校验可见性，**不碰 inbox**；且**续事件租约**到 `now + SAY_GRACE_SECONDS`＝1h，见 §11 偏差 11）
| `retract` | `queued` → `send_started` → **`sent`** | `sent`（只对目标行写 `retracted_at`；**不碰 inbox、不结束事件**，可在事件收尾后执行，见 §11 偏差 15） | `retry_wait` →（重试用尽）`needs_attention`；**永不进 `outcome_unknown`**（租约 120s 先过期，3300s 幂等窗口永远等不到，见 §11 偏差 3） |
| `retract` | `queued` → `send_started` → **`sent`** | `sent`（飞书删除成功），并把原行 `retracted_at` 写上 | 同上 |

**v7→v8 状态映射是恒等**（v8 只新增 `sent` 和 `retracted_at`，没有重命名任何状态）。
所以迁移不需要 `CASE state` 映射表，但**必须断言迁移前没有 in-flight 行**（见 §7 第 6 步），
否则那些 `send_started`/`verifying` 行会带着旧 `attempt_id`/租约进来。

`sent` 必须四处登记：

| 位置 | 现状 | 改成 |
|---|---|---|
| `UNFINISHED_OUTBOX_STATES`(350) | 7 个状态 | **不变**（`sent` 是已完成） |
| `drop_pending`(383) / `prune_expired`(412) | 依 `UNFINISHED_OUTBOX_STATES` | 不变 |
| `stats()`(1424) `GROUP BY state` | 按 state 计数 | `sent` 计「已发出」、`reply_verified` 计「已确认」，status 里**不合并**显示 |
| `claim_outbox`(555) 取行 SQL | `IN ('queued','retry_wait','verify_pending')` | 追加 `send_started`（租约过期重发；v7 这里本来就漏了） |

---

## 4. 顺序规则（只有一条，写进代码不是注释）

> **sender 只能取「同一 `inbox_id` 下 seq 最小的未完成行」。**

放行（不阻塞后续）的状态全集，**必须写死成常量并在测试里钉住**：
`reply_verified` / `sent` / `retracted` / `dead` / `dropped` / `needs_attention` / `outcome_unknown`。
阻塞的：其余（`queued` / `retry_wait` / `send_started` / `verify_pending` / `verifying`）。

- `inbox_id IS NULL` 的主动消息不在任何顺序域里，按 `created_at` 先来先发。
- 不同消息之间不保证顺序（B 档不需要）；撤回天然排在它要撤回的那条之后（seq 更大）。

反例：`SELECT * FROM outbox WHERE state='queued' ORDER BY created_at`（v7 现状是
`ORDER BY next_due,created_at,id`）——多行下会让「做完了」先于「收到我看看」发出。

### 4.1 这条 SQL 已实测（判据可复跑）

内存 SQLite 跑 6 个场景：

| # | 场景 | 实测 |
|---|---|---|
| 1 | `say(seq1,next_due=50)` + `reply(seq2,next_due=10)` | 拿到 **say**（seq 优先于 next_due）✅ |
| 2 | seq1 置终态 | 拿到 seq2 ✅ |
| 3 | seq2 置 `needs_attention` | seq3 **能发**，不堵死 ✅ |
| 4 | seq3 置 `dead` | seq4 **能发** ✅ |
| 5 | 同消息 seq1 是 `verify_pending` | seq2 **被阻塞** ✅ |
| 6 | seq1 置 `reply_verified` | 解锁 ✅ |

`EXPLAIN QUERY PLAN`：`SCAN o` + `CORRELATED SCALAR SUBQUERY 1` + `SEARCH p USING INDEX outbox_seq`。
单机每天几十到几百行没问题；到万级再加 `CREATE INDEX outbox_active ON outbox(state, next_due)`。

---

## 5. 接口合同

### 5.1 两个标识符（最容易写错的地方）

v7 里 `outbox.id` 既是 outbox 主键、又是 inbox 主键，`mid` 一个变量通吃。v8 必须拆开：

| 标识符 | 指谁 | 传给谁 |
|---|---|---|
| `row_id`（int） | outbox 的一行 | `mark_send_dispatched` / `outbox_sent` / `outbox_marked` / `outbox_verified` / `fail_outbox` / `get_outbox` |
| `inbox_id`（str） | 飞书原消息 | `get_message()` / `transport.reply()` / `transport.get_message()` / `verify_reply()` / inbox 状态更新 |
| `chat_id`（str） | 目标会话 | `transport.send_text()`（主动消息）/ 审计 |

`sender()` 里 `mid = outgoing.get('original_id') or outgoing.get('id')` 拆成 `row_id` / `inbox_id`
（主动消息没有 `inbox_id`，走 `send_text(chat_id, ...)`）。**sender 里不允许再出现 `mid` 这个变量名。**
写混的后果是静默失败：拿 inbox_id 去 `WHERE row_id=?` 查不到 → `_live()` False → 只打一行 error、不重试。

### 5.2 Python 接口

类名是 `Store`（`store.py:34`），不是 `EventStore`——本文早先写错了名字，实现时照 `Store`。

```python
Store.append_outbox(inbox_id, kind, body='', *, chat_id=None, target_mid=None,
                    dedupe_uuid=None) -> (row_id, seq)
    # inbox_id 为 None 时表示主动消息（此时 chat_id 必填），seq 恒为 1
    # dedupe_uuid 给了就用它当 uuid（调用方幂等键，如 say/retract 的 CLI 重试）
    # 实现：INSERT ... ON CONFLICT(uuid) DO NOTHING，冲突则返回既有行 → 重放安全

Store.claim_outbox(now, lease_seconds) -> row | None   # 只返回 min(seq) 可发的那行
Store.mark_send_dispatched(row_id, attempt, now)        # attempt 来自 claim 返回的 attempt_id
Store.outbox_sent(row_id, attempt, now, reply_id)       # 仅 kind='reply'：→ verify_pending
Store.outbox_marked(row_id, attempt, now, message_id)   # 仅 say/retract：→ sent，message_id 存 reply_id
Store.outbox_verified(row_id, attempt, now)             # 仅 kind='reply'：→ reply_verified
Store.outbox_retracted(row_id, attempt, now, target_row_id)  # retract 成功：→ sent + 原行 retracted_at
Store.fail_outbox(row_id, attempt, now, error, delay, permanent=False)
```

`outbox_sent()` 今天会校验 `reply_id` 非空（`store.py:585-586`），**这个校验保留**；
say/retract 不走它，走 `outbox_marked()`——否则传 `None` 直接 `ValueError`，
传假值又会被 `mode` 判成 verify（第 3 轮评审的阻断 3）。

- `append_outbox` 算 seq 必须带 `WHERE inbox_id = ?`：
  `SELECT COALESCE(MAX(seq),0)+1 FROM outbox WHERE inbox_id = ?`（漏了就是全局自增，seq 从 1 开始就废了）。
  与 `BEGIN IMMEDIATE` 同事务，`_transaction` 已是 `BEGIN IMMEDIATE` + 进程内 `RLock`。
- `attempt_id` 只有一个来源：`claim_outbox` 返回的 `attempt_id`；`_live()` 用它 + 租约校验。
- `claim_outbox` 取行 SQL（kind 感知、主动消息不参与顺序域）：

```sql
SELECT o.* FROM outbox o
 WHERE o.state IN ('queued','retry_wait','send_started','verify_pending')
   AND o.next_due <= :now
   AND (o.state != 'send_started' OR o.lease_until IS NULL OR o.lease_until < :now)
   AND (o.inbox_id IS NULL OR NOT EXISTS (
         SELECT 1 FROM outbox p
          WHERE p.inbox_id = o.inbox_id AND p.seq < o.seq
            AND p.state NOT IN ('reply_verified','sent','retracted','dead','dropped',
                                'needs_attention','outcome_unknown')))
 ORDER BY o.next_due, COALESCE(o.inbox_id,''), o.seq
 LIMIT 1
```

- `outcome_unknown` 升级（`store.py:549-554`）**必须加 kind 限制**，否则一条 say 超时会把整条任务标红：

```sql
UPDATE outbox SET state='outcome_unknown', ...
 WHERE kind='reply' AND state IN ('queued','retry_wait') AND reply_id IS NULL
   AND first_send_at IS NOT NULL AND first_send_at <= :now - UNCERTAIN;
-- 紧随其后的 UPDATE inbox SET needs_attention 同样只对 reply 生效
```

- `mode` 判定（`store.py:559`）改成 kind 优先：

```python
mode = 'verify' if row['kind'] == 'reply' and (row['state'] == 'verify_pending' or row['reply_id']) else 'send'
```

理由：say 行也要把飞书返回的 message_id 存进 `reply_id`（retract 靠它对账），
所以 `reply_id is not None` 不再等价于「待校验」。

### 5.3 必改点全量清单（HY4 两轮评审的合集）

**a) schema 与 DDL 本体**

| 位置 | 改什么 |
|---|---|
| `store.py:44` | `SCHEMA_VERSION = 8`（注释写明与 `PRAGMA user_version` 同改，v6 就差点漏） |
| `store.py:101-124` | 建表 DDL 换 v8；两条触发器的列名换新列（`outbox_immutable` 引用 `id,original_id` 的话，v8 表上建触发器会报 `no such column`） |
| `store.py:350` | `UNFINISHED_OUTBOX_STATES` 不变，但新增 `OUTBOX_BLOCKING_STATES` / `OUTBOX_RELEASING_STATES` 两个常量（§4 的放行全集） |

**b) 主键与 JOIN（12 处 `WHERE id=?` + 5 处 `o.id=i.id`）**

| 位置 | 现状 | 改成 |
|---|---|---|
| `store.py:377` | `EXISTS (… o.id = i.id …)` | `o.inbox_id = i.id` |
| `store.py:433` | `SELECT id FROM outbox WHERE state IN (…)` | `SELECT row_id` |
| `store.py:436` | `JOIN outbox o ON o.id=i.id` | `ON o.inbox_id=i.id` |
| `store.py:495-505` | `WHERE id IN (SELECT id FROM outbox …)` | **子查询要 `inbox_id` 不是 `row_id`**：这行更新的是 inbox（503），喂进去的必须是 inbox id；把子查询换成 `row_id` 会让 inbox 永远更新不到 |
| `store.py:542` | `SELECT id … verify_attempts>=8` | `SELECT row_id` |
| `store.py:549-554` | `outcome_unknown` + inbox 联动 | 加 `kind='reply'`（§5.2） |
| `store.py:563,567,569,576,588,597,607,616` | `WHERE id=?` | `WHERE row_id=?` |
| `store.py:855` | `complete_external_reply` 判重 `SELECT * FROM outbox WHERE id=?`（**入参是消息 mid，字符串**） | **`WHERE inbox_id = ? AND kind = 'reply'`**。⚠️ v1.5 曾写成 `WHERE row_id=?`，那是错的：入参是消息 id，不是 outbox 行号；照抄会**判重静默失效 → 重复回复** |
| `store.py:919` | `complete_external_skip` 判重 `SELECT 1 FROM outbox WHERE id=?` | `WHERE inbox_id = ? AND kind = 'reply'`（skip 的含义是「不许再回一条」，say 行不该让它判定为「已回复」） |
| `store.py:1384` | `get_outbox(mid)` | `get_outbox(row_id)` |
| `store.py:1399,1405,1424` | `JOIN outbox o ON o.id=i.id` | `ON o.inbox_id=i.id`；**并处理一 inbox 多行放大计数**（见 c） |
| `metrics.py:89` | `JOIN outbox o ON o.id = i.id` | `ON o.inbox_id=i.id AND o.kind='reply'` |
| `service.py:999` | `mid = original_id or id` | 拆 `row_id` / `inbox_id`（§5.1） |
| `service.py:1002-1023,1035-1052` | 传 `mid` | `fail_outbox/outbox_*` 传 `row_id`；`get_message/transport` 传 `inbox_id`；`send_guard` 用 `row_id + attempt_id` |

**b2) 第 3 轮评审补充的漏项（v1.4 漏了）**

| 位置 | 现状 | 改成 |
|---|---|---|
| `store.py:450` | `prune_expired` 里 `UPDATE outbox SET state='dropped' … WHERE id=?` | `WHERE row_id=?` |
| `store.py:546` | 验证重试用尽：`UPDATE outbox … WHERE id=?`（入参是 outbox id） | `WHERE row_id=?` |
| `store.py:548` | 紧邻的 `UPDATE inbox … WHERE id=?`（入参是同一条消息的 **inbox id**） | 保持 inbox id（`inbox_id`），**不要**跟着改成 row_id——这里是同一段代码里的两个标识符 |
| `store.py:547,552,618` | `UPDATE inbox SET needs_attention …` 三处 | 全部加 `AND/SELECT` 来源限定 `kind='reply'`（与 §5.2 的 outcome_unknown 同一条规则） |
| `store.py:503` | `UPDATE inbox … WHERE id IN (SELECT id FROM outbox WHERE state='outcome_unknown')` | 子查询改 `SELECT inbox_id`；否则 v8 下会把 row_id 当 inbox id |
| `service.py:1032` | 异常分支 `fail_outbox(outgoing.get('original_id') or outgoing.get('id'), …)` | `fail_outbox(outgoing['row_id'], …)` |
| `service.py:1040-1041` | `send_guard` 里 `mid = original_id or id` 再 `get_outbox(mid)` | `send_guard` 用 `row_id` 取 outbox；需要原消息时另取 `inbox_id` |
| `tests/` 里的 outbox fixture | 直接 `INSERT INTO outbox(id=, original_id=…)`、断言 `row['id']` | 全部换 v8 列名；开工前先跑一次全仓 `grep -rn "outbox" tests/` |
| `cli.py` 计数/回执 | 按 `id` 关联 inbox 与 outbox | 改按 `inbox_id`，并注意一 inbox 多行时去重 |

**开工第一步**：在改代码前先跑一遍这两条，把清单落到 issue 里，而不是照本文抄——
本文也可能漏（三轮评审已经证明这一点）：

```
grep -rn "o\.id *= *i\.id\|original_id\|get_outbox(" bridge/ tools/ tests/ --include=*.py
grep -rn "FROM outbox\|INTO outbox\|UPDATE outbox" bridge/ tools/ tests/ --include=*.py
```

**b3) 全新安装路径（第 3 轮评审阻断 1：最容易漏、出错必崩）**

只写"外部迁移脚本"是不够的——`Store.__init__` 里的 `executescript` 就是**新库的建表路径**：

| 位置 | 现状 | 改成 |
|---|---|---|
| `store.py:44` | `SCHEMA_VERSION = 7` | `= 8` |
| `store.py:101-124` | `executescript` 里是 v7 DDL（`id/original_id`） | 换 v8 DDL + 部分唯一索引 + 两条触发器（列名换新） |
| `store.py:203-209` | `if version < 7: PRAGMA user_version=7` | 追加 `if version < 8:` 推进到 8（注意注释里那句"DDL 只写一次"的纪律：v8 DDL 也只能在 executescript 里写一份） |
| `store.py:64-68` | 只有上界：`version > SCHEMA_VERSION` 才拒 | 补下界：**代码要求 v8 时遇到 v7 库必须报错**「库还在 v7，先跑迁移」；否则 v7 库会静默穿过、然后 `no such column: row_id` |

**c) 一 inbox 多行带来的放大计数**：`drop_pending`(383)、`prune_expired`(412)、`stats`(1424)、
`app_recent`(1399)、`conversation_replies`(1405) 原先是「一行 outbox = 一条消息」，v8 后必须
`COUNT(DISTINCT inbox_id)` 或 `WHERE kind='reply'`，否则同一条消息被数两次。

**d) 出口现状（HY4 阻断 9：今天**不**成立）**

| 现状 | 事实 | S1 怎么处理 |
|---|---|---|
| `cli.py:1362` `notify` | `Transport(...).send_dm(...)` 直发，注释明写「不经过 outbox」 | **本次不收编**，写进 `CONTRACTS.md` 例外 + S2 任务；门禁白名单显式允许这一处 |
| `transport.py:355/367/454` | 三处 `POST /open-apis/im/v1/messages`（`send_text`/`send_dm`/`reply`） | 真正的发送层，门禁要盯**调用方**而不是字符串 |
| `transport.py` 无 DELETE | 仓库里还没有撤回 API | S1 新增 `Transport.delete_message(mid)`，唯一调用方是 sender |
| `gateway.py:208` 白名单 | `('ack','fail','reply','skip')` | 加 `say` / `retract` |

### 5.4 CLI（按**真实现状**签名，不说「语义不变」）

现有 `events reply` 的必填参数是 `--consumer / --event / --claim-token`（`cli.py:1416-1419`），
正文只从 stdin/`--body-file` 读（防 `ps` 泄露）。新增两条照同一约束：

| 命令 | 必填 | 可选 | 说明 |
|---|---|---|---|
| `events reply` | `--consumer --event --claim-token` | `--body-file --receipt-file --task-*` | 语义不变 |
| `events say` | `--consumer` + **`(--event --claim-token)` 与 `--chat` 二选一** | `--reply-to om_xxx`、`--uuid`、`--body-file` | 有 event 就必须带 claim_token（和 reply 同一套鉴权）；主动消息没有 event，只给 `--chat` |
| `events retract` | `--consumer --event --claim-token --message-id <mid>` | `--uuid` | 只能撤「本库有 `reply_id` 对账记录」的消息；否则退出码 4 |

> v1.5 把 `events say` 写成「必填 `--event --claim-token`」同时又支持 `--chat` 主动消息——
> 这两者互斥，照抄写不出主动消息那条路（实施就绪度评审指出的硬矛盾 2）。

- 三条都只写 outbox；sender 负责发/删。
- 输出一行 JSON：`{"ok":true,"inbox_id":…,"row_id":…,"seq":…}`。
- `--uuid` 是**调用方幂等键**（`say`/`retract` 用），重放同一条命令不会多出一行。
- **退出码**：本仓 CLI 现在全是 `raise SystemExit(str)` → 退出码 1。S1 起定义一个窄的约定：
  `raise SystemExit(4)` 前先把原因打到 stderr（Python 里 `SystemExit(4)` 就是退出码 4），
  只用于「拒绝执行」这一类（撤回别的人消息、目标不在本库）。补 `test_cli_exit_code_4`。

### 5.5 HTTP 网关（真实路由）

真实形态是 `/v1/events/{id}/{action}`，`action ∈ {ack, fail, reply, skip}`（`gateway.py:208`）。
S1 扩成 `{ack, fail, reply, skip, say, retract}`，路径示例 `POST /v1/events/123/say`。
鉴权沿用现有 Bearer token + 事件归属校验；新增 action 要进白名单常量，并有路由白名单测试。

### 5.6 发布路径：CLI/网关必须走 `complete_external_*`，不能裸写 outbox

现状（`store.py:838-900`、`gateway.py:299-349`）：`events reply` / 网关 `/v1/events/{id}/reply`
都**不是**直接插 outbox，而是经过 `complete_external_reply(consumer, event_id, now, body,
external_uuid, claim_token=…, app_daily_cap=…)`，它一次事务里做六件事：

1. 校验事件归属（`claim_token` + `consumer`，`owned_done` 或 `_event_live`）；
2. 判重（同一条消息是否已有回复，见 §5.3 b 的 855 行）；
3. 配额（`app_reply_blocked(..., daily_cap)`）；
4. `INSERT INTO outbox(...)`；
5. `UPDATE inbox SET state='reply_queued'`；
6. 事件 `done` + 消费者计数 + 任务步 + turn 队列。

所以 S1 的 `say` / `retract` 也必须**扩展这条发布路径**，而不是在 CLI 里直接调 `append_outbox`：

```python
complete_external_reply(consumer, event_id, now, body, external_uuid, claim_token=None,
                        app_daily_cap=20, kind='reply')   # kind='reply'|'say'|'retract'
```

| kind | 校验 | 判重 | 配额 | inbox 状态 | 事件收尾 |
|---|---|---|---|---|---|
| `reply` | claim_token + consumer | `inbox_id=? AND kind='reply'` | `app_daily_cap` | `reply_queued` | done + 计数 |
| `say` | claim_token + consumer（`--chat` 主动消息跳过 claim 校验，但仍需 consumer） | `uuid` 相同即幂等重放 | 同样受日配额约束（防刷屏） | **不改** | **不收尾**（事件保持 leased；见 §11 偏差 8——否则后面的终回复发不出去） |
| `retract` | claim_token + consumer | `uuid` 相同即幂等 | 不受配额 | **不改** | done + 计数 |

`append_outbox()` 是**内部** API（只有 `complete_external_*` 和 sender 的失败回写会调），
不进 CLI 合同。`Store.complete_external_skip` 的判重同步改成 `kind='reply'`（§5.3 b 的 919 行）。

### 5.7 v1.6 预先定掉的 6 条决策（实施时不再问人）

| # | 决策点 | 定成 | 影响 |
|---|---|---|---|
| 1 | `reply` 的 `seq` 从哪来 | 也走 `MAX(seq)+1 WHERE inbox_id=?`，**不硬编码 1**（迁移出来的 v7 行才是 seq=1；`complete_external_*` 新写的一律算）。主动消息（`inbox_id IS NULL`）的 `uuid = dedupe_uuid or uuid4()` | S1a |
| 2 | `chat_id` 归属 | `inbox_id` 非空时**一律从 inbox 查并覆盖入参**，查不到 → `ValueError`；只有主动消息用调用方传的 `--chat` | S1a |
| 3 | `retracted_at` 谁写 | 只由 `outbox_retracted()` 一个事务写两行（retract 行 → `sent`，原行 → `retracted_at`）；`outbox_marked()` 只服务 say | S1a |
| 4 | `events say` 参数 | `--consumer` 必填；`(--event+--claim-token)` 与 `--chat` 互斥二选一（§5.4） | S1d |
| 5 | 迁移遇 `inbox.chat_id IS NULL` | `inbox.chat_id` 本就可空（`store.py:92`）。`--dry-run` 先统计，**>0 即中止**并打印 inbox id，不补默认值 | S1b |
| 6 | 多行计数口径 | 一律 `WHERE kind='reply'`（不用 `COUNT(DISTINCT)`，语义更直白） | S1a |

---

## 6. 防回退门禁（按真实调用点写，不按字符串猜）

`tests/test_single_exit.py` 的规则是「**谁允许调用发送层**」，用文件+函数白名单表达：

| 允许 | 调用 | 说明 |
|---|---|---|
| `service.py::BridgeService.sender` | `Transport.reply` / `send_text` / `delete_message` / `get_message` | 唯一出口 |
| `cli.py::_notify`（现行 `notify` 子命令） | `Transport.send_dm` | **唯一例外**，理由写在 `CONTRACTS.md`，S2 收编 |

其他任何文件/函数出现 `Transport.reply|send_text|send_dm|delete_message` → 测试失败并打印文件:行号。
（不用 `im/v1/messages` 字符串做判据：那样会禁掉 `transport.py` 自己这个共享层，却放过调用方。）

Windows 干活端在另一个仓库，本地 grep 扫不到 → 门禁只能覆盖本仓，跨仓约束写进
`CONTRACTS.md`（干活端只许调 `feishu-bridge events say/retract`，不许碰飞书 API）。

---

## 7. 迁移 v7 → v8（一次性、可回滚）

**前置**：`PRAGMA user_version` 必须是 7（`store.py:44` 的 `SCHEMA_VERSION` 同步改 8）。

```
0. 停服务：先停 bridge（sender 线程必须停），确认没有正在 transport.reply() 的请求
1. 断言无 in-flight：SELECT COUNT(*) FROM outbox WHERE state IN ('send_started','verifying') = 0
   —— 不为 0 就中止并打印这些行的 id，绝不带着半次发送进迁移
1b. 断言 inbox.chat_id 非空：v7 的 inbox.chat_id 可空（`store.py:92`），空了就回填不出来 →
    `--dry-run` 先统计，>0 即中止并打印 inbox id（不补默认值、不猜）
2. 备份：VACUUM INTO 'bridge.db.v7-<ts>'（WAL 下 cp 拿到的不是一致快照：-wal 里可能有未 checkpoint 的提交）
   然后关连接
3. 连接：PRAGMA foreign_keys=ON；BEGIN IMMEDIATE
4. DROP TRIGGER outbox_keep_rows; DROP TRIGGER outbox_immutable; DROP INDEX outbox_due;
5. ALTER TABLE outbox RENAME TO outbox_v7;
6. 建 outbox（v8）+ outbox_seq（部分唯一）+ outbox_due + 两个触发器（列名换新）
7. ASSERT (SELECT COUNT(*) FROM outbox_v7 WHERE id IS NOT original_id) = 0   -- 前提校验，不靠巧合
8. INSERT INTO outbox (inbox_id,chat_id,seq,kind,body,body_hash,uuid,created_at,state,next_due,
                       attempts,verify_attempts,attempt_id,lease_until,first_send_at,
                       reply_id,sent_at,verified_at,error)
   SELECT o.original_id, i.chat_id, 1, 'reply', o.body, o.body_hash, o.uuid, o.created_at,
          o.state, o.next_due, o.attempts, o.verify_attempts, NULL, NULL, o.first_send_at,
          o.reply_id, o.sent_at, o.verified_at, o.error
     FROM outbox_v7 o JOIN inbox i ON i.id = o.original_id;
   -- attempt_id/lease_until 归零（第 1 步已保证没有 in-flight 行）
   -- chat_id 从 inbox 回填（v7 outbox 没有这一列）
9. 断言：行数一致；每个 inbox_id 恰好 1 行；uuid 无重复；各 state 计数新旧一致；
   没有 chat_id 为空的行
10. DROP TABLE outbox_v7;
11. PRAGMA user_version = 8;
12. COMMIT；再断言 + PRAGMA foreign_key_check 为空 + sqlite_master 里 2 触发器 2 索引
```

**回滚**：用第 2 步的 `VACUUM INTO` 备份文件替换（不是「迁移只读旧表」——第 7-10 步确实在写）。
**拒绝并存**：新代码要求 `user_version >= 8`，旧代码要求 `<= 7`；旧代码打开 v8 库必须启动失败
（`store.py:64-68` 今天已经有这条分支，只需把英文文案换成中文并补测试）。

落点：`tools/migrate-outbox-v8.py`，带 `--dry-run` / `--verify`，禁止用 `executescript`。

### 7.1 迁移已实测（含两个真踩到的坑）

内存 SQLite 上把 v7 结构 + 3 行数据（`reply_verified`/`queued`/`send_started`）跑了一遍：

| 结果 | 说明 |
|---|---|
| 第一版脚本**失败** | `index outbox_due already exists` —— `ALTER TABLE RENAME` 会连带搬走索引和触发器，名字仍被占用 |
| 修法 | 第 4 步补 `DROP INDEX outbox_due` |
| 修正后 | 3 行 → 3 行，`seq=1,kind='reply'`，状态原样，`user_version=8`，`foreign_key_check` 空、`integrity_check=ok` |
| 事务性 | **必须逐条 `execute` 包在 `BEGIN IMMEDIATE` 里**；`executescript` 执行前自动 COMMIT，中途失败留半个库（第一次实测就是这么坏的） |
| 触发器复验 | 改 `body`/`seq` 被拦、改 `state` 放行、`DELETE` 被拦、重复 `seq` 被唯一索引拦、`kind` 非法被 CHECK 拦 |

另外实测过（v1.4 新增查证）：`VACUUM INTO` 在 WAL 库上可用（sqlite 3.51）；
部分唯一索引 `(inbox_id,seq) WHERE inbox_id IS NOT NULL` 允许主动消息多行、同时拦住同一 `(m1,1)`。

**uuid 语义**：v7 的 `uuid` 来自 `reply_uuid(mid)`（`cli.py:1653`）或调用方；v8 派生规则用于**新行**，
迁移保留旧 uuid。两套语义并存是事实，`uuid` 只保证唯一、不保证可推导——需要幂等的调用方自己传 `--uuid`。

---

## 8. 测试清单（先写用例再写实现，共 38 条；含迁移 5 条、门禁 3 条）

| 用例 | 断言 |
|---|---|
| `test_append_outbox_seq_increments` | 同一 inbox 连写 3 条 → seq 1,2,3；不同 inbox 各自从 1（钉住 `WHERE inbox_id=?`） |
| `test_append_outbox_proactive_no_seq_conflict` | 两条主动消息（`inbox_id NULL`）都是 seq=1 且不冲突 |
| `test_append_outbox_idempotent_uuid` | 同 `dedupe_uuid` 重放 → 返回同一行，不多出行 |
| `test_append_outbox_concurrent` | 两个连接同 inbox 并发 append → 不撞 `(inbox_id,seq)` |
| `test_claim_outbox_respects_seq` | seq1 未终态时 seq2 永不被 claim（即使 next_due 更早） |
| `test_dead_row_does_not_block_next_seq` | 前驱 `dead`/`dropped` → 后继能发 |
| `test_needs_attention_row_does_not_block` | 前驱 `needs_attention`/`outcome_unknown` → 后继能发（否则永久饿死） |
| `test_send_started_lease_expiry_requeue` | `send_started` 租约过期后能被重新 claim |
| `test_say_then_reply_order` | say+reply → 先 say 后 reply |
| `test_retract_only_own_message` | 撤回非本库 mid → 退出码 4，不落行 |
| `test_retract_marks_target` | retract 成功后原行 `retracted_at` 非空，原 `state` 不变 |
| `test_retract_after_target` | retract 的 seq > target 的 seq；target 未发时 retract 排队 |
| `test_immutability_triggers` | UPDATE body/seq/kind 被 ABORT；UPDATE state/retracted_at 正常 |
| `test_say_not_marked_outcome_unknown` | say 超窗 → 不变 `outcome_unknown`、不把 inbox 标 `needs_attention` |
| `test_say_reply_id_not_verify_mode` | say 行 `reply_id` 非空时 `mode` 仍是 `send` |
| `test_sent_state_not_in_unfinished` | `sent` 不进 `UNFINISHED_OUTBOX_STATES`；cleanup/stats 不误收 |
| `test_get_outbox_by_row_id` | `get_outbox(row_id)` 命中；`get_outbox(<inbox_id>)` 返回 `None`（正例钉住契约，不用「故意传错必须挂」这种不可判定的反向断言） |
| `test_metrics_single_sample_per_message` | 一消息 say+reply 两行 → metrics 只算 1 个样本 |
| `test_status_counts_not_inflated` | `drop_pending`/`prune`/`stats` 对多行 inbox 不放大计数 |
| `test_migration_v7_to_v8` | v7 库 6 种真实状态 → 迁移后行数/状态/uuid/chat_id 全对，`user_version=8` |
| `test_migration_refuses_inflight` | 迁移前有 `send_started` 行 → 中止并打印行 id |
| `test_migration_index_name_collision` | 带 `outbox_due` 索引的 v7 库迁移不报 `already exists` |
| `test_migration_atomic_rollback` | 用 `--inject-fault` 在 INSERT 后注入异常 → 回滚到 v7 原样（行数/状态/`user_version` 全对），可重跑 |
| `test_old_code_refuses_v8` | v7 断言打开 v8 库 → 中文报错退出 |
| `test_claim_matches_4_1` | §4.1 的 6 个场景逐条跑（同一条 SQL 的回归钉子） |
| `test_cli_exit_code_4` | 撤回不在本库的消息 → stderr 有原因、进程退出码 4 |
| `test_single_exit_gate` | 白名单外调用 `Transport.reply/send_text/send_dm/delete_message` → 失败 |
| `test_gateway_route_whitelist` | `/v1/events/{id}/{say,retract}` 可用；未知 action 404 |
| `test_fresh_install_v8_schema` | 空目录建库 → `PRAGMA table_info(outbox)` 有 `row_id/inbox_id/chat_id/seq/kind`，`user_version=8` |
| `test_new_code_refuses_v7` | v7 库（`user_version=7`）打开即报错并提示跑迁移（下界拒绝） |
| `test_no_outbox_dot_id_remains` | 全仓 `grep "o\.id *= *i\.id"` / `original_id` 零残留（门禁） |
| `test_say_failure_does_not_touch_inbox` | say 行失败（`needs_attention`）→ inbox 状态不变 |
| `test_stats_sent_not_pending` | 只有 say 行（已 `sent`）的消息 → `oldest_pending_at` 不含它 |
| `test_reply_conflict_ignores_say_rows` | `complete_external_reply` 的判重（`store.py:855`）只对 `kind='reply'` 生效 |
| `test_two_senders_claim_one_row` | 两个 sender 并发 claim → 同一行只被一个拿到 |
| `test_app_guard_counts_messages_not_rows` | `app_recent`/`conversation_replies` 对多行 inbox 不放大计数 |
| `test_latency_one_sample_per_message` | metrics 延迟样本一消息只一条（`kind='reply'`） |
| `test_migration_detects_orphan` | v7 库里有孤儿 `original_id`（不在 inbox）→ 迁移中止并报出来（`foreign_keys=ON` 下会整条失败，不能静默丢） |

**真机判据（贴输出，不写"已验证"）**

1. macOS：`events say` + `events reply` → 群里按序两句，`verify_reply` 只校验 reply。
2. macOS：`events retract --message-id <say 的 mid>` → 可复跑判据两条：
   `transport.get_message(mid)` 里 `deleted=True`（飞书侧真没了）+ `SELECT reply_id, retracted_at FROM outbox WHERE row_id=?` 两列都有值。
3. 迁移：生产库副本跑 `--dry-run` 与实迁移，贴行数与状态分布对比。
4. Windows 开发机：网关 `POST /v1/events/{id}/say` → 群里出现 → `retract` 撤回。

---

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| WAL 下 `cp` 备份不一致 | 停服 + `VACUUM INTO`；迁移脚本禁用 `executescript` |
| in-flight 发送进迁移（飞书已发、本库丢状态→重发） | 第 0 步停服务 + 第 1 步断言 `send_started/verifying` 为 0，不为 0 直接中止 |
| 两个标识符写混（最隐蔽） | §5.1 契约 + `test_two_identifiers_not_swapped`；sender 禁用 `mid` 变量名 |
| say 把任务标红 | `outcome_unknown`/inbox 联动加 `kind='reply'` + 专门用例 |
| 一 inbox 多行放大计数 | 所有 JOIN/计数按 `kind='reply'` 或 `COUNT(DISTINCT inbox_id)` + 用例 |
| 「单一出口」名不副实 | 门禁按调用方白名单（sender + notify 例外）；`notify` 例外写进 `CONTRACTS.md`，S2 收编 |
| 两文档定义冲突 | 本文为唯一事实来源；B-loop §5.1 已改链接 |
| 撤回权限/时限 | 只撤自己发的；失败进 `needs_attention` 并回「撤回没成功，原因 X」，不静默 |
| 旧代码打开新库 | 版本双向断言 + 测试；宁可启动失败也不降级 |

---

## 11. 实施回写（PR-1 落地后的 4 条事实修正）

实现 PR-1 时发现设计稿有 4 处与实际代码语义不符，已按实际实现回写；写在这里是为了让 PR-2/PR-3
的接手者看到的是一份**与代码一致**的设计，不是"设计说 A、代码做 B"。

| # | 设计稿原文 | 实际 | 回写到 |
|---|---|---|---|
| 1 | 主动消息的 `uuid`「派生或调用方给」 | 主动消息没有天然身份（没有 inbox、没有 seq 域），实现为**每次 `uuid4()`**；要幂等必须显式传 `dedupe_uuid` | §5.7 决策 1 补一句：`inbox_id IS NULL` 时 `uuid = dedupe_uuid or uuid4()` |
| 2 | §7 只规定「v7 必须跑迁移脚本」 | 迁移逻辑只有一份 `migrate_outbox_to_v8()`：**v7 走脚本**（停服 + `VACUUM INTO` + 三道断言）；**v1..v6 由 `Store.__init__` 版本链就地升级**（老库只会出现在开发机上，生产一定是 v7） | §7 末补这条分界；`Store` 对 `version == 7` 直接拒绝并给中文提示（下界拒绝） |
| 3 | §3.1「say 失败 → `needs_attention`」 | `fail_outbox` 的「超幂等窗口」分支对 say **不可达**（租约 120s 先过期）；say 的失败路径就是 `retry_wait`，超窗清扫由 `claim_outbox` 负责且已加 `kind='reply'` | §3.1 表格已改 |
| 4 | §5.4「退出码 4」 | 属 PR-3 范围，PR-1 未动 CLI；届时用 `raise SystemExit(4)`（Python 里就是退出码 4） | 保留 §5.4 原约定 |

**发布前对抗自查后的 4 条回写（2026-11-17，这一轮是「装到生产前一刻」的自检）**

| # | 问题 | 处置 |
|---|---|---|
| 11 | **say 之后第二句发不出去**：`reply_timeout_seconds` 默认 600s，事件超时被 `expire_external` 判死，发布口只放行 `leased` → 「先答 say、30 分钟后再 reply」必被拒。**S1 的产品目标直接失效** | 新增机制常量 `SAY_GRACE_SECONDS = 3600.0`：say 新写入时同事务续 `events.lease_until = now + 1h`（只续 `leased`）；`expire_external(..., say_grace)` 对「有已发出的 say 且在宽限期内」的消息不判死、只续租。判据用例：`test_reply_still_lands_thirty_minutes_after_say` |
| 12 | **迁移死锁**：停服后残留 `send_started` 会让迁移永远中止（租约回收要靠运行中的 `claim_outbox`，停服后没人做） | 迁移脚本默认仍拒绝；新增 `--park-inflight`：在途行 → `needs_attention`（**绝不回 `queued`**，否则可能重发一条其实已发出的消息），与迁移同一个 `BEGIN IMMEDIATE`，逐行打印。§7 第 1 步相应改为「为 0 继续；不为 0 时二选一」 |
| 13 | **回滚会留 v8 的 WAL**：只把 `.v7` 备份覆盖回主库、留下 `-wal/-shm`，SQLite 会拿不属于该库的 WAL 帧去恢复 | 回滚步骤写死「停服 → 恢复 `.v7` → **删 `state.sqlite3-wal`/`-shm`** → 起旧 release」；原因写进 `docs/ops-s1-deploy.md` |
| 14 | **备份文件 0644，而生产库 0600**；备份里是消息正文 | `backup()` 在 `VACUUM INTO` 后 `os.chmod(target, 0o600)`，用例断言 `st_mode & 0o777 == 0o600` |

| 15 | **撤回把事件收尾了，终回复被拒**（真机验收 2026-11-17 现场撞到：say → retract → reply 第三步返回 `completed event has no durable reply`，主人的消息因此没被正式回复） | 「说错了能撤回」的前提是撤完还能继续干，所以 `retract` 改成**旁路动作**：只有 `reply` 结束事件；`say` 续租、`retract` 既不结束事件也不改 inbox；并且**事件已收尾时也允许 retract**（撤掉已发出的回复）。判据：`test_retract_does_not_close_the_event_then_reply_lands`、`test_retract_after_completed_reply_is_allowed` |

运维手册：**`docs/ops-s1-deploy.md`**（停服 → 查在途 → dry-run → 实迁 → 切 release → 三条验收；回滚含删 WAL；两个半态的报错原文；三个「别做」）。

**PR-3（CLI/网关/门禁）落地后的 3 条回写**

| # | 设计稿原文 | 实际 | 回写到 |
|---|---|---|---|
| 8 | §5.6 表：`say` 也要「事件收尾 done + 计数」 | **做不到**：事件一旦 `done`，同一事件的终回复就只剩 `completed event has no durable reply`——「先答一句、干完再报」直接失效 | say **只写 outbox**，不收尾事件、不改 inbox、不计 ack；`retract`/`reply` 才收尾。用例 `test_say_then_reply_keeps_write_order`、`test_say_keeps_event_leased_and_is_idempotent` |
| 9 | §6 门禁允许「`service.py` sender + `cli.py` notify」两个例外 | 实际还有第三个：`cli.py` 的 `_send_hold_notice`（挂起回执）必须绕过 outbox——挂起时 sender 是停的，状态通知排队就永远发不出去 | 门禁白名单 = `service.py` 整文件 + `cli.py` 的 `_notify`/`_send_hold_notice`；`CONTRACTS.md` 已把三者都写成「控制面回执，S2 收编」 |
| 10 | 退出码 4 的适用范围模糊 | 实际只在两种「拒绝执行」用：`retract_target_not_found`、`--reply-to` 指向未知消息；另加一条防呆——同一条消息已排过撤回就不再排第二次 DELETE（否则会打到飞书报错） | §5.4 已补 |

**PR-2（sender 按行发）落地后的 3 条回写**

| # | 设计稿原文 | 实际 | 回写到 |
|---|---|---|---|
| 5 | 撤回时「用 `target_row_id` 写原行 `retracted_at`」 | sender 手里只有飞书 `target_mid`，没有 row_id | 新增 `Store.get_outbox_by_message_id(message_id)`：只查**本桥发过的**行；查不到 → `needs_attention`，且**不回话**（避免递归） |
| 6 | 「主动消息 → `transport.send_text`」，没给幂等键 | 不传幂等键，重试会在会话里发第二条 | `Transport.send_text(chat_id, body, dedupe_uuid=None)`；sender 传该行的 `uuid`，既有回执调用不传、行为不变 |
| 7 | say 应与 reply 走同一套发送前校验 | say 是「对已发出的回复做补充」，不是「回答原问题」；版本校验/撤回校验/群在册校验会更容易把补充说明挡掉 | say 只保留 `send_guard`（attempt/租约/停止位）与配额；被飞书拒 → `retry_wait`/`needs_attention`，不静默 |

## 10. 工作量与拆分（v1.6 按实施就绪度评审重估）

原估 3.5 天（28 人时）**偏乐观 2.5–3 倍**：漏算了 `tests/` 的 167 处 outbox 引用（6 个测试文件、
全仓 1109 条用例会一起红）。重估 **9–11 人日（72–88 人时）**：

| 步 | 内容 | 估时 |
|---|---|---|
| S1a | `OUTBOX_V8_DDL` 常量 + `Store.__init__` DDL/`SCHEMA_VERSION=8` + append/claim/marked/retracted/sent/fail 七个方法 + 全量必改点（约 40 处）+ **tests fixture 改造（167 处）** | 4–5 天 |
| S1b | 迁移脚本（停服断言、`VACUUM INTO`、chat_id 回填、`--dry-run/--verify/--inject-fault`）+ 5 条迁移测试。**与 S1a 同 PR** | 1.5 天 |
| S1c | sender 按行发送（`row_id`/`inbox_id`/`chat_id` 三键拆分、kind 分叉、主动消息走 `send_text`）+ `Transport.delete_message` + 真机两步 demo | 1.5 天 |
| S1d | `complete_external_reply(kind=…)` 扩展 + CLI `say`/`retract` + 网关两个 action + 出口门禁 + `CONTRACTS.md` 的 notify 例外说明 | 1.5 天 |
| 收尾 | 全仓 `pytest -q`（1109 条）+ 两端真机判据 + 脱敏门禁 | 0.5–1 天 |

**PR 分组（三个 PR，不能更少）**

1. **PR-1 = S1a + S1b**（结构 + 迁移 + 版本门禁切换，必须原子）
2. **PR-2 = S1c**（sender 按行发；依赖 PR-1 的 `claim_outbox` 返回值与 `send_guard(row_id)`）
3. **PR-3 = S1d**（发布路径扩展 + CLI/网关；依赖 PR-2 的 `delete_message`）

**最可能超时的是 PR-1**：必改点清单在四轮评审里还在往外冒（855/919 就是第 4 轮才发现的），
且 167 处 fixture 改造原本没派给任何一步。开工第一步必须先跑 §5.3 的两条 grep，把清单落成
issue 并标出 548/855/919 三处的标识符归属，再动手。
