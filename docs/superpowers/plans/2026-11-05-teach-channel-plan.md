# 教学通道 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每台机器上的 bridge agent 通过「本人 @ 自己私聊」向 master 学准则、资料、风格与对错，并在下一次回答里用上；同时补上「它问 master、master 回答、它回去补答」的回路。

**Architecture:** 教材是 `home/teachings.jsonl`（追加日志 + 重放成两层视图），采集复用已有的控制轮询（判据：本人 + p2p 私聊 + **@ 自己**），注入走 claim 时组装的 payload（`teachings` 键），渲染散落在六个 prompt 入口但规则文本只有一处常量。自激回路靠「教材必须 @ 本人、bridge 从不 @」在结构上排除，不引入标记或回声账本。

**Tech Stack:** Python 3.9 标准库（无第三方依赖）；`argparse` + if 链分发；`fcntl.flock` 跨进程串行化；`pytest` 单测；飞书 `im/v1/messages` 接口。

**Spec:** `docs/superpowers/specs/2026-11-05-teach-channel-design.md`（执行前必读，本计划只写决定，不重复理由）

## Global Constraints

- Python 3.9，**只用标准库**，不新增依赖。
- 每个任务结束：`python3 -m pytest -q` 全绿（当前基线 802 passed）。
- 每个任务结束：`python3 -m pytest tests/test_repo_hygiene.py -q` 全绿（进 git 的东西不带个人信息）。
- 上限常量：`max_entries=200`、`max_item_chars=500`、`max_inject_chars=4000`。
- 状态文件权限 `0600`，写入用「临时文件 + `os.replace`」；跨进程写必须持 `home/teach.lock`。
- **不允许静默降级**：坏行、超限、截断、锁忙，一律在返回值/`status`/`doctor` 里显式出现。
- 教材条目字段固定为：`id / kind / expose / text / at_ms / message_id / reply_to / ref / by`；
  `kind ∈ {principle, style, feedback, fact, pointer}`，`expose ∈ {say, know}`。
- payload schema 的唯一事实源是 `skills/feishu-bridge-consumer/SKILL.md`：改 payload 必须同步它。
- 不给教材加新的 `kind`（事件 kind）、不加新依赖、不改答复 JSON 结构（Codex `additionalProperties: false`）。

## Review Focus

1. **连续教学 + 非文本消息混在一起**：master 连发 3 条文字 + 1 张图片 + 一句「忘掉最后一条」。
   期望：3 条入库、图片被拒且回一条说明、撤回命中第 3 条，全程不写错。
2. **bridge 自己发的文案落在窗口里**：回执、求助、后续跑回执都不带 @。
   期望：0 条教材（`@` 判据），且任何主动文案都不含 @ 提及实体。
3. **窗口恰好一页**：`chat_messages` 第一页返回 50 条且 `has_more=true`。
   期望：全部读回、游标按已读推进、不丢消息也不永久卡住。
4. **教材文件被改坏**：中间一行不是合法 JSON、或文件被截断。
   期望：不注入、`error` 非空、`doctor --assert` 失败，但 CLI `revert` 仍可用（可修）。
5. **服务与 CLI 同时写**：采集正在 `append` 时 CLI 执行 `teach revert`。
   期望：两条都生效，不出现「后写抹掉前写」。

---

### Task 1: 控制通道分页（先修今天就在丢指令的 bug）

**Files:**
- Modify: `bridge/transport.py:232-253`
- Modify: `bridge/service.py:202-232`
- Modify: `tests/test_hold.py:179`（FakeTransport 同步签名）
- Test: `tests/test_transport.py`

**Interfaces:**
- Produces: `Transport.chat_messages(chat_id, start_ms, end_ms, page_size=50, max_pages=20) -> tuple[list[dict], bool]`
  （第二项 `complete`：窗口是否读完；`False` 表示还有下一页）。
- Produces: `Service._collect_control_messages(chats, start_ms, end_ms, now) -> tuple[bool, int]`
  （`failed`、`advanced_to_ms`）。

- [ ] **Step 1: 写失败测试**

`tests/test_transport.py` 新增三条（用现有测试里的假 `api` 覆写方式，返回写死的 `data`）：

