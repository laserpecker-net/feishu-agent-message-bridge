# 私聊派活/提问通道 设计稿（2026-11-05）

> 结论先说：**通往"私聊也能派活、也能提问"的路只有一条——把那条私聊接进已经存在的采集管道，
> 然后把教材通道从"第二个入口"降级成"管道里的一个分流器"**。本稿基于当前代码（release
> `a81fa57b9982a20a`）写，所有改动点都指到函数级；不新造并行实现。

## 0. 要解决的两件事

1. 本人想在教学私聊里**派活 / 提问**（"帮我查一下 UK 账户余额"、"你的信息库在哪？"），
   现在只能去群里 @ 自己，这条私聊只会把话收成教材候选。
2. 由此暴露的结构问题：**教材通道是一套独立入口**，它自己实现了准入（@、发送者、会话、时间、
   去重），和 `details` worker 的准入是两份。加"任务"语义之前，必须先把入口收成一份
   （AGENTS.md §0.5：两套实现并行 = 未完成的重构）。

## 1. 现状事实（都是读过代码/实测过的）

### 1.1 一条群消息现在怎么走

```
collect_scan('live'|'backfill')                 service.py:708
  └ transport.search(start,end,token)           transport.py:194  filter={is_at_me:true, chat_type:'group'}
      └ store.ingest_search_page(...)           store.py:231     只写 inbox(id)  ← 只有消息 ID
          └ details() 线程                       service.py:~755   claim_message('details')
              └ transport.get_message(mid)      取详情（含 mentions / body / sender）
              └ 准入：deleted / mentioned / own_message / bot / 服务激活时间 / not_joined_group
              └ complete_details(state, event)  state ∈ analysis_queued|external_queued|ignored
                  └ 事件 'message'（dedupe_key='message:'+mid）
                      └ 消费者 claim → 处理 → events reply
                          └ outbox → sender 线程 → 飞书回复
```

### 1.2 关键事实 1：`details` 早就支持"自己 @ 自己"

`details()` 里：

```python
self_chat = chat_id in self_chat_ids(self.config)
...
elif sender.get('id') == self.config['target_open_id']:
    if not self_chat:            # 非白名单会话里自己发的消息 → 不回复（旧守卫）
        reason = 'own_message'
```

也就是说：**只要消息进了 inbox，且该会话在 `self_chat_ids` 里，"本人自己发的话"就会被当正常消息
处理**——准入、事件、消费者、outbox 全都已经是通的。群里的"自己 @ 自己"走的正是这条路。

### 1.3 关键事实 2：私聊消息进不来，唯一原因是采集只搜群

`transport.search()` 的 filter 是硬编码的 `is_at_me:true + chat_type:'group'`。私聊消息永远不会
被搜到，所以永远进不了 inbox。实测（2026-11-05，真账号）：

```
{{'filter': {'chat_ids': [oc_8057…], 'is_at_me': False}}              } → 10 条，全部来自该私聊
{{'filter': {'chat_type':'p2p', 'chat_ids':[oc_8057…], 'is_at_me':True}} → 2 条，全部 @ 了自己
```

**`filter.chat_ids` 可用**，可以精确只搜那一个私聊，不会把别的私聊卷进来。

### 1.4 关键事实 3：教材通道现在是第二个入口

`control_tick()` 自己轮询 `self_chat_ids + teach chat`，把读到的消息交给
`handle_teach_answer` / `handle_teach_message` / `handle_control_message`。它自己维护
`_control_seen` 去重、自己判 `mentioned()`、自己判发送者与会话。**这些 details 全都已经做过。**
教材永远不会进 inbox（所以不会产生事件、不会被消费者回答）——这就是"私聊不能派活"的机制。

### 1.5 回执与答复是两条投递路径

- 教材回执：`_teach_reply()` → `transport.reply()` **直发**（不经 outbox，失败只记日志）。
- 回答/答复：outbox（`complete_analysis` / `events reply` 写 outbox 行，sender 线程带重试与核验）。

## 2. 方案比较