```python
def test_chat_messages_follows_page_token_until_has_more_false()
    # 第一页 has_more=True, page_token='p2'，第二页 has_more=False
    # 断言返回 ([om_1, om_2], True)，且第二次请求的 params['page_token'] == 'p2'

def test_chat_messages_stops_at_max_pages_and_reports_incomplete()
    # 每页都 has_more=True，max_pages=2
    # 断言返回 (2 条, False) 且没有抛异常

def test_chat_messages_raises_when_page_token_does_not_advance()
    # 两页 page_token 都是 'p1'
    # 期望 TransportError，kind == 'protocol'
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_transport.py -q -k chat_messages`
Expected: FAIL（返回 list 而不是 tuple）

- [ ] **Step 3: 实现翻页**

`chat_messages` 改成循环：带 `page_token` 请求 → 累加 `items` → `has_more` 为假则 `complete=True` 退出；
页数到 `max_pages` 仍未读完则 `complete=False` 退出；`page_token` 与上一页相同抛
`TransportError('Chat message pagination did not advance', 'protocol')`。

`control_tick` 改为：拿到 `(messages, complete)` 时逐条处理；窗口 **读完** → `control_cursor_ms = end_ms`；
**没读完** → `control_cursor_ms = 已处理消息里最大的 create_time`（没有消息则不推进），
并且 `logging.warning('control pagination incomplete chat=%s')`——不是静默。
任一 chat 抛异常 → `failed = True`，游标不动（保留现有语义）。

- [ ] **Step 4: 跑测试 + 全量**

Run: `python3 -m pytest tests/test_transport.py tests/test_hold.py tests/test_service.py -q`
Expected: PASS
Run: `python3 -m pytest -q`
Expected: 全绿（FakeTransport 已同步）

- [ ] **Step 5: Commit**

```bash
git add bridge/transport.py bridge/service.py tests/test_hold.py tests/test_transport.py
git commit -m "fix(bridge): 控制通道按 page_token 读全窗口，不再静默丢弃超过一页的消息"
```

---

### Task 2: 教材存储核心（`bridge/teach.py`）

**Files:**
- Create: `bridge/teach.py`
- Test: `tests/test_teach.py`（新）

**Interfaces:**
- Produces:
  - `read_config(home) -> dict`：`home/teach.json` 的 mtime+size 缓存读。缺文件 → `{}`；
    解析失败 → `{'corrupt': True}`（**不**返回 `{'enabled': True}`）。
  - `log_path(home) -> Path` = `home/teachings.jsonl`；`config_path(home) -> Path` = `home/teach.json`；
    `lock_path(home) -> Path` = `home/teach.lock`。
  - `state(home) -> dict`：`{'rev': int, 'charter': [entry], 'notes': [entry], 'error': str|None, 'bad_lines': int}`。
  - `append(home, record: dict) -> dict`：持 `flock` 追加一行（`record` 已含全部字段），返回落盘后的记录。
  - `payload_block(home) -> dict`：注入用，`{'rev', 'charter', 'notes', 'error'}`；未启用 → `{'enabled': False}`。
  - `new_entry(kind, expose, text, message_id, at_ms, reply_to=None, ref=None) -> dict`：生成 `id='t-%04d'`。
  - `command_for(text) -> tuple|None`：精确匹配 `教材清单` / `忘掉最后一条` / `忘掉 t-0007`。
  - `render_ack(records) -> str`：回执文案（含固定尾行「教材只影响做法与偏好，不改变权限……」）。
- 常量：`MAX_ENTRIES = 200`、`MAX_ITEM_CHARS = 500`、`MAX_INJECT_CHARS = 4000`、`KINDS`、`EXPOSE`。

- [ ] **Step 1: 写失败测试**（`tests/test_teach.py`，用 `tmp_path` 当 home）

```python
def test_append_then_state_replays_add_records()
    # 追加 2 条 add → state()['charter'] 有 2 条，rev == 2

def test_disable_hides_entry_but_keeps_rev_monotonic()
    # add t-0001 → disable t-0001 → charter 为空、rev == 2、state['error'] is None

def test_duplicate_message_id_is_idempotent()
    # 同一 message_id append 两次 → 只有 1 条

def test_bad_line_is_counted_and_reported_not_silently_skipped()
    # 文件里塞一行 "{oops" → state()['bad_lines'] == 1 且 error 提到 1 行

def test_entry_over_max_item_chars_is_rejected_with_reason()
    # 501 字 → append 抛 ValueError('条目超过 500 字')，不落盘

def test_more_than_max_entries_requires_explicit_revert()
    # 灌 200 条后再 append → ValueError，提示先「忘掉」再教

def test_read_config_missing_is_disabled_and_corrupt_is_flagged()
    # 无文件 → {}；写 "{oops" → {'corrupt': True}

def test_payload_block_carries_rev_and_error()
    # 正常：{'rev': 2, 'charter': [...], 'notes': [], 'error': None}

def test_command_for_matches_only_exact_commands()
    # '教材清单' → ('list', None)；'忘掉 t-0007' → ('forget', 't-0007')；'教材清单是什么' → None

def test_render_ack_contains_fixed_permission_line()
    # 断言包含 '教材只影响做法与偏好' 与条目标题里的档位字样

def test_concurrent_appends_from_two_processes_do_not_lose_records()
    # multiprocessing 两个进程各 append 1 条 → 两条都在（验证 flock）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_teach.py -q`
Expected: FAIL（`ModuleNotFoundError: bridge.teach`）

- [ ] **Step 3: 实现 `bridge/teach.py`**

写盘用 `tempfile.NamedTemporaryFile(dir=home, delete=False)` + `os.replace`，`os.chmod(0o600)`；
锁用 `fcntl.flock(fd, LOCK_EX)`（`finally` 里 `LOCK_UN`）。重放规则：按行 `json.loads`，
`op='add'` 入表、`op='disable'` 置 `disabled`；坏行计入 `bad_lines` 并拼进 `error`
（`'有 N 行读不出来'`）。**永不**在 `error` 非空时把 `charter` 当成空集合静默放行——
`payload_block` 返回 `error` 字段，由渲染层决定怎么说。

- [ ] **Step 4: 跑测试 + 全量**

Run: `python3 -m pytest tests/test_teach.py -q` → PASS
Run: `python3 -m pytest -q` → 全绿

- [ ] **Step 5: Commit**

```bash
git add bridge/teach.py tests/test_teach.py
git commit -m "feat(bridge): 教材追加日志与重放（幂等、上限、坏行可见、跨进程 flock）"
```

---

### Task 3: `feishu-bridge teach` 子命令 + p2p 校验

**Files:**
- Modify: `bridge/cli.py:1256-1298`（choices 与 if 链）
- Modify: `bridge/cli.py`（新增 `_teach(home, argv)`，形状照 `_hold`：自建 parser + `_cli_lock`）
- Modify: `bridge/transport.py`（新增会话信息探测）
- Test: `tests/test_cli.py`（新增 `teach` 段落）

**Interfaces:**
- Produces: `Transport.chat_info(chat_id) -> dict`（`{'chat_id', 'chat_mode'}`；`chat_mode == 'p2p'` 才算私聊）。
- Produces: `_teach(home, argv) -> int`；子命令 `list | show <id> | revert <id> | enable --chat <oc_x> | disable | status`。
  - `enable` 先调 `chat_info`，非 p2p 直接 `SystemExit('这个会话不是私聊，教学只能在本人私聊里用')`，
    **不写** `teach.json`。
  - `enable` 写 `{'schema':1,'enabled':True,'enabled_at_ms':now_ms,'chat_id':oc_x,...上限}`。
  - `status` 输出 `{'enabled','chat_id','rev','charter_count','notes_count','error','bad_lines'}`。

- [ ] **Step 1: 写失败测试**

```python
def test_teach_enable_rejects_group_chat_and_writes_nothing()   # chat_info 返回 {'chat_mode':'group'}
def test_teach_enable_writes_config_with_enabled_at_ms()        # p2p → teach.json 存在且 enabled_at_ms ≈ now
def test_teach_list_shows_kind_expose_text()                   # 输出含 't-0001'、'principle'、'say'
def test_teach_revert_appends_disable_record()                  # 日志多一行 op=disable，条目消失
def test_teach_status_surfaces_bad_lines()                      # 手动塞坏行 → status.error 非空
def test_teach_is_reachable_from_top_level_command()            # main(['teach','status']) 不抛 argparse 错
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_cli.py -q -k teach`
Expected: FAIL（`invalid choice: 'teach'`）