| | A. 私有入口（poller 注入 inbox 行） | **B. 统一入口（新增自聊扫描 + 教材降级为分流器）** |
|---|---|---|
| 改动 | teach poller 里对"任务类"消息调一个 `ingest_direct(mid)` 塞 inbox | 采集加一个 `self` 扫描；教材处理搬进 `details()`；**删掉** `control_tick` 的教材轮询 |
| 入口数量 | 2（两份准入继续并存、各自演化） | 1（准入只有 details 一份） |
| 绕过记账 | 是（inbox 行不是扫描出来的，`scans` 表不知情） | 否（与群消息同一条记账） |
| 风险 | 小 | 中（动核心管道，需真机复验） |
| 长期 | 每次加语义都要在两处对齐 | 加语义只改一个分流器 |

**选 B。** 理由不是"更干净"，而是：A 会让你下次改准入时必须改两处，而两处迟早不一致
（本仓已经为这种结构付过一次代价，见 HISTORY §0 那张表）。

## 3. 详细设计（方案 B）

### 3.1 采集：加一个 `self` 扫描（只搜教材私聊）

```python
# transport.py
def search(self, start_ms, end_ms, page_token=None, chat_id=None):
    flt = {'is_at_me': True,
           'time_range': {'start_time': iso(start_ms), 'end_time': iso(end_ms)}}
    if chat_id:                       # 新增：精确到单个会话，不再要求 chat_type
        flt['chat_ids'] = [chat_id]
    else:
        flt['chat_type'] = 'group'
```

```python
# service.py 采集循环
scan_name = 'self' 时：collect_scan('self')，调度间隔 self_scan_seconds（默认 30）
前提：teach_log.enabled_chat(home) 非空，否则**不注册扫描**（未启用 = 零行为变化）；
`teach enable` / `teach disable` 与扫描注册联动：启用时 `begin_scan('self', now-1s, now)`，
停用时 `reset_scan('self')`，不留残余游标。
不参与 backfill。首次跑的起点取 `max(service_epoch_ms, teach.enabled_at_ms)`，最长回放 1 个窗口
（`WINDOW_DEFAULT_MS`）；再老的消息 `create_time < enabled_at_ms` 本来就会被分流器丢掉。
**明确不做历史回填**：那会把启用之前的私聊消息全捞出来重新回执一遍，纯烧配额。
```

窗口机制（`WINDOW_DEFAULT_MS` / shrink-on-notice）与群扫描共用同一套，不额外发明。

### 3.2 `details()`：在事件生成前加一个分流点（单主）

**必须先改一处**：`details()` 的 `self_chat` 判定今天只看 `self_chat_ids(config)`，而那个清单里
放的是两个**群**，教材私聊（p2p）不在里面。于是同一句"本人自己发的话"在今天会被判成
`own_message` 直接忽略。所以：

```python
teach_chat = teach_log.enabled_chat(self.home)
self_chat = chat_id in self_chat_ids(self.config) or (teach_chat and chat_id == teach_chat)
```

```python
if self.is_control_message(message):        # 已有：下班/上班
    ...
if chat_id == teach_log.enabled_chat(self.home):
    if self.route_self_chat(message, now):  # 教材/指令/候选 → 已处理
        self.store.complete_details(mid, job['attempt_id'], time.time(), fields,
                                    state='ignored', error='teach_channel')
        continue                            # 不产生事件 → 不会被消费者回答
    # 返回 False：当成正常消息 → 走 external_queued → 消费者回答 → outbox → 回复落回这个私聊
```

`route_self_chat` = 现有 `handle_teach_answer` + `handle_teach_message` 的逻辑（入参就是详情消息），
删掉其中重复的准入（@、发送者、会话，details 已经判过），保留 `enabled_at_ms` 下界。

两处细节必须显式做，不能靠"顺手"：

- **下班/上班**：教材私聊里说「下班」现在由 poller 让给控制通道处理；入口换到 details 之后，
  分流器里要显式 `if hold.command_for(text): self.handle_control_message(message, now); return True`。
  不写这一行 = 教材私聊里说「下班」会被记成候选（行为倒退，真机上最容易踩）。