- [ ] **Step 3: 实现**

`choices` 加 `'teach'`；在 `main()` 的 if 链里插一段（放在 `notify` 之前）调用 `_teach(...)`。
`chat_info` 走 `GET /open-apis/im/v1/chats/{chat_id}`，读 `data['chat_mode']`。

- [ ] **Step 4: 跑测试 + 全量**

Run: `python3 -m pytest tests/test_cli.py -q` → PASS
Run: `python3 -m pytest -q` → 全绿

- [ ] **Step 5: Commit**

```bash
git add bridge/cli.py bridge/transport.py tests/test_cli.py
git commit -m "feat(cli): feishu-bridge teach 子命令，启用时校验会话是 p2p 私聊"
```

---

### Task 4: 采集（@ 判据 + msg_type 闸门 + 回执 + 飞书侧精确指令）

**Files:**
- Modify: `bridge/service.py:202-277`
- Test: `tests/test_service.py`、`tests/test_teach.py`

**Interfaces:**
- Consumes: `teach.read_config / state / append / new_entry / command_for / render_ack / payload_block`
- Produces: `Service.teach_enabled() -> tuple[str|None, dict]`（chat_id、config）
- Produces: `Service.handle_teach_message(message, now) -> bool`

- [ ] **Step 1: 写失败测试**

```python
def test_teach_message_requires_self_mention()            # 不带 mentions → 不落盘
def test_teach_message_requires_p2p_chat_id_match()       # chat_id 不等于 teach.json → 不落盘
def test_teach_message_rejects_non_text_msg_type()        # msg_type='image' → 不落盘 + 回执含「换成文字」
def test_teach_message_before_enabled_at_ms_is_ignored()  # create_time < enabled_at_ms → 不落盘
def test_teach_ack_does_not_contain_mention()             # 回执 mentions 为空（自激结构性排除）
def test_teach_exact_command_lists_and_forgets()          # 「教材清单」→ 回执含条目；「忘掉最后一条」→ 日志 +1 条 disable
def test_teach_held_state_still_accepts_teaching()        # hold 生效时照常教学
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_service.py -q -k teach`
Expected: FAIL（`handle_teach_message` 不存在）

- [ ] **Step 3: 实现**

`control_tick` 里每个 chat 先取 `teach_enabled()`；命中则对每条消息先
`handle_teach_message`（返回 True 表示已处理），未命中再走原 `handle_control_message`。
`handle_teach_message` 顺序：`message_id` 合法 → 已在 `_control_seen` 则跳过 →
`sender == target_open_id` → `chat_id == config['chat_id']` → `mentioned(message, target_open_id)` →
`create_time >= enabled_at_ms` → `msg_type in ('text','post')` → `command_for(text)`
（命中则执行并回执）→ 否则 `append` + `render_ack` 回执。

回执用 `transport.reply(message, ...)`（与原控制回执同一条路径），**不带任何 mention**。

- [ ] **Step 4: 跑测试 + 全量**

Run: `python3 -m pytest tests/test_service.py tests/test_teach.py -q` → PASS
Run: `python3 -m pytest -q` → 全绿

- [ ] **Step 5: Commit**

```bash
git add bridge/service.py tests/test_service.py tests/test_teach.py
git commit -m "feat(bridge): 教学消息采集（@ 判据、msg_type 闸门、回执、精确指令）"
```

---

### Task 5: `_event_view` 收敛 + claim 注入

**Files:**
- Create: `bridge/events_view.py`
- Modify: `bridge/cli.py:979-987`、`bridge/gateway.py:90-97`
- Test: `tests/test_gateway.py`、`tests/test_cli.py`