- **重放幂等**：新入口第一次跑会按窗口回放（最长 1 天）。已处理过的消息必须**不再回执**：
  教材按 `message_id` 查 `teachings.jsonl`（已有 → 跳过），候选按 `pending.message_id`
  （同一个 → 跳过）。否则升级那一刻会往私聊里补发一串回执。

**回执仍用 `transport.reply` 直发**（与今天一致，不新造回执队列）。

### 3.3 路由规则（确定性，全部复用 `bridge/teach.py`，不调模型）

| 消息内容 | 归属 | 处理 | 消费者看到吗 |
|---|---|---|---|
| `原则：/风格：/反馈：/事实：/资料：/指针：…` | 教材 | 直接落盘 + 回执 | 否 |
| `教材清单` / `忘掉最后一条` / `忘掉 t-0007` / `记` / `不记` | 指令 | 执行 + 回执 | 否 |
| `问：…` | 提问 | 去掉前缀作为正文 → 正常消息事件 | **是** |
| `做：…` | 派活 | 同上，`payload.self_intent='task'` | **是** |
| 引用回复某条待答私聊 | 答复 | 现有问答回路（重放 followup 事件） | 否 |
| 无前缀 + 判读=提问 | 候选 | 不落盘，回执：「要我现在就答？回『答』；想记成教材回『记』」 | 否（除非回「答」） |
| 无前缀 + 判读=派活 | 候选 | 回执：「要我现在就做？回『做』」 | 否（除非回「做」） |
| 无前缀 + 判读=陈述/评价 | 候选 | 回执问确认（现状） | 否 |
| 空 / 图片 / 文件 | 忽略 | 回执说明只认文字 | 否 |

两条新指令：`答`（把候选当提问发出去）、`做`（当派活发出去）。它们与 `记` 共用同一个单槽候选
（`home/teach_pending.json`），所以"一句话 → 三个出口：记 / 答 / 做"。

### 3.4 单主与去重

- 删除 `control_tick` 的教材轮询分支（`teach_chat` 加入 `chats` 的那几行 + `handle_teach_*` 的调用），
  `control_tick` 只留"自聊群里的 下班/上班"。
- 教材落盘幂等仍然靠：`teachings.jsonl` 里 `message_id` 唯一 + `_append_locked` 内查重。
- `_control_seen` 只服务控制指令，不再参与教材。

### 3.5 自激防护（这是本设计唯一的硬约束，必须写死）

- 采集用 `is_at_me: true`：**只有 @ 了自己的消息才可能被搜到**。
- bridge 自己发的任何消息（回执、回答、求助）**永不带 @**（既有不变量，
  `BridgeMessagesNeverAtAnyoneTests` 钉住）。
- 所以：回答落回私聊后，下一次采集搜不到它 → 不会变成教材、不会触发新任务。
  **不需要回声账本、不需要消息标记、不需要前缀排除。**
- 代价：私聊里派活/提问也要 @ 自己一下（飞书里 @ 后第一个候选就是自己，一次按键）。
  **这个代价必须保留**——去掉 @ 就只能靠"排除 bridge 发过的 id"这种账本，而账本会漏（重启丢内存、
  多端并发、重发）。见 §7 待定项 1。

### 3.6 失败语义（不许静默）

| 情况 | 行为 |
|---|---|
| 私聊扫描 API 失败 | `self.collector_health('self', …)` 记失败 + `error('collector')`；`doctor --assert` 增一项 `self_scan`，连续失败 → warn/fail |
| 教材私聊里答不出来（消费者超时/失败） | 走现有事件失败链：重试 → `needs_attention`；私聊里**不额外发提示**（那条提示本身要花配额） |
| app 每日回复配额耗尽 | 现有 `app_reply_blocked`：事件直接 dead + attention，private chat 无回复。回执（教材类）仍直发 |
| 候选过期 | 30 分钟未确认自动丢弃（`pending.expires_at`），避免"几天后回一句『记』把无关内容记成教材" |
| 教材文件损坏 | 现状：不注入 + `error` 暴露（`teach status` 可查） |