**Interfaces:**
- Produces: `event_view(event, *, detailed=False, with_token=False, teachings=None) -> dict`
  - 基础字段恒有：`id/kind/dedupe_key/payload/created_at/attempts/lease_until`
  - `detailed=True` 追加：`state/consumer/next_due/error`
  - `with_token=True` 追加：`claim_token`
  - `teachings is not None` 且 `payload` 是 dict → `payload['teachings'] = teachings`；
    `payload` 不是 dict → 抛 `ValueError('event payload 不是对象，无法注入教材')`（显式失败，不跳过）

- [ ] **Step 1: 写失败测试**

```python
def test_two_views_agree_on_shared_fields()          # cli claim 输出与 gateway 输出逐字段一致（detailed 差异除外）
def test_claim_payload_carries_teachings_block()     # 有教材时 payload['teachings']['rev'] == 2
def test_claim_payload_teachings_disabled_shape()    # 未启用时 payload['teachings'] == {'enabled': False}
def test_gateway_claim_and_cli_claim_byte_equal()    # 同一事件两条路径的 teachings 序列化逐字一致
def test_non_dict_payload_raises_instead_of_skipping()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_gateway.py tests/test_cli.py -q -k "teachings or view"`
Expected: FAIL

- [ ] **Step 3: 实现并删除旧实现**

新建 `bridge/events_view.py`，把 `cli._event_view` 与 `gateway._event_view` 改成薄封装后**删除**
`gateway.py` 里的那份（AGENTS §0.1：根治好之后把旧实现删掉）。`gateway.EVENT_KINDS` 副本同样删除，
改为从 `store` 导入。

- [ ] **Step 4: 跑测试 + 全量**

Run: `python3 -m pytest -q` → 全绿

- [ ] **Step 5: Commit**

```bash
git add bridge/events_view.py bridge/cli.py bridge/gateway.py tests/test_gateway.py tests/test_cli.py
git commit -m "refactor(bridge): 收敛两份 _event_view，claim payload 注入 teachings"
```

---

### Task 6: 六个渲染点接教材（规则文本只有一处）

**Files:**
- Modify: `agent_prompt.py`（`TEACHING_RULES` 常量 + `build_prompt(..., teachings=None)`）
- Modify: `dsh-consumer.py:366`、`codex-consumer.py:290`、`agentkit/driver.py:58`
- Modify: `bridge/agent.py:558`、`bridge/agent.py:640`
- Modify: `skills/feishu-bridge-consumer/SESSION-PROMPT.txt`、`skills/feishu-bridge-consumer-remote/SESSION-PROMPT-WINDOWS.txt`
- Modify: `skills/feishu-bridge-consumer/SKILL.md`（payload schema 唯一事实源）
- Test: `tests/test_agent_prompt.py`

**Interfaces:**
- Produces: `agent_prompt.TEACHING_RULES: str`（§8 六条，逐字）
- Produces: `agent_prompt.render_teachings(teachings: dict) -> str`（`say`/`know` 分开标注；超预算按
  `MAX_INJECT_CHARS=4000` 截断并**在文本里写明「已截断，还有 N 条」**，不静默）
- Produces: `build_prompt(payload, workspace=None, timeout_seconds=0, tool_budget=12, owner_name=None, teachings=None) -> str`
- Consumes: `event_view(...)` 注入的 `payload['teachings']`

- [ ] **Step 1: 写失败测试**

```python
def test_prompt_includes_teaching_rules_and_charter()
def test_know_entries_are_marked_do_not_quote()          # 渲染里 know 条目出现在「不许原样说出」小节
def test_overflow_is_announced_not_silently_truncated()   # 断言出现「已截断」字样与剩余条数
def test_teachings_disabled_renders_nothing()             # {'enabled': False} → 输出不含教材段
def test_rules_text_is_identical_in_all_six_surfaces()    # 六处 prompt 都逐字包含 TEACHING_RULES
def test_static_guard_covers_all_six_entry_points()       # 断言六个文件都调用了带 teachings 的组装函数
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_agent_prompt.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

`TEACHING_RULES` 放 `agent_prompt.py`；`build_prompt` 在「不可信数据」段**之后**单独成段注入
（显式分区）。四处代码入口传 `teachings=payload.get('teachings')`；
`agentkit/driver.py:58` 顺手补上漏传的 `owner_name` 与 `tool_budget`。
两个 `.txt` 各追加同一段文本（复制，由测试钉住逐字一致）。`SKILL.md` 的 payload schema 补
`teachings` 与 `owner_answer`。

- [ ] **Step 4: 跑测试 + 全量**

Run: `python3 -m pytest tests/test_agent_prompt.py -q` → PASS
Run: `python3 -m pytest -q` → 全绿

- [ ] **Step 5: Commit**

```bash
git add agent_prompt.py dsh-consumer.py codex-consumer.py agentkit/driver.py bridge/agent.py \
        skills/feishu-bridge-consumer/SESSION-PROMPT.txt \
        skills/feishu-bridge-consumer-remote/SESSION-PROMPT-WINDOWS.txt \
        skills/feishu-bridge-consumer/SKILL.md tests/test_agent_prompt.py
git commit -m "feat: 教材注入六个 prompt 渲染点，规则文本单一来源并被测试钉住"
```

---

### Task 7: 问答回路（问出去的问题回得来）

**Files:**
- Modify: `bridge/teach.py`（问答登记：`questions_path`、`register_question`、`pending_questions`、`answer_question`）
- Modify: `bridge/cli.py:840-869`（`_notify` 增 `--question-event` / `--needs`，发送成功后登记）
- Modify: `consumer_runtime.py:231-236`（`escalate()` 传参）
- Modify: `bridge/service.py`（匹配 `parent_id` → 落教材 + 回执 + 重放事件）
- Modify: `bridge/cli.py`（`teach questions`）
- Test: `tests/test_notify.py`、`tests/test_consumer_escalation.py`、`tests/test_service.py`

**Interfaces:**
- Produces: `teach.register_question(home, event_id, needs, question, dm_message_id, at_ms) -> dict`
  （记录 `{"id":"q-0001","event_id","needs","question","dm_message_id","at_ms","answered_at_ms":None}`）
- Produces: `teach.pending_questions(home) -> list[dict]`
- Produces: `teach.answer_question(home, qid, text, at_ms) -> dict`（幂等：已答过 → 返回原记录 + `duplicate=True`）
- Produces: `Service.handle_teach_answer(message, now) -> bool`
- Produces: 重放事件 `dedupe_key=f'followup:{原 mid}:{qid}'`，payload 增 `owner_answer` 键

- [ ] **Step 1: 写失败测试**

```python
def test_notify_records_question_with_dm_message_id()        # --question-event 时 questions.jsonl 有 1 条，dm_message_id == 返回的 mid
def test_escalate_passes_event_and_needs()                   # 断言 CLI 参数含 --question-event 与 --needs decision
def test_answer_by_quote_reply_is_routed()                   # parent_id 命中 → 教材 +1 条、问题变 answered、回执一条
def test_answer_requires_quote_when_multiple_pending()       # 2 个待答 + 无 parent_id → 回执要求引用，零写入
def test_answer_without_quote_works_when_exactly_one_pending()
def test_duplicate_answer_is_idempotent()                    # 同一 qid 再答 → duplicate，不重复重放
def test_answer_reads_query_api_emits_followup_event()       # 断言新事件 dedupe_key 与 owner_answer
def test_followup_event_respects_daily_reply_cap()           # 配额耗尽 → 不重放，status 显示排队（不静默丢）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_notify.py tests/test_consumer_escalation.py tests/test_service.py -q -k "question or answer or followup"`
Expected: FAIL

- [ ] **Step 3: 实现**

`_notify` 在 `send_dm` 成功后调 `teach.register_question(...)`（`--question-event` 缺省则不登记，
保持现有调用方行为不变）。`Service.control_tick` 对每条消息先试 `handle_teach_answer`
（`parent_id` 命中 `pending_questions` 的 `dm_message_id`），再试 `handle_teach_message`。
回答处理：`answer_question` → `append` 教材（`needs` 映射 kind/expose）→ 回执 →
`store.emit_event('message', dedupe_key, payload)` 重放。配额不足时**不重放**，
在 `meta['followup_pending']` 记数并在 `status` 显示。

- [ ] **Step 4: 跑测试 + 全量**

Run: `python3 -m pytest -q` → 全绿