### 3.7 判据（可复跑，脱敏后贴 HISTORY）

1. `python3 -m pytest -q` 全绿；`tests/test_repo_hygiene.py` 3 passed。
2. `feishu-bridge teach status` 里 `pending_teaching` 能反映候选（终端可见）。
3. 真机：私聊 @ 自己 发 `问：现在几点？` → 收到回答（不是「没当教材记」），且
   `feishu-bridge events list --limit 3` 里出现对应 `message` 事件、`consumer` 是被订阅的消费者、
   事件最终 `done`。
4. 真机：私聊 @ 自己 发 `做：把今天的 inbox 数量报给我` → 同上，回复在私聊里。
5. 真机：私聊 @ 自己 发 `你怎么这么笨` → 回执说「评价」，**教材条数不变**。
6. 真机：回答发出后再等 3 个采集周期 → 教材条数仍然不变（自激判据：回答没被当教材）。
7. `grep -rn "control_tick" bridge/` 里不再出现教材轮询；`grep -rn "handle_teach_message"`
   只出现在 `details` 的分流调用里（旧入口已删，判据可 grep 复跑）。

## 4. 数据与安全

- 扫描范围**只有** `teach.json.chat_id` 这一个会话（`filter.chat_ids`），不搜其他私聊。
- 私聊消息进入事件 payload → 提示词，与群里消息同一条路径、同样的"不可信数据"约束。
- `teach_pending.json` 0600、单槽、30 分钟过期；`teachings.jsonl` 06xx 规则不变。
- 教材仍不进 SQLite、不进 git；`init.json` 里只记 `rev/hash/entries`。

## 5. 兼容与回滚

- `teach.json` 结构不变（新增可选 `self_scan_seconds`、`pending_ttl_minutes`）。
- 教学未启用 → 自聊扫描不注册、分流点永不命中 ⇒ 行为与今天完全一致。
- 回滚：`~/…/current` 指回上一代 release + `feishu-bridge restart`（`installation.json` 里有记录）。
- 升级期只有一代 release 在跑（launchd 单实例），不存在新旧同时轮询同一会话。

## 6. 任务编排（设计通过后执行）

| 任务 | 内容 | 依赖 |
|---|---|---|
| T10a | `transport.search` 支持 `chat_id`；`collect_scan('self')` 调度与健康项；`doctor` 增 `self_scan` | — |
| T10b | `details()` 分流点 + `route_self_chat`（搬 `handle_teach_*`，删重复准入） | T10a |
| T10c | **删除** `control_tick` 教材轮询与相关死代码/测试 | T10b |
| T11a | `问：`/`做：` 前缀 + `答`/`做` 一键指令 + 候选过期 | T10b |
| T11b | `payload.self_intent`，消费者提示词里说明"这是本人直接派给你的活/问你的话" | T11a |
| T12 | 真机④：§3.7 的 7 条判据 + 贴进 HISTORY | T11b |

每项都要：全量测试绿 → 脱敏门禁绿 → 中文提交 → 真机再验 → 更新 `CONTRACTS.md`（教材通道那一节
改写成"分流器"，删除旧准入顺序）。

## 7. 待你拍板的三件事

1. **私聊里派活要不要保留「@ 自己」？** 推荐保留：它是自激防护的结构前提（零账本、重启不丢）。
   去掉它省一次 @，但只能用"排除 bridge 发过的消息 id"这种账本，会漏。
2. **前缀够不够？** 推荐 `问：` / `做：` 两个前缀 + 无前缀时回执给「答」/「做」一键指令。
   不再加第三个词（"你帮我"≠派活，判读只用来给出口，不用来自动执行）。
3. **私聊派活的权限要不要与群里一致？** 推荐一致（都是本人说的，权限模型不变：不删文件、
   不读密钥、不外传）。若你想私聊更严（例如只读），那是另一条规则，早说比晚说便宜。