- [ ] **Step 5: Commit**

```bash
git add bridge/teach.py bridge/cli.py bridge/service.py consumer_runtime.py \
        tests/test_notify.py tests/test_consumer_escalation.py tests/test_service.py
git commit -m "feat(bridge): 问答回路——master 引用回复即落教材并重放后续跑"
```

---

### Task 8: 机械归并（⑤a，不碰模型）

**Files:**
- Modify: `bridge/teach.py`（`tidy(home) -> dict`）
- Modify: `bridge/cli.py`（`teach tidy [--dry-run]`）
- Test: `tests/test_teach.py`

**Interfaces:**
- Produces: `teach.tidy(home, dry_run=False) -> dict`
  返回 `{'before': int, 'after': int, 'dropped_duplicates': int, 'merged': [[ids]], 'skipped': int, 'dry_run': bool}`
- 语义：只删**完全重复**（同 `kind` + 同 `text`）、只合并**同一天相邻同类**，不改写任何文字；
  被合并的旧条目追加 `op='supersede'` 记录，原条目保留（可整体回退）。

- [ ] **Step 1: 写失败测试**

```python
def test_tidy_drops_exact_duplicates_and_reports_counts()
def test_tidy_does_not_change_entry_text()               # 归并前后所有 text 的集合不变（除被删重复项）
def test_tidy_dry_run_writes_nothing()
def test_tidy_is_reversible_via_snapshot()               # 归并后 rev 增加，但原条目仍可 show
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_teach.py -q -k tidy`
Expected: FAIL

- [ ] **Step 3: 实现 + ④ 真机验收**

实现 `tidy`；然后在真机上跑一次完整闭环并把输出贴进 `docs/HISTORY.md`：
私聊 @ 自己 教一句 → `feishu-bridge teach list` 看到 → `events claim` 的 payload 里带 `teachings`
→ 下一次真实回答体现它。

- [ ] **Step 4: 跑测试 + 全量 + 门禁**

Run: `python3 -m pytest tests/test_teach.py -q` → PASS
Run: `python3 -m pytest -q` → 全绿
Run: `python3 -m pytest tests/test_repo_hygiene.py -q` → 全绿

- [ ] **Step 5: Commit**

```bash
git add bridge/teach.py bridge/cli.py tests/test_teach.py docs/HISTORY.md
git commit -m "feat(bridge): 教材机械归并（去重/合并，不改写文字，可回退）+ 真机验收实录"
```

---

## 任务编排（依赖与写入范围）

| 任务 | 依赖 | 主要写入范围 |
|---|---|---|
| T1 分页 | — | `bridge/transport.py`、`bridge/service.py`、`tests/test_hold.py`、`tests/test_transport.py` |
| T2 存储 | T1 | `bridge/teach.py`、`tests/test_teach.py` |
| T3 CLI | T2 | `bridge/cli.py`、`bridge/transport.py`、`tests/test_cli.py` |
| T4 采集 | T3 | `bridge/service.py`、`tests/test_service.py` |
| T5 视图 | T4 | `bridge/events_view.py`、`bridge/cli.py`、`bridge/gateway.py`、`tests/test_gateway.py` |
| T6 渲染 | T5 | `agent_prompt.py`、四个消费者入口、两个 `SESSION-PROMPT.txt`、`SKILL.md` |
| T7 问答 | T6 | `bridge/teach.py`、`bridge/notify` 路径、`consumer_runtime.py`、`bridge/service.py` |
| T8 归并 | T2 | `bridge/teach.py`、`bridge/cli.py` |

严格串行：T1→T2→T3→T4→T5→T6→T7，T8 可在 T7 之后或与 T7 并行（写入范围仅在 teach.py/cli.py，
需与 T7 错开，故排在最后）。**中途可交付点**：T4 完成后教材已经可以写入并撤回（还看不到效果），
T6 完成后「教一句，下次回答就变」，可以把 T1–T6 当成第一个可用版本。

## 不在本计划内

- ⑥b 模型归纳（等攒到 20~30 条真实教材再单独出计划）。
- 能力授权（MCP/子代理/删除/密钥仍然硬禁）。
- 跨机器汇总教材。
